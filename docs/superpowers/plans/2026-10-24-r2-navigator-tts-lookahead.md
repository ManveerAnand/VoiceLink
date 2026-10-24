# R2 Navigator Structured TTS Look-Ahead Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional provider-neutral R2 Navigator event that publishes a bounded, structured current-plus-future TTS window before the unchanged Web Speech utterance is submitted.

**Architecture:** Pure helpers convert the existing `ITtsQueueItem[]` into protocol-neutral units and deterministic boundaries. A small session state machine owns stream, generation, revision, and block IDs. The webview emits snapshot/cancel IPC only when the host enables a listener; the renderer exposes that listener without knowing VoiceLink.

**Tech Stack:** TypeScript 5.9, Electron IPC, Web Speech API, Node 22 test runner through `tsx`, existing `@xmldom/xmldom` test DOM.

**Spec:** `C:/Code/reader/docs/superpowers/specs/2026-10-24-adaptive-tts-prefetch-design.md`

## Global Constraints

- Keep `speechSynthesis.speak()` and one-sentence utterance behavior unchanged.
- Protocol 1 emits at most 12 units, 12,000 Unicode scalar values total, and 4,000 scalar values per unit.
- Dispatch the snapshot synchronously before the matching `speechSynthesis.speak()` call; never wait for host work.
- A host with no listener must not build or emit look-ahead snapshots.
- A stop/close emits cancel and the next start uses a new UUID; seek, voice, rate, language, and queue changes increment generation.
- R2 emits document semantics only. It must not import VoiceLink names, URLs, HTTP concepts, pause durations, or `synthesisHint`.
- Use Node.js >=22 and npm >=11, matching the repository engine floor.

## Review Focus

- Supplementary Unicode characters must count as one scalar value, and an oversized current unit must suppress the snapshot without changing speech.
- A heading that also starts a section must resolve to `heading-to-body` using the normative precedence and produce symmetric adjacent boundaries.
- Repeated pause/resume must retain stream/generation and must not increase the window or revision until a sentence is actually resubmitted.
- Internal voice/rate restarts must increment generation without emitting cancel; explicit stop must emit exactly one cancel and never reuse the stream ID.
- Registering or removing the listener while a webview is active must enable/disable snapshot construction without affecting ordinary TTS state events.

---

### Task 1: Add the public event contract and listener gate

**Files:**
- Create: `src/electron/common/tts-lookahead.ts`
- Modify: `src/electron/common/events.ts`
- Modify: `src/electron/renderer/webview/state.ts`
- Modify: `src/electron/renderer/readaloud.ts`
- Modify: `src/electron/renderer/dom.ts`
- Modify: `src/electron/renderer/webview/preload.ts`
- Modify: `package.json`
- Modify: `package-lock.json`
- Create: `test/tts-lookahead-listener.test.ts`

**Interfaces:**
- Produces: `ITtsLookAheadSnapshot`, `ITtsLookAheadCancel`, `ITtsLookAheadEvent`, `TtsBlockKind`, and `TtsBoundaryKind` from `src/electron/common/tts-lookahead.ts`.
- Produces: `ttsLookAheadListen(listener: ((event: ITtsLookAheadEvent) => void) | undefined): void`.
- Produces IPC constants `R2_EVENT_TTS_LOOKAHEAD_ENABLE`, `R2_EVENT_TTS_LOOKAHEAD`, and `R2_EVENT_TTS_LOOKAHEAD_CANCEL`.
- Consumes later: Task 3 sends the snapshot and cancel payloads defined here.

- [ ] **Step 1: Add the failing listener-gate tests**

Add `tsx@4.23.15` as an exact dev dependency and `"test:lookahead": "tsx --test test/tts-lookahead-*.test.ts"` to `package.json`, updating the lockfile with npm. Create tests named `listener_enables_all_active_webviews`, `undefined_listener_disables_all_active_webviews`, and `ipc_payload_reaches_listener`. Assert that registration sends `{ doEnable: true }`, removal sends `{ doEnable: false }`, and snapshot/cancel channels produce the corresponding discriminated union event without changing `TTSStateEnum` callbacks.

- [ ] **Step 2: Run the focused test and verify the red state**

Run: `npx tsx --test test/tts-lookahead-listener.test.ts`

Expected: FAIL because the types, events, and `ttsLookAheadListen` do not exist.

- [ ] **Step 3: Define the types and listener interface**

Use these stable public shapes:

```ts
export type ITtsLookAheadEvent =
    { type: "snapshot"; payload: ITtsLookAheadSnapshot } |
    { type: "cancel"; payload: ITtsLookAheadCancel };

export interface ITtsLookAheadCancel {
    protocol: 1;
    streamId: string;
}

export function ttsLookAheadListen(
    listener: ((event: ITtsLookAheadEvent) => void) | undefined,
): void;
```

