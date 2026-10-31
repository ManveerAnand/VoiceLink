# VoiceLink Final-Review Follow-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the three residual final-review findings so the adaptive look-ahead branch is bounded, cancellation-safe, privacy-regression-safe, and ready for a new whole-branch review.

**Architecture:** Preserve the existing isolated speculative worker and exact-fingerprint authoritative single-flight. Make worker activity transitions lossless before blocking synthesis, make the producer task own single-flight cleanup independently of waiter lifetime, and configure the privacy regression to observe ordinary standard-library INFO logs while restoring global logger state.

**Tech Stack:** Python 3.11+, asyncio, multiprocessing, FastAPI/HTTPX, pytest, Loguru, standard-library logging.

**Spec:** `docs/superpowers/specs/2026-10-24-adaptive-tts-prefetch-design.md`

## Global Constraints

- Look-ahead remains disabled by default and legacy `/v1/tts` behavior remains the fallback.
- Authoritative TTS must never wait for speculative inference.
- All queues and lifecycle state remain bounded; stale speculative work must not block newer work.
- No raw book text may be emitted through Loguru or standard-library logging.
- Cache reuse remains exact-input only; structural silence is never stored in canonical PCM.
- `streamId` remains URL-scoped and bridge request IDs remain stable only across one outbound request's retries.
- No dependency additions and no C++ protocol changes are required by this follow-up.

## Review Focus

- Status backpressure during the active transition must delay synthesis rather than lose the transition; Task 1 tests a saturated status channel.
- Cancellation immediately after synthesis begins must replace the non-interruptible child exactly once; Task 1 tests the real process-ordering contract.
- Cancelling every single-flight waiter must not cancel the producer or retain it after completion; Task 2 tests both conditions.
- A later non-overlapping request with the same ID and fingerprint must not reuse a completed single-flight task; Task 2 asserts a fresh authoritative operation after cleanup.
- Standard-library INFO output must be visible to the secret-bearing privacy assertion and logger state must be restored; Task 3 tests capture and restoration.

---

### Task 1: Make speculative active-state cancellation lossless

**Files:**
- Modify: `server/lookahead/worker.py`
- Test: `tests/server/test_lookahead_scheduler.py`

**Interfaces:**
- Consumes: `SpeculativeWorker.start()`, `submit(SynthesisJob)`, `cancel(UUID)`, `_worker_main(...)`, and the existing separate control/status queues.
- Produces: a bounded, lossless `("active", job_id)` / `("idle", job_id)` handshake that `cancel()` can observe before deciding whether to restart the child.

- [ ] **Step 1: Write the failing real-ordering regression**

Add a test that fills or backpressures the bounded status channel, starts a blocking real worker entrypoint, submits a job, and cancels after synthesis has begun. Assert the active transition is not dropped, the stale child is terminated/replaced, and the cancelled result is never returned.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m pytest tests/server/test_lookahead_scheduler.py -q -k "lossless_active or saturated_status"`

Expected: FAIL because `put_nowait(("active", ...))` drops the transition when the capacity-one status queue is full.

- [ ] **Step 3: Implement a bounded lossless status handshake**

Change `_worker_main(...)` and `SpeculativeWorker.start()/cancel()` so the child publishes active state losslessly before entering blocking synthesis and publishes idle state on exit. Backpressure may delay speculative work but must not block the authoritative lane. Keep control and job queues separate and bounded; do not replace this with unbounded shared state.

- [ ] **Step 4: Run worker tests and verify GREEN**

Run: `python -m pytest tests/server/test_lookahead_scheduler.py -q`

Expected: all scheduler/worker tests pass, including the saturated-status regression.

- [ ] **Step 5: Commit**

```bash
git add server/lookahead/worker.py tests/server/test_lookahead_scheduler.py
git commit -m "fix: make speculative cancellation state lossless"
```

### Task 2: Give single-flight producers task-owned cleanup

**Files:**
- Modify: `server/lookahead/service.py`
- Test: `tests/server/test_tts_lookahead_integration.py`

**Interfaces:**
- Consumes: `LookAheadService._single_flight`, `_single_flight_lock`, and the exact `(request_id, synthesis_fingerprint)` key.
- Produces: a producer coroutine whose `finally` removes only its own task from `_single_flight`, regardless of how many shielded waiters remain.

- [ ] **Step 1: Write the cancelled-waiters regression**

Add an async integration test that starts two identical overlapping requests, waits until the authoritative model is active, cancels both HTTP/request tasks, releases the model, and waits conditionally for producer completion. Assert the map becomes empty and a later non-overlapping request with the same ID/fingerprint starts a fresh authoritative operation rather than reusing the completed task.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m pytest tests/server/test_tts_lookahead_integration.py -q -k "single_flight and cancelled"`

Expected: FAIL because waiter-owned `finally` cleanup observes `task.done() == false` and no later callback removes the producer.

- [ ] **Step 3: Implement producer-owned cleanup**

Wrap authoritative production in a coroutine that removes the exact key only when `_single_flight[key] is asyncio.current_task()`, under `_single_flight_lock`, in `finally`. Waiters continue to use `asyncio.shield()`; mismatched fingerprints remain isolated and `_MAX_SINGLE_FLIGHT` remains binding.

- [ ] **Step 4: Run integration tests and verify GREEN**

Run: `python -m pytest tests/server/test_tts_lookahead_integration.py -q`

Expected: all TTS look-ahead integration tests pass, including overlap, mismatch, cancellation cleanup, and later reuse.

- [ ] **Step 5: Commit**

```bash
git add server/lookahead/service.py tests/server/test_tts_lookahead_integration.py
git commit -m "fix: clean up cancelled single-flight producers"
```

### Task 3: Make the cross-logger privacy regression observe INFO logs

**Files:**
- Modify: `tests/server/test_tts_lookahead_integration.py`

**Interfaces:**
- Consumes: `test_protocol_worker_cache_and_tts_logs_never_contain_raw_secret` and the root standard-library logger.
- Produces: a test-local capture configuration that observes INFO-and-above records and restores the original root level and handler state in `finally`.

- [ ] **Step 1: Add a capture-sensitivity regression**

Refactor the test-local standard logging capture into a context/helper in the test module. Add a focused test proving an INFO probe reaches the capture while the root logger's original level and handlers are restored after exit.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m pytest tests/server/test_tts_lookahead_integration.py -q -k "stdlib_info_capture"`

Expected: FAIL with the current handler-only setup because the default root WARNING threshold filters INFO.

- [ ] **Step 3: Configure and restore capture state**

Set the capture handler and root logger to observe INFO during the secret-bearing flow, then restore the exact prior root level and remove only the test handler in `finally`. Keep the existing combined Loguru + stdlib assertion.

- [ ] **Step 4: Run privacy and full verification gates**

Run:

```text
python -m pytest tests/server/test_tts_lookahead_integration.py -q
python -m pytest tests/server -q
git diff --check
git status --short
```

Expected: all tests pass, diff check is clean, and status contains only the intended follow-up changes before commit.

- [ ] **Step 5: Commit**

```bash
git add tests/server/test_tts_lookahead_integration.py
git commit -m "test: capture standard info logs in privacy regression"
```

## Final Verification

- [ ] Run `python -m pytest tests/server -q` and record the exact pass count.
- [ ] Rebuild and run CTest for x64 Debug, x64 Release, and Win32 Release to prove the unchanged bridge still integrates cleanly.
- [ ] Run `git diff --check` and confirm a clean working tree.
- [ ] Request a fresh whole-branch review against the original implementation base after all three task reviews are clean.
