# ============================================================================
# VoiceLink — Qwen3 TTS API Router
# ============================================================================
#
# ENDPOINTS:
#   POST /v1/qwen3/tts       — Synthesize text using Qwen3 voice (built-in or custom)
#   POST /v1/qwen3/clone     — Clone a voice from reference audio + text
#   POST /v1/qwen3/design    — Design a voice from text description (1.7B only)
#   GET  /v1/qwen3/speakers  — List available Qwen3 voices
#   GET  /v1/qwen3/languages — List supported TTS languages
#   GET  /v1/qwen3/status    — Qwen3 model status (loaded, tier, etc.)
#
# WHY A SEPARATE ROUTER?
# Qwen3 has capabilities that Kokoro doesn't (voice cloning, voice design).
# Rather than bloating the existing /v1/tts endpoint with model-specific
# branching, we give Qwen3 its own namespace under /v1/qwen3/*.
# The COM DLL routes to the correct endpoint based on the voice's Model
# registry value.
#
# LAZY LOADING:
# The Qwen3 model is NOT loaded at server startup. It loads on the first
# request to any /v1/qwen3/* endpoint and unloads after 5 minutes idle.
# This keeps startup fast and VRAM free when only Kokoro is being used.
# ============================================================================

import os
import json
import time
import shutil
import asyncio
from typing import Generator
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from loguru import logger


# ---- Request / Response Models ----

class Qwen3TTSRequest(BaseModel):
    """Request body for POST /v1/qwen3/tts (built-in or custom voice)."""

    text: str = Field(
        ...,
        min_length=1,
        max_length=50_000,
        description="Text to synthesize.",
    )
    voice: str = Field(
        default="qwen3_serena",
        description=(
            "Voice ID. Built-in: 'qwen3_serena', 'qwen3_aiden', etc. "
            "Custom: 'qwen3_custom_{name}'."
        ),
    )
    speed: float = Field(
        default=1.0,
        ge=0.25,
        le=4.0,
        description="Speed multiplier.",
    )
    language: str = Field(
        default="auto",
        description=(
            "Language for synthesis. 'auto' for automatic detection. "
            "Supported: english, chinese, japanese, korean, french, "
            "german, italian, spanish, portuguese, russian."
        ),
    )


class Qwen3DesignRequest(BaseModel):
    """Request body for POST /v1/qwen3/design (voice from description)."""

    description: str = Field(
        ...,
        min_length=10,
        max_length=2000,
        description="Natural language description of the desired voice.",
    )
    sample_text: str = Field(
        default="Hello, this is a preview of the designed voice.",
        max_length=500,
        description="Text to speak in the designed voice for preview.",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Name to save the voice profile as.",
    )


class Qwen3SpeakerResponse(BaseModel):
    """One voice entry in the /v1/qwen3/speakers response."""

    id: str
    name: str
    language: str
    gender: str
    description: str
    tags: list[str]
    sample_rate: int = 24000


class Qwen3StatusResponse(BaseModel):
    """Response for GET /v1/qwen3/status."""

    loaded: bool
    tier: str | None
    model_name: str | None
    idle_seconds: float | None


# ---- Router ----

router = APIRouter(prefix="/v1/qwen3", tags=["Qwen3 TTS"])

# The Qwen3Model instance — set by main.py via set_qwen3_model()
_qwen3_model = None  # type: ignore


def get_qwen3_model():
    """
    Get or lazy-create the Qwen3 model instance.

    Called on every Qwen3 endpoint request. If the model hasn't been
    created yet, this imports it (triggering torch/CUDA load) and
    instantiates it with the configured tier.
    """
    global _qwen3_model

    if _qwen3_model is None:
        # Lazy import to avoid loading torch at server startup
        from server.models import get_qwen3_class
        from server.config import settings

        tier = getattr(settings.model, "qwen3_tier", "standard")
        logger.info(f"Creating Qwen3Model on first request (tier={tier})")

        Qwen3Class = get_qwen3_class()
        _qwen3_model = Qwen3Class(tier=tier, device="cuda")

    return _qwen3_model


def set_qwen3_model(model):
    """Allow main.py to inject a pre-created model (for testing)."""
    global _qwen3_model
    _qwen3_model = model


