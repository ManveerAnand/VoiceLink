"""Isolated, best-effort Kokoro inference for look-ahead PCM."""

from __future__ import annotations

from dataclasses import dataclass, replace
from multiprocessing import get_context
from multiprocessing.queues import Queue
from queue import Empty, Full
from threading import Event, RLock, Thread, current_thread
from time import monotonic
from typing import Any, Callable, Protocol
from uuid import UUID


_MAX_EPOCH = (1 << 63) - 1


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
    epoch: int = 0


@dataclass(frozen=True, slots=True)
class SpeculativeResult:
    job_id: UUID
    cache_key: str
    pcm: bytes
    synthesis_seconds: float
    audio_seconds: float
    failure: str | None = None
    epoch: int = 0


@dataclass(frozen=True, slots=True)
class _Cancel:
    job_id: UUID


class _Stop:
    pass


class _ProcessLike(Protocol):
    def start(self) -> None: ...

    def is_alive(self) -> bool: ...

    def join(self, timeout: float | None = None) -> None: ...

    def terminate(self) -> None: ...


class _QueueLike(Protocol):
    def get(self, block: bool = True, timeout: float | None = None) -> Any: ...

    def get_nowait(self) -> Any: ...

    def put_nowait(self, obj: Any) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _ChildState:
    process: _ProcessLike
    jobs: _QueueLike
    controls: _QueueLike
    permissions: _QueueLike
    results: _QueueLike
    status: _QueueLike


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
    permissions: Queue,
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

    def wait_for_permission(job_id: UUID) -> bool:
        while True:
            if drain_controls() or job_id in cancelled:
                return False
            try:
                permission = permissions.get(timeout=0.1)
            except Empty:
                continue
            if permission == job_id:
                return True

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
            try:
                if not wait_for_permission(item.id):
                    continue
                started = monotonic()
                pcm = b"".join(model.synthesize(item.text, voice=item.voice, speed=item.speed))
                elapsed = monotonic() - started
                if item.id not in cancelled:
                    results.put_nowait(
                        SpeculativeResult(
                            item.id,
                            item.cache_key,
                            pcm,
                            elapsed,
                            len(pcm) / 48_000,
                            epoch=item.epoch,
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
                            epoch=item.epoch,
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
        self._permissions = None
        self._results = None
        self._status = None
        self._ready = False
        self._active_job_ids: set[UUID] = set()
        self._epoch = 0
        self._restart_pending = False
        self._manager_wake = Event()
        self._manager_stop = Event()
        self._manager_thread: Thread | None = None
        self._lock = RLock()

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    @property
    def ready(self) -> bool:
        with self._lock:
            self._drain_status_locked()
            self._refresh_ready_locked()
            return self._ready

    def start(self) -> bool:
        if self.ready:
            return True
        self.stop()
        with self._lock:
            self._epoch = self._next_epoch(self._epoch)
        state = self._launch_child()
        if state is None:
            return False
        with self._lock:
            self._install_child_locked(state)
            self._manager_stop.clear()
            self._manager_wake.clear()
            manager = Thread(
                target=self._manage_restarts,
                name=f"voicelink-lookahead-worker-manager-{id(self):x}",
                daemon=True,
            )
            self._manager_thread = manager
        manager.start()
        return True

    def _launch_child(self, abort: Event | None = None) -> _ChildState | None:
        context = get_context("spawn")
        jobs = context.Queue(maxsize=self._max_jobs)
        controls = context.Queue(maxsize=self._max_jobs)
        permissions = context.Queue(maxsize=1)
        results = context.Queue(maxsize=self._max_jobs)
        status = context.Queue(maxsize=2)
        process = context.Process(
            target=self._entrypoint,
            args=(
                self._model_settings,
                jobs,
                controls,
                permissions,
                results,
                status,
            ),
            daemon=True,
        )
        child = _ChildState(process, jobs, controls, permissions, results, status)
        process.start()
        deadline = monotonic() + self._startup_timeout
        while True:
            if abort is not None and abort.is_set():
                self._teardown_child(child)
                return None
            remaining = max(0.0, deadline - monotonic())
            try:
                state, _reason = status.get(timeout=min(0.05, remaining))
            except Empty:
                if monotonic() >= deadline:
                    self._teardown_child(child)
                    return None
                continue
            if state == "ready" and process.is_alive():
                return child
            self._teardown_child(child)
            return None

    def submit(self, job: SynthesisJob) -> bool:
        with self._lock:
            self._drain_status_locked()
            self._refresh_ready_locked()
            if not self._ready or self._jobs is None:
                return False
            if job.epoch not in (0, self._epoch):
                return False
            queued_job = job if job.epoch == self._epoch else replace(job, epoch=self._epoch)
            try:
                self._jobs.put_nowait(queued_job)
                return True
            except Full:
                return False

    def cancel(self, job_id: UUID) -> None:
        with self._lock:
            if self._controls is None:
                return
            self._drain_status_locked()
            self._refresh_ready_locked()
            if not self._ready:
                return
            if job_id in self._active_job_ids:
                self._request_restart_locked()
                return
            try:
                self._controls.put_nowait(_Cancel(job_id))
            except Full:
                # The scheduler has already detached this job; cancellation is best effort.
                return

    def drain_results(self) -> list[SpeculativeResult]:
        with self._lock:
            self._drain_status_locked()
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

    def _drain_status_locked(self) -> int:
        if self._status is None:
            return 0
        drained = 0
        while True:
            try:
                state, job_id = self._status.get_nowait()
            except Empty:
                return drained
            drained += 1
            if state == "active" and isinstance(job_id, UUID):
                self._active_job_ids.add(job_id)
                if self._permissions is not None:
                    try:
                        self._permissions.put_nowait(job_id)
                    except Full:
                        self._request_restart_locked()
                        return drained
            elif state == "idle" and isinstance(job_id, UUID):
                self._active_job_ids.discard(job_id)

    def _refresh_ready_locked(self) -> None:
        if self._ready and self._process is not None and not self._process.is_alive():
            self._ready = False

    def _request_restart_locked(self) -> None:
        """Publish one bounded restart request without waiting on process management."""
        manager = self._manager_thread
        if self._restart_pending or manager is None or not manager.is_alive():
            return
        self._restart_pending = True
        self._ready = False
        self._epoch = self._next_epoch(self._epoch)
        self._manager_wake.set()

    def _manage_restarts(self) -> None:
        try:
            while True:
                self._manager_wake.wait()
                self._manager_wake.clear()
                if self._manager_stop.is_set():
                    return
                with self._lock:
                    if not self._restart_pending:
                        continue
                    old_child = self._detach_child_locked()
                self._teardown_child(old_child)
                if self._manager_stop.is_set():
                    return
                replacement = self._launch_child(self._manager_stop)
                if replacement is None:
                    with self._lock:
                        self._restart_pending = False
                    return
                with self._lock:
                    if self._manager_stop.is_set():
                        discard = replacement
                    else:
                        self._install_child_locked(replacement)
                        self._restart_pending = False
                        discard = None
                self._teardown_child(discard)
        finally:
            with self._lock:
                if self._manager_thread is current_thread():
                    self._manager_thread = None

    def stop(self) -> None:
        self._manager_stop.set()
        self._manager_wake.set()
        with self._lock:
            manager = self._manager_thread
        if manager is not None and manager is not current_thread():
            manager.join(max(0.1, 2 * self._shutdown_timeout + 0.5))
        with self._lock:
            child = self._detach_child_locked()
            self._restart_pending = False
            if self._manager_thread is not None and not self._manager_thread.is_alive():
                self._manager_thread = None
        self._teardown_child(child)

    def _install_child_locked(self, child: _ChildState) -> None:
        self._process = child.process
        self._jobs = child.jobs
        self._controls = child.controls
        self._permissions = child.permissions
        self._results = child.results
        self._status = child.status
        self._active_job_ids.clear()
        self._ready = True

    def _detach_child_locked(self) -> _ChildState | None:
        if self._process is None:
            self._ready = False
            self._active_job_ids.clear()
            return None
        assert self._jobs is not None
        assert self._controls is not None
        assert self._permissions is not None
        assert self._results is not None
        assert self._status is not None
        child = _ChildState(
            self._process,
            self._jobs,
            self._controls,
            self._permissions,
            self._results,
            self._status,
        )
        self._process = None
        self._jobs = None
        self._controls = None
        self._permissions = None
        self._results = None
        self._status = None
        self._ready = False
        self._active_job_ids.clear()
        return child

    def _teardown_child(self, child: _ChildState | None) -> None:
        if child is None:
            return
        process = child.process
        controls = child.controls
        if process.is_alive():
            try:
                controls.put_nowait(_Stop())
            except Full:
                pass
        process.join(self._shutdown_timeout)
        if process.is_alive():
            process.terminate()
            process.join(self._shutdown_timeout)
        for queue in (
            child.jobs,
            child.controls,
            child.permissions,
            child.results,
            child.status,
        ):
            if queue is None:
                continue
            try:
                cancel_join = getattr(queue, "cancel_join_thread", None)
                if callable(cancel_join):
                    cancel_join()
                queue.close()
            except Exception:
                pass

    @staticmethod
    def _next_epoch(epoch: int) -> int:
        return 1 if epoch >= _MAX_EPOCH else epoch + 1
