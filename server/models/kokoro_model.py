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

import re
import threading

import numpy as np
from typing import Generator
from loguru import logger

from server.models.base import TTSModel, VoiceInfo, VoiceUnavailableError


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


# Kokoro only splits text on newlines (then at ~510 tokens), so a few sentences
# become ONE chunk and nothing is heard until all of it is generated. We put the
# first sentence in its own chunk so audio starts early; the rest keeps Kokoro's
# default chunking (splitting every sentence is ~24 % slower in total on CPU).
_SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*\s+(?=\S)")
_MIN_FIRST_CHARS = 15  # shortcut: "Dr. " / "e.g. " would otherwise be spoken alone
_MAX_SINGLE_CHUNK_CHARS = 400  # well under Kokoro's 510-token limit


def _split_first_sentence(text: str) -> str:
    """Insert a newline after the first sentence (unless the text already has one there)."""
    text = text.strip()
    newline = text.find("\n")
    for m in _SENTENCE_END.finditer(text, 0, len(text) if newline < 0 else newline):
        if m.end() >= _MIN_FIRST_CHARS:
            return text[: m.end()].rstrip() + "\n" + text[m.end():]
    return text


def _build_kmodel(device: str):
    """
    Build Kokoro's acoustic model directly on `device`, without the memory spike.

    KModel() creates every weight with random values, then torch.load()s the
    312 MB checkpoint and copies it over. That commits ~1 GB of RAM which Windows
    never gets back. Here the module is built on the meta device (no memory),
    and the checkpoint is memory-mapped and loaded straight onto `device`.
    Raises if anything looks unexpected; the caller then uses Kokoro's own loader.
    """
    import io
    import itertools

    import torch
    from huggingface_hub import hf_hub_download
    from kokoro import KModel

    repo = "hexgrad/Kokoro-82M"  # KPipeline's default
    config = hf_hub_download(repo, "config.json")
    weights = hf_hub_download(repo, KModel.MODEL_NAMES[repo])

    # KModel always torch.load()s `model`; hand it an empty checkpoint.
    empty = io.BytesIO()
    torch.save({}, empty)
    empty.seek(0)
    with torch.device("meta"):
        model = KModel(repo_id=repo, config=config, model=empty)

    # Same as KModel: the keys carry a "module." prefix, loaded non-strictly.
    for part, state in torch.load(weights, map_location=device, mmap=True, weights_only=True).items():
        state = {k.removeprefix("module."): v for k, v in state.items()}
        getattr(model, part).load_state_dict(state, strict=False, assign=True)

    # What the checkpoint lacks keeps its default value in Kokoro's loader. Rebuild
    # those defaults here: InstanceNorm's affine weight/bias and the Albert
    # embeddings' index buffers and the STFT's Hann window. Anything else we don't
    # know about makes the smoke forward below fail.
    for mod in model.modules():
        window = vars(mod).get("window")
        if isinstance(window, torch.Tensor) and window.is_meta:
            mod.window = torch.hann_window(window.shape[0], periodic=True, dtype=torch.float32)
        for name, t in list(itertools.chain(mod.named_parameters(recurse=False), mod.named_buffers(recurse=False))):
            if not t.is_meta:
                continue
            if name == "position_ids":
                buffer = torch.arange(t.shape[-1], device=device).expand(t.shape)
            elif name == "token_type_ids":
                buffer = torch.zeros(t.shape, dtype=t.dtype, device=device)
            elif isinstance(mod, torch.nn.modules.instancenorm._InstanceNorm) and name in ("weight", "bias"):
                fill = torch.ones if name == "weight" else torch.zeros
                setattr(mod, name, torch.nn.Parameter(fill(t.shape, dtype=t.dtype, device=device)))
                continue
            else:
                raise RuntimeError(f"unexpected unloaded tensor {type(mod).__name__}.{name}")
            mod.register_buffer(name, buffer, persistent=False)

    model = model.to(device).eval()
    model("həlˈO", torch.zeros(1, 256, device=device))  # a broken build fails here, not on a user's request
    return model


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
        self._on_cuda = False
        # Kokoro isn't documented as thread-safe and streaming requests overlap.
        # Held per generated chunk (not per request) so concurrent requests
        # interleave instead of one waiting for another's whole text.
        self._lock = threading.Lock()

    def load(self) -> None:
        """Load the Kokoro pipeline (downloads model on first run)."""
        if self._loaded:
            logger.debug("Kokoro already loaded, skipping.")
            return

        logger.info(f"Loading Kokoro model (lang={self._lang_code})...")

        import torch
        from kokoro import KPipeline

        # Kokoro auto-selects CUDA when device is None
        device = None if self._device == "auto" else self._device
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        elif device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA requested but not available")
        try:
            kmodel = _build_kmodel(device)
            self._pipeline = KPipeline(lang_code=self._lang_code, model=kmodel, repo_id="hexgrad/Kokoro-82M")
        except Exception as e:
            logger.warning(f"Lean Kokoro load failed ({e}); using Kokoro's own loader (uses ~1 GB more RAM).")
            self._pipeline = KPipeline(lang_code=self._lang_code, device=device)
        self._on_cuda = device == "cuda"
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

        logger.debug(f"Synthesizing: voice={voice}, speed={speed}, text={text[:80]}...")

        # Load the voice file now so a missing one fails with a clear error.
        # (The app runs the server with HF_HUB_OFFLINE=1, so it never downloads.)
        try:
            with self._lock:
                self._pipeline.load_voice(voice)
        except Exception as e:
            logger.error(f"Voice '{voice}' could not be loaded: {e}")
            raise VoiceUnavailableError(
                f"Voice '{voice}' files are missing. Use Settings -> Restore to download them."
            ) from e

        # --- Run Kokoro pipeline ---
        # pipeline() returns a generator of (graphemes, phonemes, audio) tuples.
        # Each tuple corresponds to roughly one sentence/clause.
        # audio is a numpy float32 array in range [-1.0, 1.0] at 24kHz.
        chunk_index = 0
        results = self._pipeline(_split_first_sentence(text), voice=voice, speed=speed)
        while True:
            with self._lock:  # one chunk of inference at a time
                result = next(results, None)
            if result is None:
                break
            graphemes, phonemes, audio_raw = result
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

    def release_cache(self) -> None:
        """Hand the GPU memory PyTorch cached for the last request back to the driver."""
        if self._on_cuda:
            import torch

            torch.cuda.empty_cache()

    def single_chunk(self, text: str) -> bool:
        split = _split_first_sentence(text)
        return "\n" not in split and len(split) <= _MAX_SINGLE_CHUNK_CHARS

    def list_voices(self) -> list[VoiceInfo]:
        """Return all known Kokoro voices."""
        return KOKORO_VOICES.copy()

    @property
    def model_name(self) -> str:
        return "Kokoro"

    @property
    def is_loaded(self) -> bool:
        return self._loaded
