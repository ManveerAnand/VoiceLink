# ============================================================================
# VoiceLink — Qwen3 TTS Model Backend (CUDA Graph Accelerated)
# ============================================================================
#
# HOW THIS WORKS:
# 1. Uses faster-qwen3-tts (pip install faster-qwen3-tts) which wraps
#    Qwen3-TTS with CUDA graph capture for ~6-10x speedup.
# 2. Supports three modes:
#    - Built-in voices (9 speakers via CustomVoice model)
#    - Voice cloning (Base model — clone from 3s audio)
#    - Voice design (VoiceDesign model, 1.7B only — describe a voice)
# 3. Lazy loaded: model loads on first request, unloads after idle timeout.
# 4. First inference is slow (~100s) due to CUDA graph capture warmup,
#    subsequent calls run at ~2x realtime on RTX 4060 Laptop.
#
# QWEN3-TTS ARCHITECTURE:
#   Text + Speaker → Qwen3-TTS LM → Discrete Codes → Tokenizer-12Hz → Audio
#   - Tokenizer-12Hz: 12.5 Hz, 16-layer multi-codebook codec
#   - Output: 24kHz audio (same as Kokoro — no resampling needed)
#
# MODEL VARIANTS:
#   0.6B-Base: 9 built-in speakers, ~1.2 GB
#   0.6B-CustomVoice: Voice cloning via 3s audio reference
#   1.7B-Base: Same 9 speakers, higher quality
#   1.7B-CustomVoice: Better voice cloning
#   1.7B-VoiceDesign: Text-described voice generation
#
# BUILT-IN SPEAKERS (from CustomVoice model):
#   English-friendly: Serena, Aiden, Dylan, Eric, Ryan, Vivian
#   Other: ono_anna, sohee, uncle_fu
#   We expose the 6 English speakers for VoiceLink.
# ============================================================================

import os
import re
import time
import json
import threading
import numpy as np
import torch
from typing import Generator
from pathlib import Path
from loguru import logger

from faster_qwen3_tts import FasterQwen3TTS
from server.models.base import TTSModel, VoiceInfo


# --- Sentence / Paragraph Splitting ---
# Split on sentence-ending punctuation followed by whitespace.
_SENTENCE_RE = re.compile(r'(?<=[.!?;:])[\s]+')

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


