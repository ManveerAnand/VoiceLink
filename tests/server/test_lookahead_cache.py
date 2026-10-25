from uuid import UUID, uuid4

import pytest

from server.lookahead.cache import PcmCache, SynthesisConfig, synthesis_input_digest
from server.lookahead.protocol import LookAheadSnapshot
from server.lookahead.store import ReplaceResult, SnapshotStore


def config(**overrides: object) -> SynthesisConfig:
    values: dict[str, object] = {
        "route": "kokoro",
        "model_version": "kokoro-1.0",
        "voice": "af_heart",
        "speed": 1.0,
        "format": "pcm_24k_16bit",
        "language": "en-GB",
        "options": {"seed": 7},
    }
    values.update(overrides)
    return SynthesisConfig(**values)  # type: ignore[arg-type]


def lookahead_snapshot(generation: int) -> LookAheadSnapshot:
    return LookAheadSnapshot.model_validate(
        {
            "protocol": 1,
            "generation": generation,
            "revision": 1,
            "currentSequence": 1,
            "source": {"rate": 1.0},
            "units": [
                {
                    "sequence": 1,
                    "text": "One.",
                    "block": {"id": "block", "kind": "paragraph"},
                    "position": {"indexInBlock": 0, "firstInBlock": True, "lastInBlock": True},
                    "boundaryBefore": "sentence",
                    "boundaryAfter": "sentence",
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("changed", "value"),
    [
        ("route", "qwen"),
        ("model_version", "kokoro-1.1"),
        ("voice", "af_bella"),
        ("speed", 1.25),
        ("format", "pcm_48k_16bit"),
        ("language", "fr-FR"),
        ("options", {"seed": 8}),
    ],
)
def test_synthesis_input_digest_changes_for_every_synthesis_input(
    changed: str, value: object
) -> None:
    baseline = synthesis_input_digest("a b", config())

    assert synthesis_input_digest("a  b", config()) != baseline
    assert synthesis_input_digest("a b", config(**{changed: value})) != baseline


def test_pcm_cache_evicts_least_recently_used_entries_to_meet_byte_budget() -> None:
    cache = PcmCache(max_bytes=5, ttl_seconds=10)
    assert cache.put("first", b"aa", now=0)
    assert cache.put("second", b"bb", now=0)
    assert cache.get("first", now=1) == b"aa"

    assert cache.put("third", b"cc", now=2)

    assert cache.get("second", now=2) is None
    assert cache.get("first", now=2) == b"aa"
    assert cache.get("third", now=2) == b"cc"


def test_pcm_cache_expires_entries_at_ttl() -> None:
    cache = PcmCache(max_bytes=10, ttl_seconds=3)
    assert cache.put("speech", b"pcm", now=10)

    assert cache.get("speech", now=12.9) == b"pcm"
    assert cache.get("speech", now=13) is None


def test_oversized_pcm_does_not_evict_existing_entries() -> None:
    cache = PcmCache(max_bytes=4, ttl_seconds=10)
    assert cache.put("kept", b"pcm", now=0)

    assert not cache.put("too-large", b"audio", now=1)

    assert cache.get("kept", now=1) == b"pcm"
    assert cache.get("too-large", now=1) is None


def test_higher_generation_discards_stream_work_but_preserves_canonical_pcm() -> None:
    cancelled: list[UUID] = []
    store = SnapshotStore(on_cancel_job=cancelled.append)
    cache = PcmCache(max_bytes=10, ttl_seconds=10)
    stream_id = uuid4()
    key = synthesis_input_digest("One.", config())
    assert cache.put(key, b"pcm", now=0)
    assert store.replace(stream_id, lookahead_snapshot(1), raw_body_bytes=1, now=0) is ReplaceResult.ACCEPTED
    job_id = uuid4()
    assert store.track_pending_job(stream_id, generation=1, job_id=job_id)
    store.track_result(stream_id, generation=1, sequence=1, cache_key=key)

    assert store.replace(stream_id, lookahead_snapshot(2), raw_body_bytes=2, now=1) is ReplaceResult.ACCEPTED

    assert cancelled == [job_id]
    assert store.result_key(stream_id, generation=1, sequence=1) is None
    assert cache.get(key, now=1) == b"pcm"


def test_callback_failure_preserves_accepted_and_idempotent_snapshot_state() -> None:
    def fail_cancellation(_: UUID) -> None:
        raise RuntimeError("worker unavailable")

    store = SnapshotStore(on_cancel_job=fail_cancellation)
    stream_id = uuid4()
    assert store.replace(stream_id, lookahead_snapshot(1), raw_body_bytes=1, now=0) is ReplaceResult.ACCEPTED
    assert store.track_pending_job(stream_id, generation=1, job_id=uuid4())

    assert store.replace(stream_id, lookahead_snapshot(2), raw_body_bytes=2, now=1) is ReplaceResult.ACCEPTED
    assert store.get(stream_id).snapshot.generation == 2  # type: ignore[union-attr]
    assert store.replace(stream_id, lookahead_snapshot(2), raw_body_bytes=2, now=2) is ReplaceResult.DUPLICATE


def test_completed_job_is_not_cancelled_by_a_later_generation() -> None:
    cancelled: list[UUID] = []
    store = SnapshotStore(on_cancel_job=cancelled.append)
    stream_id = uuid4()
    job_id = uuid4()
    assert store.replace(stream_id, lookahead_snapshot(1), raw_body_bytes=1, now=0) is ReplaceResult.ACCEPTED
    assert store.track_pending_job(stream_id, generation=1, job_id=job_id)

    assert store.complete_pending_job(stream_id, generation=1, job_id=job_id)
    assert store.replace(stream_id, lookahead_snapshot(2), raw_body_bytes=2, now=1) is ReplaceResult.ACCEPTED

    assert cancelled == []


def test_pending_job_ids_must_be_uuids() -> None:
    store = SnapshotStore()
    stream_id = uuid4()
    assert store.replace(stream_id, lookahead_snapshot(1), raw_body_bytes=1, now=0) is ReplaceResult.ACCEPTED

    with pytest.raises(TypeError, match="UUID"):
        store.track_pending_job(stream_id, generation=1, job_id="job-1")  # type: ignore[arg-type]
