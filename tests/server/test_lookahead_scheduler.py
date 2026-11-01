from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from queue import Full, Queue
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from server.config import LookAheadSettings
from server.lookahead.cache import PcmCache, SynthesisConfig, synthesis_input_digest
from server.lookahead.correlation import BoundClaim, CorrelationRegistry
from server.lookahead.protocol import LookAheadSnapshot, LookAheadUnit
from server.lookahead.scheduler import LookAheadScheduler, RunwayEstimator
from server.lookahead.service import LookAheadService
from server.lookahead.store import SnapshotStore
from server.lookahead.worker import (
    SpeculativeResult,
    SpeculativeWorker,
    SynthesisJob,
    _Cancel,
    _Stop,
    _worker_main,
)


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
        self.closed = False
        self.joined_thread = False
        self.join_cancelled = False

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

    def close(self) -> None:
        self.closed = True

    def cancel_join_thread(self) -> None:
        self.join_cancelled = True

    def join_thread(self) -> None:
        self.joined_thread = True


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
        self.queues = [
            FakeQueue(),
            FakeQueue(),
            FakeQueue(),
            FakeQueue(),
            FakeQueue(status, capacity=1),
        ]
        self.created_queues = list(self.queues)
        self.process: FakeProcess | None = None

    def Queue(self, maxsize):
        queue = self.queues.pop(0)
        queue.capacity = maxsize
        return queue

    def Process(self, **kwargs):
        self.process = FakeProcess(**kwargs)
        return self.process


