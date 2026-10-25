from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from server.lookahead.cache import SynthesisConfig
from server.lookahead.correlation import (
    AuthoritativeObservation,
    CorrelationDecision,
    CorrelationOutcome,
    CorrelationRegistry,
    correlation_text_digest,
)
from server.lookahead.protocol import LookAheadSnapshot
from server.lookahead.store import SnapshotRecord


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


def snapshot_record(
    *texts: str,
    accepted_at: float = 100.0,
    generation: int = 1,
    current_sequence: int = 10,
    hint: dict[str, object] | None = None,
    source: dict[str, object] | None = None,
) -> SnapshotRecord:
    payload: dict[str, object] = {
        "protocol": 1,
        "generation": generation,
        "revision": 1,
        "currentSequence": current_sequence,
        "source": source or {"rate": 9.0, "voiceName": "provider-neutral"},
        "units": [
            {
                "sequence": current_sequence + offset,
                "text": text,
                "block": {"id": "block", "kind": "paragraph"},
                "position": {
                    "indexInBlock": offset,
                    "firstInBlock": offset == 0,
                    "lastInBlock": offset == len(texts) - 1,
                },
                "boundaryBefore": "sentence",
                "boundaryAfter": "sentence",
            }
            for offset, text in enumerate(texts)
        ],
    }
    if hint is not None:
        payload["synthesisHint"] = hint
    snapshot = LookAheadSnapshot.model_validate(payload)
    return SnapshotRecord(
        snapshot, 100, f"body-{generation}-{current_sequence}", accepted_at
    )


def observation(
    text: str,
    *,
    arrived_at: float = 100.0,
    request_id: UUID | None = None,
    engine_instance: UUID | None = None,
    bridge_protocol: int = 2,
    synthesis_config: SynthesisConfig | None = None,
) -> AuthoritativeObservation:
    return AuthoritativeObservation(
        request_id=request_id or uuid4(),
        engine_instance=engine_instance or uuid4(),
        bridge_protocol=bridge_protocol,
        text=text,
        config=synthesis_config or config(),
        arrived_at=arrived_at,
    )


def assert_bound(
    decision: CorrelationDecision, stream_id: UUID, sequence: int = 10
) -> None:
    assert decision.outcome is CorrelationOutcome.BOUND
    assert decision.reason == "unique-match"
    assert decision.claim is not None
    assert decision.claim.stream_id == stream_id
    assert decision.claim.current_sequence == sequence


def test_correlation_digest_normalizes_only_nfc_newlines_and_unicode_whitespace() -> (
    None
):
    canonical = correlation_text_digest("Caf\u00e9\nnext sentence")

    assert correlation_text_digest("Cafe\u0301\r\nnext\u2003\t sentence") == canonical
    assert correlation_text_digest("caf\u00e9\nnext sentence") != canonical
    assert correlation_text_digest("Caf\u00e9,\nnext sentence") != canonical


def test_snapshot_first_claims_a_mutually_unique_request() -> None:
    registry = CorrelationRegistry()
    stream_id = uuid4()
    registry.observe_snapshot(stream_id, snapshot_record("One sentence."))

    decision = registry.observe_request(observation("One sentence."))

    assert_bound(decision, stream_id)
    assert decision.claim is not None
    assert decision.claim.config == config()
    assert tuple(unit.text for unit in decision.claim.units) == ("One sentence.",)


def test_request_first_is_claimed_when_snapshot_arrives() -> None:
    registry = CorrelationRegistry()
    stream_id = uuid4()
    request = observation("One sentence.")
    assert registry.observe_request(request).outcome is CorrelationOutcome.UNCLAIMED

    decisions = registry.observe_snapshot(stream_id, snapshot_record("One sentence."))

    assert len(decisions) == 1
    assert_bound(decisions[0], stream_id)
    assert decisions[0].request_id == request.request_id


@pytest.mark.parametrize(
    ("difference", "expected"),
    [(5.0, CorrelationOutcome.BOUND), (5.000001, CorrelationOutcome.UNCLAIMED)],
)
def test_five_second_eligibility_boundary_is_exact(
    difference: float, expected: CorrelationOutcome
) -> None:
    registry = CorrelationRegistry(correlation_ttl_seconds=5.0)
    stream_id = uuid4()
    registry.observe_snapshot(stream_id, snapshot_record("Boundary.", accepted_at=10.0))

    decision = registry.observe_request(
        observation("Boundary.", arrived_at=10.0 + difference)
    )

    assert decision.outcome is expected


