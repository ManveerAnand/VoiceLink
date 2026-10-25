from __future__ import annotations

from dataclasses import dataclass, replace
from threading import Event
from uuid import UUID, uuid4

import pytest

from server.config import LookAheadSettings
from server.lookahead.cache import PcmCache, SynthesisConfig, synthesis_input_digest
from server.lookahead.correlation import BoundClaim
from server.lookahead.protocol import LookAheadSnapshot, LookAheadUnit
from server.lookahead.scheduler import LookAheadScheduler, RunwayEstimator
from server.lookahead.store import SnapshotStore
from server.lookahead.worker import SpeculativeResult, SpeculativeWorker


def config() -> SynthesisConfig:
    return SynthesisConfig(
        route="kokoro", model_version="kokoro", voice="af_heart", speed=1.0,
        format="pcm_24k_16bit", language="en-GB",
    )


def units(count: int, *, start: int = 10) -> tuple[LookAheadUnit, ...]:
    return tuple(
        LookAheadUnit.model_validate({
            "sequence": start + index, "text": f"unit {start + index} " * 5, "lang": "en-GB", "direction": "ltr",
            "block": {"id": f"b{index}", "kind": "paragraph"},
            "position": {"indexInBlock": index, "firstInBlock": index == 0, "lastInBlock": index == count - 1},
            "boundaryBefore": "sentence", "boundaryAfter": "sentence",
        })
        for index in range(count)
    )


def claim(stream_id: UUID, *, generation: int = 1, count: int = 4, start: int = 10) -> BoundClaim:
    future = units(count, start=start)
    return BoundClaim(stream_id, generation, future[0].sequence, future, config())


@dataclass
class FakeWorker:
    ready: bool = True

    def __post_init__(self) -> None:
        self.submitted = []
        self.cancelled: list[UUID] = []

    def submit(self, job) -> bool:
        self.submitted.append(job)
        return True

    def cancel(self, job_id: UUID) -> None:
        self.cancelled.append(job_id)


@dataclass
class RejectingWorker(FakeWorker):
    def submit(self, job) -> bool:
        self.submitted.append(job)
        return False


class FakeQueue:
    def __init__(self, initial=(), *, capacity: int = 48) -> None:
        self.items = list(initial)
        self.capacity = capacity

    def put_nowait(self, item) -> None:
        if len(self.items) >= self.capacity:
            from queue import Full
            raise Full
        self.items.append(item)

    def put(self, item) -> None:
        self.put_nowait(item)

    def get(self, timeout=None):
        if not self.items:
            from queue import Empty
            raise Empty
        return self.items.pop(0)

    def get_nowait(self):
        return self.get()


class FakeProcess:
    def __init__(self, *, target, args, daemon) -> None:
        self.target, self.args, self.daemon = target, args, daemon
        self.alive = False
        self.joined = 0
        self.terminated = 0

    def start(self) -> None:
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout) -> None:
        self.joined += 1

    def terminate(self) -> None:
        self.terminated += 1
        self.alive = False


class FakeContext:
    def __init__(self, status=()) -> None:
        self.queues = [FakeQueue(), FakeQueue(), FakeQueue(status, capacity=1)]
        self.process: FakeProcess | None = None

    def Queue(self, maxsize):
        queue = self.queues.pop(0)
        queue.capacity = maxsize
        return queue

    def Process(self, **kwargs):
        self.process = FakeProcess(**kwargs)
        return self.process


def scheduler(worker: FakeWorker, store: SnapshotStore | None = None, **settings) -> LookAheadScheduler:
    return LookAheadScheduler(
        worker=worker,
        store=store or SnapshotStore(),
        cache=PcmCache(max_bytes=100_000, ttl_seconds=100),
        settings=LookAheadSettings(enabled=True, **settings),
        clock=lambda: 10.0,
    )


def test_scheduler_selects_only_in_order_future_units() -> None:
    worker = FakeWorker()
    subject = scheduler(worker)

    subject.schedule_claim(claim(uuid4()))

    assert [job.sequence for job in worker.submitted] == [11, 12]


def test_scheduler_deduplicates_exact_synthesis_keys_across_streams() -> None:
    worker = FakeWorker()
    subject = scheduler(worker)
    first, second = uuid4(), uuid4()

    subject.schedule_claim(claim(first, count=2))
    subject.schedule_claim(claim(second, count=2))

    assert len(worker.submitted) == 1


def test_scheduler_enforces_the_48_job_global_cap() -> None:
    worker = FakeWorker()
    subject = scheduler(worker, max_queued_jobs=48, max_runway_seconds=60)
    subject.estimator.target_runway_seconds = 60

    for start in range(10, 70, 12):
        subject.schedule_claim(claim(uuid4(), count=12, start=start))

    assert len(worker.submitted) == 48
    assert subject.queued_jobs == 48


