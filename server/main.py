# ============================================================================
# VoiceLink — Inference Server Entry Point
# ============================================================================
#
# WHAT THIS FILE DOES:
# 1. Creates the FastAPI application
# 2. Loads the TTS model on startup (in a lifespan context manager)
# 3. Mounts the API routers (/v1/tts, /v1/voices, /v1/health)
# 4. Runs with Uvicorn when executed directly
#
# HOW TO RUN:
#   From the VoiceLink project root:
#     python -m server.main
#
#   Or with uvicorn directly (supports hot-reload for development):
#     uvicorn server.main:app --host 127.0.0.1 --port 7860 --reload
#
# LIFESPAN:
# FastAPI's lifespan context manager handles startup and shutdown.
# This is where we load the model (startup) and unload it (shutdown).
# The model stays in memory for the entire lifetime of the server.
# ============================================================================

import asyncio
import os
import socket
import sys
import time
import logging
import warnings
import asyncio
from contextlib import asynccontextmanager

# Suppress noisy third-party warnings before any imports trigger them
# 1) sox package uses Python logging to warn about missing SoX binary
logging.getLogger("sox").setLevel(logging.CRITICAL)
# 2) sox __init__ runs `os.popen('sox -h')` which writes to stderr via shell;
#    force-import sox early with stderr suppressed so later faster-qwen3-tts gets cached module
_real_stderr_fd = os.dup(2)
_devnull_fd = os.open(os.devnull, os.O_WRONLY)
os.dup2(_devnull_fd, 2)
try:
    import sox  # noqa: F401 — cached for faster-qwen3-tts later
except Exception:
    pass
os.dup2(_real_stderr_fd, 2)
os.close(_real_stderr_fd)
os.close(_devnull_fd)
# 3) flash-attn / transformers advisory warnings
os.environ["TRANSFORMERS_NO_ADVISORY_WARNINGS"] = "1"
warnings.filterwarnings("ignore", message=".*flash.attn.*")
warnings.filterwarnings("ignore", message=".*Torch was not compiled with flash attention.*")

from fastapi import FastAPI, HTTPException, Request
from loguru import logger

from server.config import settings


