import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
import logging
from queue import Queue
from threading import Event, Thread
from types import SimpleNamespace
from typing import AsyncIterator, Iterator
from uuid import UUID, uuid4

import numpy as np
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from loguru import logger

from server.config import LookAheadSettings
from server.lookahead.cache import PcmCache, SynthesisConfig, synthesis_input_digest
from server.lookahead.correlation import CorrelationRegistry
from server.lookahead.scheduler import LookAheadScheduler
from server.lookahead.service import AudioEnvelope, LookAheadService
from server.lookahead.store import SnapshotStore
from server.lookahead.worker import (
    SpeculativeResult,
    SynthesisJob,
    _Stop,
    _worker_main,
)
from server.models.kokoro_model import KokoroModel
from server.routers import tts as tts_router
from server.routers.lookahead import create_router


class ManualClock:
    def __init__(self, now: float = 100.0) -> None:
        self.value = now

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class DeterministicModel:
    is_loaded = True
    model_name = "Kokoro"

    def __init__(
        self, clock: ManualClock, waits: dict[str, float] | None = None
    ) -> None:
        self.clock = clock
        self.waits = waits or {}
        self.calls: list[tuple[str, str | None, float]] = []

    def synthesize(self, text: str, voice: str | None = None, speed: float = 1.0):
        self.calls.append((text, voice, speed))
        self.clock.advance(self.waits.get(text, 0.0))
        yield pcm_for(text)


class BlockingModel(DeterministicModel):
    def __init__(self, clock: ManualClock) -> None:
        super().__init__(clock)
        self.started = Event()
        self.release = Event()

    def synthesize(self, text: str, voice: str | None = None, speed: float = 1.0):
        self.calls.append((text, voice, speed))
        self.started.set()
        assert self.release.wait(2.0), "test did not release deterministic model"
        yield pcm_for(text)


class FailingModel(DeterministicModel):
    def synthesize(self, text: str, voice: str | None = None, speed: float = 1.0):
        self.calls.append((text, voice, speed))
        raise RuntimeError("deterministic synthesis failure")
        yield b""  # pragma: no cover - keeps this a generator


@dataclass
class FakeWorker:
    ready: bool = True

    def __post_init__(self) -> None:
        self.submitted: list[object] = []
        self.cancelled: list[UUID] = []

    def submit(self, job: object) -> bool:
        self.submitted.append(job)
        return True

    def cancel(self, job_id: UUID) -> None:
        self.cancelled.append(job_id)

    def drain_results(self) -> list[SpeculativeResult]:
        return []


@dataclass
class Stack:
    client: AsyncClient
    model: object
    clock: ManualClock
    service: LookAheadService
    registry: CorrelationRegistry
    scheduler: LookAheadScheduler
    worker: FakeWorker
    cache: PcmCache


def pcm_for(text: str) -> bytes:
    return b"speech<" + text.encode("utf-8") + b">"


@contextmanager
def capture_standard_logs(messages: list[str]) -> Iterator[None]:
    root_logger = logging.getLogger()
    original_level = root_logger.level

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(self.format(record))

    capture_handler = CaptureHandler()
    try:
        root_logger.addHandler(capture_handler)
        root_logger.setLevel(logging.INFO)
        yield
    finally:
        root_logger.setLevel(original_level)
        root_logger.removeHandler(capture_handler)


def bridge_headers(
    engine_instance: UUID, request_id: UUID | None = None
) -> dict[str, str]:
    return {
        "X-VoiceLink-Bridge-Protocol": "2",
        "X-VoiceLink-Engine-Instance": str(engine_instance),
        "X-VoiceLink-Request-Id": str(request_id or uuid4()),
    }


def synthesis_config(*, voice: str = "af_heart", speed: float = 1.0) -> SynthesisConfig:
    return SynthesisConfig(
        route="kokoro",
        model_version="Kokoro",
        voice=voice,
        speed=speed,
        format="pcm_24k_16bit",
        language="",
    )