def _split_sentences(text: str) -> list[_Chunk]:
    """Split text into sentences, preserving paragraph boundaries.

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

        for sent_idx, sent in enumerate(merged):
            is_para_start = (sent_idx == 0 and para_idx > 0)
            chunks.append(_Chunk(sent, is_para_start))

    return chunks if chunks else [_Chunk(text)]

def _estimate_max_tokens(text: str) -> int:
    """Estimate max codec tokens for text to prevent degenerate overgeneration.
    English speech ~12-15 chars/sec, codec at 12 Hz. Allow 3x headroom."""
    estimated_secs = max(len(text) / 12, 2.0)
    return min(int(estimated_secs * 12 * 3), 2048)

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
        self._cv_model = None     # FasterQwen3TTS CustomVoice (built-in speakers)
        self._base_model_inst = None  # FasterQwen3TTS Base (voice cloning)
        self._loaded = False
        self._last_used = 0.0
        self._idle_timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._sample_rate = 24000  # Updated from model on load

        # Model IDs based on tier
        if tier == "full":
            self._base_model = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
            self._custom_model = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
            self._design_model = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
        else:
            self._base_model = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
            self._custom_model = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
            self._design_model = None  # VoiceDesign only available in 1.7B

        self._tokenizer_model = "Qwen/Qwen3-TTS-Tokenizer-12Hz"

        # User-created voice profiles directory
        self._voices_dir = Path(
            os.environ.get("VOICELINK_DATA_DIR", r"C:\ProgramData\VoiceLink")
        ).joinpath("voices")

    def load(self) -> None:
        """Load the CustomVoice model (built-in speakers) with CUDA graphs."""
        with self._lock:
            if self._loaded:
                logger.debug("Qwen3 already loaded, skipping.")
                return

            logger.info(f"Loading FasterQwen3TTS CustomVoice ({self._tier}) on {self._device}...")
            t0 = time.perf_counter()

            try:
                self._cv_model = FasterQwen3TTS.from_pretrained(
                    self._custom_model,
                )
                self._loaded = True
                self._last_used = time.time()

                elapsed = time.perf_counter() - t0
                logger.info(f"FasterQwen3TTS CustomVoice loaded in {elapsed:.1f}s")
                logger.info("Note: First TTS call will be slow (~100s) for CUDA graph capture.")

            except Exception as e:
                logger.error(f"Failed to load FasterQwen3TTS: {e}")
                raise

    def _ensure_base_loaded(self):
        """Lazy-load the Base model for voice cloning with CUDA graphs."""
        if self._base_model_inst is not None:
            return

        logger.info(f"Loading FasterQwen3TTS Base ({self._tier}) for voice cloning...")
        t0 = time.perf_counter()
        self._base_model_inst = FasterQwen3TTS.from_pretrained(
            self._base_model,
        )
        elapsed = time.perf_counter() - t0
        logger.info(f"FasterQwen3TTS Base loaded in {elapsed:.1f}s")
        logger.info("Note: First clone call will be slow (~100s) for CUDA graph capture.")

    def unload(self) -> None:
        """Release all Qwen3-TTS models from GPU memory."""
        with self._lock:
            unloaded = []
            if self._cv_model is not None:
                del self._cv_model
                self._cv_model = None
                unloaded.append("CustomVoice")
            if self._base_model_inst is not None:
                del self._base_model_inst
                self._base_model_inst = None
                unloaded.append("Base")

            self._loaded = False

            if unloaded:
                try:
                    import torch
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
        if elapsed >= IDLE_TIMEOUT_SECS:
            logger.info(f"Qwen3-TTS idle for {elapsed:.0f}s, unloading to free GPU...")
            self.unload()
        else:
            # Race condition: request came in just before timer fired
            self._reset_idle_timer()

    def _ensure_loaded(self):
        """Lazy load: ensure the model is in memory before use."""
        if not self._loaded:
            self.load()
        self._touch()

    def synthesize(
        self,
        text: str,
        voice: str | None = None,
        speed: float = 1.0,
        language: str = "auto",
    ) -> Generator[bytes, None, None]:
        """
        Synthesize text to speech using a Qwen3 built-in speaker.
        Splits long text into sentences, generates each with full-context
        decode, and crossfades between them for seamless audio.
        """
        self._ensure_loaded()

        if self._cv_model is None:
            raise RuntimeError("Qwen3 CustomVoice model failed to load.")

        # Parse voice ID: "qwen3_serena" -> speaker="serena"
        if voice and voice.startswith("qwen3_"):
            speaker = voice[6:]  # Strip "qwen3_" prefix
        else:
            speaker = "serena"  # Default English speaker

        # Resolve language — capitalize for Qwen3 API (e.g. "english" -> "English")
        lang = language.strip().capitalize() if language and language != "auto" else "Auto"

        sentences = _split_sentences(text)
        logger.info(f"Qwen3 TTS: speaker={speaker}, lang={lang}, text_len={len(text)}, sentences={len(sentences)}")

        try:
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
                    # Insert paragraph silence gap
                    silence = np.zeros(_PARAGRAPH_SILENCE_SAMPLES, dtype=np.float32)
                    yield self._audio_to_pcm(silence)
                    prev_tail = None

                t0 = time.perf_counter()
                wavs, sample_rate = self._cv_model.generate_custom_voice(
                    text=chunk.text,
                    speaker=speaker,
                    language=lang,
                )
                self._sample_rate = sample_rate
                elapsed = time.perf_counter() - t0
                logger.debug(f"  sentence {i+1}/{len(sentences)}: {len(chunk.text)} chars in {elapsed:.2f}s")

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
                    if prev_tail is not None and i == len(sentences) - 1:
                        pass  # already blended above, emit full
                    prev_tail = None
                    pcm = self._audio_to_pcm(audio)

                if pcm:
                    yield pcm

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
    ) -> Generator[bytes, None, None]:
        """
        Synthesize text using a cloned voice (Base model + generate_voice_clone).
        Splits long text into sentences, generates each with full-context
        decode, and crossfades between them for seamless audio.

        Uses full in-context learning (xvec_only=False) to preserve the original
        voice's character, accent, and speaking style from the reference audio.
        """
        self._ensure_loaded()
        self._ensure_base_loaded()

        if self._base_model_inst is None:
            raise RuntimeError("Qwen3 Base model failed to load.")

        # Resolve language
        lang = language.strip().capitalize() if language and language != "auto" else "Auto"

        sentences = _split_sentences(text)
        logger.info(f"Qwen3 clone TTS: ref={ref_audio_path}, lang={lang}, text_len={len(text)}, sentences={len(sentences)}")

        try:
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

                max_tokens = _estimate_max_tokens(chunk.text)
                t0 = time.perf_counter()
                wavs, sample_rate = self._base_model_inst.generate_voice_clone(
                    text=chunk.text,
                    language=lang,
                    ref_audio=ref_audio_path,
                    ref_text=ref_text,
                    xvec_only=False,
                    max_new_tokens=max_tokens,
                    repetition_penalty=1.1,
                )
                self._sample_rate = sample_rate
                elapsed = time.perf_counter() - t0
                logger.debug(f"  sentence {i+1}/{len(sentences)}: {len(chunk.text)} chars in {elapsed:.2f}s")

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
                    if prev_tail is not None and i == len(sentences) - 1:
                        pass  # already blended above, emit full
                    prev_tail = None
                    pcm = self._audio_to_pcm(audio)

                if pcm:
                    yield pcm

        except Exception as e:
            logger.error(f"Qwen3 clone synthesis failed: {e}")
            raise

    def design_voice(
        self,
        description: str,
        sample_text: str = "Hello, this is a test of my designed voice.",
    ) -> bytes:
        """
        Design a voice from a text description (1.7B VoiceDesign model only).
        Returns raw PCM audio of the sample text spoken in the designed voice.
        """
        if self._design_model is None:
            raise RuntimeError("Voice design requires the Full (1.7B) model tier.")

        self._ensure_loaded()

        # For voice design, we'd need a separate VoiceDesign model instance.
        # For now, raise if someone tries this without the full tier.
        logger.info(f"Qwen3 voice design: desc={description[:80]}")

        try:
            # Voice design would need its own model loaded from self._design_model
            raise NotImplementedError("Voice design is not yet implemented.")
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
