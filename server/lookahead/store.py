"""Atomic storage for the latest accepted snapshot of each stream."""

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from threading import RLock
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

    def __init__(self) -> None:
        self._records: dict[UUID, SnapshotRecord] = {}
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
        with self._lock:
            previous = self._records.get(stream_id)
            if previous is not None:
                result = self._compare(previous, snapshot, digest)
                if result is not ReplaceResult.ACCEPTED:
                    return result

            if self._exceeds_budget(stream_id, snapshot):
                return ReplaceResult.BUDGET_EXHAUSTED

            self._records[stream_id] = SnapshotRecord(
                snapshot=snapshot,
                raw_body_bytes=raw_body_bytes,
                body_digest=digest,
                accepted_at=now,
            )
            return ReplaceResult.ACCEPTED

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
        with self._lock:
            self._records.pop(stream_id, None)

    def get(self, stream_id: UUID) -> SnapshotRecord | None:
        with self._lock:
            return self._records.get(stream_id)