def snapshot(
    *texts: str,
    generation: int = 1,
    revision: int = 1,
    current_sequence: int = 10,
    boundaries: tuple[str, ...] | None = None,
) -> dict[str, object]:
    chosen_boundaries = boundaries or tuple("sentence" for _ in texts)
    return {
        "protocol": 1,
        "generation": generation,
        "revision": revision,
        "currentSequence": current_sequence,
        "source": {"rate": 1.0, "lang": "en-GB"},
        "synthesisHint": {
            "route": "kokoro",
            "voice": "af_heart",
            "speed": 1.0,
            "format": "pcm_24k_16bit",
        },
        "units": [
            {
                "sequence": current_sequence + offset,
                "text": text,
                "lang": "en-GB",
                "block": {"id": f"block-{offset}", "kind": "paragraph"},
                "position": {
                    "indexInBlock": offset,
                    "firstInBlock": offset == 0,
                    "lastInBlock": offset == len(texts) - 1,
                },
                "boundaryBefore": chosen_boundaries[offset],
                "boundaryAfter": (
                    chosen_boundaries[offset + 1]
                    if offset + 1 < len(chosen_boundaries)
                    else chosen_boundaries[offset]
                ),
            }
            for offset, text in enumerate(texts)
        ],
    }


@asynccontextmanager
async def running_stack(
    *,
    model: object | None = None,
    enabled: bool = True,
    cache_max_bytes: int = 1_000_000,
) -> AsyncIterator[Stack]:
    clock = getattr(model, "clock", None) or ManualClock()
    actual_model = model or DeterministicModel(clock)
    store = SnapshotStore()
    worker = FakeWorker()
    cache = PcmCache(max_bytes=cache_max_bytes, ttl_seconds=600)
    settings = LookAheadSettings(
        enabled=enabled,
        cache_max_bytes=max(cache_max_bytes, 1),
        cache_ttl_seconds=600,
    )
    scheduler = LookAheadScheduler(
        worker=worker,
        store=store,
        cache=cache,
        settings=settings,
        clock=clock,
    )
    registry = CorrelationRegistry(clock=clock)
    service = LookAheadService(
        settings=settings,
        store=store,
        scheduler=scheduler,
        registry=registry,
        model_version="Kokoro",
        clock=clock,
    )
    app = FastAPI()
    app.include_router(tts_router.router)
    app.include_router(
        create_router(
            settings=settings,
            store=store,
            worker_ready=lambda: scheduler.ready,
            service=service,
        )
    )
    tts_router.set_model(actual_model)
    tts_router.set_lookahead_service(service)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield Stack(
                client,
                actual_model,
                clock,
                service,
                registry,
                scheduler,
                worker,
                cache,
            )
    finally:
        tts_router.set_lookahead_service(None)
        tts_router.set_model(None)


async def put_snapshot(
    stack: Stack, body: dict[str, object], stream_id: UUID | None = None
):
    return await stack.client.put(
        f"/v1/tts/lookahead/{stream_id or uuid4()}", json=body
    )


async def post_tts(
    stack: Stack,
    text: str,
    *,
    headers: dict[str, str] | None = None,
    voice: str = "af_heart",
):
    return await stack.client.post(
        "/v1/tts",
        headers=headers,
        json={"text": text, "voice": voice, "speed": 1.0},
    )


def complete_first_speculative_job(stack: Stack, pcm: bytes) -> None:
    job = stack.worker.submitted[0]
    stack.scheduler.handle_result(
        SpeculativeResult(
            job_id=job.id,
            cache_key=job.cache_key,
            pcm=pcm,
            synthesis_seconds=0.01,
            audio_seconds=1.0,
        )
    )


@pytest.mark.asyncio
async def test_disabled_feature_preserves_legacy_body_and_headers_byte_for_byte() -> (
    None
):
    async with running_stack(enabled=False) as stack:
        response = await post_tts(stack, "legacy output")

    assert response.status_code == 200
    assert response.content == pcm_for("legacy output")
    assert response.headers["X-Audio-Length"] == str(len(response.content))
    assert "X-Audio-Prefix-Bytes" not in response.headers
    assert "X-Speech-Audio-Length" not in response.headers


