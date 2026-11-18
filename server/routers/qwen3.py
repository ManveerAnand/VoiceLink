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
import threading
from typing import Generator
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from loguru import logger

from server.models.base import Qwen3Error


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
_create_lock = threading.Lock()


def _data_dir() -> Path:
    return Path(os.environ.get("VOICELINK_DATA_DIR", r"C:\ProgramData\VoiceLink"))


def _configured_tier() -> str:
    """The tier chosen in the app's Settings (the app writes it to <data>/.qwen3_tier)."""
    try:
        tier = (_data_dir() / ".qwen3_tier").read_text().strip()
    except OSError:
        from server.config import settings
        tier = getattr(settings.model, "qwen3_tier", "standard")
    return "full" if tier == "full" else "standard"


def get_qwen3_model():
    """
    Get or lazy-create the Qwen3 model instance.

    Called on every Qwen3 endpoint request. If the model hasn't been
    created yet, this imports it (triggering torch/CUDA load) and
    instantiates it with the configured tier. When the user switches tier in
    Settings the old instance is dropped and a new one is made.
    """
    global _qwen3_model

    # One creation at a time; other requests wait for it. Handlers call this through
    # asyncio.to_thread so the wait never blocks the event loop (Kokoro, /v1/health).
    with _create_lock:
        return _get_or_create()


def _get_or_create():
    global _qwen3_model

    tier = _configured_tier()
    if _qwen3_model is not None and _qwen3_model.tier != tier:
        logger.info(f"Qwen3 tier changed to {tier}; unloading the {_qwen3_model.tier} models")
        _qwen3_model.unload()
        _qwen3_model = None

    if _qwen3_model is None:
        # Lazy import to avoid loading torch at server startup
        from server.models import get_qwen3_class

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
    return _data_dir().joinpath("voices")


# The static KV cache holds 2048 positions (see qwen3_model.py); a long reference
# clip eats into it, and more than a minute gains nothing.
MAX_REF_SECONDS = 60
MIN_REF_SECONDS = 1.0


def _safe_name(name: str) -> str:
    """Folder-safe profile name; 400 if nothing is left."""
    safe = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
    if not safe:
        raise HTTPException(status_code=400, detail="Voice names can only contain letters, numbers, spaces, - and _.")
    return safe


def _load_reference(src: Path, dst: Path) -> float:
    """Convert any audio file to 24 kHz mono WAV at dst. Returns its length in seconds."""
    import librosa
    import soundfile as sf

    try:
        data, _ = librosa.load(str(src), sr=24000, mono=True)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not read that audio file ({type(e).__name__}). Use a WAV, MP3, FLAC or OGG file.",
        )
    secs = len(data) / 24000
    if secs > MAX_REF_SECONDS:
        raise HTTPException(status_code=400, detail=f"The clip is {secs:.0f} s long; the limit is {MAX_REF_SECONDS} s. 10 to 20 seconds is ideal.")
    if float(abs(data).max()) < 0.003:
        raise HTTPException(status_code=400, detail="The clip is silent or almost silent. Check the recording level.")
    # Silence before and after the speech (every microphone recording has some) makes the
    # cloned voice open each sentence with a long pause, so keep only 0.1 s around the speech.
    _, (start, end) = librosa.effects.trim(data, top_db=35)
    data = data[max(start - 2400, 0):end + 2400]
    secs = len(data) / 24000
    if secs < MIN_REF_SECONDS:
        raise HTTPException(status_code=400, detail=f"The clip has only {secs:.1f} s of speech. Use at least 3 seconds of speech.")
    sf.write(str(dst), data, 24000, subtype="PCM_16")
    return secs


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
    model = await asyncio.to_thread(get_qwen3_model)

    voice = request.voice
    logger.info(f"Qwen3 TTS request: voice={voice}, text_len={len(request.text)}")
    t0 = time.perf_counter()

    # Resolve voice profile for custom voices (before entering the thread)
    ref_audio = None
    ref_text = None
    xvec_only = False
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

        if "ref_audio" not in meta:
            raise HTTPException(status_code=409, detail=f"Voice '{profile_name}' has no reference audio and cannot speak.")
        ref_audio = str(profile_dir / meta["ref_audio"])
        ref_text = meta.get("ref_text", "")
        xvec_only = bool(meta.get("xvec_only", False))

    # Use an asyncio.Queue to bridge the sync generator thread → async streaming response.
    # Items: audio bytes, an Exception (generation failed), or None (done).
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    async def _producer():
        """Run blocking synthesis in a thread, pushing audio to queue."""
        def _generate():
            try:
                if ref_audio is not None:
                    chunks = model.synthesize_cloned(
                        text=request.text,
                        ref_audio_path=ref_audio,
                        ref_text=ref_text,
                        speed=request.speed,
                        language=request.language,
                        xvec_only=xvec_only,
                    )
                else:
                    chunks = model.synthesize(
                        text=request.text,
                        voice=voice,
                        speed=request.speed,
                        language=request.language,
                    )
                for chunk in chunks:
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except Exception as e:
                if not isinstance(e, Qwen3Error):
                    logger.exception(f"Qwen3 synthesis error: {e}")
                loop.call_soon_threadsafe(queue.put_nowait, e)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)  # Sentinel: done

        await asyncio.to_thread(_generate)

    producer_task = asyncio.create_task(_producer())

    # Wait for the first audio so a failure (engine missing, model not
    # downloaded, bad voice...) is reported as an HTTP error with its reason
    # instead of an empty 200 response.
    first = await queue.get()
    if isinstance(first, Exception):
        await producer_task
        if isinstance(first, Qwen3Error):
            raise first
        # ValueError: the model rejected the input (unknown speaker, unsupported language)
        raise HTTPException(
            status_code=400 if isinstance(first, ValueError) else 500,
            detail=f"Qwen3 could not generate speech: {first}",
        )
    if first is None:
        await producer_task
        raise HTTPException(status_code=500, detail="Qwen3 produced no audio for this text.")

    sr = getattr(model, '_sample_rate', 24000)

    async def stream_chunks():
        """Consume streaming audio chunks from queue and yield for HTTP response."""
        total_bytes = len(first)
        yield first

        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                if isinstance(chunk, Exception):
                    continue  # already logged; the audio so far is all we can send
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


