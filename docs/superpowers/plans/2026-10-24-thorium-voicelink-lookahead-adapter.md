# Thorium VoiceLink Look-Ahead Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Thorium opt into R2 Navigator's provider-neutral TTS look-ahead events and forward safe, bounded snapshots to a compatible loopback VoiceLink server without affecting ordinary reading.

**Architecture:** A renderer adapter registers R2's optional listener and forwards its discriminated events over a dedicated Electron IPC channel. Main-process pure helpers convert R2 payloads to normative protocol-1 JSON, enforce negotiated bounds, and coalesce revisions. A loopback-only client performs capability probing, PUT/DELETE delivery, timeout/backoff, and silent fallback.

**Tech Stack:** TypeScript, Electron renderer/main IPC, Node `http`, Jest, existing Thorium webpack/lint/test toolchain.

**Spec:** `C:/Code/reader/docs/superpowers/specs/2026-10-24-adaptive-tts-prefetch-design.md`

## Global Constraints

- Depend on the accepted R2 Navigator look-ahead API; do not maintain a Thorium-only fork of its behavior.
- Never call VoiceLink from the publication webview or renderer HTTP stack; network work stays in Electron main.
- Allow only `127.0.0.1` on a validated port, default 7860; do not follow redirects or use proxy/cookie/auth infrastructure.
- Look-ahead transport is best effort and must never block, delay, cancel, or modify `speechSynthesis.speak()`.
- Remove `streamId` from the PUT body and use it only as the canonical lowercase UUID path segment.
- Do not send `synthesisHint` in the first implementation because Thorium cannot yet prove exact SAPI token/rate equivalence.
- Tail-truncate to negotiated limits; if the current unit alone exceeds any limit, send nothing for that revision.
- After `429`, retain no rejected state client-side, back off 1, 2, 4, 8, 16, then 30 seconds, and send only a newer current revision.
- Experimental enablement is `THORIUM_VOICELINK_LOOKAHEAD=1`; disabled is the default.

## Review Focus

- A hostile or malformed renderer IPC payload must be rejected in main without network access or process failure.
- Capability timeout, malformed JSON, redirect, connection refusal, server restart, and protocol mismatch must leave TTS unchanged.
- Rapid revisions from seek/next must coalesce by `(streamId, generation, revision)` without allowing an older PUT to arrive last.
- Cancel must win over queued PUT work, be idempotent, and prevent a canceled stream's delayed request from being sent afterward.
- Supplementary Unicode, percent-encoding, UUID validation, and UTF-8 byte limits must match VoiceLink's wire rules exactly.

---

### Task 1: Sync the accepted R2 Navigator API into Thorium

**Files:**
- Modify: `src/r2-xxx-js/r2-navigator-js.md`
- Sync from R2: `src/r2-xxx-js/r2-navigator-js/electron/common/events.ts`
- Sync from R2: `src/r2-xxx-js/r2-navigator-js/electron/common/tts-lookahead.ts`
- Sync from R2: `src/r2-xxx-js/r2-navigator-js/electron/renderer/readaloud.ts`
- Sync from R2: `src/r2-xxx-js/r2-navigator-js/electron/renderer/common/tts-lookahead.ts`
- Sync from R2: `src/r2-xxx-js/r2-navigator-js/electron/renderer/common/tts-lookahead-session.ts`
- Sync from R2: `src/r2-xxx-js/r2-navigator-js/electron/renderer/dom.ts`
- Sync from R2: `src/r2-xxx-js/r2-navigator-js/electron/renderer/webview/preload.ts`
- Sync from R2: `src/r2-xxx-js/r2-navigator-js/electron/renderer/webview/readaloud.ts`
- Sync from R2: `src/r2-xxx-js/r2-navigator-js/electron/renderer/webview/state.ts`

**Interfaces:**
- Consumes: accepted R2 PR commit implementing `ttsLookAheadListen()` and common payload types.
- Produces: the same API under Thorium's `@r2-navigator-js/electron/renderer` path alias.