@pytest.mark.asyncio
async def test_snapshot_first_claim_schedules_future_and_serves_exact_cache_hit() -> (
    None
):
    engine = uuid4()
    async with running_stack() as stack:
        accepted = await put_snapshot(
            stack,
            snapshot("current", "future", boundaries=("sentence", "paragraph")),
        )
        current = await post_tts(stack, "current", headers=bridge_headers(engine))
        assert len(stack.worker.submitted) == 1
        assert stack.worker.submitted[0].text == "future"
        cached = b"canonical-future-pcm"
        complete_first_speculative_job(stack, cached)

        future = await post_tts(stack, "future", headers=bridge_headers(engine))

    assert accepted.status_code == 202
    assert current.content == pcm_for("current")
    assert current.headers["X-Audio-Prefix-Bytes"] == "0"
    assert current.headers["X-Speech-Audio-Length"] == str(len(current.content))
    prefix_bytes = int(future.headers["X-Audio-Prefix-Bytes"])
    assert prefix_bytes == 8_640
    assert future.content == (b"\x00" * prefix_bytes) + cached
    assert future.headers["X-Speech-Audio-Length"] == str(len(cached))
    assert future.headers["X-Audio-Length"] == str(len(future.content))
    assert stack.model.calls == [("current", "af_heart", 1.0)]


@pytest.mark.asyncio
async def test_request_first_late_snapshot_claims_and_serves_future_cache_hit() -> None:
    clock = ManualClock()
    model = BlockingModel(clock)
    engine = uuid4()
    async with running_stack(model=model) as stack:
        request = asyncio.create_task(
            post_tts(stack, "current", headers=bridge_headers(engine))
        )
        assert await asyncio.to_thread(model.started.wait, 1.0)
        accepted = await put_snapshot(
            stack,
            snapshot("current", "future", boundaries=("sentence", "paragraph")),
        )
        model.release.set()
        response = await request
        cached = b"request-first-future-pcm"
        complete_first_speculative_job(stack, cached)
        future = await post_tts(stack, "future", headers=bridge_headers(engine))

        assert accepted.status_code == 202
        assert response.headers["X-Audio-Prefix-Bytes"] == "0"
        assert [job.text for job in stack.worker.submitted] == ["future"]
        assert future.content == (b"\x00" * 8_640) + cached
        assert future.headers["X-Speech-Audio-Length"] == str(len(cached))
        assert model.calls == [("current", "af_heart", 1.0)]


@pytest.mark.asyncio
async def test_correlation_normalization_never_reuses_whitespace_different_pcm() -> (
    None
):
    authoritative = "same   words"
    snapshotted = "same words"
    async with running_stack() as stack:
        stack.cache.put(
            synthesis_input_digest(snapshotted, synthesis_config()),
            b"wrong-canonical-pcm",
            stack.clock(),
        )
        await put_snapshot(stack, snapshot(snapshotted))
        response = await post_tts(stack, authoritative, headers=bridge_headers(uuid4()))

        assert response.content == pcm_for(authoritative)
        assert stack.model.calls == [(authoritative, "af_heart", 1.0)]
        assert response.headers["X-Audio-Prefix-Bytes"] == "0"


@pytest.mark.asyncio
async def test_ambiguous_candidates_use_legacy_audio_without_timing_headers() -> None:
    async with running_stack() as stack:
        await put_snapshot(stack, snapshot("shared sentence"))
        await put_snapshot(stack, snapshot("shared sentence"))
        response = await post_tts(
            stack, "shared sentence", headers=bridge_headers(uuid4())
        )

    assert response.content == pcm_for("shared sentence")
    assert "X-Audio-Prefix-Bytes" not in response.headers
    assert "X-Speech-Audio-Length" not in response.headers


@pytest.mark.asyncio
async def test_transport_retry_does_not_advance_bound_sequence_twice() -> None:
    engine = uuid4()
    request_id = uuid4()
    headers = bridge_headers(engine, request_id)
    async with running_stack() as stack:
        await put_snapshot(stack, snapshot("first", "second"))
        first = await post_tts(stack, "first", headers=headers)
        assert stack.registry.next_expected_sequence(engine) == 11
        retry = await post_tts(stack, "first", headers=headers)
        assert stack.registry.next_expected_sequence(engine) == 11
        second = await post_tts(stack, "second", headers=bridge_headers(engine))

        assert first.status_code == retry.status_code == second.status_code == 200
        assert stack.registry.next_expected_sequence(engine) == 12
        assert [call[0] for call in stack.model.calls] == ["first", "second"]
        assert "X-Audio-Prefix-Bytes" in second.headers


