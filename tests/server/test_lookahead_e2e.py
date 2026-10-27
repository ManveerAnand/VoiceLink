from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
from math import ceil
from types import SimpleNamespace
from typing import AsyncIterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.config import LookAheadSettings
from server.lookahead.cache import PcmCache, SynthesisConfig, synthesis_input_digest
from server.lookahead.correlation import CorrelationRegistry
from server.lookahead.scheduler import LookAheadScheduler
from server.lookahead.service import LookAheadService
from server.lookahead.store import SnapshotStore
from server.lookahead.worker import SpeculativeResult
from server.routers import tts as tts_router
from server.routers.lookahead import create_router


class ManualClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeModel:
    is_loaded = True
    model_name = "Kokoro"

    def __init__(self, clock: ManualClock, latency: float = 0.05) -> None:
        self.clock = clock
        self.latency = latency
        self.calls: list[tuple[str, str | None, float]] = []

    def synthesize(self, text: str, voice: str | None = None, speed: float = 1.0):
        self.calls.append((text, voice, speed))
        self.clock.advance(self.latency)
        yield pcm_for(text)


@dataclass
class BlockingFakeWorker:
    ready: bool = True

    def __post_init__(self) -> None:
        self.submitted: list[object] = []
        self.cancelled: list[UUID] = []

    def submit(self, job: object) -> bool:
        self.submitted.append(job)
        return self.ready

    def cancel(self, job_id: UUID) -> None:
        self.cancelled.append(job_id)

    def drain_results(self) -> list[SpeculativeResult]:
        return []


@dataclass
class Stack:
    client: AsyncClient
    clock: ManualClock
    model: FakeModel
    worker: BlockingFakeWorker
    scheduler: LookAheadScheduler
    service: LookAheadService
    cache: PcmCache


def pcm_for(text: str) -> bytes:
    return b"pcm<" + text.encode("utf-8") + b">"


def bridge_headers(engine: UUID) -> dict[str, str]:
    return {
        "X-VoiceLink-Bridge-Protocol": "2",
        "X-VoiceLink-Engine-Instance": str(engine),
        "X-VoiceLink-Request-Id": str(uuid4()),
    }


def snapshot(
    *texts: str,
    generation: int = 1,
    revision: int = 1,
    current_sequence: int = 10,
    voice: str = "af_heart",
    speed: float = 1.0,
) -> dict[str, object]:
    return {
        "protocol": 1,
        "generation": generation,
        "revision": revision,
        "currentSequence": current_sequence,
        "source": {"rate": 1.0, "lang": "en-GB"},
        "synthesisHint": {
            "route": "kokoro",
            "voice": voice,
            "speed": speed,
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
                "boundaryBefore": "paragraph" if offset else "sentence",
                "boundaryAfter": "sentence",
            }
            for offset, text in enumerate(texts)
        ],
    }


def config(*, voice: str = "af_heart", speed: float = 1.0) -> SynthesisConfig:
    return SynthesisConfig(
        route="kokoro",
        model_version="Kokoro",
        voice=voice,
        speed=speed,
        format="pcm_24k_16bit",
        language="",
    )


