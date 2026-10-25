"""Isolated, best-effort Kokoro inference for look-ahead PCM."""

from __future__ import annotations

from dataclasses import dataclass
from multiprocessing import get_context
from multiprocessing.queues import Queue
from queue import Empty, Full
from time import monotonic
from typing import Callable
from uuid import UUID


@dataclass(frozen=True, slots=True)
class SynthesisJob:
    """A serializable speculative Kokoro job; it never contains a model object."""

    id: UUID
    stream_id: UUID
    generation: int
    sequence: int
    text: str
    voice: str
    speed: float
    cache_key: str


@dataclass(frozen=True, slots=True)
class SpeculativeResult:
    job_id: UUID
    cache_key: str
    pcm: bytes
    synthesis_seconds: float
    audio_seconds: float


@dataclass(frozen=True, slots=True)
class _Cancel:
    job_id: UUID


class _Stop:
    pass


def _lower_priority() -> None:
    """Avoid competing with the authoritative process on Windows when possible."""
    try:
        import ctypes

        below_normal = 0x00004000
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), below_normal
        )
    except Exception:
        pass


def _worker_main(
    model_settings: dict[str, str],
    jobs: Queue,
    results: Queue,
    status: Queue,
) -> None:
    """Spawn target: intentionally load a second Kokoro instance only in the child."""
    _lower_priority()
    try:
        from server.models import load_model

        model = load_model(
            "kokoro",
            lang_code=model_settings["lang_code"],
            device=model_settings["device"],
        )
    except Exception as exc:
        status.put(("failed", type(exc).__name__))
        return
    status.put(("ready", None))
    cancelled: set[UUID] = set()
    try:
        while True:
            item = jobs.get()
            if isinstance(item, _Stop):
                return
            if isinstance(item, _Cancel):
                cancelled.add(item.job_id)
                continue
            if not isinstance(item, SynthesisJob) or item.id in cancelled:
                continue
            started = monotonic()
            try:
                pcm = b"".join(model.synthesize(item.text, voice=item.voice, speed=item.speed))
                elapsed = monotonic() - started
                if item.id not in cancelled:
                    results.put(
                        SpeculativeResult(
                            item.id, item.cache_key, pcm, elapsed, len(pcm) / 48_000
                        )
                    )
            except Exception:
                # Individual speculative failures must not affect the authoritative lane.
                continue
    finally:
        try:
            model.unload()
        except Exception:
            pass


class SpeculativeWorker:
    """Own one spawned process and bounded IPC queues for speculative work."""

    def __init__(
        self,
        *,
        lang_code: str = "a",
        device: str = "auto",
        max_jobs: int = 48,
        startup_timeout: float = 15.0,
        shutdown_timeout: float = 2.0,
        entrypoint: Callable[..., None] = _worker_main,
    ) -> None:
        self._model_settings = {"lang_code": lang_code, "device": device}
        self._max_jobs = max_jobs
        self._startup_timeout = startup_timeout
        self._shutdown_timeout = shutdown_timeout
        self._entrypoint = entrypoint
        self._process = None
        self._jobs = None
        self._results = None
        self._status = None
        self._ready = False

    @property
    def ready(self) -> bool:
        if self._ready and self._process is not None and not self._process.is_alive():
            self._ready = False
        return self._ready

    def start(self) -> bool:
        if self.ready:
            return True
        self.stop()
        context = get_context("spawn")
        self._jobs = context.Queue(maxsize=self._max_jobs)
        self._results = context.Queue(maxsize=self._max_jobs)
        self._status = context.Queue(maxsize=1)
        self._process = context.Process(
            target=self._entrypoint,
            args=(self._model_settings, self._jobs, self._results, self._status),
            daemon=True,
        )
        self._process.start()
        try:
            state, _reason = self._status.get(timeout=self._startup_timeout)
        except Empty:
            self.stop()
            return False
        self._ready = state == "ready" and self._process.is_alive()
        if not self._ready:
            self.stop()
        return self._ready

    def submit(self, job: SynthesisJob) -> bool:
        if not self.ready or self._jobs is None:
            return False
        try:
            self._jobs.put_nowait(job)
            return True
        except Full:
            return False

    def cancel(self, job_id: UUID) -> None:
        if self._jobs is None or not self.ready:
            return
        try:
            self._jobs.put_nowait(_Cancel(job_id))
        except Full:
            # The scheduler has already detached this job; cancellation is best effort.
            return

    def drain_results(self) -> list[SpeculativeResult]:
        if self._results is None:
            return []
        drained: list[SpeculativeResult] = []
        while True:
            try:
                item = self._results.get_nowait()
            except Empty:
                return drained
            if isinstance(item, SpeculativeResult):
                drained.append(item)

    def stop(self) -> None:
        process = self._process
        jobs = self._jobs
        self._ready = False
        if process is not None:
            if process.is_alive() and jobs is not None:
                try:
                    jobs.put_nowait(_Stop())
                except Full:
                    pass
            process.join(self._shutdown_timeout)
            if process.is_alive():
                process.terminate()
                process.join(self._shutdown_timeout)
        self._process = self._jobs = self._results = self._status = None