@pytest.mark.asyncio
async def test_overlapping_same_request_id_synthesizes_once_and_mismatched_payloads_do_not_join() -> None:
    clock = ManualClock()
    model = BlockingModel(clock)
    engine, request_id = uuid4(), uuid4()
    headers = bridge_headers(engine, request_id)
    async with running_stack(model=model) as stack:
        first = asyncio.create_task(post_tts(stack, "same", headers=headers))
        assert await asyncio.to_thread(model.started.wait, 1.0)
        same_retry = asyncio.create_task(post_tts(stack, "same", headers=headers))
        await asyncio.sleep(0.01)
        try:
            assert [call[0] for call in model.calls] == ["same"]
        finally:
            model.release.set()
        first_response, retry_response = await asyncio.gather(first, same_retry)

    assert first_response.content == retry_response.content == pcm_for("same")

    mismatched_model = BlockingModel(ManualClock())
    async with running_stack(model=mismatched_model) as stack:
        first = asyncio.create_task(post_tts(stack, "first", headers=headers))
        assert await asyncio.to_thread(mismatched_model.started.wait, 1.0)
        mismatch = asyncio.create_task(post_tts(stack, "second", headers=headers))
        await asyncio.sleep(0.01)
        try:
            assert [call[0] for call in mismatched_model.calls] == ["first", "second"]
        finally:
            mismatched_model.release.set()
        await asyncio.gather(first, mismatch)


@pytest.mark.asyncio
async def test_single_flight_cancelled_waiters_remove_finished_producer_before_later_request() -> None:
    engine, request_id = uuid4(), uuid4()
    headers = bridge_headers(engine, request_id)
    request = tts_router.TTSRequest(text="same", voice="af_heart", speed=1.0, format="pcm")
    producer_started = asyncio.Event()
    producer_release = asyncio.Event()

    async def blocked_producer() -> list[bytes]:
        producer_started.set()
        await producer_release.wait()
        return [b"first"]

    async with running_stack() as stack:
        class LockObserver:
            def __init__(self, lock: asyncio.Lock) -> None:
                self._lock = lock
                self._entries = 0
                self.second_lookup_complete = asyncio.Event()

            async def __aenter__(self) -> object:
                result = await self._lock.__aenter__()
                self._entries += 1
                return result

            async def __aexit__(self, *args: object) -> bool:
                result = await self._lock.__aexit__(*args)
                if self._entries == 2:
                    self.second_lookup_complete.set()
                return result

        lock_observer = LockObserver(stack.service._single_flight_lock)
        stack.service._single_flight_lock = lock_observer  # type: ignore[assignment]
        first = asyncio.create_task(
            stack.service.run_authoritative_singleflight(headers, request, blocked_producer)
        )
        await asyncio.wait_for(producer_started.wait(), timeout=1.0)
        second = asyncio.create_task(
            stack.service.run_authoritative_singleflight(headers, request, blocked_producer)
        )
        await asyncio.wait_for(lock_observer.second_lookup_complete.wait(), timeout=1.0)
        producer_task = next(iter(stack.service._single_flight.values()))
        assert not second.done()

        try:
            first.cancel()
            second.cancel()
            cancelled_waiters = await asyncio.gather(first, second, return_exceptions=True)
            assert all(isinstance(result, asyncio.CancelledError) for result in cancelled_waiters)
            assert not producer_task.done()

            producer_release.set()
            assert await asyncio.wait_for(asyncio.shield(producer_task), timeout=1.0) == [b"first"]
            assert not stack.service._single_flight

            fresh_calls = 0

            async def fresh_producer() -> list[bytes]:
                nonlocal fresh_calls
                fresh_calls += 1
                return [b"fresh"]

            assert await stack.service.run_authoritative_singleflight(
                headers, request, fresh_producer
            ) == [b"fresh"]
            assert fresh_calls == 1
        finally:
            producer_release.set()
            await asyncio.gather(asyncio.shield(producer_task), return_exceptions=True)