def _pcm_response(pcm: bytes, safe_name: str, name: str, voice_only: bool = False) -> StreamingResponse:
    """Preview audio of a freshly created voice, as the clone/design endpoints return it."""
    return StreamingResponse(
        content=iter([pcm]),
        media_type="audio/pcm",
        headers={
            "X-Audio-Sample-Rate": "24000",
            "X-Audio-Sample-Width": "16",
            "X-Audio-Channels": "1",
            "X-Audio-Length": str(len(pcm)),
            "X-Voice-Id": f"qwen3_custom_{safe_name}",
            "X-Voice-Name": quote(name),
            "X-Voice-Only": "1" if voice_only else "0",  # clone fell back to voice-only (transcript did not match)
        },
    )


def _new_profile_dir(safe_name: str) -> Path:
    """Fresh folder for a new voice; 409 if that name is taken."""
    profile_dir = _voices_dir() / safe_name
    if (profile_dir / "meta.json").exists():
        raise HTTPException(
            status_code=409,
            detail=f"You already have a voice named '{safe_name}'. Pick another name or delete that one first.",
        )
    shutil.rmtree(profile_dir, ignore_errors=True)  # leftovers of a crashed attempt
    profile_dir.mkdir(parents=True)
    return profile_dir


@router.post(
    "/clone",
    summary="Clone a voice from reference audio",
    description="Upload a short audio clip (+ its transcript) to create a cloned voice.",
)
async def qwen3_clone_voice(
    name: str = Form(..., min_length=1, max_length=100, description="Name for the cloned voice"),
    audio: UploadFile = File(..., description="Reference audio clip: 10-20 s of one person speaking clearly (3 s minimum, 60 s maximum)"),
    transcript: str = Form(
        default="",
        max_length=2000,
        description=(
            "Exactly what is said in the clip. Gives the most faithful clone. "
            "Leave empty to clone from the voice alone (no transcript needed, less faithful)."
        ),
    ),
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
    1. Save the clip as 24 kHz mono WAV in a new voice profile folder
    2. Generate a preview with the cloned voice
    3. Save the profile (meta.json + preview) and return the preview audio
    Any failure removes the half-made profile and returns the reason in `detail`.
    """
    model = await asyncio.to_thread(get_qwen3_model)
    safe_name = _safe_name(name)
    transcript = transcript.strip()
    xvec_only = not transcript  # no transcript: speaker embedding only

    contents = await audio.read()
    if len(contents) > 40 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="That audio file is too big (limit 40 MB). A 10-20 second clip is enough.")

    profile_dir = _new_profile_dir(safe_name)
    try:
        upload = profile_dir / ("upload" + (Path(audio.filename or "").suffix[:6] or ".wav"))
        upload.write_bytes(contents)
        wav_path = profile_dir / "reference.wav"
        secs = await asyncio.to_thread(_load_reference, upload, wav_path)
        upload.unlink(missing_ok=True)
        # Cloning with a transcript only works when it says what the clip says; a transcript far too
        # long or too short for the clip cannot (normal speech is 10-18 characters per second)
        if transcript and not 3 <= len(transcript) / secs <= 20:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The transcript has {len(transcript)} characters but the clip has {secs:.0f} s of speech "
                    "(about 10-18 characters per second is normal). It must be exactly what is said in the clip. "
                    "Fix it, or leave it empty to clone from the voice alone."
                ),
            )

        t0 = time.perf_counter()
        preview_pcm = await asyncio.to_thread(lambda: b"".join(model.synthesize_cloned(
            text=preview_text,
            ref_audio_path=str(wav_path),
            ref_text=transcript,
            xvec_only=xvec_only,
        )))
        if not preview_pcm:
            raise HTTPException(status_code=500, detail="Qwen3 produced no audio from this clip. Try a clearer recording.")
        if model.clone_downgraded:  # the model could not continue from the transcript, so don't use it
            xvec_only = True

        meta = {
            "name": name,
            "source": "cloned",
            "ref_audio": "reference.wav",
            "ref_text": transcript,
            "xvec_only": xvec_only,
            "ref_seconds": round(secs, 1),
            "language": "en-US",
            "gender": gender if gender in ("male", "female") else "unknown",
            "description": description or f"Cloned voice: {name}",
            "created": time.time(),
            "saved": False,  # kept only after the user presses Save (see qwen3_save_clone)
        }
        (profile_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        (profile_dir / "preview.pcm").write_bytes(preview_pcm)
    except (HTTPException, Qwen3Error):
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(profile_dir, ignore_errors=True)
        logger.exception(f"Voice cloning failed: {e}")
        raise HTTPException(status_code=500, detail=f"Voice cloning failed: {e}")

    logger.info(
        f"Voice cloned: {name} ({secs:.1f}s clip, {'x-vector only' if xvec_only else 'with transcript'}, "
        f"{len(preview_pcm)} bytes preview, {time.perf_counter() - t0:.1f}s)"
    )
    return _pcm_response(preview_pcm, safe_name, name, voice_only=xvec_only)


def purge_unsaved_voices() -> None:
    """Drop voices that were created in Voice Studio but never saved (called at server start)."""
    for profile_dir in _voices_dir().glob("*/"):
        try:
            meta = json.loads((profile_dir / "meta.json").read_text())
        except (OSError, ValueError):
            continue
        if not meta.get("saved", True):
            shutil.rmtree(profile_dir, ignore_errors=True)
            logger.info(f"Removed unsaved voice preview: {profile_dir.name}")


@router.post(
    "/clone/{voice_name}/save",
    summary="Keep a new voice in the library",
)
async def qwen3_save_clone(voice_name: str):
    """Mark a freshly cloned/designed voice as saved so it shows up in the voice lists."""
    meta_path = _voices_dir() / _safe_name(voice_name) / "meta.json"
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="That voice no longer exists. Create it again.")
    meta = json.loads(meta_path.read_text())
    meta["saved"] = True
    meta_path.write_text(json.dumps(meta, indent=2))
    return {"saved": voice_name}


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

    Requires the Full (1.7B) model tier. The VoiceDesign model speaks the
    sample text in a voice matching the description; that clip is kept as the
    voice's reference, so the saved voice speaks any text like a clone would.

    Returns preview audio and saves the voice profile.
    """
    model = await asyncio.to_thread(get_qwen3_model)

    if model.tier != "full":
        raise HTTPException(
            status_code=400,
            detail="Voice design needs the Full (1.7B) Qwen3 tier. Switch to it in Settings > Qwen3.",
        )

    safe_name = _safe_name(request.name)
    profile_dir = _new_profile_dir(safe_name)
    try:
        t0 = time.perf_counter()
        preview_pcm = await asyncio.to_thread(model.design_voice, request.description, request.sample_text)
        if not preview_pcm:
            raise HTTPException(status_code=500, detail="Qwen3 produced no audio for that description. Try rewording it.")

        import numpy as np
        import soundfile as sf
        sf.write(str(profile_dir / "reference.wav"), np.frombuffer(preview_pcm, dtype=np.int16), 24000, subtype="PCM_16")

        meta = {
            "name": request.name,
            "source": "designed",
            "ref_audio": "reference.wav",
            "ref_text": request.sample_text,
            "description": request.description,
            "language": "en-US",
            "gender": "unknown",
            "created": time.time(),
            "saved": False,
        }
        (profile_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        (profile_dir / "preview.pcm").write_bytes(preview_pcm)
    except (HTTPException, Qwen3Error):
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(profile_dir, ignore_errors=True)
        logger.exception(f"Voice design failed: {e}")
        raise HTTPException(status_code=500, detail=f"Voice design failed: {e}")

    logger.info(f"Voice designed: {request.name} ({len(preview_pcm)} bytes preview, {time.perf_counter() - t0:.1f}s)")
    return _pcm_response(preview_pcm, safe_name, request.name)

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
                    if not meta.get("saved", True):
                        continue  # preview in Voice Studio, not in the library yet
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
