"""Structure-aware pause policy and PCM prefix-silence generation.

Boundary silence is intentionally generated outside canonical speech synthesis and
cache storage.  The canonical speech bytes can therefore be reused unchanged,
while this module adds only the residual structural gap for a request.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from server.lookahead.protocol import BoundaryKind


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """The PCM properties needed to size signed 16-bit silence frames."""

    sample_rate: int
    channels: int
    sample_width_bytes: int = 2

    def __post_init__(self) -> None:
        """Only signed 16-bit PCM is supported by the bridge contract."""
        if self.sample_width_bytes != 2:
            raise ValueError("PCM audio format must use signed 16-bit samples")
        if (
            not isinstance(self.sample_rate, int)
            or isinstance(self.sample_rate, bool)
            or self.sample_rate <= 0
        ):
            raise ValueError("sample_rate must be a positive integer")
        if (
            not isinstance(self.channels, int)
            or isinstance(self.channels, bool)
            or self.channels <= 0
        ):
            raise ValueError("channels must be a positive integer")


class ProsodyPolicy:
    """Map semantic boundaries to conservative default pauses in milliseconds."""

    DEFAULT_GAPS_MS = MappingProxyType(
        {
            BoundaryKind.NONE: 0,
            BoundaryKind.SENTENCE: 80,
            BoundaryKind.PARAGRAPH: 180,
            BoundaryKind.LIST_ITEM: 220,
            BoundaryKind.HEADING_TO_BODY: 320,
            BoundaryKind.SECTION: 400,
            BoundaryKind.DOCUMENT: 450,
        }
    )

    def desired_gap_ms(self, boundary: BoundaryKind, stream_start: bool = False) -> int:
        """Return the desired structural gap, suppressing it at stream start."""
        if stream_start:
            return 0
        return self.DEFAULT_GAPS_MS[BoundaryKind(boundary)]


def prefix_silence_pcm(
    boundary: BoundaryKind,
    request_wait_ms: float,
    audio_format: AudioFormat,
    stream_start: bool = False,
) -> bytes:
    """Return residual signed 16-bit zero PCM for a boundary's leading gap.

    The duration is ``max(0, desired_gap - request_wait)``.  Frame count uses
    the specified nearest-integer rounding before multiplying by channels and
    bytes per sample, so the result is always aligned to complete PCM frames.
    """
    desired_gap_ms = ProsodyPolicy().desired_gap_ms(boundary, stream_start=stream_start)
    residual_ms = max(0, desired_gap_ms - request_wait_ms)
    frames = round(residual_ms * audio_format.sample_rate / 1000)
    frame_bytes = audio_format.channels * audio_format.sample_width_bytes
    return b"\x00" * (frames * frame_bytes)
