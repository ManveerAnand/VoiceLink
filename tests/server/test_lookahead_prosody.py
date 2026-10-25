import pytest

from server.lookahead.prosody import AudioFormat, ProsodyPolicy, prefix_silence_pcm
from server.lookahead.protocol import BoundaryKind


def test_default_pause_durations_are_exact_and_structurally_ordered() -> None:
    policy = ProsodyPolicy()

    durations = {
        boundary: policy.desired_gap_ms(boundary, stream_start=False)
        for boundary in BoundaryKind
    }

    assert durations == {
        BoundaryKind.NONE: 0,
        BoundaryKind.SENTENCE: 80,
        BoundaryKind.PARAGRAPH: 180,
        BoundaryKind.LIST_ITEM: 220,
        BoundaryKind.HEADING_TO_BODY: 320,
        BoundaryKind.SECTION: 400,
        BoundaryKind.DOCUMENT: 450,
    }
    assert durations[BoundaryKind.NONE] < durations[BoundaryKind.SENTENCE]
    assert durations[BoundaryKind.SENTENCE] < durations[BoundaryKind.PARAGRAPH]
    assert durations[BoundaryKind.PARAGRAPH] < durations[BoundaryKind.LIST_ITEM]
    assert durations[BoundaryKind.LIST_ITEM] < durations[BoundaryKind.HEADING_TO_BODY]
    assert durations[BoundaryKind.HEADING_TO_BODY] < durations[BoundaryKind.SECTION]
    assert durations[BoundaryKind.SECTION] < durations[BoundaryKind.DOCUMENT]


@pytest.mark.parametrize("boundary", list(BoundaryKind))
def test_stream_start_suppresses_every_structural_pause(boundary: BoundaryKind) -> None:
    assert ProsodyPolicy().desired_gap_ms(boundary, stream_start=True) == 0


def test_prefix_silence_uses_only_the_residual_after_request_wait() -> None:
    audio_format = AudioFormat(sample_rate=1_000, channels=2)

    # 180 ms residual = 180 complete frames, two channels, signed 16-bit samples.
    silence = prefix_silence_pcm(
        BoundaryKind.PARAGRAPH,
        request_wait_ms=0,
        audio_format=audio_format,
    )

    assert len(silence) == 180 * 2 * 2
    assert silence == b"\x00" * len(silence)

    residual = prefix_silence_pcm(
        BoundaryKind.PARAGRAPH,
        request_wait_ms=30,
        audio_format=audio_format,
    )
    assert len(residual) == 150 * 2 * 2
    assert residual == b"\x00" * len(residual)

    # Wait longer than the desired gap: no negative duration and no bytes.
    assert (
        prefix_silence_pcm(
            BoundaryKind.PARAGRAPH,
            request_wait_ms=181,
            audio_format=audio_format,
        )
        == b""
    )


def test_prefix_silence_rounds_frames_and_remains_frame_aligned() -> None:
    audio_format = AudioFormat(sample_rate=1_000, channels=2)

    # 80 ms - 79.6 ms = 0.4 ms; round(0.4 frames) gives zero frames.
    assert (
        prefix_silence_pcm(
            BoundaryKind.SENTENCE,
            request_wait_ms=79.6,
            audio_format=audio_format,
        )
        == b""
    )

    # 1.6 ms residual rounds to two frames, never a partial channel/frame.
    rounded = prefix_silence_pcm(
        BoundaryKind.SENTENCE,
        request_wait_ms=78.4,
        audio_format=audio_format,
    )
    assert len(rounded) == 2 * audio_format.channels * 2
    assert len(rounded) % (audio_format.channels * 2) == 0
    assert rounded == b"\x00" * len(rounded)


def test_prefix_silence_uses_sample_rate_when_rounding_frame_count() -> None:
    audio_format = AudioFormat(sample_rate=16_000, channels=1)

    # 80 ms at 16 kHz is 1,280 frames, not 80 frames.
    silence = prefix_silence_pcm(
        BoundaryKind.SENTENCE,
        request_wait_ms=0,
        audio_format=audio_format,
    )

    assert len(silence) == round(80 * 16_000 / 1_000) * 1 * 2
    assert silence == b"\x00" * len(silence)


def test_stream_start_has_no_leading_document_silence_and_speech_bytes_stay_canonical() -> (
    None
):
    audio_format = AudioFormat(sample_rate=24_000, channels=1)
    canonical_speech = b"\x01\x00\xff\x7f"

    prefix = prefix_silence_pcm(
        BoundaryKind.DOCUMENT,
        request_wait_ms=0,
        audio_format=audio_format,
        stream_start=True,
    )
    response = prefix + canonical_speech

    assert prefix == b""
    assert response == canonical_speech
    assert response[-len(canonical_speech) :] == canonical_speech


def test_audio_format_requires_positive_rate_and_channels_and_signed_16_bit_samples() -> (
    None
):
    with pytest.raises(ValueError):
        AudioFormat(sample_rate=0, channels=1)
    with pytest.raises(ValueError):
        AudioFormat(sample_rate=1_000, channels=0)
    with pytest.raises(ValueError):
        AudioFormat(sample_rate=1_000, channels=1, sample_width=1)
