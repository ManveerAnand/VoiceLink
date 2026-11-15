# ============================================================================
# VoiceLink — Qwen3 TTS Model Backend (CUDA Graph Accelerated)
# ============================================================================
#
# HOW THIS WORKS:
# 1. Uses faster-qwen3-tts (pip install faster-qwen3-tts) which wraps
#    Qwen3-TTS with CUDA graph capture for ~6-10x speedup. The app installs it
#    together with the Qwen3 models (Settings -> Qwen3 / Voice Studio).
# 2. Supports three modes:
#    - Built-in voices (9 speakers via CustomVoice model)
#    - Voice cloning (Base model — clone from a short reference clip)
#    - Voice design (VoiceDesign model, 1.7B only — describe a voice)
# 3. Lazy loaded: models load on first request, unload after idle timeout.
# 4. The first inference is slow (CUDA graph capture warmup); later calls
#    run at roughly realtime.
#
# QWEN3-TTS ARCHITECTURE:
#   Text + Speaker → Qwen3-TTS LM → Discrete Codes → Tokenizer-12Hz → Audio
#   - Tokenizer-12Hz: 12.5 Hz, 16-layer multi-codebook codec
#   - Output: 24kHz audio (same as Kokoro — no resampling needed)
#
# MODEL VARIANTS (Standard tier = 0.6B, Full tier = 1.7B):
#   *-CustomVoice: the built-in speakers
#   *-Base:        voice cloning from a reference clip
#   1.7B-VoiceDesign: text-described voice generation (Full tier only)
#
# BUILT-IN SPEAKERS (from CustomVoice model):
#   English-friendly: Serena, Aiden, Dylan, Eric, Ryan, Vivian
#   Other: ono_anna, sohee, uncle_fu
#   We expose the 6 English speakers for VoiceLink.
#
# STATIC KV CACHE LIMIT:
#   faster-qwen3-tts decodes into a static cache of max_seq_len = 2048
#   positions: prompt (text + reference-clip codes) + generated codec frames
#   (12.5 per second of audio). A prompt longer than that raises an error and
#   a decode that reaches it is cut off silently. Text is therefore generated
#   in chunks of at most MAX_CHUNK_CHARS (about 20 s of speech, ~250 frames),
#   and cloning references are limited to 60 s (see routers/qwen3.py).
# ============================================================================

import os
import re
import time
import json
import threading
import numpy as np
import torch
from typing import Callable, Generator
from pathlib import Path
from loguru import logger

from server.models.base import GPU_EXCLUSIVE_LOCK, TTSModel, VoiceInfo, Qwen3Error


def _faster_qwen3():
    """Import the runtime lazily so a missing package is a clear message, not a 500 traceback."""
    try:
        from faster_qwen3_tts import FasterQwen3TTS
    except ImportError as e:
        raise Qwen3Error(
            f"The Qwen3 voice engine is not installed (missing Python package '{e.name or e}'). "
            "Open Settings > Qwen3 and press Install."
        ) from e
    return FasterQwen3TTS


def _local_model_path(model_id: str) -> str:
    """Folder of a downloaded model; never starts a surprise multi-GB download."""
    from huggingface_hub import snapshot_download
    try:
        return snapshot_download(model_id, local_files_only=True)
    except Exception as e:
        raise Qwen3Error(
            f"The Qwen3 model {model_id} is not downloaded. Open Settings > Qwen3 and download the models."
        ) from e


# --- Sentence / Paragraph Splitting ---
# Split on sentence-ending punctuation followed by whitespace (or CJK ones).
# Only split on . ! ? — NOT on ; or : which often appear mid-sentence.
_SENTENCE_RE = re.compile(r'(?<=[.!?])\s+|(?<=[。！？])')

# Longest piece sent to the model in one call (see "STATIC KV CACHE LIMIT" above)
MAX_CHUNK_CHARS = 300