def test_claimed_authoritative_text_is_removed_after_candidate_ttl() -> None:
    registry = CorrelationRegistry(correlation_ttl_seconds=5.0)
    stream_id = uuid4()
    request = observation("Private\u2003phrase", arrived_at=10.0)
    registry.observe_snapshot(
        stream_id, snapshot_record("Private phrase", accepted_at=10.0)
    )
    assert_bound(registry.observe_request(request), stream_id)

    registry.expire(15.000001)

    assert request.request_id not in registry._requests  # type: ignore[attr-defined]
    assert all(
        not hasattr(binding, "claim_observation")
        for binding in registry._bindings_by_engine.values()  # type: ignore[attr-defined]
    )
    assert "Private\u2003phrase" not in repr(vars(registry))


def test_absent_hint_is_neutral_and_exact_hint_matches_actual_config() -> None:
    hint = {
        "route": "kokoro",
        "voice": "af_heart",
        "speed": 1.0,
        "format": "pcm_24k_16bit",
    }
    absent = CorrelationRegistry()
    exact = CorrelationRegistry()

    absent_stream = uuid4()
    absent.observe_snapshot(absent_stream, snapshot_record("No hint."))
    assert_bound(absent.observe_request(observation("No hint.")), absent_stream)
    stream_id = uuid4()
    exact.observe_snapshot(stream_id, snapshot_record("Exact hint.", hint=hint))
    assert_bound(exact.observe_request(observation("Exact hint.")), stream_id)


@pytest.mark.parametrize(
    "hint",
    [
        {
            "route": "other",
            "voice": "af_heart",
            "speed": 1.0,
            "format": "pcm_24k_16bit",
        },
        {"route": "kokoro", "voice": "other", "speed": 1.0, "format": "pcm_24k_16bit"},
        {
            "route": "kokoro",
            "voice": "af_heart",
            "speed": 1.1,
            "format": "pcm_24k_16bit",
        },
        {"route": "kokoro", "voice": "af_heart", "speed": 1.0, "format": "wav"},
    ],
)
def test_any_mismatched_synthesis_hint_field_prevents_claim(
    hint: dict[str, object],
) -> None:
    registry = CorrelationRegistry()
    registry.observe_snapshot(uuid4(), snapshot_record("Mismatch.", hint=hint))

    assert (
        registry.observe_request(observation("Mismatch.")).outcome
        is CorrelationOutcome.UNCLAIMED
    )


def test_provider_neutral_source_is_not_a_correlation_predicate() -> None:
    registry = CorrelationRegistry()
    stream_id = uuid4()
    record = snapshot_record(
        "Same speech.", source={"rate": 99.0, "voiceName": "not-af-heart", "lang": "fr"}
    )
    registry.observe_snapshot(stream_id, record)

    assert_bound(registry.observe_request(observation("Same speech.")), stream_id)


def test_two_snapshots_for_one_request_are_ambiguous_in_both_arrival_orders() -> None:
    snapshot_first = CorrelationRegistry()
    request_first = CorrelationRegistry()
    streams = (uuid4(), uuid4())
    for stream_id in streams:
        snapshot_first.observe_snapshot(stream_id, snapshot_record("Repeated."))
    first_decision = snapshot_first.observe_request(observation("Repeated."))

    request_first.observe_request(observation("Repeated."))
    request_first.observe_snapshot(streams[0], snapshot_record("Repeated."))
    late_decisions = request_first.observe_snapshot(
        streams[1], snapshot_record("Repeated.")
    )

    assert first_decision.outcome is CorrelationOutcome.AMBIGUOUS
    assert snapshot_first.bound_stream_ids() == ()
    assert any(
        decision.outcome is CorrelationOutcome.AMBIGUOUS for decision in late_decisions
    )
    assert request_first.bound_stream_ids() == ()


def test_ambiguous_request_never_claims_later_within_its_observation_window() -> None:
    registry = CorrelationRegistry()
    first_stream = uuid4()
    second_stream = uuid4()
    registry.observe_snapshot(first_stream, snapshot_record("Repeated."))
    registry.observe_snapshot(second_stream, snapshot_record("Repeated."))
    request = observation("Repeated.")
    assert registry.observe_request(request).outcome is CorrelationOutcome.AMBIGUOUS

    registry.release_stream(second_stream)
    decisions = registry.observe_snapshot(uuid4(), snapshot_record("Unrelated."))

    assert not any(
        decision.outcome is CorrelationOutcome.BOUND for decision in decisions
    )
    assert registry.bound_stream_ids() == ()


