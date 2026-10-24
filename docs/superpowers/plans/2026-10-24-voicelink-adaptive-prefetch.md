# VoiceLink Adaptive TTS Prefetch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a backward-compatible VoiceLink look-ahead API, conservative stream correlation, isolated Kokoro pre-generation, bounded exact-key PCM caching, and structure-aware short pauses.

**Architecture:** A `LookAheadService` owns protocol validation, revisioned snapshot state, five-second mutual-uniqueness correlation, bindings, and scheduling. Authoritative synthesis stays in the existing model lane; a separately loaded low-priority worker process performs speculative Kokoro inference and publishes exact-key PCM into a bounded cache. The existing `/v1/tts` route consults the service but remains the authoritative fallback for every miss or ambiguity.

**Tech Stack:** Python 3, FastAPI/Pydantic, pytest/httpx, multiprocessing `spawn`, Kokoro, C++17/WinHTTP/Windows SAPI, CMake/CTest.

**Spec:** `C:/Code/reader/docs/superpowers/specs/2026-10-24-adaptive-tts-prefetch-design.md`

## Global Constraints

- Keep the existing `/v1/tts` JSON body and legacy PCM response semantics compatible.
- Bind all HTTP endpoints to the existing loopback server; do not add remote exposure or book identifiers.
- Protocol 1 limits are 12 units, 12,000 Unicode scalar values total, 4,000 per unit, and 65,536 UTF-8 body bytes.
- Initial correlation observations live exactly five seconds and require a mutually unique eligibility edge.
- Correlation normalization is NFC + CRLF-to-LF + Unicode whitespace collapse only; cache reuse always hashes exact UTF-8 synthesis text and every synthesis-affecting option.
- Prefix silence is allowed only for safely correlated bridge-protocol-2 requests.
- Speculative inference must use separate capacity and must not occupy the authoritative model lane.
- The feature remains disabled by default until the speculative worker is ready and the experimental setting is enabled.
- The first implementation prefetches Kokoro `/v1/tts` only; Qwen routes retain unchanged on-demand behavior.

## Review Focus

- Two readers speaking the same normalized sentence within five seconds must remain unclaimed, even when arrival order differs.
- Exact text differing only by collapsed whitespace may correlate but must never share prefetched PCM unless the exact synthesis key also matches.
- A running speculative job, worker crash, or full cache must not delay or fail an authoritative cache miss.
- Old bridges, missing headers, config changes, retries, and concurrent request IDs must preserve legacy PCM/timing and must not double-advance bindings.
- Generation replacement, DELETE, TTL expiry, and a late worker result must remove stream associations while retaining only valid independently keyed canonical PCM.

---

### Task 1: Establish server tests and the normative protocol API

**Files:**
- Create: `server/requirements-dev.txt`
- Create: `tests/server/conftest.py`
- Create: `server/lookahead/__init__.py`
- Create: `server/lookahead/protocol.py`
- Create: `server/lookahead/store.py`
- Create: `server/routers/lookahead.py`
- Modify: `server/config.py`
- Modify: `server/main.py`
- Create: `tests/server/test_lookahead_protocol.py`

**Interfaces:**
- Produces: Pydantic `LookAheadSnapshot`, `LookAheadUnit`, `LookAheadCapabilities`, `ProtocolErrorBody`.
- Produces: `SnapshotStore.replace(stream_id: UUID, snapshot: LookAheadSnapshot, raw_body_bytes: int, now: float) -> ReplaceResult`.
- Produces: `SnapshotStore.cancel(stream_id: UUID) -> None`.
- Produces endpoints `GET /v1/capabilities`, `PUT /v1/tts/lookahead/{streamId}`, and `DELETE /v1/tts/lookahead/{streamId}`.
- Consumes later: Tasks 2 and 4 read accepted `SnapshotRecord` values from `SnapshotStore`.

- [ ] **Step 1: Add the pytest harness and failing protocol tests**

