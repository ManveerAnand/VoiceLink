"""Atomic storage for the latest accepted snapshot of each stream."""

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from threading import RLock
from collections.abc import Callable
from uuid import UUID

from server.lookahead.protocol import LookAheadSnapshot


class ReplaceResult(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    STALE = "stale"
    REVISION_CONFLICT = "revision-conflict"
    BUDGET_EXHAUSTED = "budget-exhausted"


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot: LookAheadSnapshot
    raw_body_bytes: int
    body_digest: str
    accepted_at: float


class SnapshotStore:
    """Keep one replaceable snapshot per stream under a single lock."""

    def __init__(self, *, on_cancel_job: Callable[[str], None] | None = None) -> None:
        self._records: dict[UUID, SnapshotRecord] = {}
        self._pending_jobs: dict[UUID, dict[int, set[str]]] = {}
        self._result_keys: dict[UUID, dict[int, dict[int, str]]] = {}
        self._on_cancel_job = on_cancel_job or (lambda _job_id: None)
        self._lock = RLock()
        self._max_active_streams: int | None = None
        self._max_global_characters: int | None = None

    def configure_budgets(self, *, max_active_streams: int, max_global_characters: int) -> None:
        self._max_active_streams = max_active_streams
        self._max_global_characters = max_global_characters

    def replace(
        self,
        stream_id: UUID,
        snapshot: LookAheadSnapshot,
        raw_body_bytes: int,
        now: float,
    ) -> ReplaceResult:
        """Atomically classify and, only for acceptance, replace a stream snapshot."""
        digest = sha256(raw_body_bytes.to_bytes(8, "big") + snapshot.model_dump_json().encode()).hexdigest()
        # The router supplies the exact body digest through this private attribute so the
        # public method keeps the normative protocol signature.
        digest = getattr(snapshot, "_body_digest", digest)
        cancelled_jobs: tuple[str, ...] = ()
        with self._lock:
            previous = self._records.get(stream_id)
            if previous is not None:
                result = self._compare(previous, snapshot, digest)
                if result is not ReplaceResult.ACCEPTED:
                    return result

            if self._exceeds_budget(stream_id, snapshot):
                return ReplaceResult.BUDGET_EXHAUSTED

            if previous is not None and snapshot.generation > previous.snapshot.generation:
                cancelled_jobs = self._invalidate_generations_before(stream_id, snapshot.generation)
            self._records[stream_id] = SnapshotRecord(
                snapshot=snapshot,
                raw_body_bytes=raw_body_bytes,
                body_digest=digest,
                accepted_at=now,
            )
        for job_id in cancelled_jobs:
            self._on_cancel_job(job_id)
        return ReplaceResult.ACCEPTED

    def track_pending_job(self, stream_id: UUID, *, generation: int, job_id: str) -> bool:
        """Associate speculative work with the currently accepted generation only."""
        with self._lock:
            record = self._records.get(stream_id)
            if record is None or generation != record.snapshot.generation:
                return False
            self._pending_jobs.setdefault(stream_id, {}).setdefault(generation, set()).add(job_id)
            return True

    def track_result(self, stream_id: UUID, *, generation: int, sequence: int, cache_key: str) -> bool:
        """Associate a sequence with an opaque canonical-cache key, never PCM itself."""
        with self._lock:
            record = self._records.get(stream_id)
            if record is None or generation != record.snapshot.generation:
                return False
            self._result_keys.setdefault(stream_id, {}).setdefault(generation, {})[sequence] = cache_key
            return True

    def result_key(self, stream_id: UUID, *, generation: int, sequence: int) -> str | None:
        with self._lock:
            return self._result_keys.get(stream_id, {}).get(generation, {}).get(sequence)

    def _invalidate_generations_before(self, stream_id: UUID, generation: int) -> tuple[str, ...]:
        pending = self._pending_jobs.get(stream_id, {})
        cancelled = tuple(
            job_id
            for old_generation, job_ids in pending.items()
            if old_generation < generation
            for job_id in job_ids
        )
        retained_pending = {
            old_generation: job_ids
            for old_generation, job_ids in pending.items()
            if old_generation >= generation
        }
        if retained_pending:
            self._pending_jobs[stream_id] = retained_pending
        else:
            self._pending_jobs.pop(stream_id, None)

        results = self._result_keys.get(stream_id, {})
        retained_results = {
            old_generation: keys
            for old_generation, keys in results.items()
            if old_generation >= generation
        }
        if retained_results:
            self._result_keys[stream_id] = retained_results
        else:
            self._result_keys.pop(stream_id, None)
        return cancelled

    @staticmethod
    def _compare(
        previous: SnapshotRecord, snapshot: LookAheadSnapshot, digest: str
    ) -> ReplaceResult:
        old = previous.snapshot
        if (
            snapshot.generation < old.generation
            or snapshot.current_sequence < old.current_sequence
        ):
            return ReplaceResult.STALE
        if snapshot.generation == old.generation:
            if snapshot.revision < old.revision:
                return ReplaceResult.STALE
            if snapshot.revision == old.revision:
                return (
                    ReplaceResult.DUPLICATE
                    if digest == previous.body_digest
                    else ReplaceResult.REVISION_CONFLICT
                )
        return ReplaceResult.ACCEPTED

    def _exceeds_budget(self, stream_id: UUID, snapshot: LookAheadSnapshot) -> bool:
        if stream_id not in self._records and self._max_active_streams is not None:
            if len(self._records) >= self._max_active_streams:
                return True
        if self._max_global_characters is None:
            return False
        total = sum(record.snapshot.scalar_count() for record in self._records.values())
        existing = self._records.get(stream_id)
        if existing is not None:
            total -= existing.snapshot.scalar_count()
        return total + snapshot.scalar_count() > self._max_global_characters

    def cancel(self, stream_id: UUID) -> None:
        cancelled_jobs: tuple[str, ...]
        with self._lock:
            self._records.pop(stream_id, None)
            cancelled_jobs = tuple(
                job_id
                for job_ids in self._pending_jobs.pop(stream_id, {}).values()
                for job_id in job_ids
            )
            self._result_keys.pop(stream_id, None)
        for job_id in cancelled_jobs:
            self._on_cancel_job(job_id)

    def get(self, stream_id: UUID) -> SnapshotRecord | None:
        with self._lock:
            return self._records.get(stream_id)