Put the complete snapshot/unit/block/position types from spec Section 7 in `tts-lookahead.ts`. Keep `streamId` in the R2 event payload; Thorium removes it from the HTTP body.

- [ ] **Step 4: Wire enable state and renderer IPC handling**

Add `ttsLookAheadEnabled: boolean` to both reader/webview state interfaces, initialize it `false` in preload, propagate the setting alongside existing TTS settings when a webview becomes ready, and extend `ttsHandleIpcMessage()` to forward snapshot/cancel only to `_ttsLookAheadListener`.

- [ ] **Step 5: Run the listener tests and TypeScript build**

Run: `npx tsx --test test/tts-lookahead-listener.test.ts && npm run build`

Expected: all listener tests PASS and TypeScript compilation exits 0.

- [ ] **Step 6: Commit**

```bash
git add package.json package-lock.json src/electron/common/events.ts src/electron/common/tts-lookahead.ts src/electron/renderer/dom.ts src/electron/renderer/readaloud.ts src/electron/renderer/webview/preload.ts src/electron/renderer/webview/state.ts test/tts-lookahead-listener.test.ts
git commit -m "feat: add optional TTS look-ahead event contract"
```

### Task 2: Build deterministic structured look-ahead units

**Files:**
- Create: `src/electron/renderer/common/tts-lookahead.ts`
- Modify: `src/electron/renderer/common/dom-text-utils.ts`
- Create: `test/tts-lookahead-builder.test.ts`

**Interfaces:**
- Consumes: `ITtsQueueItem`, `ITtsQueueItemReference`, and the Task 1 common types.
- Produces: `classifyTtsBlock(element: Element): ITtsLookAheadBlock`.
- Produces: `deriveTtsBoundary(previous: ITtsLookAheadUnitSeed | undefined, next: ITtsLookAheadUnitSeed | undefined): TtsBoundaryKind`.
- Produces: `TtsBlockIdAllocator.idFor(element: Element): string`.
- Produces: `buildTtsLookAheadUnits(queue: ITtsQueueItem[], currentGlobalIndex: number, limits: ITtsLookAheadLimits, blockIds: TtsBlockIdAllocator): ITtsLookAheadUnit[]`.

- [ ] **Step 1: Write failing structure and Unicode-limit tests**

Create fixtures covering `h1`–`h6`, `p`, `li`, `blockquote`, `caption`/`figcaption`, `th`/`td`, `pre`, MathML, image descriptions, page breaks, `epub:type`, ARIA role, inherited language/direction, and unknown elements. Assert:

```ts
assert.equal(units[0].block.kind, "heading");
assert.equal(units[0].block.headingLevel, 2);
assert.equal(units[0].boundaryAfter, "heading-to-body");
assert.equal(units[1].boundaryBefore, units[0].boundaryAfter);
assert.equal([...unitsWithEmoji[0].text].length, expectedScalars);
```

Also pin precedence to `document > heading-to-body > section > list-item > paragraph > sentence > none`, session-local block IDs, sentence positions inside a block, and use of the next non-transmitted queue unit to calculate the final transmitted `boundaryAfter`.

- [ ] **Step 2: Run the builder test and verify it fails**

Run: `npx tsx --test test/tts-lookahead-builder.test.ts`

Expected: FAIL because the builder module is absent.

- [ ] **Step 3: Implement block classification and boundary derivation**

Use tag name, `computeEpubTypes(element)`, ARIA role, and existing language/direction helpers. `TtsBlockIdAllocator` owns a `WeakMap<Element, string>` and returns opaque `b1`, `b2`, … IDs; never expose DOM IDs or publication paths.

- [ ] **Step 4: Implement bounded document-order unit construction**

Flatten through existing `getTtsQueueItemRef()` semantics, stop at 12 units, reject an individual unit over 4,000 scalars, and stop before exceeding 12,000 total scalars. Return `[]` when the current unit itself is over limit. Do not mutate the TTS queue or its range arrays.

- [ ] **Step 5: Run focused tests, lint, and build**

Run: `npx tsx --test test/tts-lookahead-builder.test.ts && npm run lint:typescript:full && npm run build`