def _voices_dir() -> Path:
    """Path to user-created voice profiles."""
    return Path(
        os.environ.get("VOICELINK_DATA_DIR", r"C:\ProgramData\VoiceLink")
    ).joinpath("voices")


# ---- Endpoints ----

@router.post(
    "/tts",
    summary="Synthesize text with Qwen3 voice",
    description="Returns streaming PCM audio (24kHz, 16-bit, mono).",
    response_class=StreamingResponse,
)
async def qwen3_synthesize(request: Qwen3TTSRequest):
    """
    Synthesize text using a Qwen3 voice (built-in or custom).

    For custom voices (qwen3_custom_{name}), the reference audio and
    transcript are loaded from the voice profile directory.

    Audio is streamed per-sentence: the first sentence's audio is sent
    as soon as it's generated, so playback starts while the rest generates.
    """
    model = get_qwen3_model()

    voice = request.voice
    logger.info(f"Qwen3 TTS request: voice={voice}, text_len={len(request.text)}")
    t0 = time.perf_counter()

    # Resolve voice profile for custom voices (before entering the thread)
    ref_audio = None
    ref_text = None
    if voice.startswith("qwen3_custom_"):
        profile_name = voice[len("qwen3_custom_"):]
        profile_dir = _voices_dir() / profile_name
        meta_path = profile_dir / "meta.json"

        if not meta_path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"Voice profile '{profile_name}' not found.",
            )

        with open(meta_path) as f:
            meta = json.load(f)

        ref_audio = str(profile_dir / meta["ref_audio"])
        ref_text = meta["ref_text"]

    sr = getattr(model, '_sample_rate', 24000)

    # Use an asyncio.Queue to bridge the sync generator thread → async streaming response.
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    loop = asyncio.get_event_loop()

    async def _producer():
        """Run blocking synthesis in a thread, pushing audio to queue."""
        def _generate():
            try:
                if ref_audio is not None:
                    for chunk in model.synthesize_cloned(
                        text=request.text,
                        ref_audio_path=ref_audio,
                        ref_text=ref_text,
                        speed=request.speed,
                        language=request.language,
                    ):
                        loop.call_soon_threadsafe(queue.put_nowait, chunk)
                else:
                    for chunk in model.synthesize(
                        text=request.text,
                        voice=voice,
                        speed=request.speed,
                        language=request.language,
                    ):
                        loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except Exception as e:
                logger.exception(f"Qwen3 synthesis error: {e}")
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)  # Sentinel: done

        await asyncio.to_thread(_generate)

    async def stream_chunks():
        """Consume streaming audio chunks from queue and yield for HTTP response."""
        total_bytes = 0
        # Start producer in background
        producer_task = asyncio.create_task(_producer())

        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                total_bytes += len(chunk)
                yield chunk
        finally:
            await producer_task

        elapsed = time.perf_counter() - t0
        audio_seconds = total_bytes / (sr * 2)
        logger.info(
            f"Qwen3 TTS complete: {audio_seconds:.1f}s audio in {elapsed:.2f}s "
            f"({audio_seconds / max(elapsed, 0.001):.1f}x realtime), "
            f"{total_bytes:,} bytes"
        )

    return StreamingResponse(
        content=stream_chunks(),
        media_type="audio/pcm",
        headers={
            "X-Audio-Sample-Rate": str(sr),
            "X-Audio-Sample-Width": "16",
            "X-Audio-Channels": "1",
        },
    )