def test_scheduler_enforces_the_60_second_per_stream_runway_cap() -> None:
    worker = FakeWorker()
    subject = scheduler(worker, max_runway_seconds=60)
    subject.estimator.target_runway_seconds = 60
    oversized = tuple(unit.model_copy(update={"text": "x" * 150}) for unit in units(40))
    large_claim = BoundClaim(uuid4(), 1, 10, oversized, config())

    subject.schedule_claim(large_claim)

    assert subject.scheduled_runway_seconds <= 60


def test_runway_estimator_adapts_to_observed_real_time_factor() -> None:
    estimator = RunwayEstimator()

    estimator.observe(synthesis_seconds=8, audio_seconds=2)
    selected = estimator.select(units(4), cached_seconds=0)

    assert estimator.rtf == pytest.approx(1.75)  # alpha=0.25 from an initial RTF of 1
    assert 3 <= estimator.target_runway_seconds <= 60
    assert [unit.sequence for unit in selected] == [10, 11, 12]


def test_new_generation_cancels_pending_work_and_late_result_uses_exact_key() -> None:
    worker = FakeWorker()
    store = SnapshotStore(on_cancel_job=worker.cancel)
    subject = scheduler(worker, store)
    stream = uuid4()
    old = claim(stream, generation=1, count=2)
    snapshot = LookAheadSnapshot.model_validate({
        "protocol": 1, "generation": 1, "revision": 1, "currentSequence": 10,
        "source": {"rate": 1, "voiceName": "Heart", "lang": "en-GB"}, "units": [unit.model_dump(by_alias=True, exclude_none=True) for unit in old.units],
    })
    store.replace(stream, snapshot, 1, now=0)
    subject.schedule_claim(old)
    job = worker.submitted[0]
    newer = snapshot.model_copy(update={"generation": 2, "revision": 2})
    store.replace(stream, newer, 2, now=1)

    subject.handle_result(SpeculativeResult(job.id, job.cache_key, b"pcm", 0.1, 1.0))

    assert worker.cancelled == [job.id]
    assert subject.cache.get(job.cache_key, now=10) == b"pcm"
    assert store.result_key(stream, generation=1, sequence=job.sequence) is None


def test_scheduler_has_no_capability_or_jobs_when_worker_is_not_ready() -> None:
    worker = FakeWorker(ready=False)
    subject = scheduler(worker)

    subject.schedule_claim(claim(uuid4()))

    assert worker.submitted == []
    assert not subject.ready


def test_scheduler_does_not_keep_associations_when_bounded_worker_rejects_a_job() -> None:
    worker = RejectingWorker()
    cancelled: list[UUID] = []
    store = SnapshotStore(on_cancel_job=cancelled.append)
    subject = scheduler(worker, store)
    stream = uuid4()
    initial = claim(stream, count=2)
    snapshot = LookAheadSnapshot.model_validate({
        "protocol": 1, "generation": 1, "revision": 1, "currentSequence": 10,
        "source": {"rate": 1, "voiceName": "Heart", "lang": "en-GB"},
        "units": [unit.model_dump(by_alias=True, exclude_none=True) for unit in initial.units],
    })
    assert store.replace(stream, snapshot, 1, now=0).value == "accepted"

    subject.schedule_claim(initial)
    assert worker.submitted
    newer = snapshot.model_copy(update={"generation": 2, "revision": 2})
    store.replace(stream, newer, 2, now=1)

    assert subject.queued_jobs == 0
    assert subject.scheduled_runway_seconds == 0
    assert cancelled == []


def test_scheduler_drains_associations_when_a_worker_crashes() -> None:
    worker = FakeWorker()
    subject = scheduler(worker)
    subject.schedule_claim(claim(uuid4(), count=2))
    worker.ready = False

    subject.poll()

    assert subject.queued_jobs == 0
    assert subject.scheduled_runway_seconds == 0


def test_worker_marks_itself_unready_after_load_failure_or_startup_timeout(monkeypatch) -> None:
    import server.lookahead.worker as worker_module

    failure_context = FakeContext(status=[("failed", "RuntimeError")])
    monkeypatch.setattr(worker_module, "get_context", lambda method: failure_context)
    assert not SpeculativeWorker(startup_timeout=0).start()

    timeout_context = FakeContext()
    monkeypatch.setattr(worker_module, "get_context", lambda method: timeout_context)
    assert not SpeculativeWorker(startup_timeout=0).start()


def test_worker_uses_bounded_nonblocking_queue_and_finite_stop(monkeypatch) -> None:
    import server.lookahead.worker as worker_module

    context = FakeContext(status=[("ready", None)])
    monkeypatch.setattr(worker_module, "get_context", lambda method: context)
    subject = SpeculativeWorker(max_jobs=1, startup_timeout=0, shutdown_timeout=0)
    assert subject.start()
    job = worker_module.SynthesisJob(uuid4(), uuid4(), 1, 1, "text", "af_heart", 1, "key")
    assert subject.submit(job)
    assert not subject.submit(job)

    subject.stop()

    assert context.process is not None
    assert context.process.joined >= 1
    assert context.process.terminated == 1