def claim_port() -> "socket.socket":
    """
    Bind the server port before the slow model load, or exit.

    uvicorn only binds after startup finishes, so while the model loads (~30 s)
    nothing is listening yet and a second copy would also start, loading a
    second model onto the GPU. A bound socket makes later copies fail at once
    (no SO_REUSEADDR: on Windows that would let them share the port).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((settings.server.host, settings.server.port))
    except OSError:
        print(
            f"VoiceLink server already running on {settings.server.host}:{settings.server.port}; exiting.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)
    return sock


# Only when run as the server (not when imported, e.g. by tests or uvicorn --reload)
_port_sock = claim_port() if __name__ == "__main__" else None

from server.models import load_model
from server.routers.tts import (
    router as tts_router,
    set_lookahead_service,
    set_model,
    set_start_time,
)
from server.routers.qwen3 import router as qwen3_router
from server.routers.lookahead import create_router
from server.lookahead.cache import PcmCache
from server.lookahead.correlation import CorrelationRegistry
from server.lookahead.scheduler import LookAheadScheduler
from server.lookahead.service import LookAheadService
from server.lookahead.store import SnapshotStore
from server.lookahead.worker import SpeculativeWorker


# These objects are intentionally distinct from the authoritative TTS model.
# Construction has no model-load side effect; the child starts only in lifespan.
lookahead_worker = SpeculativeWorker(
    lang_code=settings.model.kokoro_lang_code,
    device=settings.model.device,
    max_jobs=settings.lookahead.max_queued_jobs,
)
_lookahead_scheduler_holder: dict[str, LookAheadScheduler] = {}
lookahead_store = SnapshotStore(
    on_cancel_job=lambda job_id: _lookahead_scheduler_holder["scheduler"].cancel(job_id)
)
lookahead_scheduler = LookAheadScheduler(
    worker=lookahead_worker,
    store=lookahead_store,
    cache=PcmCache(
        max_bytes=settings.lookahead.cache_max_bytes,
        ttl_seconds=settings.lookahead.cache_ttl_seconds,
    ),
    settings=settings.lookahead,
)
_lookahead_scheduler_holder["scheduler"] = lookahead_scheduler
lookahead_registry = CorrelationRegistry(
    correlation_ttl_seconds=settings.lookahead.correlation_ttl_seconds,
    binding_ttl_seconds=settings.lookahead.stream_ttl_seconds,
)
lookahead_service = LookAheadService(
    settings=settings.lookahead,
    store=lookahead_store,
    scheduler=lookahead_scheduler,
    registry=lookahead_registry,
    model_version="Kokoro",
)
set_lookahead_service(lookahead_service)


def setup_logging():
    """
    Configure loguru for nice colored output.

    Loguru replaces Python's built-in logging with:
    - Colored output by default
    - Easy file rotation
    - Better exception formatting
    - No boilerplate (just `from loguru import logger; logger.info("hi")`)
    """
    # Remove default handler, add our own
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.server.log_level,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize="NO_COLOR" not in os.environ,  # the app sets NO_COLOR so server.log is plain text
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manages server startup and shutdown.

    Startup:  Load the TTS model into memory (GPU/CPU).
    Shutdown: Unload the model, free memory.

    This runs BEFORE any requests are accepted (startup)
    and AFTER all requests are done (shutdown).
    """
    # --- STARTUP ---
    setup_logging()
    set_start_time(time.time())

    logger.info("=" * 60)
    logger.info("  VoiceLink Inference Server starting...")
    logger.info("=" * 60)
    logger.info(f"Host:  {settings.server.host}")
    logger.info(f"Port:  {settings.server.port}")
    logger.info(f"Model: {settings.model.default_model}")
    if settings.model.qwen3_enabled:
        logger.info(f"Qwen3: enabled (tier={settings.model.qwen3_tier}, lazy-loaded)")
    else:
        logger.info("Qwen3: disabled")

    # Load the TTS model
    model = None
    t0 = time.perf_counter()
    try:
        model = load_model(
            settings.model.default_model,
            lang_code=settings.model.kokoro_lang_code,
            device=settings.model.device,
        )
        set_model(model)
        elapsed = time.perf_counter() - t0
        logger.info(f"Model loaded in {elapsed:.1f}s")
    except Exception as e:
        logger.error(f"Failed to load model: {e}")
        logger.error("Server will start but TTS will return 503 errors.")
    # The kokoro package replaces our log handler with a colored one when imported
    setup_logging()

    logger.info("")
    logger.info(f"Server ready at http://{settings.server.host}:{settings.server.port}")
    logger.info(f"API docs at   http://{settings.server.host}:{settings.server.port}/docs")
    logger.info("")

    poll_task: asyncio.Task[None] | None = None
    if settings.lookahead.enabled:
        if not lookahead_worker.start():
            logger.warning("Look-ahead unavailable: isolated Kokoro worker did not become ready")

    async def poll_lookahead_maintenance() -> None:
        while True:
            lookahead_service.maintain()
            if settings.lookahead.enabled:
                lookahead_scheduler.poll()
            await asyncio.sleep(0.05)

    poll_task = asyncio.create_task(poll_lookahead_maintenance())

    # --- SERVE REQUESTS ---
    try:
        yield
    finally:
        if poll_task is not None:
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass
        lookahead_worker.stop()

        # --- SHUTDOWN ---
        logger.info("Server shutting down...")
        if model is not None:
            model.unload()
        logger.info("Goodbye!")


# --- Create the FastAPI app ---
app = FastAPI(
    title="VoiceLink Inference Server",
    description=(
        "Local TTS inference server that bridges neural text-to-speech models "
        "with Windows SAPI. Provides streaming PCM audio from models like Kokoro."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Mount routers
app.include_router(tts_router)
app.include_router(
    create_router(
        settings=settings.lookahead,
        store=lookahead_store,
        worker_ready=lambda: lookahead_scheduler.ready,
        service=lookahead_service,
    )
)

# Qwen3 router is always mounted — the router itself handles lazy loading.
# If Qwen3 is disabled, requests will still fail gracefully because the model
# won't be installed. This avoids conditional import issues.
app.include_router(qwen3_router)


@app.post("/v1/shutdown")
async def shutdown(request: Request):
    """
    Stop the server process (used by the VoiceLink app's Stop button).

    Loopback only. The custom header can't be sent by a web page without a CORS
    preflight, which this server never allows, so a site can't stop it.
    """
    if request.client is None or request.client.host != "127.0.0.1":
        raise HTTPException(status_code=403, detail="Local requests only")
    if request.headers.get("x-voicelink") != "1":
        raise HTTPException(status_code=403, detail="Missing X-VoiceLink header")
    logger.info("Shutdown requested")
    # Exit after the response is sent. os._exit skips teardown, but the OS frees
    # the GPU memory with the process.
    asyncio.get_running_loop().call_later(0.3, os._exit, 0)
    return {"status": "shutting down"}


# --- Direct execution ---
if __name__ == "__main__":
    import uvicorn

    uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.server.host,
            port=settings.server.port,
            log_level=settings.server.log_level.lower(),
        )
    ).run(sockets=[_port_sock])  # the socket claimed at startup
