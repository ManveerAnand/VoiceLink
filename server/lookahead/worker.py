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
    failure: str | None = None


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
    controls: Queue,
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

    def drain_controls() -> bool:
        while True:
            try:
                control = controls.get_nowait()
            except Empty:
                return False
            if isinstance(control, _Stop):
                return True
            if isinstance(control, _Cancel):
                cancelled.add(control.job_id)

    try:
        while True:
            if drain_controls():
                return
            try:
                item = jobs.get(timeout=0.1)
            except Empty:
                continue
            if isinstance(item, _Stop):
                return
            if not isinstance(item, SynthesisJob):
                continue
            if item.id in cancelled:
                cancelled.discard(item.id)
                continue
            status.put(("active", item.id))
            started = monotonic()
            try:
                pcm = b"".join(model.synthesize(item.text, voice=item.voice, speed=item.speed))
                elapsed = monotonic() - started
                if item.id not in cancelled:
                    results.put_nowait(
                        SpeculativeResult(
                            item.id, item.cache_key, pcm, elapsed, len(pcm) / 48_000
                        )
                    )
            except Exception as exc:
                # Tell the parent to release the bounded slot; failures never touch
                # the authoritative model or its request lane.
                try:
                    results.put_nowait(
                        SpeculativeResult(
                            item.id, item.cache_key, b"", 0.0, 0.0,
                            failure=type(exc).__name__,
                        )
                    )
                except Full:
                    # A full result queue is reaped by the parent job deadline.
                    pass
            finally:
                cancelled.discard(item.id)
                status.put(("idle", item.id))
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
        self._controls = None
        self._results = None
        self._status = None
        self._ready = False
        self._active_job_ids: set[UUID] = set()

    @property
    def ready(self) -> bool:
        self._drain_status()
        if self._ready and self._process is not None and not self._process.is_alive():
            self._ready = False
        return self._ready

    def start(self) -> bool:
        if self.ready:
            return True
        self.stop()
        context = get_context("spawn")
        self._jobs = context.Queue(maxsize=self._max_jobs)
        self._controls = context.Queue(maxsize=self._max_jobs)
        self._results = context.Queue(maxsize=self._max_jobs)
        self._status = context.Queue(maxsize=1)
        self._process = context.Process(
            target=self._entrypoint,
            args=(self._model_settings, self._jobs, self._controls, self._results, self._status),
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
        if self._controls is None or not self.ready:
            return
        if job_id in self._active_job_ids:
            self._restart_after_active_cancellation()
            return
        try:
            self._controls.put_nowait(_Cancel(job_id))
        except Full:
            # The scheduler has already detached this job; cancellation is best effort.
            return

    def drain_results(self) -> list[SpeculativeResult]:
        self._drain_status()
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

    def _drain_status(self) -> None:
        if self._status is None:
            return
        while True:
            try:
                state, job_id = self._status.get_nowait()
            except Empty:
                return
            if state == "active" and isinstance(job_id, UUID):
                self._active_job_ids.add(job_id)
            elif state == "idle" and isinstance(job_id, UUID):
                self._active_job_ids.discard(job_id)

    def _restart_after_active_cancellation(self) -> None:
        """A blocking backend cannot honor active cancellation, so replace only its child."""
        self.stop()
        self.start()

    def stop(self) -> None:
        process = self._process
        jobs = self._jobs
        controls = self._controls
        results = self._results
        status = self._status
        self._ready = False
        if process is not None:
            if process.is_alive() and controls is not None:
                try:
                    controls.put_nowait(_Stop())
                except Full:
                    pass
            process.join(self._shutdown_timeout)
            if process.is_alive():
                process.terminate()
                process.join(self._shutdown_timeout)
        for queue in (jobs, controls, results, status):
            if queue is None:
                continue
            try:
                queue.close()
                queue.join_thread()
            except Exception:
                pass
        self._active_job_ids.clear()
        self._process = self._jobs = self._controls = self._results = self._status = None