def test_one_snapshot_for_two_requests_is_ambiguous_in_both_arrival_orders() -> None:
    request_first = CorrelationRegistry()
    snapshot_first = CorrelationRegistry()
    stream_id = uuid4()
    requests = (
        observation("Repeated.", engine_instance=uuid4()),
        observation("Repeated.", engine_instance=uuid4()),
    )
    for request in requests:
        request_first.observe_request(request)
    decisions = request_first.observe_snapshot(stream_id, snapshot_record("Repeated."))

    snapshot_first.observe_snapshot(stream_id, snapshot_record("Repeated."))
    snapshot_first.observe_request(requests[0])
    late_decision = snapshot_first.observe_request(requests[1])

    assert any(
        decision.outcome is CorrelationOutcome.AMBIGUOUS for decision in decisions
    )
    assert request_first.bound_stream_ids() == ()
    assert late_decision.outcome is CorrelationOutcome.AMBIGUOUS
    assert snapshot_first.bound_stream_ids() == ()


def test_unrelated_digests_do_not_affect_mutual_uniqueness() -> None:
    registry = CorrelationRegistry()
    matching_stream = uuid4()
    registry.observe_snapshot(matching_stream, snapshot_record("Match."))
    registry.observe_snapshot(uuid4(), snapshot_record("Unrelated."))
    registry.observe_request(observation("Different."))

    assert_bound(registry.observe_request(observation("Match.")), matching_stream)


def test_bound_stream_advances_exactly_once_and_retry_does_not_double_advance() -> None:
    registry = CorrelationRegistry()
    stream_id = uuid4()
    engine = uuid4()
    registry.observe_snapshot(stream_id, snapshot_record("First.", "Second.", "Third."))
    first = observation("First.", engine_instance=engine)
    assert_bound(registry.observe_request(first), stream_id)
    assert first.request_id is not None
    registry.complete_request(first.request_id)
    second = observation("Second.", engine_instance=engine)

    advanced = registry.observe_request(second)
    retry = registry.observe_request(
        observation(
            "Second.",
            arrived_at=101.0,
            request_id=second.request_id,
            engine_instance=engine,
        )
    )

    assert advanced.outcome is CorrelationOutcome.ADVANCED
    assert advanced.claim is not None and advanced.claim.current_sequence == 11
    assert retry.outcome is CorrelationOutcome.RETRY
    assert retry.claim is not None and retry.claim.current_sequence == 11
    assert registry.next_expected_sequence(engine) == 12


def test_competing_unclaimed_snapshot_suspends_instead_of_advancing() -> None:
    registry = CorrelationRegistry()
    bound_stream = uuid4()
    competing_stream = uuid4()
    engine = uuid4()
    registry.observe_snapshot(bound_stream, snapshot_record("First.", "Repeated."))
    first = observation("First.", engine_instance=engine)
    assert_bound(registry.observe_request(first), bound_stream)
    assert first.request_id is not None
    registry.complete_request(first.request_id)
    registry.observe_snapshot(competing_stream, snapshot_record("Repeated."))

    decision = registry.observe_request(
        observation("Repeated.", engine_instance=engine)
    )

    assert decision.outcome is CorrelationOutcome.SUSPENDED
    assert decision.reason == "ambiguous"
    assert registry.bound_stream_ids() == ()


def test_consecutive_identical_units_advance_by_expected_sequence() -> None:
    registry = CorrelationRegistry()
    stream_id = uuid4()
    engine = uuid4()
    registry.observe_snapshot(stream_id, snapshot_record("Repeated.", "Repeated."))
    first = observation("Repeated.", engine_instance=engine)
    assert_bound(registry.observe_request(first), stream_id)
    assert first.request_id is not None
    registry.complete_request(first.request_id)

    decision = registry.observe_request(
        observation("Repeated.", engine_instance=engine)
    )

    assert decision.outcome is CorrelationOutcome.ADVANCED
    assert decision.claim is not None and decision.claim.current_sequence == 11