Pin `pytest>=8,<9`, `pytest-asyncio>=0.24,<1`, and `httpx>=0.27,<1` in `server/requirements-dev.txt`. Build a lightweight FastAPI test app around the look-ahead router without loading Kokoro. Test the exact capability JSON and `202 accepted`, `200 duplicate`, `409 stale`, `409 revision-conflict`, `413 over-limit`, `426 unsupported-protocol`, `429 budget-exhausted`, and idempotent `204 DELETE` responses.

- [ ] **Step 2: Run the protocol test and verify it fails**

Run: `python -m pytest tests/server/test_lookahead_protocol.py -q`

Expected: FAIL because the look-ahead package and router do not exist.

- [ ] **Step 3: Implement strict protocol models and scalar/body validation**

Define `PROTOCOL = 1`, `MAX_UNITS = 12`, `MAX_CHARACTERS = 12_000`, `MAX_UNIT_CHARACTERS = 4_000`, and `MAX_BODY_BYTES = 65_536`. Count Unicode scalars with Python `len(str)` and body bytes before JSON parsing. Require consecutive sequences beginning at `currentSequence`; reject `null` optionals and unknown enum strings.

- [ ] **Step 4: Implement atomic revision storage and endpoint responses**

Use a lock around replacement. Same generation/revision plus identical SHA-256 body digest is duplicate; same revision/different digest conflicts; lower generation/revision or regressing current sequence is stale. Every non-2xx result leaves prior state unchanged. `429` is returned before mutation when configured active-stream/global-character budgets are exhausted.

- [ ] **Step 5: Add settings and mount the router**

Add `LookAheadSettings` with defaults: `enabled=False`, `max_active_streams=8`, `stream_ttl_seconds=30`, `cache_max_bytes=268_435_456`, `cache_ttl_seconds=600`, `max_queued_jobs=48`, `max_runway_seconds=60`, and `correlation_ttl_seconds=5`. Capability `lookahead` is absent until both `enabled` and worker readiness are true; tests inject readiness.

- [ ] **Step 6: Run focused tests and commit**

Run: `python -m pytest tests/server/test_lookahead_protocol.py -q`

Expected: PASS with no model download or load.

```bash
git add server/requirements-dev.txt server/config.py server/main.py server/lookahead server/routers/lookahead.py tests/server
git commit -m "feat: add versioned TTS look-ahead protocol"
```

### Task 2: Implement exact-key PCM cache and generation-safe state

**Files:**
- Create: `server/lookahead/cache.py`
- Modify: `server/lookahead/store.py`
- Create: `tests/server/test_lookahead_cache.py`

**Interfaces:**
- Produces immutable `SynthesisConfig(route, model_version, voice, speed, format, language, options)`.
- Produces `synthesis_input_digest(text: str, config: SynthesisConfig) -> str`.
- Produces `PcmCache.get(key: str, now: float) -> bytes | None` and `PcmCache.put(key: str, pcm: bytes, now: float) -> bool`.
- Produces store invalidation callbacks that discard generation-scoped associations without evicting canonical cache entries.

- [ ] **Step 1: Write failing exactness, LRU, TTL, and invalidation tests**

Assert that raw strings `"a  b"` and `"a b"`, voice, speed, route, model version, format, language, and backend options create different keys. Fill beyond 256 MiB using an injected small test budget and assert oldest-byte-budget eviction. Assert TTL expiry and that a higher generation clears queued/stream results but leaves exact-key canonical PCM readable.

- [ ] **Step 2: Run the cache tests and verify they fail**

Run: `python -m pytest tests/server/test_lookahead_cache.py -q`

Expected: FAIL because cache/key types do not exist.

- [ ] **Step 3: Implement the thread-safe byte-bounded cache**

Use `OrderedDict[str, CacheEntry]`, a lock, total-byte accounting, monotonic timestamps, LRU promotion on get, and zero raw text in keys/logs. Oversized single values return `False` without evicting the whole cache.

- [ ] **Step 4: Make generation invalidation stream-scoped**

On higher generation, cancel pending job IDs and delete stream/result associations for older generations. Do not remove cache entries because they have no stream/generation key.

- [ ] **Step 5: Run tests and commit**

