"""Composition root for optional look-ahead on the authoritative TTS route."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import json
from time import monotonic
from threading import RLock
from typing import Callable, Mapping, Protocol
from uuid import UUID

from loguru import logger

from server.config import LookAheadSettings
from server.lookahead.cache import SynthesisConfig, synthesis_input_digest
from server.lookahead.correlation import (
    AuthoritativeObservation,
    BoundClaim,
    CorrelationDecision,
    CorrelationOutcome,
    CorrelationRegistry,
)
from server.lookahead.prosody import AudioFormat, prefix_silence_pcm
from server.lookahead.scheduler import LookAheadScheduler
from server.lookahead.store import SnapshotRecord, SnapshotStore


BRIDGE_PROTOCOL_HEADER = "X-VoiceLink-Bridge-Protocol"
ENGINE_INSTANCE_HEADER = "X-VoiceLink-Engine-Instance"
REQUEST_ID_HEADER = "X-VoiceLink-Request-Id"
_SAFE_OUTCOMES = {
    CorrelationOutcome.BOUND,
    CorrelationOutcome.ADVANCED,
    CorrelationOutcome.RETRY,
}
_MAX_COUNTER = (1 << 63) - 1
_CORRELATION_REASONS = frozenset(
    {
        "ambiguous",
        "binding-updated",
        "concurrent-request",
        "config-mismatch",
        "exact-next-sequence",
        "expired",
        "generation-suspended",
        "higher-generation",
        "old-bridge",
        "released",
        "request-id-conflict",
        "retry",
        "sequence-gap",
        "suspended",
        "synthesis-key-mismatch",
        "unique-match",
        "unclaimed",
    }
)


class AuthoritativeRequest(Protocol):
    text: str
    voice: str | None
    speed: float
    format: str


@dataclass(slots=True)
class AudioEnvelope:
    prefix_pcm: bytes
    speech_pcm: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def body(self) -> bytes:
        return self.prefix_pcm + self.speech_pcm


@dataclass(slots=True)
class AuthoritativeContext:
    request_id: UUID | None
    observation: AuthoritativeObservation | None
    cache_key: str | None
    cached_speech: bytes | None = None
    claim: BoundClaim | None = None
    outcome: CorrelationOutcome = CorrelationOutcome.LEGACY
    stream_start: bool = False
    finished: bool = False
    envelope: AudioEnvelope | None = None


class LookAheadService:
    """Connect snapshots and authoritative observations without owning synthesis."""

    def __init__(
        self,
        *,
        settings: LookAheadSettings,
        store: SnapshotStore,
        scheduler: LookAheadScheduler,
        registry: CorrelationRegistry | None = None,
        model_version: str | Callable[[], str] = "Kokoro",
        default_voice: str = "af_heart",
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.settings = settings
        self.store = store
        self.scheduler = scheduler
        self.registry = registry or CorrelationRegistry(
            correlation_ttl_seconds=settings.correlation_ttl_seconds,
            binding_ttl_seconds=settings.stream_ttl_seconds,
            clock=clock,
        )
        self._model_version = model_version
        self._default_voice = default_voice
        self._clock = clock
        self._active: dict[UUID, AuthoritativeContext] = {}
        self._stream_start_by_request: OrderedDict[UUID, bool] = OrderedDict()
        self._counters = {
            "snapshot_accepted": 0,
            "snapshot_duplicate": 0,
            "snapshot_rejected": 0,
            "snapshot_stale": 0,
            "snapshot_revision_conflict": 0,
            "snapshot_budget_exhausted": 0,
            "snapshot_invalid": 0,
            "snapshot_over_limit": 0,
            "snapshot_unsupported_protocol": 0,
            **{f"correlation_{outcome.value}": 0 for outcome in CorrelationOutcome},
            **{
                f"correlation_reason_{reason.replace('-', '_')}": 0
                for reason in _CORRELATION_REASONS
            },
            "correlation_reason_other": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "prefix_bytes": 0,
        }
        self._last_metrics_log_at = float("-inf")
        self._lock = RLock()

    def now(self) -> float:
        return self._clock()

    def begin_authoritative(
        self,
        headers: Mapping[str, str],
        request: AuthoritativeRequest,
        now: float,
    ) -> AuthoritativeContext:
        """Observe an authoritative call before any cache lookup or synthesis."""
        if not self.settings.enabled:
            return AuthoritativeContext(None, None, None)

        config = self._config_for(request)
        request_id = self._uuid_header(headers, REQUEST_ID_HEADER)
        observation = AuthoritativeObservation(
            request_id=request_id,
            engine_instance=self._uuid_header(headers, ENGINE_INSTANCE_HEADER),
            bridge_protocol=self._protocol_header(headers),
            text=request.text,
            config=config,
            arrived_at=now,
        )
        self._expire(now)
        decision = self.registry.observe_request(observation)
        try:
            cache_key = synthesis_input_digest(request.text, config)
            cached_speech = self.scheduler.cache.get(cache_key, now)
            context = AuthoritativeContext(
                request_id=request_id,
                observation=observation,
                cache_key=cache_key,
                cached_speech=cached_speech,
                outcome=decision.outcome,
            )
            with self._lock:
                self._record_decision(decision)
                self._increment(
                    "cache_hits" if cached_speech is not None else "cache_misses"
                )
                self._apply_decision(context, decision)
                if request_id is not None:
                    self._active[request_id] = context
            self._emit_metrics_if_due(now)
            return context
        except Exception:
            if request_id is not None:
                self.registry.complete_request(request_id, now=now)
            raise

    def observe_snapshot(
        self, stream_id: UUID, record: SnapshotRecord
    ) -> list[CorrelationDecision]:
        """Feed one accepted snapshot to correlation and schedule every safe claim."""
        if not self.settings.enabled:
            return []
        decisions = self.registry.observe_snapshot(stream_id, record)
        with self._lock:
            for decision in decisions:
                self._record_decision(decision)
                if decision.claim is not None and decision.outcome in _SAFE_OUTCOMES:
                    self.scheduler.schedule_claim(decision.claim)
                if decision.request_id is None:
                    continue
                context = self._active.get(decision.request_id)
                if context is not None:
                    self._apply_decision(context, decision, schedule=False)
                elif decision.claim is not None and decision.outcome in _SAFE_OUTCOMES:
                    self._remember_stream_start(
                        decision.request_id,
                        decision.outcome is CorrelationOutcome.BOUND,
                    )
        self._emit_metrics_if_due(record.accepted_at)
        return decisions

    def finish_authoritative(
        self,
        context: AuthoritativeContext,
        speech_pcm: bytes | None,
        elapsed: float,
        now: float,
    ) -> AudioEnvelope:
        """Complete correlation exactly once and add only safe, non-canonical silence."""
        with self._lock:
            if context.finished:
                return context.envelope or AudioEnvelope(b"", speech_pcm or b"")
            context.finished = True

        try:
            if (
                not self.settings.enabled
                or context.cache_key is None
                or speech_pcm is None
            ):
                envelope = AudioEnvelope(b"", speech_pcm or b"")
            else:
                self.scheduler.cache.put(context.cache_key, speech_pcm, now)
                prefix = self._safe_prefix(context, elapsed)
                with self._lock:
                    self._increment("prefix_bytes", len(prefix))
                if context.claim is None:
                    envelope = AudioEnvelope(b"", speech_pcm)
                else:
                    total = len(prefix) + len(speech_pcm)
                    envelope = AudioEnvelope(
                        prefix,
                        speech_pcm,
                        {
                            "X-Audio-Prefix-Bytes": str(len(prefix)),
                            "X-Speech-Audio-Length": str(len(speech_pcm)),
                            "X-Audio-Length": str(total),
                        },
                    )
            context.envelope = envelope
            self._emit_metrics_if_due(now)
            return envelope
        finally:
            if context.request_id is not None:
                self.registry.complete_request(context.request_id, now=now)
                with self._lock:
                    if self._active.get(context.request_id) is context:
                        self._active.pop(context.request_id, None)

    def cancel_stream(self, stream_id: UUID) -> None:
        decision = self.registry.release_stream(stream_id)
        with self._lock:
            if decision is not None:
                self._record_decision(decision)
            for context in self._active.values():
                if context.claim is not None and context.claim.stream_id == stream_id:
                    context.claim = None
                    context.outcome = CorrelationOutcome.RELEASED
        self.scheduler.cancel_stream(stream_id)
        self._emit_metrics_if_due(self._clock())

    def record_snapshot_status(self, status: str) -> None:
        """Count one bounded protocol result without retaining request data."""
        normalized = status.replace("-", "_")
        name = f"snapshot_{normalized}"
        with self._lock:
            if name in self._counters:
                self._increment(name)
            if status not in {"accepted", "duplicate"}:
                self._increment("snapshot_rejected")
        self._emit_metrics_if_due(self._clock())

    def metrics_snapshot(self, *, now: float | None = None) -> dict[str, int | float]:
        """Return a fixed-size, numeric-only operational snapshot."""
        with self._lock:
            counters = dict(self._counters)
        cache_entries, cache_bytes = self.scheduler.cache.stats(
            self._clock() if now is None else now
        )
        return {
            **counters,
            "speculative_rtf": round(self.scheduler.estimator.rtf, 6),
            "runway_seconds": round(self.scheduler.scheduled_runway_seconds, 6),
            "cancellations": self.scheduler.cancelled_jobs,
            "cache_entries": cache_entries,
            "cache_bytes": cache_bytes,
            "queue_size": self.scheduler.queued_jobs,
        }

    def _increment(self, name: str, amount: int = 1) -> None:
        self._counters[name] = min(_MAX_COUNTER, self._counters[name] + amount)

    def _record_decision(self, decision: CorrelationDecision) -> None:
        self._increment(f"correlation_{decision.outcome.value}")
        reason = decision.reason if decision.reason in _CORRELATION_REASONS else "other"
        self._increment(f"correlation_reason_{reason.replace('-', '_')}")

    def _expire(self, now: float) -> None:
        for decision in self.registry.expire(now):
            with self._lock:
                self._record_decision(decision)
                claim = decision.claim
                if claim is not None:
                    for context in self._active.values():
                        if (
                            context.claim is not None
                            and context.claim.stream_id == claim.stream_id
                        ):
                            context.claim = None
                            context.outcome = CorrelationOutcome.RELEASED
            if claim is not None:
                self.store.cancel(claim.stream_id)
                self.scheduler.cancel_stream(claim.stream_id)

    def _emit_metrics_if_due(self, now: float) -> None:
        with self._lock:
            if (
                now - self._last_metrics_log_at
                < self.settings.metrics_log_interval_seconds
            ):
                return
            self._last_metrics_log_at = now
        try:
            payload = json.dumps(
                self.metrics_snapshot(now=now),
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            logger.info("look-ahead metrics {}", payload)
        except Exception:
            # Instrumentation must never change authoritative synthesis behavior.
            pass

    def _config_for(self, request: AuthoritativeRequest) -> SynthesisConfig:
        version = (
            self._model_version()
            if callable(self._model_version)
            else self._model_version
        )
        return SynthesisConfig(
            route="kokoro",
            model_version=version,
            voice=request.voice or self._default_voice,
            speed=request.speed,
            format=request.format,
            language="",
        )

    def _apply_decision(
        self,
        context: AuthoritativeContext,
        decision: CorrelationDecision,
        *,
        schedule: bool = True,
    ) -> None:
        context.outcome = decision.outcome
        if decision.claim is None or decision.outcome not in _SAFE_OUTCOMES:
            context.claim = None
            return
        context.claim = decision.claim
        if context.request_id is not None:
            if decision.outcome is CorrelationOutcome.BOUND:
                context.stream_start = True
                self._remember_stream_start(context.request_id, True)
            elif decision.outcome is CorrelationOutcome.ADVANCED:
                context.stream_start = False
                self._remember_stream_start(context.request_id, False)
            else:
                context.stream_start = self._stream_start_by_request.get(
                    context.request_id,
                    decision.claim.current_sequence
                    == min(unit.sequence for unit in decision.claim.units),
                )
        if schedule:
            self.scheduler.schedule_claim(decision.claim)

    def _safe_prefix(self, context: AuthoritativeContext, elapsed: float) -> bytes:
        if context.claim is None or context.observation is None:
            return b""
        if context.observation.bridge_protocol != 2:
            return b""
        if context.outcome not in _SAFE_OUTCOMES:
            return b""
        unit = next(
            (
                candidate
                for candidate in context.claim.units
                if candidate.sequence == context.claim.current_sequence
            ),
            None,
        )
        if unit is None:
            return b""
        return prefix_silence_pcm(
            unit.boundary_before,
            max(0.0, elapsed) * 1000.0,
            AudioFormat(sample_rate=24_000, sample_width=2, channels=1),
            stream_start=context.stream_start,
        )

    def _remember_stream_start(self, request_id: UUID, stream_start: bool) -> None:
        self._stream_start_by_request[request_id] = stream_start
        self._stream_start_by_request.move_to_end(request_id)
        while len(self._stream_start_by_request) > 256:
            self._stream_start_by_request.popitem(last=False)

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        direct = headers.get(name)
        if direct is not None:
            return direct
        wanted = name.casefold()
        return next(
            (value for key, value in headers.items() if key.casefold() == wanted), None
        )

    @classmethod
    def _protocol_header(cls, headers: Mapping[str, str]) -> int:
        return 2 if cls._header(headers, BRIDGE_PROTOCOL_HEADER) == "2" else 0

    @classmethod
    def _uuid_header(cls, headers: Mapping[str, str], name: str) -> UUID | None:
        value = cls._header(headers, name)
        if value is None:
            return None
        try:
            return UUID(value)
        except (ValueError, AttributeError):
            return None