@asynccontextmanager
async def running_stack(*, enabled: bool = True) -> AsyncIterator[Stack]:
    clock = ManualClock()
    model = FakeModel(clock)
    worker = BlockingFakeWorker()
    scheduler_holder: dict[str, LookAheadScheduler] = {}
    store = SnapshotStore(
        on_cancel_job=lambda job_id: scheduler_holder["scheduler"].cancel(job_id)
    )
    cache = PcmCache(max_bytes=1_000_000, ttl_seconds=600)
    settings = LookAheadSettings(enabled=enabled, metrics_log_interval_seconds=30)
    scheduler = LookAheadScheduler(
        worker=worker,
        store=store,
        cache=cache,
        settings=settings,
        clock=clock,
    )
    scheduler_holder["scheduler"] = scheduler
    service = LookAheadService(
        settings=settings,
        store=store,
        scheduler=scheduler,
        registry=CorrelationRegistry(clock=clock),
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
    tts_router.set_model(model)
    tts_router.set_lookahead_service(service)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield Stack(client, clock, model, worker, scheduler, service, cache)
    finally:
        tts_router.set_lookahead_service(None)
        tts_router.set_model(None)


async def post_tts(
    stack: Stack,
    text: str,
    engine: UUID,
    *,
    voice: str = "af_heart",
    speed: float = 1.0,
):
    return await stack.client.post(
        "/v1/tts",
        headers=bridge_headers(engine),
        json={"text": text, "voice": voice, "speed": speed},
    )


def complete_job(stack: Stack, index: int = 0) -> None:
    job = stack.worker.submitted[index]
    stack.scheduler.handle_result(
        SpeculativeResult(
            job_id=job.id,
            cache_key=job.cache_key,
            pcm=pcm_for(job.text),
            synthesis_seconds=0.02,
            audio_seconds=1.0,
        )
    )


@pytest.mark.asyncio
async def test_full_fake_model_lifecycle_is_ordered_exact_and_observable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="server.lookahead.service")
    secret_current = "PRIVATE-current-7f31"
    secret_next = "PRIVATE-next-9a42"
    stream = uuid4()
    engine = uuid4()

    async with running_stack() as stack:
        capabilities = await stack.client.get("/v1/capabilities")
        accepted = await stack.client.put(
            f"/v1/tts/lookahead/{stream}",
            json=snapshot(secret_current, secret_next),
        )
        current = await post_tts(stack, secret_current, engine)
        complete_job(stack)
        stack.clock.advance(31)
        following = await post_tts(stack, secret_next, engine)
        duplicate = await stack.client.put(
            f"/v1/tts/lookahead/{stream}",
            json=snapshot(secret_current, secret_next),
        )
        stale = await stack.client.put(
            f"/v1/tts/lookahead/{stream}",
            json=snapshot(secret_current, secret_next, revision=0),
        )
        metrics = stack.service.metrics_snapshot()

    assert capabilities.json()["lookahead"]["protocols"] == [1]
    assert accepted.status_code == 202
    assert duplicate.status_code == 200
    assert stale.status_code == 409
    assert current.content == pcm_for(secret_current)
    prefix_bytes = int(following.headers["X-Audio-Prefix-Bytes"])
    assert prefix_bytes == 8_640
    assert following.content == (b"\x00" * prefix_bytes) + pcm_for(secret_next)
    assert stack.model.calls == [(secret_current, "af_heart", 1.0)]
    assert [job.text for job in stack.worker.submitted] == [secret_next]
    assert metrics["snapshot_accepted"] == 1
    assert metrics["snapshot_duplicate"] == 1
    assert metrics["snapshot_rejected"] == 1
    assert metrics["snapshot_stale"] == 1
    assert metrics["correlation_bound"] == 1
    assert metrics["correlation_advanced"] == 1
    assert metrics["cache_hits"] == 1
    assert metrics["cache_misses"] == 1
    assert metrics["prefix_bytes"] == 8_640
    assert metrics["cache_entries"] == 2
    assert metrics["cache_bytes"] == len(pcm_for(secret_current)) + len(
        pcm_for(secret_next)
    )
    assert metrics["queue_size"] == 0
    assert metrics["runway_seconds"] == 0
    assert all(isinstance(value, (int, float)) for value in metrics.values())
    logged = "\n".join(record.getMessage() for record in caplog.records)
    structured = [
        record.lookahead_metrics
        for record in caplog.records
        if hasattr(record, "lookahead_metrics")
    ]
    assert structured
    assert all(
        isinstance(value, (int, float))
        for payload in structured
        for value in payload.values()
    )
    assert secret_current not in logged
    assert secret_next not in logged
    assert all(len(token) != 64 for token in logged.split())


@pytest.mark.asyncio
async def test_seek_generation_cancels_pending_work_and_records_cancellation() -> None:
    stream = uuid4()
    engine = uuid4()
    async with running_stack() as stack:
        await stack.client.put(
            f"/v1/tts/lookahead/{stream}", json=snapshot("old", "obsolete")
        )
        await post_tts(stack, "old", engine)
        old_job = stack.worker.submitted[0]
        replaced = await stack.client.put(
            f"/v1/tts/lookahead/{stream}",
            json=snapshot("replacement", "new future", generation=2, revision=0),
        )

        assert replaced.status_code == 202
        assert old_job.id in stack.worker.cancelled
        assert stack.scheduler.queued_jobs == 0
        assert stack.service.metrics_snapshot()["cancellations"] >= 1


@pytest.mark.asyncio
async def test_metrics_exclude_expired_cache_entries() -> None:
    async with running_stack() as stack:
        stack.cache.put("opaque-test-key", b"pcm", stack.clock())
        stack.clock.advance(601)

        metrics = stack.service.metrics_snapshot()

    assert metrics["cache_entries"] == 0
    assert metrics["cache_bytes"] == 0