Run: `python -m pytest tests/server/test_lookahead_cache.py tests/server/test_lookahead_protocol.py -q`

Expected: PASS.

```bash
git add server/lookahead/cache.py server/lookahead/store.py tests/server/test_lookahead_cache.py
git commit -m "feat: add bounded exact-input PCM cache"
```

### Task 3: Implement conservative correlation and binding state

**Files:**
- Create: `server/lookahead/correlation.py`
- Create: `tests/server/test_lookahead_correlation.py`

**Interfaces:**
- Consumes: Task 1 `SnapshotRecord`; Task 2 `SynthesisConfig` and exact digest.
- Produces: `correlation_text_digest(text: str) -> str`.
- Produces: `AuthoritativeObservation(request_id, engine_instance, bridge_protocol, text, config, arrived_at)`.
- Produces: `CorrelationRegistry.observe_request(observation) -> CorrelationDecision`.
- Produces: `CorrelationRegistry.observe_snapshot(record) -> list[CorrelationDecision]`.
- Produces: `CorrelationRegistry.complete_request(request_id: UUID) -> None` and binding release/suspend methods.

- [ ] **Step 1: Write the failing mutual-uniqueness tests**

Cover snapshot-first and request-first claims, five-second boundary/expiry, absent versus exact/mismatched `synthesisHint`, ignored provider-neutral `source` hints, two snapshots/one request, one snapshot/two requests, unrelated digests, and arrival-order independence. Pin normalization to NFC, CRLF-to-LF, and whitespace collapse with no case or punctuation folding.

- [ ] **Step 2: Add failing binding state-machine tests**

Assert `unclaimed -> bound -> advance`, same request ID retry does not advance twice, unexpected exact key/config/sequence suspends, concurrent different request IDs on one engine suspends, higher generation uniquely rebinds, and DELETE/TTL releases. Old bridges never bind.

- [ ] **Step 3: Run the correlation tests and verify they fail**

Run: `python -m pytest tests/server/test_lookahead_correlation.py -q`

Expected: FAIL because the registry does not exist.

- [ ] **Step 4: Implement eligibility as a bipartite graph**

For each active snapshot/request pair, create an edge only when every Section 10 predicate passes. Claim only an edge whose snapshot degree and request degree are both one. Recompute affected degrees on either arrival so ordering cannot choose a winner.

- [ ] **Step 5: Implement bindings, retry IDs, suspension, and privacy-safe diagnostics**

Keep at most one bound stream and one different in-flight request per engine instance. Log only stream UUID, sequence, lengths, digests, timing, and reason codes; never raw unit/request text.

- [ ] **Step 6: Run tests and commit**

Run: `python -m pytest tests/server/test_lookahead_correlation.py tests/server/test_lookahead_cache.py -q`

Expected: PASS.

```bash
git add server/lookahead/correlation.py tests/server/test_lookahead_correlation.py
git commit -m "feat: correlate TTS streams conservatively"
```

### Task 4: Implement structural pause policy independently of speech PCM

**Files:**
- Create: `server/lookahead/prosody.py`
- Create: `tests/server/test_lookahead_prosody.py`

**Interfaces:**
- Produces `ProsodyPolicy.desired_gap_ms(boundary: BoundaryKind, stream_start: bool) -> int`.
- Produces `prefix_silence_pcm(boundary, request_wait_ms, audio_format, stream_start=False) -> bytes`.
- Default milliseconds: `none=0`, `sentence=80`, `paragraph=180`, `list-item=220`, `heading-to-body=320`, `section=400`, `document=450`; stream start always returns 0.

- [ ] **Step 1: Write failing policy and PCM tests**

Assert ordering and exact defaults, `max(0, desired-request_wait)`, no negative duration, 16-bit zero PCM aligned to full frames, zero leading document pause on stream start, and unchanged canonical speech bytes.

- [ ] **Step 2: Run the prosody tests and verify they fail**

Run: `python -m pytest tests/server/test_lookahead_prosody.py -q`

Expected: FAIL because the policy module does not exist.