- [ ] **Step 1: Record the exact accepted R2 commit**

Checkout the accepted R2 commit in a sibling worktree, verify its full SHA, and replace `r2-navigator-js.md` with `https://github.com/edrlab/r2-navigator-js/tree/<full-sha>`.

- [ ] **Step 2: Sync, do not hand-edit, the changed R2 source files**

Copy the files from that exact SHA into Thorium's vendored source tree. Compare SHA-256 hashes between source and vendored copies; every copied file must match.

- [ ] **Step 3: Run Thorium type/lint checks**

Run: `npm run lint:ts && npm run build:dev:main`

Expected: both exit 0 before adding the adapter.

- [ ] **Step 4: Commit**

```bash
git add src/r2-xxx-js/r2-navigator-js.md src/r2-xxx-js/r2-navigator-js
git commit -m "chore: sync R2 TTS look-ahead API"
```

### Task 2: Convert and bound R2 events as protocol-1 wire messages

**Files:**
- Create: `src/common/ipc/ttsLookAhead.ts`
- Modify: `src/common/ipc/index.ts`
- Create: `src/main/ttsLookAhead/wire.ts`
- Create: `test/main/ttsLookAhead/wire.test.ts`

**Interfaces:**
- Consumes: R2 `ITtsLookAheadEvent` from Task 1.
- Produces IPC `CHANNEL = "TTS_LOOKAHEAD"` and payload union `{ type: "snapshot" | "cancel"; payload: unknown }`.
- Produces `toWireSnapshot(event, limits: LookAheadLimits): WireSnapshotRequest | undefined`.
- Produces `validateCancel(event): { streamId: string } | undefined`.

- [ ] **Step 1: Write failing conversion and validation tests**

Assert exact protocol JSON names, removal of body `streamId`, omission of optional `undefined` fields and `synthesisHint`, canonical lowercase UUID, tail truncation, scalar counting with emoji, 65,536-byte UTF-8 body limit, current-unit oversize drop, consecutive sequences, and rejection of malformed/unknown enums.

- [ ] **Step 2: Run the focused Jest test and verify it fails**

Run: `npm run testFile -- test/main/ttsLookAhead/wire.test.ts`

Expected: FAIL because the IPC/wire modules do not exist.

- [ ] **Step 3: Define IPC and normative wire types**

Keep IPC input typed as `unknown` at the main boundary, validate every nested field, and create a fresh plain object rather than forwarding the renderer object by reference. Compute the serialized UTF-8 byte count before returning.

- [ ] **Step 4: Implement tail-only truncation and current-unit rejection**

Apply `maxUnits`, `maxCharacters`, `maxUnitCharacters`, and `maxBodyBytes` from capabilities, each capped by protocol maxima. Recompute final JSON size after every tail removal. Never split/rewrite unit text.

- [ ] **Step 5: Run tests, lint, and commit**

Run: `npm run testFile -- test/main/ttsLookAhead/wire.test.ts && npm run lint:ts`

Expected: test PASS and lint exits 0.

```bash
git add src/common/ipc/index.ts src/common/ipc/ttsLookAhead.ts src/main/ttsLookAhead/wire.ts test/main/ttsLookAhead/wire.test.ts
git commit -m "feat: validate VoiceLink look-ahead messages"
```

### Task 3: Implement the loopback capability and delivery client

**Files:**
- Create: `src/main/ttsLookAhead/client.ts`
- Create: `test/main/ttsLookAhead/client.test.ts`

**Interfaces:**
- Consumes: Task 2 wire snapshots/cancel and `LookAheadLimits`.
- Produces `VoiceLinkLookAheadClient.probe(now?: number): Promise<CapabilitiesState>`.
- Produces `VoiceLinkLookAheadClient.replace(streamId: string, body: WireSnapshotRequest): Promise<DeliveryResult>`.
- Produces `VoiceLinkLookAheadClient.cancel(streamId: string): Promise<void>`.
- Constructor accepts `{ port=7860, timeoutMs=500, now=Date.now, request=loopbackRequest }` for deterministic tests.

