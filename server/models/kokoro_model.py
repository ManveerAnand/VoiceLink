# ============================================================================
# VoiceLink — Kokoro TTS Model Backend
# ============================================================================
#
# HOW THIS WORKS:
# 1. We load Kokoro's KPipeline (which internally loads the acoustic model
#    and vocoder)
# 2. When synthesize() is called, we feed text through the pipeline
# 3. Kokoro returns (graphemes, phonemes, audio_tensor) tuples
# 4. We convert each audio tensor to PCM bytes and yield them
#
# KOKORO'S PIPELINE:
#   Text → Misaki (phonemizer) → Kokoro model → float32 audio at 24kHz
#   We then convert: float32 [-1.0, 1.0] → int16 [-32768, 32767] (PCM)
#
# STREAMING BEHAVIOR:
# Kokoro's pipeline already yields chunks (roughly sentence-level).
# Each yield from their pipeline becomes one yield from our synthesize().
# This gives us natural streaming — first audio arrives after ~100ms.
#
# VOICE IDs:
# Kokoro uses a naming convention: {lang}{gender}_{name}
#   a = American English, b = British English, j = Japanese
#   f = female, m = male
#   Examples: af_heart, am_adam, bf_emma, jf_alpha
# ============================================================================

import numpy as np
from typing import Generator
from loguru import logger

from server.models.base import TTSModel, VoiceInfo


# --- Kokoro Voice Catalog ---
# These are the built-in voices. We define metadata here so we can
# report it to the COM DLL and Settings GUI.
#
# NOTE: Kokoro may support more voices than listed here. These are the
# ones we've verified and documented.
KOKORO_VOICES: list[VoiceInfo] = [
    # American English - Female
    VoiceInfo(
        id="af_heart",
        name="Heart",
        language="en-US",
        gender="female",
        description="Warm, expressive female voice. Great for audiobooks.",
        model="kokoro",
        tags=["warm", "expressive", "default"],
    ),
    VoiceInfo(
        id="af_bella",
        name="Bella",
        language="en-US",
        gender="female",
        description="Clear, professional female voice.",
        model="kokoro",
        tags=["clear", "professional"],
    ),
    VoiceInfo(
        id="af_nicole",
        name="Nicole",
        language="en-US",
        gender="female",
        description="Smooth, calm female voice.",
        model="kokoro",
        tags=["smooth", "calm"],
    ),
    VoiceInfo(
        id="af_sarah",
        name="Sarah",
        language="en-US",
        gender="female",
        description="Friendly, conversational female voice.",
        model="kokoro",
        tags=["friendly", "conversational"],
    ),
    VoiceInfo(
        id="af_sky",
        name="Sky",
        language="en-US",
        gender="female",
        description="Light, youthful female voice.",
        model="kokoro",
        tags=["light", "youthful"],
    ),
    # American English - Male
    VoiceInfo(
        id="am_adam",
        name="Adam",
        language="en-US",
        gender="male",
        description="Natural, conversational male voice.",
        model="kokoro",
        tags=["natural", "conversational"],
    ),
    VoiceInfo(
        id="am_michael",
        name="Michael",
        language="en-US",
        gender="male",
        description="Deep, authoritative male voice.",
        model="kokoro",
        tags=["deep", "authoritative"],
    ),
    # British English - Female
    VoiceInfo(
        id="bf_emma",
        name="Emma",
        language="en-GB",
        gender="female",
        description="British English female voice.",
        model="kokoro",
        tags=["british"],
    ),
    VoiceInfo(
        id="bf_isabella",
        name="Isabella",
        language="en-GB",
        gender="female",
        description="Elegant British female voice.",
        model="kokoro",
        tags=["british", "elegant"],
    ),
    # British English - Male
    VoiceInfo(
        id="bm_george",
        name="George",
        language="en-GB",
        gender="male",
        description="British English male voice.",
        model="kokoro",
        tags=["british"],
    ),
    VoiceInfo(
        id="bm_lewis",
        name="Lewis",
        language="en-GB",
        gender="male",
        description="Warm British male voice.",
        model="kokoro",
        tags=["british", "warm"],
    ),
]


