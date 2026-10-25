"""Conservative correlation between look-ahead snapshots and authoritative TTS."""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import OrderedDict
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
    claim_arrived_at: float
    claim_correlation_digest: str


@dataclass(frozen=True, slots=True)
class _RequestFingerprint:
    engine_instance: UUID
    bridge_protocol: int
    synthesis_digest: str


@dataclass(slots=True)
class _RetryRecord:
    fingerprint: _RequestFingerprint
    decision: CorrelationDecision
    last_seen_at: float
    completed: bool = False
    stream_id: UUID | None = None


@dataclass(slots=True)
class _GenerationSuspension:
    generation: int
    last_seen_at: float


class CorrelationRegistry:
    """Maintain short-lived candidates and conservative per-engine bindings."""

    def __init__(
        self,
        *,
        correlation_ttl_seconds: float = 5.0,
        binding_ttl_seconds: float = 30.0,
        retry_ttl_seconds: float = 30.0,
        max_retry_records: int = 256,
    ) -> None:
        self._correlation_ttl = correlation_ttl_seconds
        self._binding_ttl = binding_ttl_seconds
        self._retry_ttl = retry_ttl_seconds
        self._max_retry_records = max_retry_records
        self._snapshots: dict[UUID, SnapshotRecord] = {}
        self._requests: dict[UUID, AuthoritativeObservation] = {}
        self._retry_records: OrderedDict[UUID, _RetryRecord] = OrderedDict()
        self._retry_ids_by_stream: dict[UUID, set[UUID]] = {}
        self._claimed_requests: set[UUID] = set()
        self._blocked_requests: set[UUID] = set()
        self._blocked_streams: set[UUID] = set()
        self._suspended_generations: dict[UUID, _GenerationSuspension] = {}
        self._bindings_by_engine: dict[UUID, _Binding] = {}
        self._engine_by_stream: dict[UUID, UUID] = {}
        self._in_flight_by_engine: dict[UUID, set[UUID]] = {}
        self._in_flight_engine_by_request: dict[UUID, UUID] = {}
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

            prior = self._retry_records.get(request_id)
            if prior is not None:
                if not self._same_authoritative_call(prior.fingerprint, observation):
                    return self._suspend_for_request(observation, "request-id-conflict")
                prior.last_seen_at = max(prior.last_seen_at, observation.arrived_at)
                self._retry_records.move_to_end(request_id)
                retry = self._decision(
                    CorrelationOutcome.RETRY,
                    "retry",
                    request_id=request_id,
                    claim=prior.decision.claim,
                )
                self._set_retry_decision(request_id, retry)
                return retry

            self._retry_records[request_id] = _RetryRecord(
                fingerprint=self._fingerprint(observation),
                decision=self._decision(
                    CorrelationOutcome.UNCLAIMED,
                    "pending",
                    request_id=request_id,
                ),
                last_seen_at=observation.arrived_at,
            )
            self._trim_retry_records()

            in_flight = self._in_flight_by_engine.setdefault(engine, set())
            self._in_flight_engine_by_request[request_id] = engine
            if in_flight:
                in_flight.add(request_id)
                self._requests[request_id] = observation
                decision = self._suspend_for_request(observation, "concurrent-request")
                self._set_retry_decision(request_id, decision)
                return decision

            in_flight.add(request_id)
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
                self._set_retry_decision(request_id, decision)
                return decision

            binding = self._bindings_by_engine.get(engine)
            if binding is not None:
                decision = self._advance(binding, observation)
                self._set_retry_decision(request_id, decision)
                return decision

            decisions = self._recompute()
            for decision in decisions:
                if decision.request_id == request_id:
                    self._set_retry_decision(request_id, decision)
                    return decision

            outcome = (
                CorrelationOutcome.AMBIGUOUS
                if self._request_degree(observation) > 1
                else CorrelationOutcome.UNCLAIMED
            )
            decision = self._decision(outcome, outcome.value, request_id=request_id)
            self._set_retry_decision(request_id, decision)
            return decision

    def observe_snapshot(
        self, stream_id: UUID, record: SnapshotRecord
    ) -> list[CorrelationDecision]:
        with self._lock:
            self._prune_candidates(record.accepted_at)
            decisions: list[CorrelationDecision] = []
            suspension = self._suspended_generations.get(stream_id)
            if suspension is not None:
                if record.snapshot.generation > suspension.generation:
                    self._suspended_generations.pop(stream_id, None)
                else:
                    suspension.last_seen_at = max(
                        suspension.last_seen_at, record.accepted_at
                    )
                    self._snapshots[stream_id] = record
                    return [
                        self._decision(
                            CorrelationOutcome.SUSPENDED,
                            "generation-suspended",
                        )
                    ]
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
                if self._snapshot_competes_with_binding(stream_id, record, binding):
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

    def complete_request(self, request_id: UUID, *, now: float) -> None:
        with self._lock:
            engine = self._in_flight_engine_by_request.pop(request_id, None)
            if engine is None:
                return
            in_flight = self._in_flight_by_engine.get(engine)
            if in_flight is None:
                return
            in_flight.discard(request_id)
            if not in_flight:
                self._in_flight_by_engine.pop(engine, None)
            retry_record = self._retry_records.get(request_id)
            if retry_record is not None:
                retry_record.completed = True
                retry_record.last_seen_at = max(retry_record.last_seen_at, now)
            self._trim_retry_records()

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
            self._suspended_generations.pop(stream_id, None)
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
            in_flight = self._in_flight_by_engine.pop(engine_instance, set())
            for request_id in in_flight:
                self._in_flight_engine_by_request.pop(request_id, None)
                self._drop_retry_record(request_id)
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
            for stream_id, suspension in tuple(self._suspended_generations.items()):
                if now - suspension.last_seen_at <= self._binding_ttl:
                    continue
                self._suspended_generations.pop(stream_id, None)
                self._snapshots.pop(stream_id, None)
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
            and not self._generation_is_suspended(stream_id, record)
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
                claim_arrived_at=observation.arrived_at,
                claim_correlation_digest=correlation_text_digest(observation.text),
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
            self._set_retry_decision(request_id, decision)
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
            self._set_retry_decision(request_id, decision)
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
        if self._generation_is_suspended(stream_id, record):
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
            and abs(binding.claim_arrived_at - observation.arrived_at)
            <= self._correlation_ttl
        )

    def _snapshot_competes_with_binding(
        self,
        stream_id: UUID,
        record: SnapshotRecord,
        binding: _Binding,
    ) -> bool:
        if self._generation_is_suspended(stream_id, record):
            return False
        if abs(record.accepted_at - binding.claim_arrived_at) > self._correlation_ttl:
            return False
        current = self._unit_at(record.snapshot, record.snapshot.current_sequence)
        if current is None:
            return False
        if correlation_text_digest(current.text) != binding.claim_correlation_digest:
            return False
        hint = record.snapshot.synthesis_hint
        return hint is None or (
            hint.route == binding.config.route
            and hint.voice == binding.config.voice
            and hint.speed == binding.config.speed
            and hint.format == binding.config.format
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
        if engine is not None and engine in self._bindings_by_engine:
            self._suspend_engine(
                engine,
                reason,
                suspended_at=observation.arrived_at,
            )
        decision = self._decision(
            CorrelationOutcome.SUSPENDED,
            reason,
            request_id=request_id,
        )
        self._log(decision)
        return decision

    def _suspend_engine(
        self,
        engine: UUID,
        reason: str,
        *,
        suspended_at: float | None = None,
    ) -> CorrelationDecision:
        binding = self._remove_binding(engine)
        assert binding is not None
        if reason == "config-mismatch":
            self._suspended_generations[binding.stream_id] = _GenerationSuspension(
                generation=binding.generation,
                last_seen_at=(
                    binding.record.accepted_at if suspended_at is None else suspended_at
                ),
            )
        claim = self._claim(binding, binding.next_expected_sequence - 1)
        decision = self._decision(
            CorrelationOutcome.SUSPENDED,
            reason,
            request_id=binding.claim_request_id,
            claim=claim,
        )
        self._set_retry_decision(
            binding.claim_request_id,
            self._decision(
                CorrelationOutcome.SUSPENDED,
                reason,
                request_id=binding.claim_request_id,
            ),
        )
        self._log(decision)
        return decision

    def _remove_binding(self, engine: UUID) -> _Binding | None:
        binding = self._bindings_by_engine.pop(engine, None)
        if binding is not None:
            self._engine_by_stream.pop(binding.stream_id, None)
            self._invalidate_retry_claims(binding.stream_id)
        return binding

    def _prune_candidates(self, now: float) -> None:
        self._prune_retry_records(now)
        expired_requests = {
            request_id
            for request_id, observation in self._requests.items()
            if now - observation.arrived_at > self._correlation_ttl
        }
        for request_id in expired_requests:
            self._requests.pop(request_id)
            self._claimed_requests.discard(request_id)
            self._blocked_requests.discard(request_id)
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
        previous: _RequestFingerprint | None,
        retry: AuthoritativeObservation,
    ) -> bool:
        return previous is not None and (
            previous.engine_instance == retry.engine_instance
            and previous.bridge_protocol == retry.bridge_protocol
            and previous.synthesis_digest
            == synthesis_input_digest(retry.text, retry.config)
        )

    @staticmethod
    def _fingerprint(observation: AuthoritativeObservation) -> _RequestFingerprint:
        assert observation.engine_instance is not None
        return _RequestFingerprint(
            engine_instance=observation.engine_instance,
            bridge_protocol=observation.bridge_protocol,
            synthesis_digest=synthesis_input_digest(
                observation.text,
                observation.config,
            ),
        )

    def _generation_is_suspended(
        self,
        stream_id: UUID,
        record: SnapshotRecord,
    ) -> bool:
        suspension = self._suspended_generations.get(stream_id)
        return (
            suspension is not None
            and record.snapshot.generation <= suspension.generation
        )

    def _invalidate_retry_claims(self, stream_id: UUID) -> None:
        for request_id in self._retry_ids_by_stream.pop(stream_id, set()):
            record = self._retry_records.get(request_id)
            if record is None:
                continue
            decision = record.decision
            record.decision = self._decision(
                decision.outcome,
                decision.reason,
                request_id=request_id,
            )
            record.stream_id = None

    def _set_retry_decision(
        self,
        request_id: UUID,
        decision: CorrelationDecision,
    ) -> None:
        record = self._retry_records.get(request_id)
        if record is None:
            return
        previous_stream = record.stream_id
        current_stream = None if decision.claim is None else decision.claim.stream_id
        if previous_stream != current_stream:
            if previous_stream is not None:
                ids = self._retry_ids_by_stream.get(previous_stream)
                if ids is not None:
                    ids.discard(request_id)
                    if not ids:
                        self._retry_ids_by_stream.pop(previous_stream, None)
            if current_stream is not None:
                self._retry_ids_by_stream.setdefault(current_stream, set()).add(
                    request_id
                )
        record.decision = decision
        record.stream_id = current_stream

    def _drop_retry_record(self, request_id: UUID) -> None:
        record = self._retry_records.pop(request_id, None)
        if record is None or record.stream_id is None:
            return
        ids = self._retry_ids_by_stream.get(record.stream_id)
        if ids is None:
            return
        ids.discard(request_id)
        if not ids:
            self._retry_ids_by_stream.pop(record.stream_id, None)

    def _prune_retry_records(self, now: float) -> None:
        for request_id, record in tuple(self._retry_records.items()):
            if record.completed and now - record.last_seen_at > self._retry_ttl:
                self._drop_retry_record(request_id)
        self._trim_retry_records()

    def _trim_retry_records(self) -> None:
        while len(self._retry_records) > self._max_retry_records:
            completed_id = next(
                (
                    request_id
                    for request_id, record in self._retry_records.items()
                    if record.completed
                ),
                None,
            )
            if completed_id is None:
                return
            self._drop_retry_record(completed_id)

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