@pytest.mark.asyncio
async def test_single_flight_cancelled_waiter_removes_failed_producer() -> None:
    engine, request_id = uuid4(), uuid4()
    headers = bridge_headers(engine, request_id)
    request = tts_router.TTSRequest(text="same", voice="af_heart", speed=1.0, format="pcm")
    producer_started = asyncio.Event()
    producer_release = asyncio.Event()

    async def failing_producer() -> list[bytes]:
        producer_started.set()
        await producer_release.wait()
        raise RuntimeError("deterministic producer failure")

    async with running_stack() as stack:
        waiter = asyncio.create_task(
            stack.service.run_authoritative_singleflight(headers, request, failing_producer)
        )
        await asyncio.wait_for(producer_started.wait(), timeout=1.0)
        producer_task = next(iter(stack.service._single_flight.values()))

        waiter.cancel()
        cancelled = await asyncio.gather(waiter, return_exceptions=True)
        assert isinstance(cancelled[0], asyncio.CancelledError)
        assert not producer_task.done()

        producer_release.set()
        with pytest.raises(RuntimeError, match="deterministic producer failure"):
            await asyncio.wait_for(asyncio.shield(producer_task), timeout=1.0)
        assert not stack.service._single_flight


@pytest.mark.asyncio
async def test_later_config_mismatch_suspends_binding_and_uses_legacy_audio() -> None:
    engine = uuid4()
    async with running_stack() as stack:
        await put_snapshot(stack, snapshot("first", "second"))
        await post_tts(stack, "first", headers=bridge_headers(engine))
        mismatched = await post_tts(
            stack,
            "second",
            headers=bridge_headers(engine),
            voice="bf_emma",
        )

        assert mismatched.content == pcm_for("second")
        assert "X-Audio-Prefix-Bytes" not in mismatched.headers
        assert stack.registry.next_expected_sequence(engine) is None


@pytest.mark.asyncio
async def test_correlated_cache_miss_receives_only_residual_structural_pause() -> None:
    clock = ManualClock()
    model = DeterministicModel(clock, waits={"second": 0.05})
    engine = uuid4()
    async with running_stack(model=model) as stack:
        await put_snapshot(
            stack,
            snapshot("first", "second", boundaries=("sentence", "paragraph")),
        )
        await post_tts(stack, "first", headers=bridge_headers(engine))
        response = await post_tts(stack, "second", headers=bridge_headers(engine))

    assert response.headers["X-Audio-Prefix-Bytes"] == "6240"
    assert response.content == (b"\x00" * 6240) + pcm_for("second")


@pytest.mark.asyncio
async def test_old_bridge_never_receives_prefix_or_new_timing_headers() -> None:
    async with running_stack() as stack:
        await put_snapshot(stack, snapshot("old bridge"))
        response = await post_tts(stack, "old bridge")

    assert response.content == pcm_for("old bridge")
    assert "X-Audio-Prefix-Bytes" not in response.headers
    assert "X-Speech-Audio-Length" not in response.headers


@pytest.mark.asyncio
async def test_old_bridge_can_reuse_exact_canonical_pcm_without_new_headers() -> None:
    cached = b"old-bridge-exact-canonical-pcm"
    async with running_stack() as stack:
        stack.cache.put(
            synthesis_input_digest("old cached", synthesis_config()),
            cached,
            stack.clock(),
        )
        response = await post_tts(stack, "old cached")

    assert response.content == cached
    assert stack.model.calls == []
    assert "X-Audio-Prefix-Bytes" not in response.headers
    assert "X-Speech-Audio-Length" not in response.headers


@pytest.mark.asyncio
async def test_evicted_prefetch_falls_back_to_synthesis_with_safe_residual_pause() -> (
    None
):
    engine = uuid4()
    async with running_stack(cache_max_bytes=32) as stack:
        await put_snapshot(
            stack,
            snapshot("first", "second", boundaries=("sentence", "paragraph")),
        )
        await post_tts(stack, "first", headers=bridge_headers(engine))
        complete_first_speculative_job(stack, b"cached-second")
        stack.cache.put("evictor", b"x" * 32, stack.clock())
        response = await post_tts(stack, "second", headers=bridge_headers(engine))

    assert response.content.endswith(pcm_for("second"))
    assert int(response.headers["X-Audio-Prefix-Bytes"]) == 8_640
    assert [call[0] for call in stack.model.calls] == ["first", "second"]


class ExplodingBeginService:
    def now(self) -> float:
        return 100.0

    def begin_authoritative(self, headers, request, now):
        raise RuntimeError("look-ahead begin exploded")


class ExplodingClockService:
    def now(self) -> float:
        raise RuntimeError("look-ahead clock exploded")