- [ ] **Step 1: Write failing HTTP behavior tests around a local fake server**

Cover exact GET/PUT/DELETE paths, no body stream ID, content type, 500 ms timeout, no redirect following, only 127.0.0.1, capability protocol/limit validation, 202/200 success, non-retry 400/409/413/426, 429 backoff, network backoff, and capability refresh after a restarted server.

- [ ] **Step 2: Run the client test and verify it fails**

Run: `npm run testFile -- test/main/ttsLookAhead/client.test.ts`

Expected: FAIL because the client does not exist.

- [ ] **Step 3: Implement a minimal `node:http` loopback request helper**

Use hostname literal `127.0.0.1`, `agent: false`, finite timeout, `maxResponseBytes=65_536`, and reject every 3xx response. Validate `THORIUM_VOICELINK_PORT` as an integer 1024–65535; otherwise use 7860.

- [ ] **Step 4: Implement capability cache and exponential backoff**

Cache a successful probe for 60 seconds. Failure and 429 schedules 1/2/4/8/16/30-second delays; success resets the delay. A rejected revision is never replayed; the caller may submit a newer one after backoff.

- [ ] **Step 5: Run tests and commit**

Run: `npm run testFile -- test/main/ttsLookAhead/client.test.ts`

Expected: PASS with no external network access.

```bash
git add src/main/ttsLookAhead/client.ts test/main/ttsLookAhead/client.test.ts
git commit -m "feat: forward look-ahead to loopback VoiceLink"
```

### Task 4: Wire renderer events to a coalescing main-process dispatcher

**Files:**
- Create: `src/renderer/reader/ttsLookAhead.ts`
- Modify: `src/renderer/reader/index_reader.ts`
- Create: `src/main/ttsLookAhead/dispatcher.ts`
- Create: `src/main/ttsLookAhead/ipc.ts`
- Modify: `src/main.ts`
- Create: `test/renderer/reader/ttsLookAhead.test.ts`
- Create: `test/main/ttsLookAhead/dispatcher.test.ts`

**Interfaces:**
- Consumes: R2 `ttsLookAheadListen`, Task 2 IPC/wire conversion, Task 3 client.
- Produces `startTtsLookAheadAdapter(enabled: boolean): () => void` in renderer.
- Produces `LookAheadDispatcher.handle(event: unknown): void` and `dispose(): void` in main.
- Produces `installTtsLookAheadIpc(ipcMain, dispatcher): () => void`.

- [ ] **Step 1: Write failing renderer non-blocking tests**

Mock `ttsLookAheadListen` and `ipcRenderer.send`. Assert disabled mode registers no listener; enabled mode sends and returns synchronously without awaiting a response; disposer unregisters; snapshot and cancel retain their discriminant.

- [ ] **Step 2: Write failing dispatcher ordering tests**

With a deferred fake client, submit revisions 1, 2, 3 rapidly and assert only 1 then newest 3 are sent, never 2. Assert lower generations/revisions drop, cancel removes queued PUT and wins after an in-flight request, different stream IDs remain isolated, invalid payload causes zero network calls, and 429 queues no retry.

- [ ] **Step 3: Run both tests and verify they fail**

Run: `npm run testFile -- test/renderer/reader/ttsLookAhead.test.ts test/main/ttsLookAhead/dispatcher.test.ts`

Expected: FAIL because adapter/dispatcher are absent.

- [ ] **Step 4: Implement renderer registration and dedicated IPC**

In `index_reader.ts`, call `startTtsLookAheadAdapter(process.env.THORIUM_VOICELINK_LOOKAHEAD === "1")` once during reader startup and dispose on unload. The callback performs one `ipcRenderer.send()` only.

