# ============================================================================
# VoiceLink Inference Server — Configuration
# ============================================================================
#
# HOW THIS WORKS:
# We use Pydantic Settings to build a typed, validated config system.
# Settings can come from (in priority order):
#   1. Environment variables (VOICELINK_HOST=0.0.0.0)
#   2. A .env file in the project root
#   3. Default values defined below
#
# WHY PYDANTIC SETTINGS?
# - Automatic type validation (port must be int, not "abc")
# - Environment variable binding (12-factor app pattern)
# - IDE autocomplete — config.host shows the type
# - Nested models for grouping related settings
# ============================================================================

from pydantic_settings import BaseSettings
from pydantic import Field, field_validator
from pathlib import Path


class ServerSettings(BaseSettings):
    """Settings for the HTTP server itself."""

    host: str = Field(
        default="127.0.0.1",
        description="Bind address. 127.0.0.1 = localhost only (safe default).",
    )
    port: int = Field(
        default=7860,
        ge=1024,
        le=65535,
        description="Port to listen on. 7860 chosen to avoid common conflicts.",
    )
    log_level: str = Field(
        default="INFO",
        description="Logging level: DEBUG, INFO, WARNING, ERROR.",
    )


class ModelSettings(BaseSettings):
    """Settings for TTS model loading and inference."""

    # Which model backend to use
    default_model: str = Field(
        default="kokoro",
        description="Which TTS model to load on startup: kokoro, qwen3, etc.",
    )

    # Kokoro-specific
    kokoro_lang_code: str = Field(
        default="a",
        description="Kokoro language: 'a' = American English, 'b' = British English.",
    )
    kokoro_default_voice: str = Field(
        default="af_heart",
        description="Default Kokoro voice ID if none specified in request.",
    )

    # Qwen3-specific
    qwen3_tier: str = Field(
        default="standard",
        description="Qwen3 model tier: 'standard' (0.6B) or 'full' (1.7B).",
    )
    qwen3_enabled: bool = Field(
        default=False,
        description="Whether Qwen3 TTS is enabled (requires CUDA GPU).",
    )

    # Device selection
    device: str = Field(
        default="auto",
        description="Device: 'auto' (GPU if available, else CPU), 'cuda', 'cpu'.",
    )

    # Where to store downloaded models
    models_dir: Path = Field(
        default=Path("models"),
        description="Directory for downloaded model files.",
    )


class AudioSettings(BaseSettings):
    """Settings for audio output format."""

    sample_rate: int = Field(
        default=24000,
        description="Output sample rate in Hz. Kokoro native = 24000.",
    )
    sample_width: int = Field(
        default=2,
        description="Bytes per sample. 2 = 16-bit (matches SAPI SPSF_24kHz16BitMono).",
    )
    channels: int = Field(
        default=1,
        description="Number of audio channels. 1 = mono (SAPI standard).",
    )


class LookAheadSettings(BaseSettings):
    """Limits for the disabled-by-default optional snapshot protocol."""

    enabled: bool = False
    max_active_streams: int = Field(default=8, ge=1)
    stream_ttl_seconds: int = Field(default=30, ge=1)
    cache_max_bytes: int = Field(default=268_435_456, ge=1)
    cache_ttl_seconds: int = Field(default=600, ge=1)
    max_queued_jobs: int = Field(default=48, ge=1)
    max_runway_seconds: int = Field(default=60, ge=1)
    correlation_ttl_seconds: int = Field(default=5)
    metrics_log_interval_seconds: float = Field(default=30.0, ge=1.0, le=3600.0)

    @field_validator("correlation_ttl_seconds", mode="before")
    @classmethod
    def correlation_ttl_must_be_the_normative_integer_five(cls, value: object) -> int:
        if type(value) is not int or value != 5:
            raise ValueError("correlation_ttl_seconds must be the integer 5")
        return value


class Settings(BaseSettings):
    """Root settings object — aggregates all sub-settings."""

    server: ServerSettings = Field(default_factory=ServerSettings)
    model: ModelSettings = Field(default_factory=ModelSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    lookahead: LookAheadSettings = Field(default_factory=LookAheadSettings)

    model_config = {
        "env_prefix": "VOICELINK_",
        "env_nested_delimiter": "__",
    }


# --- Singleton ---
# Import this everywhere: `from config import settings`
settings = Settings()
