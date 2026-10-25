"""Bounded adaptive scheduling for the isolated speculative lane."""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from uuid import UUID, uuid4

from server.config import LookAheadSettings
from server.lookahead.cache import PcmCache, SynthesisConfig, synthesis_input_digest
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
            max(
                self.MIN_SECONDS,
                self.rtf * self._expected_synthesis_seconds + self.SAFETY_SECONDS,
            ),
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


class LookAheadScheduler:
    """Keep stream associations separate from canonical exact-key cache entries."""

    def __init__(
        self,
        *,
        worker: object,
        store: SnapshotStore,
        cache: PcmCache,
        settings: LookAheadSettings,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.worker = worker
        self.store = store
        self.cache = cache
        self.settings = settings
        self._clock = clock
        self.estimator = RunwayEstimator()
        self._jobs: dict[UUID, SynthesisJob] = {}
        self._job_ids_by_key: dict[str, UUID] = {}
        self._runway_by_stream: dict[UUID, float] = {}

    @property
    def ready(self) -> bool:
        return bool(self.settings.enabled and getattr(self.worker, "ready", False))

    @property
    def queued_jobs(self) -> int:
        return len(self._jobs)

    @property
    def scheduled_runway_seconds(self) -> float:
        return sum(self._runway_by_stream.values())

    def schedule_claim(self, claim: BoundClaim) -> None:
        """Schedule consecutive future units only; the current unit is authoritative."""
        self.poll()
        if not self.ready:
            return
        available = max(0, self.settings.max_queued_jobs - len(self._jobs))
        if available == 0:
            return
        stream_runway = self._runway_by_stream.get(claim.stream_id, 0.0)
        future = tuple(unit for unit in claim.units if unit.sequence > claim.current_sequence)
        # Selection deliberately stops at the adaptive target.  Iterating the
        # selected prefix preserves sequence order and never skips a gap.
        selected = self.estimator.select(future, stream_runway)
        for unit in selected:
            if available == 0 or stream_runway >= self.settings.max_runway_seconds:
                break
            key = synthesis_input_digest(unit.text, claim.config)
            seconds = self.estimator.seconds_for(unit)
            if self.cache.get(key, self._clock()) is not None:
                self.store.track_result(
                    claim.stream_id, generation=claim.generation, sequence=unit.sequence, cache_key=key
                )
                stream_runway += seconds
                continue
            # A duplicate exact key is already canonical work. Do not enqueue another.
            if key in self._job_ids_by_key:
                continue
            if stream_runway + seconds > self.settings.max_runway_seconds:
                break
            job = SynthesisJob(
                id=uuid4(), stream_id=claim.stream_id, generation=claim.generation,
                sequence=unit.sequence, text=unit.text, voice=claim.config.voice,
                speed=claim.config.speed, cache_key=key,
            )
            if not self.store.track_pending_job(
                claim.stream_id, generation=claim.generation, job_id=job.id
            ):
                # A claim may be supplied before its snapshot has arrived in the store;
                # it is still safe to prefetch, but cannot acquire a stream association.
                pass
            if not self.worker.submit(job):
                break
            self._jobs[job.id] = job
            self._job_ids_by_key[key] = job.id
            stream_runway += seconds
            available -= 1
        self._runway_by_stream[claim.stream_id] = stream_runway

    def cancel(self, job_id: UUID) -> None:
        job = self._jobs.pop(job_id, None)
        if job is not None:
            self._job_ids_by_key.pop(job.cache_key, None)
            self._runway_by_stream[job.stream_id] = max(
                0.0,
                self._runway_by_stream.get(job.stream_id, 0.0)
                - self.estimator.seconds_for_text(job.text),
            )
        self.worker.cancel(job_id)

    def poll(self) -> None:
        drain = getattr(self.worker, "drain_results", None)
        if callable(drain):
            for result in drain():
                self.handle_result(result)
        if not getattr(self.worker, "ready", False) and self._jobs:
            self._detach_all_jobs()

    def handle_result(self, result: SpeculativeResult) -> None:
        """Late results may populate the exact canonical cache, never stale stream state."""
        job = self._jobs.pop(result.job_id, None)
        if job is not None:
            self._job_ids_by_key.pop(job.cache_key, None)
            self._runway_by_stream[job.stream_id] = max(
                0.0,
                self._runway_by_stream.get(job.stream_id, 0.0)
                - self.estimator.seconds_for_text(job.text),
            )
            self.store.complete_pending_job(job.stream_id, generation=job.generation, job_id=job.id)
        # Result carries its immutable exact key, so invalidation cannot redirect PCM.
        self.cache.put(result.cache_key, result.pcm, self._clock())
        self.estimator.observe(result.synthesis_seconds, result.audio_seconds)
        if job is not None:
            self.store.track_result(
                job.stream_id, generation=job.generation, sequence=job.sequence,
                cache_key=result.cache_key,
            )

    def _detach_all_jobs(self) -> None:
        for job in tuple(self._jobs.values()):
            self.store.complete_pending_job(job.stream_id, generation=job.generation, job_id=job.id)
        self._jobs.clear()
        self._job_ids_by_key.clear()
        self._runway_by_stream.clear()