- [ ] **Step 5: Implement per-stream latest-wins serialization in main**

Maintain one in-flight operation plus one latest pending snapshot per stream. Validate/convert only in main, probe capabilities lazily, and check canceled/generation state again immediately before network send. DELETE remains best effort even when capability probing has failed.

- [ ] **Step 6: Install and tear down the IPC handler from `src/main.ts`**

Use `ipcMain.on(ttsLookAheadIpc.CHANNEL, handler)` and return/remove the exact handler during app shutdown/test disposal. Never expose a request/response promise to the renderer.

- [ ] **Step 7: Run tests, lint, and commit**

Run: `npm run testFile -- test/renderer/reader/ttsLookAhead.test.ts test/main/ttsLookAhead/dispatcher.test.ts && npm run lint:ts`

Expected: tests PASS and lint exits 0.

```bash
git add src/main.ts src/main/ttsLookAhead/dispatcher.ts src/main/ttsLookAhead/ipc.ts src/renderer/reader/index_reader.ts src/renderer/reader/ttsLookAhead.ts test/main/ttsLookAhead/dispatcher.test.ts test/renderer/reader/ttsLookAhead.test.ts
git commit -m "feat: connect R2 look-ahead to VoiceLink"
```

### Task 5: Verify complete fallback behavior and document experimental use

**Files:**
- Create: `test/main/ttsLookAhead/integration.test.ts`
- Modify: `README.md`

**Interfaces:**
- Consumes: all Thorium adapter tasks and a fake loopback VoiceLink server.
- Produces: documented experimental flag and end-to-end adapter verification.

- [ ] **Step 1: Write the failing integration scenarios**

Use the actual dispatcher/client against a local fake server. Assert enable → capabilities → bounded PUT, advance replacement, stop DELETE, unavailable server no throw, malformed capability no PUT, 429 drops revision then newer revision after backoff, current-unit oversize no PUT, and server restart reprobe. Spy on the ordinary R2 TTS calls and assert their arguments/order are identical with the adapter on and off.

- [ ] **Step 2: Run the integration test and verify any gaps**

Run: `npm run testFile -- test/main/ttsLookAhead/integration.test.ts`

Expected: FAIL until all cleanup/fallback paths are connected.

- [ ] **Step 3: Apply only the minimal lifecycle corrections**

Fix adapter/dispatcher teardown, capability invalidation, or coalescing revealed by Step 2. Do not add UI, remote hosts, persistent text, or retries of rejected bodies.

- [ ] **Step 4: Document the flag and local dependency**

Add developer documentation for `THORIUM_VOICELINK_LOOKAHEAD=1`, optional `THORIUM_VOICELINK_PORT`, protocol discovery, localhost-only traffic, no `synthesisHint` in version 1, and automatic fallback when VoiceLink is absent.

- [ ] **Step 5: Run final Thorium verification**

Run:

```bash
npm run testFile -- test/main/ttsLookAhead test/renderer/reader/ttsLookAhead.test.ts
npm run lint
npm run build
git diff --check
```

Expected: adapter tests PASS, full lint and production build exit 0, and diff check is clean.

- [ ] **Step 6: Commit**

```bash
git add README.md test/main/ttsLookAhead/integration.test.ts src/main/ttsLookAhead src/renderer/reader/ttsLookAhead.ts
git commit -m "test: verify VoiceLink adapter fallback"
```

## Thorium PR Acceptance

- With the experimental flag absent, Thorium registers no listener and behaves as before.
- With the flag enabled and VoiceLink compatible, main forwards only validated, bounded, ordered snapshots.
- Renderer/webview code never opens localhost HTTP and never waits on the adapter.
- VoiceLink absence, slowness, restart, rejection, or protocol mismatch cannot interrupt ordinary TTS.
- Stop/cancel and rapid seek cannot deliver a stale window after a newer revision.
- The PR depends only on the accepted provider-neutral R2 API; VoiceLink remains optional.