@router.post(
    "/clone",
    summary="Clone a voice from reference audio",
    description="Upload a short audio clip + transcript to create a cloned voice.",
)
async def qwen3_clone_voice(
    name: str = Form(..., min_length=1, max_length=100, description="Name for the cloned voice"),
    transcript: str = Form(..., min_length=1, max_length=2000, description="What is said in the audio clip"),
    audio: UploadFile = File(..., description="Reference audio clip (3-10 seconds, any format)"),
    gender: str = Form(default="unknown", description="Voice gender: male, female, or unknown"),
    description: str = Form(default="", max_length=500, description="Optional voice description"),
    preview_text: str = Form(
        default="Hello, this is a preview of the cloned voice.",
        max_length=500,
        description="Text to speak for preview",
    ),
):
    """
    Clone a voice from a short audio reference.

    Steps:
    1. Save uploaded audio to a voice profile directory
    2. Generate a preview using the cloned voice
    3. Save the voice profile metadata (meta.json)
    4. Return preview audio + profile info
    """
    model = get_qwen3_model()

    # Sanitize name for use as directory name
    safe_name = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid voice name.")

    profile_dir = _voices_dir() / safe_name
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Save the reference audio (original format)
    audio_filename = f"reference{Path(audio.filename or 'ref.wav').suffix}"
    audio_path = profile_dir / audio_filename

    try:
        contents = await audio.read()
        with open(audio_path, "wb") as f:
            f.write(contents)
    except Exception as e:
        if profile_dir.exists():
            shutil.rmtree(profile_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Failed to save audio: {e}")

    # Convert any audio format to WAV for the model
    wav_path = profile_dir / "reference.wav"
    try:
        import librosa
        import soundfile as sf

        audio_data, sr = librosa.load(str(audio_path), sr=24000, mono=True)
        sf.write(str(wav_path), audio_data, 24000, subtype="PCM_16")
        logger.info(f"Audio converted: {audio_filename} -> reference.wav ({len(audio_data)/24000:.1f}s @ 24000Hz)")
    except Exception as e:
        if profile_dir.exists():
            shutil.rmtree(profile_dir, ignore_errors=True)
        logger.exception(f"Audio conversion failed: {e}")
        raise HTTPException(status_code=400, detail=f"Unsupported audio format: {e}")

    # Generate preview audio with the cloned voice
    try:
        chunks: list[bytes] = []
        for chunk in model.synthesize_cloned(
            text=preview_text,
            ref_audio_path=str(wav_path),
            ref_text=transcript,
        ):
            chunks.append(chunk)
        preview_pcm = b"".join(chunks)
    except Exception as e:
        if profile_dir.exists():
            shutil.rmtree(profile_dir, ignore_errors=True)
        logger.exception(f"Voice cloning failed: {e}")
        raise HTTPException(status_code=500, detail=f"Voice cloning failed: {e}")

    # Save voice profile metadata
    meta = {
        "name": name,
        "source": "cloned",
        "ref_audio": "reference.wav",
        "ref_text": transcript,
        "language": "en-US",
        "gender": gender if gender in ("male", "female") else "unknown",
        "description": description or f"Cloned voice: {name}",
        "created": time.time(),
    }
    with open(profile_dir / "meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    # Save preview audio for later playback
    with open(profile_dir / "preview.pcm", "wb") as f:
        f.write(preview_pcm)

    logger.info(f"Voice cloned: {name} ({len(preview_pcm)} bytes preview)")

    # Return the preview audio as streaming PCM
    total_bytes = len(preview_pcm)
    return StreamingResponse(
        content=iter([preview_pcm]),
        media_type="audio/pcm",
        headers={
            "X-Audio-Sample-Rate": "24000",
            "X-Audio-Sample-Width": "16",
            "X-Audio-Channels": "1",
            "X-Audio-Length": str(total_bytes),
            "X-Voice-Id": f"qwen3_custom_{safe_name}",
            "X-Voice-Name": name,
        },
    )


@router.delete(
    "/clone/{voice_name}",
    summary="Delete a cloned voice profile",
    description="Remove a user-created voice profile from disk.",
)
async def qwen3_delete_clone(voice_name: str):
    """Delete a cloned/designed voice profile directory."""
    # Sanitize to prevent path traversal
    safe_name = "".join(c for c in voice_name if c.isalnum() or c in "-_ ").strip()
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid voice name.")

    profile_dir = _voices_dir() / safe_name
    if not profile_dir.exists():
        raise HTTPException(status_code=404, detail="Voice profile not found.")

    # Ensure it's actually inside the voices dir (prevent traversal)
    try:
        profile_dir.resolve().relative_to(_voices_dir().resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid voice path.")

    shutil.rmtree(profile_dir, ignore_errors=True)
    logger.info(f"Deleted voice profile: {safe_name}")
    return {"deleted": safe_name}


@router.post(
    "/design",
    summary="Design a voice from text description (1.7B only)",
    description="Describe a voice in natural language to generate it.",
)
async def qwen3_design_voice(request: Qwen3DesignRequest):
    """
    Design a new voice by describing it in natural language.

    Requires the Full (1.7B) model tier. The VoiceDesign model
    generates a voice matching the description.

    Returns preview audio and saves the voice profile.
    """
    model = get_qwen3_model()

    if model.tier != "full":
        raise HTTPException(
            status_code=400,
            detail="Voice design requires the Full (1.7B) model tier.",
        )

    # Sanitize name
    safe_name = "".join(c for c in request.name if c.isalnum() or c in "-_ ").strip()
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid voice name.")

    profile_dir = _voices_dir() / safe_name
    profile_dir.mkdir(parents=True, exist_ok=True)

    try:
        preview_pcm = model.design_voice(
            description=request.description,
            sample_text=request.sample_text,
        )
    except Exception as e:
        if profile_dir.exists():
            shutil.rmtree(profile_dir, ignore_errors=True)
        logger.exception(f"Voice design failed: {e}")
        raise HTTPException(status_code=500, detail=f"Voice design failed: {e}")

    # Save voice profile
    meta = {
        "name": request.name,
        "source": "designed",
        "description": request.description,
        "language": "en-US",
        "gender": "unknown",
        "created": time.time(),
    }
    with open(profile_dir / "meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    with open(profile_dir / "preview.pcm", "wb") as f:
        f.write(preview_pcm)

    logger.info(f"Voice designed: {request.name} ({len(preview_pcm)} bytes preview)")

    total_bytes = len(preview_pcm)
    return StreamingResponse(
        content=iter([preview_pcm]),
        media_type="audio/pcm",
        headers={
            "X-Audio-Sample-Rate": "24000",
            "X-Audio-Sample-Width": "16",
            "X-Audio-Channels": "1",
            "X-Audio-Length": str(total_bytes),
            "X-Voice-Id": f"qwen3_custom_{safe_name}",
            "X-Voice-Name": request.name,
        },
    )


@router.get(
    "/speakers",
    summary="List Qwen3 voices",
    response_model=list[Qwen3SpeakerResponse],
)
async def qwen3_list_speakers():
    """
    List all available Qwen3 voices (built-in + user-created).

    This does NOT require the model to be loaded — voice metadata
    is read from static definitions and disk profiles.
    """
    from server.models.qwen3_model import QWEN3_BUILTIN_VOICES

    voices: list[Qwen3SpeakerResponse] = []

    # Built-in voices
    for v in QWEN3_BUILTIN_VOICES:
        voices.append(Qwen3SpeakerResponse(
            id=v.id,
            name=v.name,
            language=v.language,
            gender=v.gender,
            description=v.description,
            tags=v.tags,
        ))

    # User-created profiles
    vdir = _voices_dir()
    if vdir.exists():
        for profile_dir in sorted(vdir.iterdir()):
            meta_path = profile_dir / "meta.json"
            if meta_path.exists():
                try:
                    with open(meta_path) as f:
                        meta = json.load(f)
                    source = meta.get("source", "cloned")
                    voices.append(Qwen3SpeakerResponse(
                        id=f"qwen3_custom_{profile_dir.name}",
                        name=meta.get("name", profile_dir.name),
                        language=meta.get("language", "en-US"),
                        gender=meta.get("gender", "unknown"),
                        description=meta.get("description", ""),
                        tags=["qwen3", f"qwen3-{source}"],
                    ))
                except Exception as e:
                    logger.warning(f"Failed to load voice profile {profile_dir}: {e}")

    return voices


@router.get(
    "/languages",
    summary="List supported TTS languages",
)
async def qwen3_languages():
    """Return the list of languages Qwen3 can synthesize."""
    from server.models.qwen3_model import SUPPORTED_LANGUAGES

    return {"languages": SUPPORTED_LANGUAGES}


@router.get(
    "/status",
    summary="Qwen3 model status",
    response_model=Qwen3StatusResponse,
)
async def qwen3_status():
    """Check whether the Qwen3 model is currently loaded."""
    if _qwen3_model is None:
        return Qwen3StatusResponse(
            loaded=False,
            tier=None,
            model_name=None,
            idle_seconds=None,
        )

    idle = time.time() - _qwen3_model._last_used if _qwen3_model.is_loaded else None
    return Qwen3StatusResponse(
        loaded=_qwen3_model.is_loaded,
        tier=_qwen3_model.tier,
        model_name=_qwen3_model.model_name,
        idle_seconds=idle,
    )
