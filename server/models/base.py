# ============================================================================
# VoiceLink — Abstract TTS Model Interface
# ============================================================================
#
# WHY AN ABSTRACT BASE CLASS?
# We want to support multiple TTS backends (Kokoro, Qwen-3, Piper, etc.)
# without changing any server code. Each model just implements this interface.
#
# THE KEY INSIGHT:
# Every TTS model does fundamentally the same thing:
#   text + voice → audio samples (PCM)
#
# The differences are in HOW they do it internally. The abstract interface
# hides those differences behind a common API. This is the "Strategy Pattern."
#
# STREAMING:
# synthesize() returns a Generator that yields PCM byte chunks. This is
# critical for low latency — the COM DLL can start playing audio before
# the model finishes generating the entire sentence.
# ============================================================================

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Generator


class VoiceUnavailableError(RuntimeError):
    """The requested voice's files are not on this machine."""


@dataclass
class VoiceInfo:
    """
    Metadata about a single voice available in a model.

    This gets sent to the COM DLL so it can register each voice
    as a separate SAPI token in the Windows Registry.
    """

    id: str                         # e.g. "af_heart"
    name: str                       # e.g. "Heart"
    language: str                   # e.g. "en-US"  (BCP 47 language tag)
    gender: str                     # "female" or "male"
    description: str = ""           # e.g. "Warm, expressive female voice"
    model: str = ""                 # e.g. "kokoro"  (which backend)
    tags: list[str] = field(default_factory=list)  # e.g. ["warm", "expressive"]
    sample_rate: int = 24000        # Native sample rate of this voice


class TTSModel(ABC):
    """
    Abstract base class for all TTS model backends.

    Lifecycle:
        1. __init__()  — Store config, don't load anything heavy yet
        2. load()      — Load the model into memory (GPU/CPU)
        3. synthesize() — Generate audio from text (can call many times)
        4. unload()    — Release model from memory

    Every model backend (Kokoro, Qwen-3, Piper, etc.) subclasses this.
    """

    @abstractmethod
    def load(self) -> None:
        """
        Load the model into memory.

        This is separate from __init__() because model loading is slow
        (seconds) and may download files. We want the server to start
        quickly and load models in the background if needed.

        Should set self.is_loaded = True when done.
        """
        ...

    @abstractmethod
    def unload(self) -> None:
        """
        Release the model from GPU/CPU memory.

        Called when switching models or shutting down.
        After this, synthesize() should raise an error.
        """
        ...

    @abstractmethod
    def synthesize(
        self,
        text: str,
        voice: str | None = None,
        speed: float = 1.0,
    ) -> Generator[bytes, None, None]:
        """
        Convert text to speech audio, yielding PCM byte chunks.

        Args:
            text:  The text to speak.
            voice: Voice ID (e.g. "af_heart"). None = use default.
            speed: Speaking rate multiplier. 1.0 = normal.

        Yields:
            bytes: Raw PCM audio chunks.
                   Format: 24kHz, 16-bit signed little-endian, mono.
                   Each chunk is a complete segment of audio that can
                   be written directly to SAPI's audio sink.

        Why a Generator?
            - Low latency: COM DLL gets first audio in ~100ms
            - Memory efficient: don't buffer entire audio in RAM
            - Cancellable: caller can stop iterating = stop generating
        """
        ...

    def single_chunk(self, text: str) -> bool:
        """True if synthesize(text) yields exactly one chunk (so its length is known up front)."""
        return False

    @abstractmethod
    def list_voices(self) -> list[VoiceInfo]:
        """
        Return metadata for all voices this model supports.

        Used by:
        - GET /v1/voices endpoint
        - COM DLL to register SAPI voice tokens
        - Settings GUI to show available voices
        """
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Human-readable model name, e.g. 'Kokoro v1.0'."""
        ...

    @property
    @abstractmethod
    def is_loaded(self) -> bool:
        """Whether the model is loaded and ready for synthesis."""
        ...