class BlockingRestartProcess(FakeProcess):
    def __init__(
        self,
        *,
        restart_started: Event,
        restart_release: Event,
        block_on_join: bool,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._restart_started = restart_started
        self._restart_release = restart_release
        self._block_on_join = block_on_join

    def join(self, timeout) -> None:
        if self._block_on_join and self.alive:
            self._restart_started.set()
            self._restart_release.wait(2.0)
        super().join(timeout)


class BlockingRestartContext:
    def __init__(self, restart_started: Event, restart_release: Event) -> None:
        self.restart_started = restart_started
        self.restart_release = restart_release
        self.queue_index = 0
        self.processes: list[BlockingRestartProcess] = []
        self.created_queues: list[FakeQueue] = []
        self.replacement_created = Event()

    def Queue(self, maxsize):
        index = self.queue_index % 5
        self.queue_index += 1
        queue = FakeQueue(
            [("ready", None)] if index == 4 else (), capacity=maxsize
        )
        self.created_queues.append(queue)
        return queue

    def Process(self, **kwargs):
        process = BlockingRestartProcess(
            **kwargs,
            restart_started=self.restart_started,
            restart_release=self.restart_release,
            block_on_join=not self.processes,
        )
        self.processes.append(process)
        if len(self.processes) > 1:
            self.replacement_created.set()
        return process


class ThreadQueue(Queue):
    def close(self) -> None:
        return None

    def join_thread(self) -> None:
        return None


class SignallingStatusQueue(ThreadQueue):
    def __init__(
        self,
        maxsize: int,
        active_attempted: Event,
        idle_published: Event | None = None,
    ) -> None:
        super().__init__(maxsize)
        self._active_attempted = active_attempted
        self._idle_published = idle_published

    def put(self, item, block=True, timeout=None) -> None:
        if item[0] == "idle" and self._idle_published is not None:
            self._idle_published.set()
        if item[0] != "active":
            super().put(item, block, timeout)
            return
        with self.not_full:
            if not block:
                if self.maxsize > 0 and self._qsize() >= self.maxsize:
                    self._active_attempted.set()
                    raise Full
            else:
                while self.maxsize > 0 and self._qsize() >= self.maxsize:
                    self._active_attempted.set()
                    self.not_full.wait()
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()


class GatedJobQueue(ThreadQueue):
    def __init__(self, maxsize: int, released: Event) -> None:
        super().__init__(maxsize)
        self._released = released

    def get(self, block=True, timeout=None):
        self._released.wait()
        return super().get(block, timeout)


class ThreadProcess:
    def __init__(self, *, target, args, daemon, terminated: Event) -> None:
        self._thread = Thread(target=target, args=args, daemon=daemon)
        self._terminated = terminated

    def start(self) -> None:
        self._thread.start()

    def is_alive(self) -> bool:
        return not self._terminated.is_set() and self._thread.is_alive()

    def join(self, timeout) -> None:
        self._thread.join(timeout)

    def terminate(self) -> None:
        self._terminated.set()


class ThreadContext:
    def __init__(
        self,
        active_attempted: Event,
        terminated: list[Event],
        jobs_released: Event,
        idle_published: Event | None = None,
    ) -> None:
        self._active_attempted = active_attempted
        self._terminated = terminated
        self._jobs_released = jobs_released
        self._idle_published = idle_published
        self._queue_index = 0

    def Queue(self, maxsize):
        index = self._queue_index
        self._queue_index += 1
        if index % 5 == 0:
            return GatedJobQueue(maxsize, self._jobs_released)
        if index % 5 == 4:
            return SignallingStatusQueue(
                maxsize, self._active_attempted, self._idle_published
            )
        return ThreadQueue(maxsize)

    def Process(self, **kwargs):
        terminated = Event()
        self._terminated.append(terminated)
        return ThreadProcess(**kwargs, terminated=terminated)


def scheduler(worker: FakeWorker, store: SnapshotStore | None = None, **settings) -> LookAheadScheduler:
    return LookAheadScheduler(
        worker=worker,
        store=store or SnapshotStore(),
        cache=PcmCache(max_bytes=100_000, ttl_seconds=100),
        settings=LookAheadSettings(enabled=True, **settings),
        clock=lambda: 10.0,
    )


def wait_for_condition(condition, *, description: str, timeout: float = 1.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if condition():
            return
        sleep(0.01)
    pytest.fail(f"timed out waiting for {description}")


def test_scheduler_selects_only_in_order_future_units() -> None:
    worker = FakeWorker()
    subject = scheduler(worker)

    subject.schedule_claim(claim(uuid4()))

    assert [job.sequence for job in worker.submitted] == [11, 12]


def test_scheduler_runway_counts_each_future_sequence_once_and_prunes_consumed_units() -> None:
    worker = FakeWorker()
    subject = scheduler(worker, max_runway_seconds=60)
    subject.estimator.target_runway_seconds = 60
    stream = uuid4()
    initial = claim(stream, count=4)
    seconds = subject.estimator.seconds_for(initial.units[1])

    subject.schedule_claim(initial)
    subject.schedule_claim(initial)
    assert subject.scheduled_runway_seconds == pytest.approx(3 * seconds)

    advanced = BoundClaim(stream, 1, 11, initial.units, initial.config)
    subject.schedule_claim(advanced)

    assert subject.scheduled_runway_seconds == pytest.approx(2 * seconds)


def test_scheduler_schedules_a_new_tail_after_consuming_a_pending_sequence() -> None:
    worker = FakeWorker()
    subject = scheduler(worker)
    stream = uuid4()
    initial = claim(stream, count=5)

    subject.schedule_claim(initial)
    assert [job.sequence for job in worker.submitted] == [11, 12]

    subject.schedule_claim(BoundClaim(stream, 1, 11, initial.units, initial.config))

    assert [job.sequence for job in worker.submitted] == [11, 12, 13]


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
    holder: dict[str, LookAheadScheduler] = {}
    store = SnapshotStore(on_cancel_job=lambda association_id: holder["scheduler"].cancel(association_id))
    subject = scheduler(worker, store)
    holder["scheduler"] = subject
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


def test_cached_only_runway_is_cleared_for_a_new_generation_and_stream_cancel() -> None:
    worker = FakeWorker()
    store = SnapshotStore()
    subject = scheduler(worker, store)
    stream = uuid4()
    first = claim(stream, generation=1, count=4)
    first_snapshot = LookAheadSnapshot.model_validate({
        "protocol": 1, "generation": 1, "revision": 1, "currentSequence": 10,
        "source": {"rate": 1, "voiceName": "Heart", "lang": "en-GB"},
        "units": [unit.model_dump(by_alias=True, exclude_none=True) for unit in first.units],
    })
    store.replace(stream, first_snapshot, 1, now=0)
    for unit in first.units[1:]:
        subject.cache.put(synthesis_input_digest(unit.text, first.config), b"pcm", now=0)

    subject.schedule_claim(first)
    assert subject.scheduled_runway_seconds >= 3
    second_snapshot = first_snapshot.model_copy(update={"generation": 2, "revision": 2})
    store.replace(stream, second_snapshot, 2, now=1)
    second = claim(stream, generation=2, count=4)
    subject.schedule_claim(second)

    assert store.result_key(stream, generation=2, sequence=11) is not None
    subject.cancel_stream(stream)
    assert subject.scheduled_runway_seconds == 0


def test_invalidating_one_shared_waiter_keeps_the_other_shared_job_alive() -> None:
    worker = FakeWorker()
    holder: dict[str, LookAheadScheduler] = {}
    store = SnapshotStore(on_cancel_job=lambda association_id: holder["scheduler"].cancel(association_id))
    subject = scheduler(worker, store)
    holder["scheduler"] = subject
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
    first_replacement = LookAheadSnapshot.model_validate({
        "protocol": 1, "generation": 2, "revision": 2, "currentSequence": 10,
        "source": {"rate": 1, "voiceName": "Heart", "lang": "en-GB"},
        "units": [unit.model_dump(by_alias=True, exclude_none=True) for unit in first_claim.units],
    })

    store.replace(first, first_replacement, 2, now=1)
    subject.handle_result(SpeculativeResult(job.id, job.cache_key, b"pcm", 0.1, 1.0))

    assert worker.cancelled == []
    assert store.result_key(first, generation=1, sequence=11) is None
    assert store.result_key(second, generation=1, sequence=11) == job.cache_key
    assert subject.cache.get(job.cache_key, now=10) == b"pcm"


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
    assert all(
        queue.closed and queue.join_cancelled and not queue.joined_thread
        for queue in context.created_queues
    )


def test_worker_control_queue_cancels_before_fifo_synthesis(monkeypatch) -> None:
    import server.models

    class Model:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def synthesize(self, text: str, *, voice: str, speed: float):
            self.calls.append(text)
            yield b"pcm"

        def unload(self) -> None:
            return None

    model = Model()
    monkeypatch.setattr(server.models, "load_model", lambda *_args, **_kwargs: model)
    job = SynthesisJob(uuid4(), uuid4(), 1, 11, "stale", "af_heart", 1.0, "key")
    jobs = FakeQueue([job, _Stop()])
    controls = FakeQueue([_Cancel(job.id)])

    _worker_main(
        {"lang_code": "a", "device": "cpu"},
        jobs,
        controls,
        FakeQueue(),
        FakeQueue(),
        FakeQueue(),
    )

    assert model.calls == []


def test_worker_restarts_child_when_a_cancelled_job_is_already_active(monkeypatch) -> None:
    import server.lookahead.worker as worker_module

    class CyclingContext:
        def __init__(self) -> None:
            self.queue_index = 0
            self.processes: list[FakeProcess] = []

        def Queue(self, maxsize):
            index = self.queue_index % 5
            self.queue_index += 1
            return FakeQueue([("ready", None)] if index == 4 else (), capacity=maxsize)

        def Process(self, **kwargs):
            process = FakeProcess(**kwargs)
            self.processes.append(process)
            return process

    context = CyclingContext()
    monkeypatch.setattr(worker_module, "get_context", lambda method: context)
    subject = SpeculativeWorker(max_jobs=1, startup_timeout=0, shutdown_timeout=0)
    assert subject.start()
    active_job = uuid4()
    subject._status.items.append(("active", active_job))  # type: ignore[union-attr]

    subject.cancel(active_job)

    wait_for_condition(
        lambda: len(context.processes) == 2 and subject.ready,
        description="replacement child readiness",
    )
    assert len(context.processes) == 2
    assert context.processes[0].terminated == 1
    assert subject.ready
    subject.stop()


def test_active_restart_invalidates_every_old_epoch_job_and_stale_result(
    monkeypatch,
) -> None:
    import server.lookahead.worker as worker_module

    restart_started = Event()
    restart_release = Event()
    restart_release.set()
    context = BlockingRestartContext(restart_started, restart_release)
    monkeypatch.setattr(worker_module, "get_context", lambda _method: context)
    worker = SpeculativeWorker(max_jobs=8, startup_timeout=0, shutdown_timeout=0)
    assert worker.start()
    subject = scheduler(worker, max_queued_jobs=8, max_runway_seconds=60)
    subject.estimator.target_runway_seconds = 60
    subject.schedule_claim(claim(uuid4(), count=5))
    queued = [entry.job for entry in subject._jobs.values()]
    assert len(queued) >= 2
    assert worker._status is not None
    worker._status.items.append(("active", queued[0].id))
    old_epoch = getattr(worker, "epoch", 0)

    try:
        subject.cancel(queued[0].id)

        assert subject.queued_jobs == 0
        assert subject.scheduled_runway_seconds == 0
        assert subject._job_ids_by_key == {}
        assert subject._waiters_by_key == {}
        assert subject._waiters_by_association == {}
        restarted_epoch = getattr(worker, "epoch", 0)
        assert restarted_epoch != old_epoch

        worker.cancel(queued[1].id)
        worker.cancel(queued[1].id)
        assert getattr(worker, "epoch", 0) == restarted_epoch

        subject.handle_result(
            SpeculativeResult(
                queued[1].id,
                queued[1].cache_key,
                b"stale-pcm",
                0.1,
                1.0,
                epoch=old_epoch,
            )
        )
        assert subject.cache.get(queued[1].cache_key, 10.0) is None

        wait_for_condition(
            lambda: len(context.processes) == 2 and worker.ready,
            description="one coalesced replacement worker",
        )
        assert len(context.processes) == 2
    finally:
        worker.stop()

    manager_thread = getattr(worker, "_manager_thread", None)
    assert manager_thread is None or not manager_thread.is_alive()
    assert all(queue.closed for queue in context.created_queues)
    assert all(not process.is_alive() for process in context.processes)


def test_begin_authoritative_returns_while_real_worker_restart_is_blocked(
    monkeypatch,
) -> None:
    import server.lookahead.worker as worker_module

    restart_started = Event()
    restart_release = Event()
    context = BlockingRestartContext(restart_started, restart_release)
    monkeypatch.setattr(worker_module, "get_context", lambda _method: context)
    worker = SpeculativeWorker(max_jobs=8, startup_timeout=0, shutdown_timeout=0)
    assert worker.start()

    now = 10.0
    settings = LookAheadSettings(
        enabled=True, max_queued_jobs=8, max_runway_seconds=60
    )
    store = SnapshotStore()
    store.configure_budgets(
        max_active_streams=8,
        max_global_characters=12_000,
        stream_ttl_seconds=30,
    )
    subject = LookAheadScheduler(
        worker=worker,
        store=store,
        cache=PcmCache(max_bytes=100_000, ttl_seconds=100),
        settings=settings,
        clock=lambda: now,
    )
    subject.estimator.target_runway_seconds = 60
    service = LookAheadService(
        settings=settings,
        store=store,
        scheduler=subject,
        registry=CorrelationRegistry(clock=lambda: now),
        model_version="kokoro",
        clock=lambda: now,
    )
    stream_id = uuid4()
    window = units(5)
    snapshot = LookAheadSnapshot.model_validate(
        {
            "protocol": 1,
            "generation": 1,
            "revision": 1,
            "currentSequence": window[0].sequence,
            "source": {"rate": 1.0, "lang": "en-GB"},
            "synthesisHint": {
                "route": "kokoro",
                "voice": "af_heart",
                "speed": 1.0,
                "format": "pcm_24k_16bit",
            },
            "units": [
                unit.model_dump(by_alias=True, exclude_none=True) for unit in window
            ],
        }
    )
    store.replace(stream_id, snapshot, raw_body_bytes=100, now=now)
    record = store.get(stream_id)
    assert record is not None
    service.observe_snapshot(stream_id, record)
    engine_id = uuid4()

    def headers(request_id: UUID) -> dict[str, str]:
        return {
            "X-VoiceLink-Bridge-Protocol": "2",
            "X-VoiceLink-Engine-Instance": str(engine_id),
            "X-VoiceLink-Request-Id": str(request_id),
        }

    def request(text: str):
        return SimpleNamespace(
            text=text, voice="af_heart", speed=1.0, format="pcm_24k_16bit"
        )

    first = service.begin_authoritative(
        headers(uuid4()), request(window[0].text), now
    )
    service.finish_authoritative(first, b"current-pcm", 0.0, now)
    queued = [entry.job for entry in subject._jobs.values()]
    assert len(queued) >= 2
    assert worker._status is not None
    worker._status.items.append(("active", queued[0].id))
    old_epoch = getattr(worker, "epoch", 0)
    request_returned = Event()

    def invoke_authoritative_request() -> None:
        async def invoke() -> None:
            service.begin_authoritative(
                headers(uuid4()), request(window[1].text), now
            )
            await asyncio.sleep(0)
            request_returned.set()

        asyncio.run(invoke())

    request_thread = Thread(target=invoke_authoritative_request)
    request_thread.start()
    try:
        assert restart_started.wait(1.0), "active cancellation did not begin restart"
        assert request_returned.wait(1.0), (
            "begin_authoritative blocked its event-loop thread on worker restart"
        )
        assert subject.queued_jobs == 0
        assert subject.scheduled_runway_seconds == 0
        restarted_epoch = getattr(worker, "epoch", 0)
        assert restarted_epoch != old_epoch

        worker.cancel(queued[1].id)
        worker.cancel(queued[1].id)
        assert getattr(worker, "epoch", 0) == restarted_epoch

        subject.handle_result(
            SpeculativeResult(
                queued[1].id,
                queued[1].cache_key,
                b"stale-pcm",
                0.1,
                1.0,
                epoch=old_epoch,
            )
        )
        assert subject.cache.get(queued[1].cache_key, now) is None
    finally:
        restart_release.set()
        request_thread.join(2.0)
        worker.stop()

    assert not request_thread.is_alive()
    manager_thread = getattr(worker, "_manager_thread", None)
    assert manager_thread is None or not manager_thread.is_alive()
    assert all(queue.closed for queue in context.created_queues)


def test_worker_lossless_active_transition_restarts_blocked_child_when_status_is_saturated(monkeypatch) -> None:
    import server.lookahead.worker as worker_module
    import server.models

    active_attempted = Event()
    idle_published = Event()
    synthesis_started = Event()
    synthesis_released = Event()
    terminated: list[Event] = []
    jobs_released = Event()

    class Model:
        def synthesize(self, _text: str, *, voice: str, speed: float):
            synthesis_started.set()
            synthesis_released.wait(2)
            if not terminated[0].is_set():
                yield b"stale"

        def unload(self) -> None:
            return None

    monkeypatch.setattr(server.models, "load_model", lambda *_args, **_kwargs: Model())
    monkeypatch.setattr(
        worker_module,
        "get_context",
        lambda _method: ThreadContext(
            active_attempted, terminated, jobs_released, idle_published
        ),
    )
    subject = SpeculativeWorker(
        max_jobs=1,
        startup_timeout=1,
        shutdown_timeout=0.05,
    )
    job = SynthesisJob(uuid4(), uuid4(), 1, 11, "stale", "af_heart", 1.0, "key")
    try:
        assert subject.start()
        assert subject._status is not None
        assert subject.submit(job)
        subject._status.put_nowait(("idle", uuid4()))
        subject._status.put_nowait(("idle", uuid4()))
        jobs_released.set()
        assert active_attempted.wait(1)

        stale_process = subject._process

        subject.cancel(job.id)
        synthesis_released.set()

        wait_for_condition(
            lambda: (subject.drain_results() == [] and idle_published.is_set()),
            description="lossless pre-synthesis cancellation handoff",
        )
        assert subject._process is stale_process
        assert not synthesis_started.is_set()
        assert subject.drain_results() == []
    finally:
        subject.stop()


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
