"""Bounded adaptive scheduling for the isolated speculative lane."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from uuid import UUID, uuid4

from server.config import LookAheadSettings
from server.lookahead.cache import PcmCache, synthesis_input_digest
from server.lookahead.correlation import BoundClaim
from server.lookahead.protocol import LookAheadUnit
from server.lookahead.store import SnapshotStore
from server.lookahead.worker import SpeculativeResult, SynthesisJob


class RunwayEstimator:
    """Estimate a bounded audio runway from observed synthesis real-time factor."""

    ALPHA = 0.25
    MIN_SECONDS = 3.0
    SAFETY_SECONDS = 1.0
    MAX_SECONDS = 60.0
    _CHARACTERS_PER_SECOND = 15.0

    def __init__(self) -> None:
        self.rtf = 1.0
        self._expected_synthesis_seconds = 2.0
        self.target_runway_seconds = self.MIN_SECONDS

    def observe(self, synthesis_seconds: float, audio_seconds: float) -> None:
        if synthesis_seconds < 0 or audio_seconds <= 0:
            return
        sample = synthesis_seconds / audio_seconds
        self.rtf = self.ALPHA * sample + (1 - self.ALPHA) * self.rtf
        self._expected_synthesis_seconds = (
            self.ALPHA * synthesis_seconds
            + (1 - self.ALPHA) * self._expected_synthesis_seconds
        )
        self.target_runway_seconds = min(
            self.MAX_SECONDS,
            max(self.MIN_SECONDS, self.rtf * self._expected_synthesis_seconds + self.SAFETY_SECONDS),
        )

    @classmethod
    def seconds_for(cls, unit: LookAheadUnit) -> float:
        return cls.seconds_for_text(unit.text)

    @classmethod
    def seconds_for_text(cls, text: str) -> float:
        return max(0.1, len(text) / cls._CHARACTERS_PER_SECOND)

    def select(self, units: tuple[LookAheadUnit, ...], cached_seconds: float) -> list[LookAheadUnit]:
        selected: list[LookAheadUnit] = []
        runway = max(0.0, cached_seconds)
        for unit in units:
            if runway >= self.target_runway_seconds:
                break
            selected.append(unit)
            runway += self.seconds_for(unit)
        return selected


@dataclass(frozen=True, slots=True)
class _Waiter:
    stream_id: UUID
    generation: int
    sequence: int
    cache_key: str
    seconds: float
    association_id: UUID
    job_id: UUID


@dataclass(frozen=True, slots=True)
class _QueuedJob:
    job: SynthesisJob
    submitted_at: float


class LookAheadScheduler:
    """Associate exact-key work with every valid stream waiter, under hard bounds."""

    DEFAULT_JOB_TIMEOUT_SECONDS = 30.0

    def __init__(
        self,
        *,
        worker: object,
        store: SnapshotStore,
        cache: PcmCache,
        settings: LookAheadSettings,
        clock: Callable[[], float] = monotonic,
        job_timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
    ) -> None:
        self.worker = worker
        self.store = store
        self.cache = cache
        self.settings = settings
        self._clock = clock
        self._job_timeout_seconds = job_timeout_seconds
        self.estimator = RunwayEstimator()
        self._jobs: dict[UUID, _QueuedJob] = {}
        self._job_ids_by_key: dict[str, UUID] = {}
        self._waiters_by_key: dict[str, dict[tuple[UUID, int, int], _Waiter]] = {}
        self._waiters_by_association: dict[UUID, _Waiter] = {}
        self._runway_by_stream: dict[tuple[UUID, int], float] = {}
        self._failed = False
        self._cancelled_jobs = 0

    @property
    def ready(self) -> bool:
        return bool(self.settings.enabled and not self._failed and getattr(self.worker, "ready", False))

    @property
    def queued_jobs(self) -> int:
        return len(self._jobs)

    @property
    def scheduled_runway_seconds(self) -> float:
        return sum(self._runway_by_stream.values())

    @property
    def cancelled_jobs(self) -> int:
        return self._cancelled_jobs

    def schedule_claim(self, claim: BoundClaim) -> None:
        """Schedule only a contiguous future prefix from a Kokoro claim."""
        self.poll()
        self._discard_stale_stream_generations(claim.stream_id, claim.generation)
        if claim.config.route != "kokoro" or not self.ready:
            return
        available = max(0, self.settings.max_queued_jobs - len(self._jobs))
        if available == 0:
            return
        stream_key = (claim.stream_id, claim.generation)
        stream_runway = self._runway_by_stream.get(stream_key, 0.0)
        future = tuple(unit for unit in claim.units if unit.sequence > claim.current_sequence)
        for unit in self.estimator.select(future, stream_runway):
            if available == 0 or stream_runway >= self.settings.max_runway_seconds:
                break
            key = synthesis_input_digest(unit.text, claim.config)
            seconds = self.estimator.seconds_for(unit)
            if stream_runway + seconds > self.settings.max_runway_seconds:
                break
            if self.cache.get(key, self._clock()) is not None:
                self.store.track_result(claim.stream_id, generation=claim.generation, sequence=unit.sequence, cache_key=key)
                stream_runway += seconds
                continue
            existing_id = self._job_ids_by_key.get(key)
            if existing_id is not None:
                self._add_waiter(claim, unit, key, seconds, existing_id)
                stream_runway += seconds
                continue
            job = SynthesisJob(
                id=uuid4(), stream_id=claim.stream_id, generation=claim.generation,
                sequence=unit.sequence, text=unit.text, voice=claim.config.voice,
                speed=claim.config.speed, cache_key=key,
            )
            association_id = uuid4()
            tracked = self.store.track_pending_job(
                claim.stream_id, generation=claim.generation, job_id=association_id
            )
            if not self.worker.submit(job):
                if tracked:
                    self.store.complete_pending_job(
                        claim.stream_id, generation=claim.generation, job_id=association_id
                    )
                break
            self._jobs[job.id] = _QueuedJob(job=job, submitted_at=self._clock())
            self._job_ids_by_key[key] = job.id
            self._add_waiter(
                claim, unit, key, seconds, job.id,
                association_id=association_id, already_tracked=tracked,
            )
            stream_runway += seconds
            available -= 1
        self._runway_by_stream[stream_key] = stream_runway

    def cancel(self, job_id: UUID) -> None:
        """Detach every stream waiting on this job before best-effort IPC cancellation."""
        waiter = self._waiters_by_association.get(job_id)
        if waiter is not None:
            self._remove_waiter(waiter)
            return
        queued = self._jobs.pop(job_id, None)
        if queued is None:
            self._cancel_worker(job_id)
            return
        key = queued.job.cache_key
        self._job_ids_by_key.pop(key, None)
        for waiter in tuple(self._waiters_by_key.pop(key, {}).values()):
            self._complete_waiter(waiter)
        self._cancel_worker(job_id)

    def cancel_stream(self, stream_id: UUID) -> None:
        """Drop every stream association, including cached-only runway accounting."""
        for waiters in tuple(self._waiters_by_key.values()):
            for waiter in tuple(waiters.values()):
                if waiter.stream_id == stream_id:
                    self._remove_waiter(waiter)
        for stream_key in tuple(self._runway_by_stream):
            if stream_key[0] == stream_id:
                self._runway_by_stream.pop(stream_key, None)

    def poll(self) -> None:
        drain = getattr(self.worker, "drain_results", None)
        if callable(drain):
            for result in drain():
                self.handle_result(result)
        now = self._clock()
        for queued in tuple(self._jobs.values()):
            if now - queued.submitted_at >= self._job_timeout_seconds:
                self.handle_result(SpeculativeResult(queued.job.id, queued.job.cache_key, b"", 0.0, 0.0, failure="timeout"))
        if (not getattr(self.worker, "ready", False) or self._failed) and self._jobs:
            self._detach_all_jobs()

    def handle_result(self, result: SpeculativeResult) -> None:
        """Insert valid canonical PCM, then complete every still-valid exact-key waiter."""
        queued = self._jobs.pop(result.job_id, None)
        if queued is None:
            if result.failure is None:
                self.cache.put(result.cache_key, result.pcm, self._clock())
            return
        expected_key = queued.job.cache_key
        self._job_ids_by_key.pop(expected_key, None)
        waiters = tuple(self._waiters_by_key.pop(expected_key, {}).values())
        for waiter in waiters:
            self._complete_waiter(waiter)
        failure = result.failure
        if result.cache_key != expected_key:
            failure = "cache-key-mismatch"
        if failure is not None:
            self._failed = True
            self._cancel_worker(result.job_id)
            self._detach_all_jobs()
            return
        self.cache.put(expected_key, result.pcm, self._clock())
        self.estimator.observe(result.synthesis_seconds, result.audio_seconds)
        for waiter in waiters:
            self.store.track_result(waiter.stream_id, generation=waiter.generation, sequence=waiter.sequence, cache_key=expected_key)

    def _add_waiter(
        self,
        claim: BoundClaim,
        unit: LookAheadUnit,
        key: str,
        seconds: float,
        job_id: UUID,
        *,
        association_id: UUID | None = None,
        already_tracked: bool = False,
    ) -> None:
        waiter_key = (claim.stream_id, claim.generation, unit.sequence)
        waiters = self._waiters_by_key.setdefault(key, {})
        if waiter_key in waiters:
            return
        association_id = association_id or uuid4()
        if not already_tracked:
            self.store.track_pending_job(
                claim.stream_id, generation=claim.generation, job_id=association_id
            )
        waiter = _Waiter(
            claim.stream_id, claim.generation, unit.sequence, key, seconds,
            association_id, job_id,
        )
        waiters[waiter_key] = waiter
        self._waiters_by_association[association_id] = waiter

    def _complete_waiter(self, waiter: _Waiter) -> None:
        self._waiters_by_association.pop(waiter.association_id, None)
        self.store.complete_pending_job(
            waiter.stream_id, generation=waiter.generation, job_id=waiter.association_id
        )
        stream_key = (waiter.stream_id, waiter.generation)
        remaining = self._runway_by_stream.get(stream_key, 0.0) - waiter.seconds
        if remaining > 0:
            self._runway_by_stream[stream_key] = remaining
        else:
            self._runway_by_stream.pop(stream_key, None)

    def _remove_waiter(self, waiter: _Waiter) -> None:
        waiters = self._waiters_by_key.get(waiter.cache_key)
        if waiters is not None:
            waiters.pop((waiter.stream_id, waiter.generation, waiter.sequence), None)
        self._complete_waiter(waiter)
        if waiters:
            return
        self._waiters_by_key.pop(waiter.cache_key, None)
        worker_job_id = self._job_ids_by_key.pop(waiter.cache_key, None)
        if worker_job_id is not None:
            self._jobs.pop(worker_job_id, None)
            self._cancel_worker(worker_job_id)

    def _discard_stale_stream_generations(self, stream_id: UUID, generation: int) -> None:
        for stream_key in tuple(self._runway_by_stream):
            if stream_key[0] == stream_id and stream_key[1] < generation:
                self._runway_by_stream.pop(stream_key, None)
        for key, waiters in tuple(self._waiters_by_key.items()):
            stale = [waiter for waiter in waiters.values() if waiter.stream_id == stream_id and waiter.generation < generation]
            for waiter in stale:
                self._remove_waiter(waiter)
            if waiters:
                continue
            self._waiters_by_key.pop(key, None)
            job_id = self._job_ids_by_key.pop(key, None)
            if job_id is not None:
                self._jobs.pop(job_id, None)
                self._cancel_worker(job_id)

    def _cancel_worker(self, job_id: UUID) -> None:
        self._cancelled_jobs = min((1 << 63) - 1, self._cancelled_jobs + 1)
        self.worker.cancel(job_id)

    def _detach_all_jobs(self) -> None:
        for job_id in tuple(self._jobs):
            self.cancel(job_id)