- [ ] **Step 3: Implement the policy and silence generator**

Calculate frames using `round(milliseconds * sample_rate / 1000)`, then multiply by channels and bytes per sample. Keep boundary silence outside `PcmCache`.

- [ ] **Step 4: Run tests and commit**

Run: `python -m pytest tests/server/test_lookahead_prosody.py -q`

Expected: PASS.

```bash
git add server/lookahead/prosody.py tests/server/test_lookahead_prosody.py
git commit -m "feat: add structure-aware TTS pause policy"
```

### Task 5: Add the isolated speculative worker and adaptive scheduler

**Files:**
- Create: `server/lookahead/worker.py`
- Create: `server/lookahead/scheduler.py`
- Modify: `server/main.py`
- Create: `tests/server/test_lookahead_scheduler.py`

**Interfaces:**
- Consumes: Task 2 cache/key and Task 3 claimed stream/config.
- Produces `SpeculativeWorker.start() -> bool`, `submit(job: SynthesisJob) -> bool`, `cancel(job_id: UUID) -> None`, `stop() -> None`, and `ready: bool`.
- Produces `RunwayEstimator.observe(synthesis_seconds: float, audio_seconds: float) -> None` and `select(units, cached_seconds) -> list[LookAheadUnit]`.
- Produces `LookAheadScheduler.schedule_claim(claim: BoundClaim) -> None` and generation/cancel hooks.

- [ ] **Step 1: Write failing scheduler tests with a fake worker**

Assert in-order future selection, no current-unit speculative job, exact-key deduplication, 48-job/global and 60-second/per-stream caps, EWMA response to RTF, stale-generation cancellation, late-result cache insertion only under its exact key, and disabled capability when worker readiness is false.

- [ ] **Step 2: Add the authoritative-isolation failure test**

Block the fake speculative worker indefinitely, submit an authoritative fake synthesis through the reserved lane, and assert it starts without waiting. This test owns the highest-risk fallback invariant.

- [ ] **Step 3: Run scheduler tests and verify they fail**

Run: `python -m pytest tests/server/test_lookahead_scheduler.py -q`

Expected: FAIL because worker/scheduler types do not exist.

- [ ] **Step 4: Implement EWMA runway and queue policy**

Use EWMA alpha `0.25`, minimum target runway `3s`, safety margin `1s`, maximum `60s`, and hard job/byte budgets. Walk future units in sequence order and never schedule around a missing earlier unit.

- [ ] **Step 5: Implement the production worker process**

Use `multiprocessing.get_context("spawn")`; load a separate Kokoro model inside the child from serializable model settings; set Windows process priority below normal when available; send jobs/results over bounded queues. On load failure, crash, timeout, or budget denial, mark not ready, drain associations, and remove look-ahead from capabilities while leaving the authoritative model untouched.

- [ ] **Step 6: Start and stop the worker from FastAPI lifespan**

Start only when `settings.lookahead.enabled` is true. Shutdown must cancel queues, join with a finite timeout, then terminate only the child process if necessary.

- [ ] **Step 7: Run tests and commit**

Run: `python -m pytest tests/server/test_lookahead_scheduler.py tests/server/test_lookahead_protocol.py -q`

Expected: PASS and no real Kokoro model load in tests.

```bash
git add server/lookahead/worker.py server/lookahead/scheduler.py server/main.py tests/server/test_lookahead_scheduler.py
git commit -m "feat: schedule isolated speculative TTS work"
```

### Task 6: Integrate look-ahead with authoritative `/v1/tts`

**Files:**
- Create: `server/lookahead/service.py`
- Modify: `server/routers/lookahead.py`
- Modify: `server/routers/tts.py`
- Modify: `server/models/kokoro_model.py`
- Create: `tests/server/test_tts_lookahead_integration.py`

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces `LookAheadService.begin_authoritative(headers, request, now) -> AuthoritativeContext`.
- Produces `LookAheadService.finish_authoritative(context, speech_pcm, elapsed, now) -> AudioEnvelope` where the envelope contains prefix bytes, speech bytes, and response headers.
- Existing `POST /v1/tts` remains the external authoritative interface.