class ExplodingFinishService:
    def now(self) -> float:
        return 100.0

    def begin_authoritative(self, headers, request, now):
        return SimpleNamespace(cached_speech=None)

    def finish_authoritative(self, context, speech_pcm, elapsed, now):
        raise RuntimeError("look-ahead finish exploded")


class FailAfterBeginClockService:
    def __init__(self, delegate: LookAheadService) -> None:
        self.delegate = delegate
        self.now_calls = 0
        self.begin_calls = 0
        self.finish_calls = 0
        self.finished_context = None
        self.finished_speech: bytes | None = b"not-called"

    def now(self) -> float:
        self.now_calls += 1
        if self.now_calls == 1:
            return self.delegate.now()
        raise RuntimeError("clock failed after begin")

    def begin_authoritative(self, headers, request, now):
        self.begin_calls += 1
        return self.delegate.begin_authoritative(headers, request, now)

    def finish_authoritative(self, context, speech_pcm, elapsed, now):
        self.finish_calls += 1
        self.finished_context = context
        self.finished_speech = speech_pcm
        return self.delegate.finish_authoritative(context, speech_pcm, elapsed, now)


class RaiseAfterExactCacheFinishService:
    def __init__(self, delegate: LookAheadService) -> None:
        self.delegate = delegate
        self.finish_calls = 0
        self.finished_context = None

    def now(self) -> float:
        return self.delegate.now()

    def begin_authoritative(self, headers, request, now):
        return self.delegate.begin_authoritative(headers, request, now)

    def finish_authoritative(self, context, speech_pcm, elapsed, now):
        self.finish_calls += 1
        self.finished_context = context
        self.delegate.finish_authoritative(context, speech_pcm, elapsed, now)
        raise RuntimeError("finish failed after exact cache hit")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service",
    [ExplodingClockService(), ExplodingBeginService(), ExplodingFinishService()],
)
async def test_every_service_exception_uses_legacy_synthesis(service: object) -> None:
    async with running_stack() as stack:
        tts_router.set_lookahead_service(service)
        response = await post_tts(stack, "fallback", headers=bridge_headers(uuid4()))

    assert response.status_code == 200
    assert response.content == pcm_for("fallback")
    assert "X-Audio-Prefix-Bytes" not in response.headers
    assert "X-Speech-Audio-Length" not in response.headers


@pytest.mark.asyncio
async def test_clock_failure_after_begin_discards_cache_and_finishes_once() -> None:
    text = "clock failure fallback"
    engine = uuid4()
    async with running_stack() as stack:
        await put_snapshot(stack, snapshot(text))
        stack.cache.put(
            synthesis_input_digest(text, synthesis_config()),
            b"cached-clock-pcm",
            stack.clock(),
        )
        service = FailAfterBeginClockService(stack.service)
        tts_router.set_lookahead_service(service)
        response = await post_tts(stack, text, headers=bridge_headers(engine))

    assert response.content == pcm_for(text)
    assert stack.model.calls == [(text, "af_heart", 1.0)]
    assert response.headers["X-Audio-Length"] == str(len(pcm_for(text)))
    assert "X-Audio-Prefix-Bytes" not in response.headers
    assert "X-Speech-Audio-Length" not in response.headers
    assert service.now_calls == 2
    assert service.begin_calls == 1
    assert service.finish_calls == 1
    assert service.finished_speech is None
    assert service.finished_context.finished is True


@pytest.mark.asyncio
async def test_finish_failure_after_exact_cache_hit_runs_legacy_model_once() -> None:
    text = "finish failure fallback"
    engine = uuid4()
    async with running_stack() as stack:
        await put_snapshot(stack, snapshot(text))
        stack.cache.put(
            synthesis_input_digest(text, synthesis_config()),
            b"cached-finish-pcm",
            stack.clock(),
        )
        service = RaiseAfterExactCacheFinishService(stack.service)
        tts_router.set_lookahead_service(service)
        response = await post_tts(stack, text, headers=bridge_headers(engine))

    assert response.content == pcm_for(text)
    assert stack.model.calls == [(text, "af_heart", 1.0)]
    assert response.headers["X-Audio-Length"] == str(len(pcm_for(text)))
    assert "X-Audio-Prefix-Bytes" not in response.headers
    assert "X-Speech-Audio-Length" not in response.headers
    assert service.finish_calls == 1
    assert service.finished_context.finished is True