@pytest.mark.parametrize(
    ("text", "changed_config", "record_current"),
    [
        ("Unexpected.", None, 10),
        ("Second.", config(speed=1.25), 10),
        ("Second.", None, 12),
    ],
)
def test_unexpected_text_config_or_sequence_suspends_binding(
    text: str, changed_config: SynthesisConfig | None, record_current: int
) -> None:
    registry = CorrelationRegistry()
    stream_id = uuid4()
    engine = uuid4()
    registry.observe_snapshot(stream_id, snapshot_record("First.", "Second."))
    first = observation("First.", engine_instance=engine)
    assert_bound(registry.observe_request(first), stream_id)
    assert first.request_id is not None
    registry.complete_request(first.request_id)
    if record_current != 10:
        decisions = registry.observe_snapshot(
            stream_id,
            snapshot_record("Third.", generation=1, current_sequence=record_current),
        )
        assert any(
            decision.outcome is CorrelationOutcome.SUSPENDED for decision in decisions
        )
        return

    decision = registry.observe_request(
        observation(text, engine_instance=engine, synthesis_config=changed_config)
    )

    assert decision.outcome is CorrelationOutcome.SUSPENDED
    assert registry.bound_stream_ids() == ()


def test_concurrent_different_request_ids_on_one_engine_suspend() -> None:
    registry = CorrelationRegistry()
    stream_id = uuid4()
    engine = uuid4()
    registry.observe_snapshot(stream_id, snapshot_record("First.", "Second."))
    first = observation("First.", engine_instance=engine)
    assert_bound(registry.observe_request(first), stream_id)

    concurrent = registry.observe_request(
        observation("Second.", engine_instance=engine)
    )

    assert concurrent.outcome is CorrelationOutcome.SUSPENDED
    assert concurrent.reason == "concurrent-request"
    assert registry.bound_stream_ids() == ()


def test_candidate_expiry_does_not_clear_authoritative_in_flight_occupancy() -> None:
    registry = CorrelationRegistry(correlation_ttl_seconds=5.0)
    engine = uuid4()
    first = observation("No snapshot.", arrived_at=10.0, engine_instance=engine)
    assert registry.observe_request(first).outcome is CorrelationOutcome.UNCLAIMED

    registry.expire(15.000001)
    second = registry.observe_request(
        observation("Still running.", arrived_at=16.0, engine_instance=engine)
    )

    assert second.outcome is CorrelationOutcome.SUSPENDED
    assert second.reason == "concurrent-request"
    assert first.request_id is not None
    registry.complete_request(first.request_id)

    third = registry.observe_request(
        observation("Second still running.", arrived_at=17.0, engine_instance=engine)
    )

    assert third.outcome is CorrelationOutcome.SUSPENDED
    assert third.reason == "concurrent-request"


def test_higher_generation_can_uniquely_rebind_after_suspension() -> None:
    registry = CorrelationRegistry()
    stream_id = uuid4()
    engine = uuid4()
    registry.observe_snapshot(stream_id, snapshot_record("Old."))
    old = observation("Old.", engine_instance=engine)
    assert_bound(registry.observe_request(old), stream_id)
    assert old.request_id is not None
    registry.complete_request(old.request_id)

    generation_decisions = registry.observe_snapshot(
        stream_id,
        snapshot_record("New.", generation=2, current_sequence=40, accepted_at=101.0),
    )
    rebound = registry.observe_request(
        observation("New.", engine_instance=engine, arrived_at=101.0)
    )

    assert any(
        decision.outcome is CorrelationOutcome.SUSPENDED
        for decision in generation_decisions
    )
    assert_bound(rebound, stream_id, sequence=40)
    assert rebound.claim is not None and rebound.claim.generation == 2