# Crossfade samples between sentences (~10ms at 24kHz)
_CROSSFADE_SAMPLES = 256

# Silence gap between paragraphs (~400ms at 24kHz)
_PARAGRAPH_SILENCE_SAMPLES = 9600


class _Chunk:
    """A text chunk with metadata about paragraph boundaries."""
    __slots__ = ("text", "is_para_start")
    def __init__(self, text: str, is_para_start: bool = False):
        self.text = text
        self.is_para_start = is_para_start


def _wrap(s: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Break an over-long sentence at a clause boundary, else a space, else mid-word."""
    out: list[str] = []
    while len(s) > limit:
        window = s[:limit]
        cut = max(window.rfind(", "), window.rfind("; "), window.rfind(": "))
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:  # no spaces at all (e.g. Chinese without punctuation)
            cut = limit - 1
        out.append(s[:cut + 1].strip())
        s = s[cut + 1:].strip()
    if s:
        out.append(s)
    return out


def _split_sentences(text: str) -> list[_Chunk]:
    """Split text into chunks of at most MAX_CHUNK_CHARS, preserving paragraph boundaries.

    Paragraph breaks (\\n\\n or more) insert a longer silence gap.
    Within a paragraph, sentences are crossfaded together.
    Short fragments (<20 chars) are merged with the previous sentence.
    """
    text = text.strip()
    if not text:
        return []

    # First split into paragraphs on blank lines
    paragraphs = re.split(r'\n\s*\n', text)
    chunks: list[_Chunk] = []

    for para_idx, para in enumerate(paragraphs):
        para = para.strip()
        if not para:
            continue

        # Split paragraph into sentences
        raw = _SENTENCE_RE.split(para)
        merged: list[str] = []
        for s in raw:
            s = s.strip()
            if not s:
                continue
            if merged and len(s) < 20:
                merged[-1] = merged[-1] + " " + s
            else:
                merged.append(s)

        if not merged:
            merged = [para]

        pieces = [p for s in merged for p in _wrap(s)]
        for sent_idx, sent in enumerate(pieces):
            is_para_start = (sent_idx == 0 and para_idx > 0)
            chunks.append(_Chunk(sent, is_para_start))

    return chunks if chunks else [_Chunk(text)]

# The 12Hz codec makes 12.5 frames per second of audio (1920 samples at 24 kHz)
CODEC_FPS = 12.5

# Extra attempts for a built-in voice whose generation never stopped
_RETRIES = 2


def _estimate_max_tokens(text: str) -> int:
    """Safety cap on codec frames for `text`.

    A healthy generation stops by itself (the model emits its end-of-speech token)
    at 10-20 characters per second of speech. This cap allows down to 8 chars/s
    (3 for Chinese/Japanese/Korean) plus 1 s, so reaching it means the model
    never stopped (or rambled on) and the audio is garbage.
    """
    cjk = sum(1 for c in text if ord(c) >= 0x2E80) > len(text) / 3
    # Digits are read as whole words ("555" -> "five five five"), so weigh them more
    size = len(text) + 4 * sum(c.isdigit() for c in text)
    secs = 1.0 + size / (3.0 if cjk else 8.0)
    return min(int(secs * CODEC_FPS), 2048)

# Supported languages (from Qwen3-TTS model.get_supported_languages())
SUPPORTED_LANGUAGES = [
    "auto", "chinese", "english", "german", "italian",
    "portuguese", "spanish", "japanese", "korean", "french", "russian",
]


# --- Qwen3 English Built-in Voices ---
# Speakers from the CustomVoice model (get_supported_speakers).
# We expose the English-friendly ones for VoiceLink's SAPI bridge.
QWEN3_BUILTIN_VOICES: list[VoiceInfo] = [
    VoiceInfo(
        id="qwen3_serena",
        name="Serena",
        language="en-US",
        gender="female",
        description="Qwen3 TTS built-in female English speaker.",
        model="qwen3",
        tags=["qwen3", "built-in"],
    ),
    VoiceInfo(
        id="qwen3_aiden",
        name="Aiden",
        language="en-US",
        gender="male",
        description="Qwen3 TTS built-in male English speaker.",
        model="qwen3",
        tags=["qwen3", "built-in"],
    ),
    VoiceInfo(
        id="qwen3_dylan",
        name="Dylan",
        language="en-US",
        gender="male",
        description="Qwen3 TTS built-in male English speaker.",
        model="qwen3",
        tags=["qwen3", "built-in"],
    ),
    VoiceInfo(
        id="qwen3_eric",
        name="Eric",
        language="en-US",
        gender="male",
        description="Qwen3 TTS built-in male English speaker.",
        model="qwen3",
        tags=["qwen3", "built-in"],
    ),
    VoiceInfo(
        id="qwen3_ryan",
        name="Ryan",
        language="en-US",
        gender="male",
        description="Qwen3 TTS built-in male English speaker.",
        model="qwen3",
        tags=["qwen3", "built-in"],
    ),
    VoiceInfo(
        id="qwen3_vivian",
        name="Vivian",
        language="en-US",
        gender="female",
        description="Qwen3 TTS built-in female English speaker.",
        model="qwen3",
        tags=["qwen3", "built-in"],
    ),
]

# Idle timeout: unload model after this many seconds of no requests
IDLE_TIMEOUT_SECS = 300  # 5 minutes


class Qwen3Model(TTSModel):
    """
    Qwen3-TTS model backend with lazy loading and idle unloading.

    The model only loads into GPU memory when a Qwen3 voice is first used,
    and unloads after 5 minutes of inactivity to free VRAM for other apps.
    """

    def __init__(self, tier: str = "standard", device: str = "cuda", **kwargs):
        """
        Args:
            tier: "standard" (0.6B) or "full" (1.7B)
            device: "cuda" (required for Qwen3)
        """
        self._tier = tier
        self._device = device
        self._models: dict[str, object] = {}  # kind ("custom" | "base" | "design") -> FasterQwen3TTS
        self._loaded = False
        self._last_used = 0.0
        self._idle_timer: threading.Timer | None = None
        self._lock = threading.Lock()      # guards loading / unloading
        self._gen_lock = threading.Lock()  # one generation at a time: the CUDA graphs are shared
        self._sample_rate = 24000  # Updated from model on load
        self._icl_failed: set[tuple[str, str]] = set()  # (reference clip, transcript) pairs the model cannot continue
        self.clone_downgraded = False  # set by synthesize_cloned when it had to drop the transcript

        # Model IDs based on tier
        if tier == "full":
            self._model_ids = {
                "base": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                "custom": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
                "design": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
            }
        else:
            self._model_ids = {
                "base": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
                "custom": "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
                "design": None,  # VoiceDesign only exists as 1.7B
            }

        # User-created voice profiles directory
        self._voices_dir = Path(
            os.environ.get("VOICELINK_DATA_DIR", r"C:\ProgramData\VoiceLink")
        ).joinpath("voices")

    def _get(self, kind: str):
        """The loaded model of this kind, loading it first.

        On the Full tier the 1.7B models (~4 GB each) can't sit side by side on
        an 8 GB card, so loading one drops the others.
        """
        with self._lock:
            model = self._models.get(kind)
            if model is not None:
                return model

            model_id = self._model_ids[kind]
            if model_id is None:
                raise Qwen3Error(
                    "Voice design needs the Full (1.7B) Qwen3 tier. Switch to it in Settings > Qwen3.",
                    status_code=400,
                )
            fq = _faster_qwen3()
            if not torch.cuda.is_available():
                raise Qwen3Error(
                    "Qwen3 needs an NVIDIA GPU, but this server's PyTorch cannot use one "
                    "(the CPU build of PyTorch is installed). Open Settings > Qwen3 and press Install to fix it."
                )
            path = _local_model_path(model_id)

            if self._tier == "full" and self._models:
                self._models.clear()
                torch.cuda.empty_cache()

            logger.info(f"Loading Qwen3 {kind} model {model_id} on {self._device}...")
            t0 = time.perf_counter()
            # Kokoro waits while this runs (see GPU_EXCLUSIVE_LOCK); capturing the CUDA graphs
            # here, not on the first request, keeps that wait inside the load
            with GPU_EXCLUSIVE_LOCK:
                model = fq.from_pretrained(path, device=self._device)
                model.warmup()
            logger.info(f"Qwen3 {kind} model loaded in {time.perf_counter() - t0:.1f}s "
                        "(the first generation also captures CUDA graphs and is slower)")
            self._models[kind] = model
            self._loaded = True
            self._sample_rate = getattr(model, "sample_rate", 24000)
            return model

    def load(self) -> None:
        """Load the CustomVoice model (built-in speakers) with CUDA graphs."""
        self._get("custom")
        self._touch()

    def unload(self) -> None:
        """Release all Qwen3-TTS models from GPU memory."""
        with self._lock:
            unloaded = list(self._models)
            self._models.clear()
            self._loaded = False

            if unloaded:
                try:
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
                logger.info(f"Qwen3-TTS unloaded ({', '.join(unloaded)}), GPU memory freed.")

            self._cancel_idle_timer()

    def _touch(self):
        """Update last-used timestamp and reset idle timer."""
        self._last_used = time.time()
        self._reset_idle_timer()

    def _reset_idle_timer(self):
        """Reset the idle unload timer."""
        self._cancel_idle_timer()
        self._idle_timer = threading.Timer(IDLE_TIMEOUT_SECS, self._idle_unload)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _cancel_idle_timer(self):
        """Cancel the current idle timer if running."""
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _idle_unload(self):
        """Called by the timer when idle timeout expires."""
        elapsed = time.time() - self._last_used
        if elapsed >= IDLE_TIMEOUT_SECS and not self._gen_lock.locked():
            logger.info(f"Qwen3-TTS idle for {elapsed:.0f}s, unloading to free GPU...")
            self.unload()
        else:
            # A request came in just before the timer fired (or is still generating)
            self._reset_idle_timer()

    def _generate_chunk(
        self,
        text: str,
        generate: Callable[[str, int], tuple],
        fallback: Callable[[str, int], tuple] | None = None,
    ) -> tuple:
        """
        Run `generate(text, max_tokens)` until the model stops by itself.

        If it runs into the cap instead, it never emitted its end-of-speech token and the
        audio is garbage: warn, then try `fallback` (a clone without its transcript), or
        else generate again (a built-in voice only fails by bad luck). Raises when every
        attempt ran away.
        """
        max_tokens = _estimate_max_tokens(text)
        attempts = [generate, fallback] if fallback else [generate] * (1 + _RETRIES)
        for n, attempt in enumerate(attempts, 1):
            wavs, sample_rate = attempt(text, max_tokens)
            frames = round(np.asarray(wavs[0]).size / (sample_rate / CODEC_FPS))
            if frames < max_tokens:
                return wavs, sample_rate
            logger.warning(
                f"Qwen3 never stopped on a {len(text)}-char chunk (hit the {max_tokens}-frame cap, "
                f"attempt {n}/{len(attempts)}); discarding that audio"
            )
        raise Qwen3Error("Qwen3 could not produce clean audio for this text. Try again or use another voice.")

    def _stream_chunks(
        self,
        text: str,
        generate: Callable[[str, int], tuple],
        fallback: Callable[[str, int], tuple] | None = None,
    ) -> Generator[bytes, None, None]:
        """
        Split text into chunks, generate each with `generate(chunk_text, max_tokens)`
        (see _generate_chunk for `fallback`) and crossfade between them for seamless
        audio. One generation at a time.
        """
        sentences = _split_sentences(text)
        with self._gen_lock:
            prev_tail: np.ndarray | None = None
            cf = _CROSSFADE_SAMPLES
            fade_out = np.linspace(1.0, 0.0, cf, dtype=np.float32)
            fade_in  = np.linspace(0.0, 1.0, cf, dtype=np.float32)

            for i, chunk in enumerate(sentences):
                # At paragraph boundary: flush previous tail + insert silence
                if chunk.is_para_start and prev_tail is not None:
                    pcm = self._audio_to_pcm(prev_tail)
                    if pcm:
                        yield pcm
                    silence = np.zeros(_PARAGRAPH_SILENCE_SAMPLES, dtype=np.float32)
                    yield self._audio_to_pcm(silence)
                    prev_tail = None

                t0 = time.perf_counter()
                wavs, sample_rate = self._generate_chunk(chunk.text, generate, fallback)
                self._sample_rate = sample_rate
                logger.debug(f"  sentence {i+1}/{len(sentences)}: {len(chunk.text)} chars in {time.perf_counter() - t0:.2f}s")

                audio = np.asarray(wavs[0], dtype=np.float32).flatten()
                if audio.size == 0:
                    continue

                # Crossfade with previous sentence tail (within same paragraph)
                if prev_tail is not None and audio.size >= cf:
                    audio[:cf] = prev_tail * fade_out + audio[:cf] * fade_in

                # Hold back tail for crossfade with next sentence
                if i < len(sentences) - 1 and audio.size > cf:
                    prev_tail = audio[-cf:].copy()
                    pcm = self._audio_to_pcm(audio[:-cf])
                else:
                    prev_tail = None
                    pcm = self._audio_to_pcm(audio)

                if pcm:
                    yield pcm

    def synthesize(
        self,
        text: str,
        voice: str | None = None,
        speed: float = 1.0,
        language: str = "auto",
    ) -> Generator[bytes, None, None]:
        """Synthesize text to speech using a Qwen3 built-in speaker."""
        model = self._get("custom")
        self._touch()

        # Parse voice ID: "qwen3_serena" -> speaker="serena"
        if voice and voice.startswith("qwen3_"):
            speaker = voice[6:]  # Strip "qwen3_" prefix
        else:
            speaker = "serena"  # Default English speaker

        # Resolve language — capitalize for Qwen3 API (e.g. "english" -> "English")
        lang = language.strip().capitalize() if language and language != "auto" else "Auto"
        logger.info(f"Qwen3 TTS: speaker={speaker}, lang={lang}, text_len={len(text)}")

        def generate(chunk_text: str, max_tokens: int):
            return model.generate_custom_voice(
                text=chunk_text,
                speaker=speaker,
                language=lang,
                temperature=0.7,
                top_p=0.9,
                top_k=50,
                repetition_penalty=1.1,
                max_new_tokens=max_tokens,
            )

        try:
            yield from self._stream_chunks(text, generate)
        except Exception as e:
            logger.error(f"Qwen3 synthesis failed: {e}")
            raise

    def synthesize_cloned(
        self,
        text: str,
        ref_audio_path: str,
        ref_text: str,
        speed: float = 1.0,
        language: str = "auto",
        xvec_only: bool = False,
    ) -> Generator[bytes, None, None]:
        """
        Synthesize text using a cloned voice (Base model + generate_voice_clone).

        By default uses full in-context learning (xvec_only=False): the reference
        clip and its exact transcript are fed to the model, which preserves the
        voice's character, accent and speaking style best. With xvec_only=True
        only a speaker embedding is used: no transcript needed, but the clone
        is less faithful.
        """
        model = self._get("base")
        self._touch()

        lang = language.strip().capitalize() if language and language != "auto" else "Auto"
        key = (ref_audio_path, ref_text)
        self.clone_downgraded = False
        if key in self._icl_failed:
            xvec_only = True
        logger.info(f"Qwen3 clone TTS: ref={ref_audio_path}, xvec_only={xvec_only}, lang={lang}, text_len={len(text)}")

        def generate(chunk_text: str, max_tokens: int, xvec: bool = xvec_only):
            return model.generate_voice_clone(
                text=chunk_text,
                language=lang,
                ref_audio=ref_audio_path,
                ref_text=ref_text,
                xvec_only=xvec,
                max_new_tokens=max_tokens,
                temperature=0.6,
                top_p=0.85,
                top_k=50,
                repetition_penalty=1.2,
            )

        def without_transcript(chunk_text: str, max_tokens: int):
            self._icl_failed.add(key)
            self.clone_downgraded = True
            logger.warning("Qwen3 cannot continue from this clip and transcript (do they match?); cloning from the voice alone")
            return generate(chunk_text, max_tokens, xvec=True)

        try:
            yield from self._stream_chunks(text, generate, None if xvec_only else without_transcript)
        except Exception as e:
            logger.error(f"Qwen3 clone synthesis failed: {e}")
            raise

    def design_voice(
        self,
        description: str,
        sample_text: str = "Hello, this is a test of my designed voice.",
    ) -> bytes:
        """
        Design a voice from a text description (1.7B VoiceDesign model, Full tier only).
        Returns raw 16-bit PCM of the sample text spoken in the designed voice.
        """
        model = self._get("design")
        self._touch()
        logger.info(f"Qwen3 voice design: desc={description[:80]}")

        try:
            def generate(chunk_text: str, max_tokens: int):
                return model.generate_voice_design(
                    text=chunk_text,
                    instruct=description,
                    language="Auto",
                    temperature=0.7,
                    top_p=0.9,
                    top_k=50,
                    repetition_penalty=1.1,
                    max_new_tokens=max_tokens,
                )

            with self._gen_lock:
                wavs, sample_rate = self._generate_chunk(sample_text, generate)
            self._sample_rate = sample_rate
            return self._audio_to_pcm(wavs[0])
        except Exception as e:
            logger.error(f"Qwen3 voice design failed: {e}")
            raise

    def _audio_to_pcm(self, audio_array: np.ndarray) -> bytes:
        """Convert model output numpy array to 16-bit PCM bytes."""
        audio_float32 = np.asarray(audio_array, dtype=np.float32).flatten()

        if audio_float32.size == 0:
            return b""

        # Clip to [-1, 1] — do NOT normalize per-chunk, as that causes
        # volume discontinuities between streaming chunks.
        audio_clipped = np.clip(audio_float32, -1.0, 1.0)
        audio_int16 = (audio_clipped * 32767).astype(np.int16)
        return audio_int16.tobytes()

    def list_voices(self) -> list[VoiceInfo]:
        """Return all Qwen3 voices: built-in + user-created profiles."""
        voices = QWEN3_BUILTIN_VOICES.copy()

        # Load user-created voice profiles from disk
        if self._voices_dir.exists():
            for profile_dir in sorted(self._voices_dir.iterdir()):
                meta_path = profile_dir / "meta.json"
                if meta_path.exists():
                    try:
                        with open(meta_path) as f:
                            meta = json.load(f)
                        if not meta.get("saved", True):
                            continue
                        source = meta.get("source", "cloned")
                        voices.append(VoiceInfo(
                            id=f"qwen3_custom_{profile_dir.name}",
                            name=meta.get("name", profile_dir.name),
                            language=meta.get("language", "en-US"),
                            gender=meta.get("gender", "unknown"),
                            description=meta.get("description", ""),
                            model="qwen3",
                            tags=["qwen3", f"qwen3-{source}"],
                        ))
                    except Exception as e:
                        logger.warning(f"Failed to load voice profile {profile_dir}: {e}")

        return voices

    @property
    def model_name(self) -> str:
        size = "1.7B" if self._tier == "full" else "0.6B"
        return f"Qwen3-TTS {size}"

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def tier(self) -> str:
        return self._tier