Expected: all builder tests PASS; lint and build exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/electron/renderer/common/dom-text-utils.ts src/electron/renderer/common/tts-lookahead.ts test/tts-lookahead-builder.test.ts
git commit -m "feat: derive structured TTS look-ahead units"
```

### Task 3: Add stream lifecycle and synchronous snapshot emission

**Files:**
- Create: `src/electron/renderer/common/tts-lookahead-session.ts`
- Modify: `src/electron/renderer/webview/readaloud.ts`
- Create: `test/tts-lookahead-session.test.ts`

**Interfaces:**
- Consumes: Task 2 `buildTtsLookAheadUnits()` and Task 1 event payloads.
- Produces: `TtsLookAheadSession.start(source: ITtsLookAheadSource): void`.
- Produces: `TtsLookAheadSession.invalidate(source: ITtsLookAheadSource): void`.
- Produces: `TtsLookAheadSession.snapshot(queue: ITtsQueueItem[], currentGlobalIndex: number): ITtsLookAheadSnapshot | undefined`.
- Produces: `TtsLookAheadSession.cancel(): ITtsLookAheadCancel | undefined`.

- [ ] **Step 1: Write failing lifecycle tests**

Assert a new UUID and zeroed generation/revision on `start`, monotonically increasing revision for advancing snapshots, higher generation with revision reset on seek/voice/rate invalidation, no state change for pause/resume, one cancel result only, and a fresh UUID after canceled restart. Inject `uuidFactory` so tests use deterministic IDs.

- [ ] **Step 2: Run the lifecycle test and verify it fails**

Run: `npx tsx --test test/tts-lookahead-session.test.ts`

Expected: FAIL because `TtsLookAheadSession` does not exist.

- [ ] **Step 3: Implement the pure session state machine**

Use this constructor and keep all counters private:

```ts
constructor(
    private readonly uuidFactory: () => string = () => crypto.randomUUID(),
    private readonly maxUnits = 12,
)
```

Snapshot `units[0].sequence` must equal `currentSequence`; lower sequence is possible only after `invalidate()` creates a higher generation.

- [ ] **Step 4: Integrate emission immediately before Web Speech submission**

In `ttsPlayQueueIndex()`, after voice/rate assignment but before scheduling `win.speechSynthesis.speak(utterance)`, synchronously call `ipcRenderer.sendToHost(R2_EVENT_TTS_LOOKAHEAD, snapshot)` when enabled. Keep the existing `setTimeout(...speak...)` and utterance text unchanged.

- [ ] **Step 5: Integrate invalidation and cancellation paths**

Normal `onend` advance changes only revision/current sequence. Explicit next/previous/preview navigation invalidates the generation. Voice/rate changes preserve the stream and invalidate it even though existing code recreates/resumes an utterance. Explicit stop, dialog close, and document teardown emit one cancel before clearing state. Pause/resume does not emit a new snapshot unless it actually resubmits a rebuilt utterance.

- [ ] **Step 6: Run lifecycle, builder, listener, lint, and build checks**

Run: `npx tsx --test test/tts-lookahead-*.test.ts && npm run lint:typescript:full && npm run build`

Expected: all tests PASS; lint and build exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/electron/renderer/common/tts-lookahead-session.ts src/electron/renderer/webview/readaloud.ts test/tts-lookahead-session.test.ts
git commit -m "feat: emit bounded TTS look-ahead snapshots"
```

### Task 4: Prove no-listener compatibility and document the host API

**Files:**
- Modify: `README.md`
- Modify: `test/tts-lookahead-listener.test.ts`
- Modify: `test/tts-lookahead-session.test.ts`

**Interfaces:**
- Consumes: all earlier R2 tasks.
- Produces: documented host-facing `ttsLookAheadListen()` contract for Thorium.

- [ ] **Step 1: Add failing compatibility tests**

Instrument the unit builder and IPC sender. Assert that an undefined listener leaves the builder call count at zero, does not emit look-ahead/cancel IPC, and preserves the exact existing utterance text/order. Add tests for listener removal during playback and an oversized current unit.

- [ ] **Step 2: Run the compatibility tests and observe any failures**

Run: `npx tsx --test test/tts-lookahead-listener.test.ts test/tts-lookahead-session.test.ts`

Expected: new assertions FAIL until all gates are applied at snapshot construction and teardown sites.

- [ ] **Step 3: Apply the minimal compatibility corrections**

Guard structure/unit construction at its entry point with `ttsLookAheadEnabled`; do not add behavior to the ordinary utterance path when disabled.

- [ ] **Step 4: Document enablement, payload semantics, limits, and fallback**

Document the two event variants, synchronous-before-speak guarantee, lifecycle rules, and that hosts must treat delivery as best effort. Do not document VoiceLink URLs in R2.

- [ ] **Step 5: Run the complete R2 verification**

Run: `npx tsx --test test/tts-lookahead-*.test.ts && npm run lint && npm run build:all`

Expected: all tests PASS; full lint and all TypeScript targets build successfully.

- [ ] **Step 6: Commit**

```bash
git add README.md test/tts-lookahead-listener.test.ts test/tts-lookahead-session.test.ts src/electron/renderer/webview/readaloud.ts
git commit -m "docs: describe structured TTS look-ahead API"
```

## R2 Navigator PR Acceptance

- The PR is independently useful as a dormant provider-neutral API.
- With no listener installed, existing TTS tests/build and audible behavior are unchanged.
- The published npm types expose the listener and payloads from the normal renderer entry point.
- Thorium can consume snapshots without importing webview-private modules.
- The PR description links the generic rationale but does not make VoiceLink a dependency.