def test_blocked_speculative_worker_does_not_delay_independent_authoritative_lane(monkeypatch) -> None:
    import server.lookahead.worker as worker_module

    # A real SpeculativeWorker boundary with a child that never drains its queue.
    # The authoritative fake shares neither the worker process nor its IPC queue.
    context = FakeContext(status=[("ready", None)])
    monkeypatch.setattr(worker_module, "get_context", lambda method: context)
    speculative = SpeculativeWorker(max_jobs=1, startup_timeout=0, shutdown_timeout=0)
    assert speculative.start()
    subject = scheduler(speculative)
    subject.schedule_claim(claim(uuid4(), count=2))
    assert subject.queued_jobs == 1

    class AuthoritativeLane:
        def __init__(self) -> None:
            self.started = Event()

        def synthesize(self) -> bytes:
            self.started.set()
            return b"authoritative-pcm"

    authoritative = AuthoritativeLane()
    try:
        assert authoritative.synthesize() == b"authoritative-pcm"
        assert authoritative.started.is_set()
    finally:
        speculative.stop()


def test_non_kokoro_claim_never_submits_or_caches_pcm() -> None:
    worker = FakeWorker()
    subject = scheduler(worker)
    incompatible = BoundClaim(uuid4(), 1, 10, units(2), replace(config(), route="qwen3"))

    subject.schedule_claim(incompatible)

    assert worker.submitted == []
    assert subject.queued_jobs == 0


def test_deduplicated_job_completes_every_valid_stream_waiter() -> None:
    worker = FakeWorker()
    store = SnapshotStore()
    subject = scheduler(worker, store)
    first, second = uuid4(), uuid4()
    first_claim, second_claim = claim(first, count=2), claim(second, count=2)
    for stream, bound in ((first, first_claim), (second, second_claim)):
        snapshot = LookAheadSnapshot.model_validate({
            "protocol": 1, "generation": 1, "revision": 1, "currentSequence": 10,
            "source": {"rate": 1, "voiceName": "Heart", "lang": "en-GB"},
            "units": [unit.model_dump(by_alias=True, exclude_none=True) for unit in bound.units],
        })
        store.replace(stream, snapshot, 1, now=0)
        subject.schedule_claim(bound)
    job = worker.submitted[0]

    subject.handle_result(SpeculativeResult(job.id, job.cache_key, b"pcm", 0.1, 1.0))

    assert store.result_key(first, generation=1, sequence=11) == job.cache_key
    assert store.result_key(second, generation=1, sequence=11) == job.cache_key


def test_new_generation_discards_old_cached_runway_before_scheduling_again() -> None:
    worker = FakeWorker()
    subject = scheduler(worker)
    stream = uuid4()
    subject.schedule_claim(claim(stream, generation=1, count=2))
    assert subject.scheduled_runway_seconds > 0

    subject.schedule_claim(claim(stream, generation=2, count=2))

    assert [job.generation for job in worker.submitted] == [1, 2]


def test_failure_result_releases_job_and_disables_speculative_capability() -> None:
    worker = FakeWorker()
    subject = scheduler(worker)
    subject.schedule_claim(claim(uuid4(), count=2))
    job = worker.submitted[0]

    subject.handle_result(SpeculativeResult(job.id, job.cache_key, b"", 0, 0, failure="synthesis-failed"))

    assert subject.queued_jobs == 0
    assert not subject.ready


def test_result_with_a_mismatched_key_is_not_inserted_under_an_unrelated_key() -> None:
    worker = FakeWorker()
    subject = scheduler(worker)
    subject.schedule_claim(claim(uuid4(), count=2))
    job = worker.submitted[0]

    subject.handle_result(SpeculativeResult(job.id, "wrong-key", b"pcm", 0.1, 1.0))

    assert subject.cache.get("wrong-key", now=10) is None
    assert not subject.ready


def test_job_deadline_releases_slots_and_disables_capability() -> None:
    worker = FakeWorker()
    now = [0.0]
    subject = LookAheadScheduler(
        worker=worker,
        store=SnapshotStore(),
        cache=PcmCache(max_bytes=100_000, ttl_seconds=100),
        settings=LookAheadSettings(enabled=True),
        clock=lambda: now[0],
        job_timeout_seconds=1,
    )
    subject.schedule_claim(claim(uuid4(), count=2))
    assert subject.queued_jobs == 1

    now[0] = 2
    subject.poll()

    assert subject.queued_jobs == 0
    assert not subject.ready