def test_config_mismatch_requires_a_higher_generation_to_rebind() -> None:
    registry = CorrelationRegistry()
    stream_id = uuid4()
    engine = uuid4()
    registry.observe_snapshot(stream_id, snapshot_record("First.", "Second."))
    first = observation("First.", engine_instance=engine)
    assert_bound(registry.observe_request(first), stream_id)
    assert first.request_id is not None
    registry.complete_request(first.request_id)

    mismatch = observation(
        "Second.",
        arrived_at=101.0,
        engine_instance=engine,
        synthesis_config=config(speed=1.25),
    )
    assert registry.observe_request(mismatch).reason == "config-mismatch"
    assert mismatch.request_id is not None
    registry.complete_request(mismatch.request_id)

    same_generation = registry.observe_snapshot(
        stream_id,
        snapshot_record(
            "Second.", generation=1, current_sequence=11, accepted_at=101.1
        ),
    )
    assert not any(
        decision.outcome is CorrelationOutcome.BOUND for decision in same_generation
    )
    assert registry.bound_stream_ids() == ()

    registry.expire(106.1)
    registry.observe_snapshot(
        stream_id,
        snapshot_record(
            "Second.", generation=2, current_sequence=11, accepted_at=106.1
        ),
    )
    rebound = registry.observe_request(
        observation("Second.", arrived_at=106.1, engine_instance=engine)
    )

    assert_bound(rebound, stream_id, sequence=11)
    assert rebound.claim is not None and rebound.claim.generation == 2


def test_bound_snapshot_refresh_returns_updated_scheduler_claim() -> None:
    registry = CorrelationRegistry()
    stream_id = uuid4()
    engine = uuid4()
    registry.observe_snapshot(stream_id, snapshot_record("First.", "Second."))
    first = observation("First.", engine_instance=engine)
    assert_bound(registry.observe_request(first), stream_id)

    decisions = registry.observe_snapshot(
        stream_id, snapshot_record("First.", "Second.", "Third.", accepted_at=101.0)
    )

    assert len(decisions) == 1
    assert decisions[0].outcome is CorrelationOutcome.BOUND
    assert decisions[0].reason == "binding-updated"
    assert decisions[0].claim is not None
    assert tuple(unit.text for unit in decisions[0].claim.units) == (
        "First.",
        "Second.",
        "Third.",
    )


def test_delete_and_ttl_release_bindings() -> None:
    registry = CorrelationRegistry(binding_ttl_seconds=30.0)
    deleted_stream = uuid4()
    expired_stream = uuid4()
    registry.observe_snapshot(deleted_stream, snapshot_record("Delete."))
    assert_bound(registry.observe_request(observation("Delete.")), deleted_stream)
    released = registry.release_stream(deleted_stream)

    registry.observe_snapshot(
        expired_stream, snapshot_record("Expire.", accepted_at=200.0)
    )
    assert_bound(
        registry.observe_request(observation("Expire.", arrived_at=200.0)),
        expired_stream,
    )
    expired = registry.expire(230.000001)

    assert released is not None and released.outcome is CorrelationOutcome.RELEASED
    assert any(
        decision.claim.stream_id == expired_stream
        for decision in expired
        if decision.claim
    )
    assert registry.bound_stream_ids() == ()


@pytest.mark.parametrize("release_kind", ["delete", "ttl"])
def test_released_binding_retry_never_returns_a_claim(release_kind: str) -> None:
    registry = CorrelationRegistry(binding_ttl_seconds=30.0)
    stream_id = uuid4()
    engine = uuid4()
    registry.observe_snapshot(stream_id, snapshot_record("Release.", accepted_at=100.0))
    request = observation("Release.", arrived_at=100.0, engine_instance=engine)
    assert_bound(registry.observe_request(request), stream_id)
    assert request.request_id is not None

    retry_at = 101.0
    if release_kind == "delete":
        registry.release_stream(stream_id)
    else:
        registry.expire(130.000001)
        retry_at = 131.0
    retry = registry.observe_request(
        observation(
            "Release.",
            arrived_at=retry_at,
            request_id=request.request_id,
            engine_instance=engine,
        )
    )

    assert retry.outcome is CorrelationOutcome.RETRY
    assert retry.claim is None


@pytest.mark.parametrize(
    ("protocol", "request_id", "engine"),
    [(1, uuid4(), uuid4()), (2, None, uuid4()), (2, uuid4(), None)],
)
def test_old_or_incomplete_bridge_requests_never_bind(
    protocol: int, request_id: UUID | None, engine: UUID | None
) -> None:
    registry = CorrelationRegistry()
    registry.observe_snapshot(uuid4(), snapshot_record("Legacy."))
    request = AuthoritativeObservation(
        request_id=request_id,
        engine_instance=engine,
        bridge_protocol=protocol,
        text="Legacy.",
        config=config(),
        arrived_at=100.0,
    )

    decision = registry.observe_request(request)

    assert decision.outcome is CorrelationOutcome.LEGACY
    assert decision.claim is None
    assert registry.bound_stream_ids() == ()