class RecordingService:
    def __init__(self) -> None:
        self.finishes: list[bytes | None] = []

    def now(self) -> float:
        return 100.0

    def begin_authoritative(self, headers, request, now):
        return SimpleNamespace(cached_speech=None)

    def finish_authoritative(self, context, speech_pcm, elapsed, now):
        self.finishes.append(speech_pcm)
        return AudioEnvelope(b"", speech_pcm or b"")


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_tts_finishes_each_started_authoritative_call_exactly_once(
    fails: bool,
) -> None:
    clock = ManualClock()
    model = FailingModel(clock) if fails else DeterministicModel(clock)
    recorder = RecordingService()
    async with running_stack(model=model) as stack:
        tts_router.set_lookahead_service(recorder)
        response = await post_tts(stack, "finish once")

    assert response.status_code == (500 if fails else 200)
    assert len(recorder.finishes) == 1
    assert (recorder.finishes[0] is None) is fails


def test_stdlib_info_capture() -> None:
    root_logger = logging.getLogger()
    original_level = root_logger.level
    original_handlers = list(root_logger.handlers)
    messages: list[str] = []

    with capture_standard_logs(messages):
        logging.getLogger("test-stdlib-info-capture").info("info probe")

    assert messages == ["info probe"]
    assert root_logger.level == original_level
    assert root_logger.handlers == original_handlers


@pytest.mark.asyncio
async def test_protocol_worker_cache_and_tts_logs_never_contain_raw_secret() -> None:
    secret = "SECRET-7f31-book-sentence-never-log"
    future_secret = "FUTURE-SECRET-a94c-prefetch-never-log"
    clock = ManualClock()
    model = KokoroModel()
    model._loaded = True

    def fake_pipeline(text: str, *, voice: str, speed: float):
        yield text, "phonemes", np.zeros(4, dtype=np.float32)

    model._pipeline = fake_pipeline
    model.clock = clock
    messages: list[str] = []
    standard_messages: list[str] = []

    sink = logger.add(messages.append, level="DEBUG", format="{message}")
    try:
        with capture_standard_logs(standard_messages):
            async with running_stack(model=model) as stack:
                await put_snapshot(stack, snapshot(secret, future_secret))
                response = await post_tts(stack, secret, headers=bridge_headers(uuid4()))
                assert response.status_code == 200
    finally:
        logger.remove(sink)

    combined = "".join(messages + standard_messages)
    assert secret not in combined
    assert future_secret not in combined


def test_worker_main_logging_boundary_never_contains_future_text(monkeypatch) -> None:
    import server.models

    future_secret = "FUTURE-SECRET-5d12-real-worker-never-log"
    model = KokoroModel()
    model._loaded = True

    def fake_pipeline(text: str, *, voice: str, speed: float):
        yield text, "phonemes", np.zeros(4, dtype=np.float32)

    model._pipeline = fake_pipeline
    monkeypatch.setattr(server.models, "load_model", lambda *_args, **_kwargs: model)
    jobs: Queue = Queue(maxsize=2)
    controls: Queue = Queue(maxsize=2)
    permissions: Queue = Queue(maxsize=1)
    results: Queue = Queue(maxsize=2)
    status: Queue = Queue(maxsize=2)
    job = SynthesisJob(
        uuid4(), uuid4(), 1, 11, future_secret, "af_heart", 1.0, "future-key"
    )
    messages: list[str] = []
    standard_messages: list[str] = []
    worker_thread = Thread(
        target=_worker_main,
        args=(
            {"lang_code": "a", "device": "cpu"},
            jobs,
            controls,
            permissions,
            results,
            status,
        ),
    )
    sink = logger.add(messages.append, level="DEBUG", format="{message}")
    try:
        with capture_standard_logs(standard_messages):
            worker_thread.start()
            assert status.get(timeout=1.0) == ("ready", None)
            jobs.put(job)
            assert status.get(timeout=1.0) == ("active", job.id)
            permissions.put(job.id)
            result = results.get(timeout=1.0)
            assert isinstance(result, SpeculativeResult)
            assert result.job_id == job.id
    finally:
        controls.put(_Stop())
        worker_thread.join(1.0)
        logger.remove(sink)

    assert not worker_thread.is_alive()
    assert future_secret not in "".join(messages + standard_messages)
