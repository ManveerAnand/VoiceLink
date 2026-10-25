"""Conservative correlation between look-ahead snapshots and authoritative TTS."""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from threading import RLock
from uuid import UUID

from server.lookahead.cache import SynthesisConfig, synthesis_input_digest
from server.lookahead.protocol import LookAheadSnapshot, LookAheadUnit
from server.lookahead.store import SnapshotRecord


logger = logging.getLogger(__name__)
_UNICODE_WHITESPACE = re.compile(r"\s+")


def correlation_text_digest(text: str) -> str:
    """Hash the narrowly normalized text used only for candidate discovery."""
    normalized = unicodedata.normalize("NFC", text).replace("\r\n", "\n")
    normalized = _UNICODE_WHITESPACE.sub(" ", normalized)
    return sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuthoritativeObservation:
    request_id: UUID | None
    engine_instance: UUID | None
    bridge_protocol: int
    text: str
    config: SynthesisConfig
    arrived_at: float


@dataclass(frozen=True, slots=True)
class BoundClaim:
    """Immutable scheduler input, deliberately free of authoritative request text."""

    stream_id: UUID
    generation: int
    current_sequence: int
    units: tuple[LookAheadUnit, ...]
    config: SynthesisConfig


class CorrelationOutcome(str, Enum):
    UNCLAIMED = "unclaimed"
    AMBIGUOUS = "ambiguous"
    BOUND = "bound"
    ADVANCED = "advanced"
    RETRY = "retry"
    SUSPENDED = "suspended"
    RELEASED = "released"
    LEGACY = "legacy"


@dataclass(frozen=True, slots=True)
class CorrelationDecision:
    outcome: CorrelationOutcome
    reason: str
    request_id: UUID | None = None
    claim: BoundClaim | None = None


@dataclass(slots=True)
class _Binding:
    stream_id: UUID
    engine_instance: UUID
    generation: int
    next_expected_sequence: int
    config: SynthesisConfig
    record: SnapshotRecord
    claim_request_id: UUID
    claim_observation: AuthoritativeObservation