class KokoroModel(TTSModel):
    """
    Kokoro TTS backend.

    Wraps Kokoro's KPipeline to implement VoiceLink's TTSModel interface.
    """

    def __init__(self, lang_code: str = "a", device: str = "auto"):
        """
        Args:
            lang_code: Kokoro language code. 'a' = American, 'b' = British.
            device: 'auto', 'cuda', or 'cpu'.
        """
        self._lang_code = lang_code
        self._device = device
        self._pipeline = None
        self._loaded = False

    def load(self) -> None:
        """Load the Kokoro pipeline (downloads model on first run)."""
        if self._loaded:
            logger.debug("Kokoro already loaded, skipping.")
            return

        logger.info(f"Loading Kokoro model (lang={self._lang_code})...")

        from kokoro import KPipeline

        self._pipeline = KPipeline(lang_code=self._lang_code)
        self._loaded = True

        logger.info("Kokoro model loaded successfully.")

    def unload(self) -> None:
        """Release the Kokoro model from memory."""
        if self._pipeline is not None:
            del self._pipeline
            self._pipeline = None
            self._loaded = False
            logger.info("Kokoro model unloaded.")

    def synthesize(
        self,
        text: str,
        voice: str | None = None,
        speed: float = 1.0,
    ) -> Generator[bytes, None, None]:
        """
        Generate speech from text, yielding PCM byte chunks.

        Each chunk is a segment of audio (roughly one sentence).

        PCM format: 24kHz, 16-bit signed little-endian, mono.
        This matches SAPI's SPSF_24kHz16BitMono exactly.
        """
        if not self._loaded or self._pipeline is None:
            raise RuntimeError("Kokoro model is not loaded. Call load() first.")

        # Default voice
        if voice is None:
            voice = "af_heart"

        logger.debug(
            f"Synthesizing: voice={voice}, speed={speed}, text_len={len(text)}"
        )

        # --- Run Kokoro pipeline ---
        # pipeline() returns a generator of (graphemes, phonemes, audio) tuples.
        # Each tuple corresponds to roughly one sentence/clause.
        # audio is a numpy float32 array in range [-1.0, 1.0] at 24kHz.
        chunk_index = 0
        for graphemes, phonemes, audio_raw in self._pipeline(
            text, voice=voice, speed=speed
        ):
            if audio_raw is None:
                continue

            # --- Convert to numpy if needed ---
            # Kokoro may return a PyTorch tensor or numpy array depending
            # on version. We handle both:
            if hasattr(audio_raw, "numpy"):
                # It's a PyTorch tensor → convert to numpy
                audio_float32 = audio_raw.detach().cpu().numpy()
            elif hasattr(audio_raw, "__array__"):
                audio_float32 = np.asarray(audio_raw, dtype=np.float32)
            else:
                audio_float32 = np.array(audio_raw, dtype=np.float32)

            if audio_float32.size == 0:
                continue

            # Convert float32 [-1.0, 1.0] → int16 [-32768, 32767]
            # This is the PCM format SAPI expects.
            #
            # WHY THIS CONVERSION:
            # - Neural TTS models output normalized float32 (standard in ML)
            # - SAPI/Windows audio expects integer PCM (standard in audio)
            # - int16 = 16-bit = CD quality = good enough for speech
            #
            # CLIPPING:
            # np.clip prevents overflow: if model outputs > 1.0, clamp to 1.0.
            # Without this, int16 conversion wraps around → horrific crackling.
            audio_clipped = np.clip(audio_float32, -1.0, 1.0)
            audio_int16 = (audio_clipped * 32767).astype(np.int16)

            # Convert to raw bytes (little-endian on Windows, which numpy uses)
            pcm_bytes = audio_int16.tobytes()

            chunk_index += 1
            logger.debug(
                f"  Chunk {chunk_index}: {len(pcm_bytes)} bytes "
                f"({len(audio_int16) / 24000:.2f}s audio)"
            )

            yield pcm_bytes

        logger.debug(f"Synthesis complete: {chunk_index} chunks yielded.")

    def list_voices(self) -> list[VoiceInfo]:
        """Return all known Kokoro voices."""
        return KOKORO_VOICES.copy()

    @property
    def model_name(self) -> str:
        return "Kokoro"

    @property
    def is_loaded(self) -> bool:
        return self._loaded
