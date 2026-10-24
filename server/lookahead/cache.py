"""Canonical, bounded PCM storage for exact synthesis inputs."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
import json
from threading import RLock
from types import MappingProxyType


def _freeze_json(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("synthesis options must use string keys")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError("synthesis options must contain JSON-compatible values")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class SynthesisConfig:
    """Every configuration value that can change canonical synthesized speech."""

    route: str
    model_version: str
    voice: str
    speed: float
    format: str
    language: str
    options: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", _freeze_json(self.options))

    def digest_payload(self) -> dict[str, object]:
        return {
            "route": self.route,
            "model_version": self.model_version,
            "voice": self.voice,
            "speed": self.speed,
            "format": self.format,
            "language": self.language,
            "options": _thaw_json(self.options),
        }


def synthesis_input_digest(text: str, config: SynthesisConfig) -> str:
    """Return an opaque key for exact UTF-8 text and all synthesis inputs."""
    payload = {"text": text, "config": config.digest_payload()}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CacheEntry:
    pcm: bytes
    expires_at: float


class PcmCache:
    """Thread-safe byte-bounded LRU cache with per-entry monotonic expiry."""

    DEFAULT_MAX_BYTES = 256 * 1024 * 1024

    def __init__(self, *, max_bytes: int = DEFAULT_MAX_BYTES, ttl_seconds: float = 300.0) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must not be negative")
        self._max_bytes = max_bytes
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self._total_bytes = 0
        self._lock = RLock()

    def get(self, key: str, now: float) -> bytes | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._total_bytes -= len(entry.pcm)
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return entry.pcm

    def put(self, key: str, pcm: bytes, now: float) -> bool:
        """Insert PCM if it fits without sacrificing cached values for an oversized item."""
        if len(pcm) > self._max_bytes:
            return False
        with self._lock:
            self._remove_expired(now)
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._total_bytes -= len(previous.pcm)
            while self._total_bytes + len(pcm) > self._max_bytes:
                _, evicted = self._entries.popitem(last=False)
                self._total_bytes -= len(evicted.pcm)
            self._entries[key] = CacheEntry(pcm=pcm, expires_at=now + self._ttl_seconds)
            self._total_bytes += len(pcm)
            return True

    def _remove_expired(self, now: float) -> None:
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._total_bytes -= len(self._entries[key].pcm)
            del self._entries[key]