@pytest.mark.asyncio
async def test_metrics_logging_failure_does_not_break_authoritative_begin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_logging(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("deterministic logging failure")

    monkeypatch.setattr("server.lookahead.service.logger.info", fail_logging)
    async with running_stack() as stack:
        context = stack.service.begin_authoritative(
            {},
            SimpleNamespace(
                text="private text",
                voice="af_heart",
                speed=1.0,
                format="pcm_24k_16bit",
            ),
            stack.clock(),
        )

    assert context.cache_key is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("voice", "speed"),
    [("bf_emma", 1.0), ("af_heart", 1.25)],
)
async def test_voice_or_rate_mismatch_suspends_and_uses_authoritative_audio(
    voice: str, speed: float
) -> None:
    engine = uuid4()
    async with running_stack() as stack:
        await stack.client.put(
            f"/v1/tts/lookahead/{uuid4()}", json=snapshot("first", "second")
        )
        await post_tts(stack, "first", engine)
        response = await post_tts(stack, "second", engine, voice=voice, speed=speed)

    assert response.content == pcm_for("second")
    assert "X-Audio-Prefix-Bytes" not in response.headers
    assert stack.service.metrics_snapshot()["correlation_suspended"] == 1


@pytest.mark.asyncio
async def test_dual_reader_ambiguity_can_use_seeded_exact_cache_but_never_prefix() -> (
    None
):
    text = "shared exact sentence"
    cached = b"seeded-canonical-pcm"
    async with running_stack() as stack:
        stack.cache.put(synthesis_input_digest(text, config()), cached, stack.clock())
        await stack.client.put(f"/v1/tts/lookahead/{uuid4()}", json=snapshot(text))
        await stack.client.put(f"/v1/tts/lookahead/{uuid4()}", json=snapshot(text))
        response = await post_tts(stack, text, uuid4())

    assert response.content == cached
    assert stack.model.calls == []
    assert "X-Audio-Prefix-Bytes" not in response.headers
    assert "X-Speech-Audio-Length" not in response.headers
    assert stack.service.metrics_snapshot()["correlation_ambiguous"] == 1


@pytest.mark.asyncio
async def test_structural_pause_does_not_grow_runway_and_dead_worker_falls_back() -> (
    None
):
    engine = uuid4()
    async with running_stack() as stack:
        await stack.client.put(
            f"/v1/tts/lookahead/{uuid4()}", json=snapshot("first", "second")
        )
        await post_tts(stack, "first", engine)
        complete_job(stack)
        runway_before = stack.scheduler.scheduled_runway_seconds
        second = await post_tts(stack, "second", engine)
        assert int(second.headers["X-Audio-Prefix-Bytes"]) == 8_640
        assert stack.scheduler.scheduled_runway_seconds == runway_before == 0

        fallback_stream = uuid4()
        fallback_engine = uuid4()
        await stack.client.put(
            f"/v1/tts/lookahead/{fallback_stream}",
            json=snapshot("restart current", "restart next"),
        )
        await post_tts(stack, "restart current", fallback_engine)
        stack.worker.ready = False
        stack.scheduler.poll()
        stack.worker.ready = True
        fallback = await post_tts(stack, "restart next", fallback_engine)

    assert fallback.content.endswith(pcm_for("restart next"))
    assert ("restart next", "af_heart", 1.0) in stack.model.calls


async def authoritative_latencies(*, enabled: bool) -> list[float]:
    texts = tuple(f"unit {index}" for index in range(24))
    engine = uuid4()
    async with running_stack(enabled=enabled) as stack:
        if enabled:
            await stack.client.put(
                f"/v1/tts/lookahead/{uuid4()}", json=snapshot(*texts)
            )
        latencies: list[float] = []
        for text in texts:
            started = stack.clock()
            await post_tts(stack, text, engine)
            latencies.append(stack.clock() - started)
        return latencies


def p95(values: list[float]) -> float:
    return sorted(values)[ceil(len(values) * 0.95) - 1]


@pytest.mark.asyncio
async def test_blocked_speculative_worker_adds_at_most_five_percent_p95_latency() -> (
    None
):
    baseline = await authoritative_latencies(enabled=False)
    isolated = await authoritative_latencies(enabled=True)

    assert p95(isolated) <= p95(baseline) * 1.05