- [ ] **Step 1: Write failing integration tests with a deterministic fake model**

Test legacy request byte-for-byte output, protocol-2 unique claim, snapshot-first/request-first future cache hit, exact-key miss after whitespace-only raw-text difference, ambiguous fallback, retry idempotence, config mismatch suspension, correlated miss residual pause, old bridge no prefix, and cache eviction fallback.

- [ ] **Step 2: Write the privacy regression test**

Use a unique secret sentence, capture all Loguru output during protocol, worker, cache, and TTS requests, and assert the sentence is absent. This must force removal of the current Kokoro debug log containing `text[:80]`.

- [ ] **Step 3: Run integration tests and verify they fail**

Run: `python -m pytest tests/server/test_tts_lookahead_integration.py -q`

Expected: FAIL because the service is not integrated.

- [ ] **Step 4: Compose store, correlation, scheduler, cache, and prosody in `LookAheadService`**

Register the authoritative observation before synthesis begins so a late snapshot can claim it. Freeze actual `/v1/tts` route/body config on claim. Use cached speech only for exact synthesis keys. On every exception or unsafe decision, call the existing synthesis path.

- [ ] **Step 5: Return prefix/speech/total byte metadata only to protocol 2**

For safely correlated protocol-2 requests, return `X-Audio-Prefix-Bytes`, `X-Speech-Audio-Length`, and existing `X-Audio-Length` as integer PCM byte counts. For old/uncorrelated clients, return the same body and existing headers as before with no prefix.

- [ ] **Step 6: Run integration and full Python tests**

Run: `python -m pytest tests/server -q`

Expected: PASS; no real model required.

- [ ] **Step 7: Commit**

```bash
git add server/lookahead/service.py server/routers/lookahead.py server/routers/tts.py server/models/kokoro_model.py tests/server/test_tts_lookahead_integration.py
git commit -m "feat: serve prefetched audio with structural pauses"
```

### Task 7: Add bridge-protocol-2 IDs and timing metadata

**Files:**
- Create: `sapi_bridge/src/protocol.h`
- Create: `sapi_bridge/src/protocol.cpp`
- Modify: `sapi_bridge/src/http_client.h`
- Modify: `sapi_bridge/src/http_client.cpp`
- Modify: `sapi_bridge/src/tts_engine.h`
- Modify: `sapi_bridge/src/tts_engine.cpp`
- Modify: `sapi_bridge/CMakeLists.txt`
- Create: `sapi_bridge/tests/protocol_test.cpp`

**Interfaces:**
- Produces `RequestMetadata { std::string bridgeProtocol, engineInstanceId, requestId; }`.
- Produces `AudioResponseMetadata { uint64_t totalBytes, prefixBytes, speechBytes; }`.
- Changes `TtsHttpClient::StreamSynthesize(...)` to consume `const RequestMetadata&` and populate `AudioResponseMetadata*`; all retry attempts receive the same metadata.
- `VoiceLinkEngine` owns one UUID for its COM-object lifetime and creates one request UUID per `Speak()` call.

- [ ] **Step 1: Write failing pure C++ protocol tests**

Test UUID format/uniqueness, exact three request headers, safe unsigned parsing of all audio byte headers, missing/malformed header fallback, `prefix + speech == total` validation, and reuse of one request ID across a simulated retry loop.

- [ ] **Step 2: Configure and run CTest to verify the red state**

Run: `cmake -S sapi_bridge -B sapi_bridge/build-test -G "Visual Studio 17 2022" -A x64 -DBUILD_TESTING=ON && cmake --build sapi_bridge/build-test --config Debug && ctest --test-dir sapi_bridge/build-test -C Debug --output-on-failure`

Expected: build/test FAIL because protocol sources are missing.

- [ ] **Step 3: Implement protocol metadata and WinHTTP headers**

Generate UUIDs with `CoCreateGuid`; do not add a third-party library. Add `X-VoiceLink-Bridge-Protocol: 2`, engine-instance, and request-ID headers in `StreamSynthesizeOnce()`. Preserve the request ID across its existing retry loop.