class CorrelationRegistry:
    """Maintain short-lived candidates and conservative per-engine bindings."""

    def __init__(
        self,
        *,
        correlation_ttl_seconds: float = 5.0,
        binding_ttl_seconds: float = 30.0,
    ) -> None:
        self._correlation_ttl = correlation_ttl_seconds
        self._binding_ttl = binding_ttl_seconds
        self._snapshots: dict[UUID, SnapshotRecord] = {}
        self._requests: dict[UUID, AuthoritativeObservation] = {}
        self._request_decisions: dict[UUID, CorrelationDecision] = {}
        self._claimed_requests: set[UUID] = set()
        self._blocked_requests: set[UUID] = set()
        self._blocked_streams: set[UUID] = set()
        self._bindings_by_engine: dict[UUID, _Binding] = {}
        self._engine_by_stream: dict[UUID, UUID] = {}
        self._in_flight_by_engine: dict[UUID, UUID] = {}
        self._lock = RLock()

    def observe_request(
        self, observation: AuthoritativeObservation
    ) -> CorrelationDecision:
        with self._lock:
            self._prune_candidates(observation.arrived_at)
            if not self._is_protocol_two(observation):
                legacy_engine = observation.engine_instance
                if (
                    legacy_engine is not None
                    and legacy_engine in self._bindings_by_engine
                ):
                    self._suspend_engine(legacy_engine, "legacy-request")
                return self._decision(CorrelationOutcome.LEGACY, "old-bridge")

            request_id = observation.request_id
            engine = observation.engine_instance
            assert request_id is not None and engine is not None

            prior = self._request_decisions.get(request_id)
            if prior is not None:
                if not self._same_authoritative_call(
                    self._requests.get(request_id), observation
                ):
                    return self._suspend_for_request(observation, "request-id-conflict")
                retry = self._decision(
                    CorrelationOutcome.RETRY,
                    "retry",
                    request_id=request_id,
                    claim=prior.claim,
                )
                self._request_decisions[request_id] = retry
                return retry

            in_flight = self._in_flight_by_engine.get(engine)
            if in_flight is not None and in_flight != request_id:
                self._requests[request_id] = observation
                decision = self._suspend_for_request(observation, "concurrent-request")
                self._request_decisions[request_id] = decision
                return decision

            self._in_flight_by_engine[engine] = request_id
            self._requests[request_id] = observation

            competing_bindings = [
                existing
                for existing in tuple(self._bindings_by_engine.values())
                if existing.engine_instance != engine
                if self._competes_with_bound_snapshot(existing, observation)
            ]
            if competing_bindings:
                for existing in competing_bindings:
                    self._blocked_streams.add(existing.stream_id)
                    self._suspend_engine(existing.engine_instance, "ambiguous")
                self._blocked_requests.add(request_id)
                decision = self._decision(
                    CorrelationOutcome.AMBIGUOUS,
                    "ambiguous",
                    request_id=request_id,
                )
                self._request_decisions[request_id] = decision
                return decision

            binding = self._bindings_by_engine.get(engine)
            if binding is not None:
                decision = self._advance(binding, observation)
                self._request_decisions[request_id] = decision
                return decision

            decisions = self._recompute()
            for decision in decisions:
                if decision.request_id == request_id:
                    self._request_decisions[request_id] = decision
                    return decision

            outcome = (
                CorrelationOutcome.AMBIGUOUS
                if self._request_degree(observation) > 1
                else CorrelationOutcome.UNCLAIMED
            )
            decision = self._decision(outcome, outcome.value, request_id=request_id)
            self._request_decisions[request_id] = decision
            return decision

    def observe_snapshot(
        self, stream_id: UUID, record: SnapshotRecord
    ) -> list[CorrelationDecision]:
        with self._lock:
            self._prune_candidates(record.accepted_at)
            decisions: list[CorrelationDecision] = []
            engine = self._engine_by_stream.get(stream_id)
            if engine is not None:
                binding = self._bindings_by_engine[engine]
                snapshot = record.snapshot
                if snapshot.generation > binding.generation:
                    decisions.append(self._suspend_engine(engine, "higher-generation"))
                elif snapshot.generation < binding.generation:
                    return decisions
                elif snapshot.current_sequence > binding.next_expected_sequence:
                    decisions.append(self._suspend_engine(engine, "sequence-gap"))
                else:
                    binding.record = record
                    decisions.append(
                        self._decision(
                            CorrelationOutcome.BOUND,
                            "binding-updated",
                            request_id=binding.claim_request_id,
                            claim=self._claim(binding, snapshot.current_sequence),
                        )
                    )

            self._snapshots[stream_id] = record

            for bound_engine, binding in tuple(self._bindings_by_engine.items()):
                if binding.stream_id == stream_id:
                    continue
                if self._eligible_pair(
                    stream_id, record, binding.claim_observation, ignore_bound=True
                ):
                    self._blocked_streams.update((stream_id, binding.stream_id))
                    decisions.append(self._suspend_engine(bound_engine, "ambiguous"))
                    decisions.append(
                        self._decision(CorrelationOutcome.AMBIGUOUS, "ambiguous")
                    )

            decisions.extend(self._recompute())
            if decisions:
                return decisions

            outcome = (
                CorrelationOutcome.AMBIGUOUS
                if self._snapshot_degree(stream_id, record) > 1
                else CorrelationOutcome.UNCLAIMED
            )
            return [self._decision(outcome, outcome.value)]

    def complete_request(self, request_id: UUID) -> None:
        with self._lock:
            observation = self._requests.get(request_id)
            if observation is None or observation.engine_instance is None:
                return
            if self._in_flight_by_engine.get(observation.engine_instance) == request_id:
                self._in_flight_by_engine.pop(observation.engine_instance, None)

    def suspend_engine(
        self, engine_instance: UUID, reason: str = "suspended"
    ) -> CorrelationDecision | None:
        with self._lock:
            if engine_instance not in self._bindings_by_engine:
                return None
            return self._suspend_engine(engine_instance, reason)

    def release_stream(self, stream_id: UUID) -> CorrelationDecision | None:
        with self._lock:
            self._snapshots.pop(stream_id, None)
            self._blocked_streams.discard(stream_id)
            engine = self._engine_by_stream.get(stream_id)
            if engine is None:
                return None
            binding = self._remove_binding(engine)
            assert binding is not None
            decision = self._decision(
                CorrelationOutcome.RELEASED,
                "released",
                claim=self._claim(binding, binding.next_expected_sequence - 1),
            )
            self._log(decision)
            return decision

    def release_engine(self, engine_instance: UUID) -> CorrelationDecision | None:
        with self._lock:
            binding = self._remove_binding(engine_instance)
            self._in_flight_by_engine.pop(engine_instance, None)
            if binding is None:
                return None
            decision = self._decision(
                CorrelationOutcome.RELEASED,
                "released",
                claim=self._claim(binding, binding.next_expected_sequence - 1),
            )
            self._log(decision)
            return decision

    def expire(self, now: float) -> list[CorrelationDecision]:
        with self._lock:
            self._prune_candidates(now)
            expired: list[CorrelationDecision] = []
            for engine, binding in tuple(self._bindings_by_engine.items()):
                if now - binding.record.accepted_at <= self._binding_ttl:
                    continue
                removed = self._remove_binding(engine)
                assert removed is not None
                self._snapshots.pop(removed.stream_id, None)
                decision = self._decision(
                    CorrelationOutcome.RELEASED,
                    "expired",
                    claim=self._claim(removed, removed.next_expected_sequence - 1),
                )
                self._log(decision)
                expired.append(decision)
            return expired

    def bound_stream_ids(self) -> tuple[UUID, ...]:
        with self._lock:
            return tuple(self._engine_by_stream)

    def next_expected_sequence(self, engine_instance: UUID) -> int | None:
        with self._lock:
            binding = self._bindings_by_engine.get(engine_instance)
            return None if binding is None else binding.next_expected_sequence

    def _advance(
        self, binding: _Binding, observation: AuthoritativeObservation
    ) -> CorrelationDecision:
        request_id = observation.request_id
        assert request_id is not None
        if observation.config != binding.config:
            return self._suspend_for_request(observation, "config-mismatch")

        expected = binding.next_expected_sequence
        unit = self._unit_at(binding.record.snapshot, expected)
        if unit is None:
            return self._suspend_for_request(observation, "sequence-gap")
        if synthesis_input_digest(
            observation.text, observation.config
        ) != synthesis_input_digest(unit.text, binding.config):
            return self._suspend_for_request(observation, "synthesis-key-mismatch")
        if (
            self._request_degree(
                observation,
                exclude_stream_id=binding.stream_id,
                ignore_bound=True,
            )
            > 0
        ):
            self._blocked_streams.add(binding.stream_id)
            self._suspend_engine(binding.engine_instance, "ambiguous")
            return self._decision(
                CorrelationOutcome.SUSPENDED,
                "ambiguous",
                request_id=request_id,
            )

        binding.next_expected_sequence += 1
        self._claimed_requests.add(request_id)
        claim = self._claim(binding, expected)
        decision = self._decision(
            CorrelationOutcome.ADVANCED,
            "exact-next-sequence",
            request_id=request_id,
            claim=claim,
        )
        self._log(decision)
        return decision

    def _recompute(self) -> list[CorrelationDecision]:
        snapshots = {
            stream_id: record
            for stream_id, record in self._snapshots.items()
            if stream_id not in self._engine_by_stream
            and stream_id not in self._blocked_streams
        }
        requests = {
            request_id: observation
            for request_id, observation in self._requests.items()
            if request_id not in self._claimed_requests
            and request_id not in self._blocked_requests
            and observation.engine_instance not in self._bindings_by_engine
        }
        snapshot_edges: dict[UUID, set[UUID]] = {
            stream_id: set() for stream_id in snapshots
        }
        request_edges: dict[UUID, set[UUID]] = {
            request_id: set() for request_id in requests
        }
        for stream_id, record in snapshots.items():
            for request_id, observation in requests.items():
                if self._eligible_pair(stream_id, record, observation):
                    snapshot_edges[stream_id].add(request_id)
                    request_edges[request_id].add(stream_id)

        unique_pairs = [
            (stream_id, next(iter(request_ids)))
            for stream_id, request_ids in snapshot_edges.items()
            if len(request_ids) == 1
            and len(request_edges[next(iter(request_ids))]) == 1
        ]
        decisions: list[CorrelationDecision] = []
        for stream_id, request_id in unique_pairs:
            if (
                stream_id in self._engine_by_stream
                or request_id in self._claimed_requests
            ):
                continue
            observation = requests[request_id]
            engine = observation.engine_instance
            assert engine is not None
            if engine in self._bindings_by_engine:
                continue
            record = snapshots[stream_id]
            binding = _Binding(
                stream_id=stream_id,
                engine_instance=engine,
                generation=record.snapshot.generation,
                next_expected_sequence=record.snapshot.current_sequence + 1,
                config=observation.config,
                record=record,
                claim_request_id=request_id,
                claim_observation=observation,
            )
            self._bindings_by_engine[engine] = binding
            self._engine_by_stream[stream_id] = engine
            self._claimed_requests.add(request_id)
            claim = self._claim(binding, record.snapshot.current_sequence)
            decision = self._decision(
                CorrelationOutcome.BOUND,
                "unique-match",
                request_id=request_id,
                claim=claim,
            )
            self._request_decisions[request_id] = decision
            self._log(decision)
            decisions.append(decision)

        ambiguous_requests = {
            request_id for request_id, edges in request_edges.items() if len(edges) > 1
        }
        ambiguous_streams = {
            stream_id for stream_id, edges in snapshot_edges.items() if len(edges) > 1
        }
        self._blocked_requests.update(ambiguous_requests)
        self._blocked_streams.update(ambiguous_streams)
        for request_id in ambiguous_requests:
            self._blocked_streams.update(request_edges[request_id])
        for stream_id in ambiguous_streams:
            self._blocked_requests.update(snapshot_edges[stream_id])
        for request_id in ambiguous_requests:
            decision = self._decision(
                CorrelationOutcome.AMBIGUOUS, "ambiguous", request_id=request_id
            )
            self._request_decisions[request_id] = decision
            decisions.append(decision)
        if ambiguous_streams and not ambiguous_requests:
            decisions.append(self._decision(CorrelationOutcome.AMBIGUOUS, "ambiguous"))
        return decisions

    def _eligible_pair(
        self,
        stream_id: UUID,
        record: SnapshotRecord,
        observation: AuthoritativeObservation,
        *,
        ignore_bound: bool = False,
    ) -> bool:
        if not self._is_protocol_two(observation):
            return False
        if abs(record.accepted_at - observation.arrived_at) > self._correlation_ttl:
            return False
        if not ignore_bound:
            if stream_id in self._engine_by_stream:
                return False
            if observation.engine_instance in self._bindings_by_engine:
                return False
        current = self._unit_at(record.snapshot, record.snapshot.current_sequence)
        if current is None:
            return False
        if correlation_text_digest(current.text) != correlation_text_digest(
            observation.text
        ):
            return False
        hint = record.snapshot.synthesis_hint
        return hint is None or (
            hint.route == observation.config.route
            and hint.voice == observation.config.voice
            and hint.speed == observation.config.speed
            and hint.format == observation.config.format
        )

    def _competes_with_bound_snapshot(
        self, binding: _Binding, observation: AuthoritativeObservation
    ) -> bool:
        if observation.request_id == binding.claim_request_id:
            return False
        return (
            self._eligible_pair(
                binding.stream_id,
                binding.record,
                observation,
                ignore_bound=True,
            )
            and abs(binding.claim_observation.arrived_at - observation.arrived_at)
            <= self._correlation_ttl
        )

    def _request_degree(
        self,
        observation: AuthoritativeObservation,
        *,
        exclude_stream_id: UUID | None = None,
        ignore_bound: bool = False,
    ) -> int:
        return sum(
            self._eligible_pair(
                stream_id,
                record,
                observation,
                ignore_bound=ignore_bound,
            )
            for stream_id, record in self._snapshots.items()
            if stream_id not in self._blocked_streams and stream_id != exclude_stream_id
        )

    def _snapshot_degree(self, stream_id: UUID, record: SnapshotRecord) -> int:
        return sum(
            self._eligible_pair(stream_id, record, observation)
            for request_id, observation in self._requests.items()
            if request_id not in self._claimed_requests
        )

    def _suspend_for_request(
        self, observation: AuthoritativeObservation, reason: str
    ) -> CorrelationDecision:
        engine = observation.engine_instance
        request_id = observation.request_id
        claim = None
        if engine is not None and engine in self._bindings_by_engine:
            suspended = self._suspend_engine(engine, reason)
            claim = suspended.claim
        decision = self._decision(
            CorrelationOutcome.SUSPENDED,
            reason,
            request_id=request_id,
            claim=claim,
        )
        self._log(decision)
        return decision

    def _suspend_engine(self, engine: UUID, reason: str) -> CorrelationDecision:
        binding = self._remove_binding(engine)
        assert binding is not None
        claim = self._claim(binding, binding.next_expected_sequence - 1)
        decision = self._decision(
            CorrelationOutcome.SUSPENDED,
            reason,
            request_id=binding.claim_request_id,
            claim=claim,
        )
        self._request_decisions[binding.claim_request_id] = decision
        self._log(decision)
        return decision

    def _remove_binding(self, engine: UUID) -> _Binding | None:
        binding = self._bindings_by_engine.pop(engine, None)
        if binding is not None:
            self._engine_by_stream.pop(binding.stream_id, None)
        return binding

    def _prune_candidates(self, now: float) -> None:
        expired_requests = {
            request_id
            for request_id, observation in self._requests.items()
            if request_id not in self._claimed_requests
            and now - observation.arrived_at > self._correlation_ttl
        }
        for request_id in expired_requests:
            observation = self._requests.pop(request_id)
            self._request_decisions.pop(request_id, None)
            self._blocked_requests.discard(request_id)
            if (
                observation.engine_instance is not None
                and self._in_flight_by_engine.get(observation.engine_instance)
                == request_id
            ):
                self._in_flight_by_engine.pop(observation.engine_instance, None)
        for stream_id, record in tuple(self._snapshots.items()):
            if stream_id in self._engine_by_stream:
                continue
            if now - record.accepted_at > self._correlation_ttl:
                self._snapshots.pop(stream_id, None)
                self._blocked_streams.discard(stream_id)

    @staticmethod
    def _is_protocol_two(observation: AuthoritativeObservation) -> bool:
        return (
            observation.bridge_protocol == 2
            and isinstance(observation.request_id, UUID)
            and isinstance(observation.engine_instance, UUID)
        )

    @staticmethod
    def _same_authoritative_call(
        previous: AuthoritativeObservation | None,
        retry: AuthoritativeObservation,
    ) -> bool:
        return previous is not None and (
            previous.request_id == retry.request_id
            and previous.engine_instance == retry.engine_instance
            and previous.bridge_protocol == retry.bridge_protocol
            and previous.text == retry.text
            and previous.config == retry.config
        )

    @staticmethod
    def _unit_at(snapshot: LookAheadSnapshot, sequence: int) -> LookAheadUnit | None:
        for unit in snapshot.units:
            if unit.sequence == sequence:
                return unit
        return None

    @staticmethod
    def _claim(binding: _Binding, current_sequence: int) -> BoundClaim:
        return BoundClaim(
            stream_id=binding.stream_id,
            generation=binding.generation,
            current_sequence=current_sequence,
            units=tuple(binding.record.snapshot.units),
            config=binding.config,
        )

    @staticmethod
    def _decision(
        outcome: CorrelationOutcome,
        reason: str,
        *,
        request_id: UUID | None = None,
        claim: BoundClaim | None = None,
    ) -> CorrelationDecision:
        return CorrelationDecision(outcome, reason, request_id, claim)

    @staticmethod
    def _log(decision: CorrelationDecision) -> None:
        claim = decision.claim
        logger.info(
            "look-ahead correlation outcome=%s reason=%s stream_id=%s generation=%s sequence=%s",
            decision.outcome.value,
            decision.reason,
            None if claim is None else claim.stream_id,
            None if claim is None else claim.generation,
            None if claim is None else claim.current_sequence,
        )
