# ============================================================================
# VoiceLink — TTS API Router
# ============================================================================
#
# ENDPOINTS:
#   POST /v1/tts       — Synthesize text to streaming PCM audio
#   GET  /v1/voices    — List available voices
#   GET  /v1/health    — Server health check
#
# WHY THESE SPECIFIC ENDPOINTS?
# - /v1/ prefix for API versioning (can add /v2/ later without breaking DLL)
# - POST for TTS because we're sending text (body) and creating audio (resource)
# - GET for voices/health because they're read-only queries
#
# STREAMING RESPONSE:
# The /v1/tts endpoint returns a StreamingResponse with chunked transfer
# encoding. This means:
# 1. HTTP response starts immediately (headers sent)
# 2. PCM audio chunks stream as they're generated
# 3. Connection closes when generation is done
#
# The COM DLL reads this stream chunk-by-chunk and writes each chunk
# to SAPI's audio sink. Result: audio starts playing within ~100ms.
# ============================================================================

import time
from typing import Generator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from loguru import logger


# ---- Request / Response Models ----

class TTSRequest(BaseModel):
    """
    Request body for POST /v1/tts.

    Example:
        {
            "text": "Hello, world!",
            "voice": "af_heart",
            "speed": 1.0,
            "format": "pcm_24k_16bit"
        }
    """

    text: str = Field(
        ...,
        min_length=1,
        max_length=50_000,
        description="Text to synthesize. Max 50K characters.",
    )
    voice: str | None = Field(
        default=None,
        description="Voice ID (e.g. 'af_heart'). None = server default.",
    )
    speed: float = Field(
        default=1.0,
        ge=0.25,
        le=4.0,
        description="Speed multiplier. 0.25 (very slow) to 4.0 (very fast).",
    )
    format: str = Field(
        default="pcm_24k_16bit",
        description="Audio format. Currently only 'pcm_24k_16bit' is supported.",
    )


class VoiceResponse(BaseModel):
    """One voice entry in the /v1/voices response."""

    id: str
    name: str
    language: str
    gender: str
    description: str
    model: str
    tags: list[str]
    sample_rate: int


class HealthResponse(BaseModel):
    """Response body for GET /v1/health."""

    status: str               # "ok" or "error"
    model: str | None         # Currently loaded model name
    model_loaded: bool        # Is the model ready?
    gpu_available: bool       # Is CUDA available?
    gpu_name: str | None      # GPU device name
    uptime_seconds: float     # Seconds since server started


# ---- Router ----

router = APIRouter(prefix="/v1", tags=["TTS"])

# These get set by main.py on startup
_model = None
_start_time: float = 0.0


def set_model(model):
    """Called by main.py to inject the loaded model."""
    global _model
    _model = model


def set_start_time(t: float):
    """Called by main.py to record server start time."""
    global _start_time
    _start_time = t


# ---- Endpoints ----

@router.post(
    "/tts",
    summary="Synthesize text to speech",
    description="Returns streaming PCM audio (24kHz, 16-bit, mono).",
    response_class=StreamingResponse,
)
async def synthesize(request: TTSRequest):
    """
    Convert text to speech audio.

    The response is a streaming binary PCM audio file.
    Content-Type: audio/pcm
    Format: 24000 Hz, 16-bit signed little-endian, mono

    The COM DLL reads this stream and pipes it directly to SAPI.
    """
    if _model is None or not _model.is_loaded:
        raise HTTPException(
            status_code=503,
            detail="TTS model not loaded. Server is starting up.",
        )

    # Validate format
    if request.format != "pcm_24k_16bit":
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format '{request.format}'. Use 'pcm_24k_16bit'.",
        )

    logger.info(
        f"TTS request: voice={request.voice}, speed={request.speed}, "
        f"text_len={len(request.text)}"
    )

    t0 = time.perf_counter()

    # ---- Pre-generate all audio ----
    # We collect all chunks first so we know the exact total byte count.
    # This lets the COM DLL fire SPEI_WORD_BOUNDARY events at perfectly
    # proportional audio offsets, giving Edge Read Aloud accurate text
    # highlighting (no drift between highlighted word and spoken audio).
    #
    # The tradeoff: time-to-first-byte increases by the full synthesis
    # time (~1-3s for a page of text). This is acceptable for reading
    # apps where users expect a short pause before audio starts.
    chunks: list[bytes] = []
    try:
        for chunk in _model.synthesize(
            text=request.text,
            voice=request.voice,
            speed=request.speed,
        ):
            chunks.append(chunk)
    except Exception as e:
        logger.exception(f"Synthesis error: {e}")
        raise HTTPException(status_code=500, detail=f"Synthesis failed: {e}")

    total_bytes = sum(len(c) for c in chunks)
    elapsed = time.perf_counter() - t0
    audio_seconds = total_bytes / (24000 * 2)  # 24kHz * 2 bytes/sample
    logger.info(
        f"TTS complete: {audio_seconds:.1f}s audio in {elapsed:.2f}s "
        f"({audio_seconds / elapsed:.1f}x realtime), "
        f"{total_bytes:,} bytes"
    )

    def stream_chunks() -> Generator[bytes, None, None]:
        """Yield pre-generated PCM chunks."""
        for chunk in chunks:
            yield chunk

    # Return streaming response with audio length header for precise
    # word-boundary event timing in the COM DLL.
    return StreamingResponse(
        content=stream_chunks(),
        media_type="audio/pcm",
        headers={
            # Tell the COM DLL the audio format
            "X-Audio-Sample-Rate": "24000",
            "X-Audio-Sample-Width": "16",
            "X-Audio-Channels": "1",
            # Exact total audio byte count — used by the DLL for
            # proportional SPEI_WORD_BOUNDARY event offsets.
            "X-Audio-Length": str(total_bytes),
        },
    )


@router.get(
    "/voices",
    summary="List available voices",
    response_model=list[VoiceResponse],
)
async def list_voices():
    """
    Return metadata for all voices the current model supports.

    Used by:
    - COM DLL to register SAPI tokens
    - Settings GUI to show voice picker
    - API clients to discover available voices
    """
    if _model is None:
        raise HTTPException(status_code=503, detail="No model loaded.")

    voices = _model.list_voices()
    return [
        VoiceResponse(
            id=v.id,
            name=v.name,
            language=v.language,
            gender=v.gender,
            description=v.description,
            model=v.model,
            tags=v.tags,
            sample_rate=v.sample_rate,
        )
        for v in voices
    ]


@router.get(
    "/health",
    summary="Health check",
    response_model=HealthResponse,
)
async def health_check():
    """
    Server health check.

    Returns status, loaded model info, GPU availability, and uptime.
    Used by COM DLL to verify server is alive before sending TTS requests.
    """
    import torch

    gpu_available = torch.cuda.is_available()
    gpu_name = torch.cuda.get_device_name(0) if gpu_available else None

    return HealthResponse(
        status="ok" if (_model is not None and _model.is_loaded) else "loading",
        model=_model.model_name if _model is not None else None,
        model_loaded=_model.is_loaded if _model is not None else False,
        gpu_available=gpu_available,
        gpu_name=gpu_name,
        uptime_seconds=time.time() - _start_time,
    )