- [ ] **Step 4: Parse response timing and offset SAPI events**

Use `speechBytes` instead of total bytes for proportional word timing and add `prefixBytes` to each `ullAudioStreamOffset`. Continue counting/writing all PCM bytes. If timing headers are absent or inconsistent, set prefix to zero and use existing `X-Audio-Length` behavior.

- [ ] **Step 5: Run bridge tests and both architecture builds**

Run x64 tests as in Step 2, then:

`cmake -S sapi_bridge -B sapi_bridge/build32-test -G "Visual Studio 17 2022" -A Win32 -DBUILD_TESTING=ON && cmake --build sapi_bridge/build32-test --config Release`

Expected: CTest PASS; x64 and Win32 DLL builds exit 0 with `/W4 /WX`.

- [ ] **Step 6: Commit**

```bash
git add sapi_bridge/CMakeLists.txt sapi_bridge/src/protocol.h sapi_bridge/src/protocol.cpp sapi_bridge/src/http_client.h sapi_bridge/src/http_client.cpp sapi_bridge/src/tts_engine.h sapi_bridge/src/tts_engine.cpp sapi_bridge/tests/protocol_test.cpp
git commit -m "feat: add look-ahead correlation metadata to SAPI bridge"
```

### Task 8: Add end-to-end fake-model coverage, metrics, and operator documentation

**Files:**
- Create: `tests/server/test_lookahead_e2e.py`
- Modify: `README.md`
- Modify: `server/config.py`
- Modify: `server/lookahead/service.py`

**Interfaces:**
- Consumes: all VoiceLink tasks.
- Produces documented experimental configuration and privacy-safe counters/timings.

- [ ] **Step 1: Write the failing end-to-end scenario**

Drive capabilities → snapshot → current request → worker completion → next request using a controllable fake worker/model. Assert ordered speech, second-request exact cache hit, structure prefix, seek generation cancellation, voice/rate config suspension, ambiguous dual-reader fallback, pause without runway growth, and worker restart fallback.

- [ ] **Step 2: Add the latency-isolation benchmark test**

Run the same fake authoritative workload with look-ahead off and with a blocked speculative worker. Assert look-ahead p95 is no more than 5% slower. Mark only the real-model benchmark as manual; keep this deterministic gate in CI.

- [ ] **Step 3: Run the end-to-end tests and verify any missing behavior**

Run: `python -m pytest tests/server/test_lookahead_e2e.py -q`

Expected: FAIL until lifecycle metrics/recovery hooks are complete.

- [ ] **Step 4: Add bounded metrics and documentation**

Expose counters through structured logs or the existing health response for accepted/rejected snapshots, correlation reasons, cache hits/misses, RTF/runway, canceled jobs, prefix bytes, and current cache/queue sizes. Document `VOICELINK_LOOKAHEAD__ENABLED=true`, two-model resource cost, limits, fallback, and that logs contain digests/lengths rather than text.

- [ ] **Step 5: Run final VoiceLink verification**

Run:

```powershell
python -m pytest tests/server -q
cmake --build sapi_bridge/build-test --config Release
ctest --test-dir sapi_bridge/build-test -C Release --output-on-failure
git diff --check
```

Expected: all Python and C++ tests PASS, Release bridge builds, and diff check is clean.

- [ ] **Step 6: Commit**

```bash
git add README.md server/config.py server/lookahead/service.py tests/server/test_lookahead_e2e.py
git commit -m "test: cover adaptive TTS prefetch end to end"
```

## VoiceLink PR Acceptance

- With look-ahead disabled, old bridge requests are byte/timing compatible with the baseline.
- With protocol 2 and a unique claim, future Kokoro synthesis overlaps current playback and exact next requests hit cache.
- Ambiguity, stale state, worker failure, resource denial, and mismatches always fall back.
- The authoritative latency-isolation gate passes and speculative execution uses a distinct model process.
- Prefix timing shifts bridge boundary events by exactly the returned prefix byte count.
- Logs and metrics contain no raw book text.
