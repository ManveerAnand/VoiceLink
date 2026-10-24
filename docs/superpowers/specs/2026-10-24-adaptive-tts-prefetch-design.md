# Adaptive TTS Look-Ahead and Structural Prosody

Date: 2026-10-24
Status: Proposed
Repositories: R2 Navigator JS, Thorium Reader, VoiceLink

## 1. Summary

Thorium currently reads one sentence at a time through the Web Speech API. The
next sentence is submitted only after the current utterance ends. With a local
neural TTS engine such as Kokoro, this creates a synthesis delay between
sentences. VoiceLink cannot remove that delay because the Windows SAPI engine
sees only the current utterance and has no knowledge of future text.

This design adds an optional, provider-neutral look-ahead channel alongside the
existing speech path. R2 Navigator publishes a bounded snapshot containing the
current sentence, several future sentences, and their document structure.
Thorium forwards the snapshot to VoiceLink. VoiceLink pre-generates future audio
and applies structure-aware pauses. The existing Web Speech/SAPI request remains
the source of truth for what is actually spoken.

The new path is an optimization. If any component does not support it, if
correlation is ambiguous, or if prefetched audio is unavailable, playback uses
the current behavior without losing, reordering, or duplicating text.

## 2. Goals

- Remove avoidable neural-synthesis gaps between sentences by generating future
  sentence audio while the current sentence is playing.
- Preserve the existing one-utterance-at-a-time Web Speech and SAPI contract.
- Preserve compatibility with existing R2 Navigator hosts, Thorium builds,
  VoiceLink bridges, and TTS providers.
- Carry semantic document structure so VoiceLink can distinguish sentence,
  paragraph, list, heading, section, and similar boundaries.
- Keep R2 Navigator independent of VoiceLink and of any localhost protocol.
- Bound CPU, memory, text exposure, and speculative work.
- Make every optimization failure fall back safely to on-demand synthesis.

## 3. Non-goals

- Sending multiple sentences in a single `SpeechSynthesisUtterance`.
- Replacing or changing the existing `/v1/tts` request contract.
- Rendering an entire chapter or book in advance.
- Passing arbitrary SSML from EPUB content to the speech engine.
- Guaranteeing uninterrupted playback when synthesis is not faster than
  consumption over a sustained interval.
- Announcing structure such as “heading level two” in the first version.
- Phoneme-level or word-level forced alignment.
- Making R2 Navigator aware of VoiceLink, Kokoro, or Windows SAPI.

## 4. Current Behavior and Root Cause

R2 Navigator already constructs a document-order TTS queue. Each queue item
contains a block element, its text nodes, combined text, sentence splits,
language, and direction. When playback reaches a sentence, R2 creates one
`SpeechSynthesisUtterance` and submits it through `speechSynthesis.speak()`.
The next sentence is submitted from the current utterance's completion path.

Chromium queues Web Speech utterances internally and sends only the active
utterance to the native TTS service. Windows SAPI similarly calls the selected
engine with the current text fragment. Consequently, queuing more Web Speech
utterances would not make future text available to VoiceLink early enough to
pre-generate it.

The local VoiceLink server also needs the completed Kokoro PCM before it can
calculate the current response length. Local measurements show that first-byte
latency grows with input size. The gap is therefore primarily a producer timing
problem, not an audio-player pause setting.

## 5. Design Principles

1. **The current utterance is authoritative.** Look-ahead data never causes
   speech by itself.
2. **The existing path remains untouched.** Current text still travels through
   Web Speech, Chromium, SAPI, the VoiceLink bridge, and `/v1/tts`.
3. **Look-ahead is a replaceable snapshot, not a command queue.** Repeating the
   same snapshot is harmless; a newer generation invalidates older work.
4. **Structure is semantic, not timed.** R2 reports document meaning. VoiceLink
   decides how that meaning maps to pauses or future prosody controls.
5. **Correlation must be conservative.** VoiceLink uses prefetched data only
   when it can identify exactly one matching stream and sequence.
6. **All speculative work is bounded.** The window, cache, request size, worker
   time, and retained PCM have explicit limits.

## 6. End-to-End Architecture

The authoritative playback path remains:

```text
R2 current sentence
  -> speechSynthesis.speak(current sentence)
  -> Chromium
  -> Windows SAPI
  -> VoiceLink bridge
  -> POST /v1/tts
  -> audio returned to SAPI
```

The optional look-ahead path is:

```text
R2 bounded structured snapshot
  -> provider-neutral R2 event/callback
  -> Thorium renderer adapter
  -> Thorium main process
  -> VoiceLink loopback look-ahead endpoint
  -> correlation + scheduler + bounded cache
```

The two paths meet only inside VoiceLink when an authoritative `/v1/tts`
request can be correlated safely with a previously received snapshot.

### 6.1 Component responsibilities

**R2 Navigator JS**

- Selects a bounded window from its existing TTS queue.
- Attaches language, direction, block structure, and boundary semantics.
- Emits a provider-neutral event or callback.
- Advances the snapshot as playback advances.
- Changes generation when the reading timeline is invalidated.
- Does nothing differently when no host listener is installed.

**Thorium Reader**

- Listens for the optional R2 look-ahead event.
- Probes whether the local VoiceLink instance supports the protocol.
- Forwards validated snapshots through Electron's main process.
- Stops forwarding when VoiceLink is unavailable or unsupported.
- Does not alter ordinary TTS playback.

**VoiceLink**

- Advertises optional look-ahead capability.
- Accepts and validates bounded snapshots on loopback.
- Correlates the current SAPI-originated request conservatively.
- Schedules future Kokoro synthesis with lower priority than current speech.
- Stores reusable canonical speech PCM in a bounded cache.
- Adds a structure-aware pause when returning authoritative audio.
- Falls back to ordinary on-demand synthesis on every uncertainty.

## 7. R2 Navigator Look-Ahead Contract

### 7.1 Event shape

R2 adds an optional event, tentatively named `R2_EVENT_TTS_LOOKAHEAD`, with a
host-facing listener such as `ttsLookAheadListen`. Naming should follow the
existing R2 event conventions during implementation.

An illustrative payload is:

```ts
interface TtsLookAheadSnapshot {
  protocol: 1;
  streamId: string;
  generation: number;
  revision: number;
  currentSequence: number;
  selectedVoice?: {
    name?: string;
    voiceURI?: string;
    lang?: string;
  };
  rate: number;
  units: TtsLookAheadUnit[];
}

interface TtsLookAheadUnit {
  sequence: number;
  text: string;
  lang?: string;
  direction?: "ltr" | "rtl" | "auto";
  block: {
    id: string;
    kind: TtsBlockKind;
    htmlTag?: string;
    headingLevel?: number;
    epubTypes?: string[];
    ariaRole?: string;
  };
  position: {
    indexInBlock: number;
    firstInBlock: boolean;
    lastInBlock: boolean;
  };
  boundaryBefore: TtsBoundaryKind;
  boundaryAfter: TtsBoundaryKind;
}
```

Initial block kinds are:

```text
heading, paragraph, list-item, blockquote, caption, table-cell,
code, math, image-description, page-break, other
```

Initial semantic boundary kinds are:

```text
none, sentence, paragraph, list-item, heading-to-body, section, document
```

The exact TypeScript names may change to match existing R2 conventions, but the
semantics are part of the protocol contract.

Thorium converts the R2-local `selectedVoice` and `rate` fields into the
normative wire `source` object below, removes `streamId` from the body because it
is carried in the URL, and may add the VoiceLink-specific `synthesisHint` under
the strict rule below. R2 itself never defines or emits `synthesisHint`.

### 7.2 Normative protocol-1 wire schema

The TypeScript event names are repository-local, but the Thorium-to-VoiceLink
JSON schema is frozen for protocol 1. JSON object property names below are
normative. Unknown properties are ignored; missing required properties or an
unknown enum value make the request invalid.

```json
{
  "protocol": 1,
  "generation": 3,
  "revision": 18,
  "currentSequence": 42,
  "source": {
    "rate": 1.0,
    "voiceName": "Heart (Kokoro)",
    "voiceURI": "optional Web Speech identifier",
    "lang": "en-GB"
  },
  "synthesisHint": {
    "route": "kokoro",
    "voice": "af_heart",
    "speed": 1.0,
    "format": "pcm_24k_16bit"
  },
  "units": [
    {
      "sequence": 42,
      "text": "Example sentence.",
      "lang": "en-GB",
      "direction": "ltr",
      "block": {
        "id": "b7",
        "kind": "paragraph",
        "htmlTag": "p",
        "epubTypes": [],
        "ariaRole": "paragraph"
      },
      "position": {
        "indexInBlock": 0,
        "firstInBlock": true,
        "lastInBlock": true
      },
      "boundaryBefore": "paragraph",
      "boundaryAfter": "paragraph"
    }
  ]
}
```

Required top-level fields are `protocol`, `generation`, `revision`,
`currentSequence`, `source.rate`, and `units`. `generation`, `revision`, and all
sequences are non-negative safe JSON integers. `source.rate` is the Web Speech
rate supplied to R2 and is informational. Other `source` properties are
optional. `synthesisHint` is optional and Thorium may include it only when its
VoiceLink voice-token metadata and rate conversion guarantee that all four
values are byte-for-byte identical to the next bridge `/v1/tts` body. Otherwise
it must be omitted; an uncertain hint is worse than no hint.

Every unit requires `sequence`, non-empty `text`, `block.id`, `block.kind`, all
three `position` fields, `boundaryBefore`, and `boundaryAfter`. Optional fields
are omitted, not sent as `null`. Units are ordered by consecutive sequence,
start at `currentSequence`, and contain no duplicates. `streamId` appears only
as the canonical lowercase UUID in the URL path; it is not duplicated in the
body.

Character limits count Unicode scalar values after JSON decoding. Transport
limits count UTF-8 request-body bytes. Implementations must not count UTF-16
code units. Protocol 1 permits at most 12 units, 12,000 scalar values across
unit texts, 4,000 scalar values in one unit, and a 64 KiB HTTP body. A server may
advertise smaller positive limits, in which case Thorium tail-truncates to them.
If the current unit by itself exceeds any advertised or protocol limit, Thorium
sends no snapshot for that sequence and ordinary playback continues. It must not
split, truncate, or rewrite the authoritative current unit for this feature.

`GET /v1/capabilities` returns this normative shape for this feature:

```json
{
  "lookahead": {
    "protocols": [1],
    "maxUnits": 12,
    "maxCharacters": 12000,
    "maxUnitCharacters": 4000,
    "maxBodyBytes": 65536
  },
  "structuredPauses": true,
  "bridgeProtocolRequiredForPauses": 2
}
```

For `PUT`, VoiceLink returns `202` with
`{"status":"accepted","generation":3,"revision":18}`, or `200` with the
same generation/revision and `status:"duplicate"` for an identical replay.
It returns `400 invalid`, `409 stale`, `409 revision-conflict`, `413 over-limit`,
`426 unsupported-protocol`, or `429 budget-exhausted`. Error bodies have the
normative form `{"error":{"code":"stale","message":"..."}}`; clients branch
on `code`, not the human-readable message. `DELETE` is idempotent and returns
`204`, including when the stream no longer exists.

All non-2xx `PUT` responses are atomic rejections: they do not replace,
invalidate, or partially schedule the previously accepted window. In
particular, `429 budget-exhausted` means no state from that request was stored.
Thorium drops that revision, applies exponential backoff starting at one second
and capped at 30 seconds, and may send only the then-current newer revision after
the backoff. It never retries the rejected body verbatim. Existing accepted
state expires normally and cannot advance correlation past its published
sequence window.

### 7.3 Snapshot semantics

- `streamId` is a random opaque identifier for one active read-aloud session.
- `generation` increases whenever previously published future units may no
  longer be valid.
- `revision` increases for every replacement within one generation.
- `sequence` is monotonic within a generation.
- `currentSequence` identifies the sentence currently being submitted or
  spoken through the authoritative path.
- `units` begins with the current unit and contains a bounded number of future
  units in document order.
- Replacing a snapshot with the same `(streamId, generation, revision)` and
  identical content is idempotent.
- A higher generation atomically invalidates queued work, in-flight stream
  associations, and generation-scoped results for earlier generations. It does
  not evict an independently keyed canonical PCM entry whose exact synthesis
  key remains valid.
- Within one generation, VoiceLink accepts only a higher revision whose
  `currentSequence` is not lower than the accepted value. A lower revision or
  regressing sequence is stale. Reusing a revision with different content is a
  conflict. Receivers reject lower generations and malformed or oversized
  windows.

A complete window is used instead of `appendNextSentence` because it is robust
to duplicate delivery, dropped events, seeks, restarts, and changes in the
adaptive window size. It also lets the receiver reconcile state without relying
on a perfectly delivered command history.

### 7.4 Extracting structure

R2 already has the relevant DOM context when it creates `ITtsQueueItem`. The
event adapter derives structure from the queue item's parent element and the
sentence's position within that item:

- `h1` through `h6` become `heading` with `headingLevel`.
- `p` becomes `paragraph`.
- `li`, `blockquote`, `caption`/`figcaption`, `th`/`td`, `pre`, and known math or
  image-description semantics map to their corresponding kinds.
- `epub:type` values and computed ARIA role are retained as semantic hints.
- Unknown elements map to `other`; they never block speech.

Block IDs are session-local opaque identifiers. They need not expose EPUB file
paths, element IDs, titles, or book identifiers.

Boundary values are derived from adjacent units. For example, two sentences in
one paragraph have a `sentence` boundary; the first body sentence after a
heading has `heading-to-body`; adjacent list items have `list-item`; and the
first unit of a new section has `section` when R2 can determine that reliably.
When structure is uncertain, R2 reports the less specific safe value rather
than guessing.

Protocol 1 carries one boundary value, so simultaneous meanings use this strict
precedence from strongest to weakest:

```text
document > heading-to-body > section > list-item > paragraph > sentence > none
```

For adjacent units A and B, `A.boundaryAfter` must equal
`B.boundaryBefore`. The first unit in a stream has `boundaryBefore: document`;
the final known unit of the document has `boundaryAfter: document`. The tail of
a truncated snapshot is not assumed to be the end of the document: its final
`boundaryAfter` is derived from the next queue unit even when that unit is not
transmitted. If the next unit is unavailable or structural classification is
uncertain, use `sentence` for same-block text, `paragraph` for a known block
change, and `none` only when no audible separation is intended.

### 7.5 Lifecycle

R2 publishes or replaces the snapshot when playback starts and when the current
sequence advances. During one active stream it increments `generation` on:

- seek or navigation to a different queue position;
- voice, language, or speech-rate change;
- document or spine-item change;
- queue reconstruction that may change sequence identity.

Pause retains the current generation but must not cause the look-ahead window to
grow. Stop or document close synchronously emits a provider-neutral
`R2_EVENT_TTS_LOOKAHEAD_CANCEL` (final public naming follows R2 conventions)
whose payload is `{ protocol: 1, streamId }`. Thorium consumes that signal and
sends the idempotent DELETE. A later start always creates a new random
`streamId`, with generation and revision reset to zero; a canceled ID is never
reused. Unexpected renderer destruction may prevent delivery, so server TTL
remains the final cleanup mechanism.

R2 dispatches each look-ahead event synchronously before it calls
`speechSynthesis.speak()` for that current sequence. This establishes ordering
inside the renderer, but HTTP forwarding remains asynchronous and may reach
VoiceLink after the authoritative request. VoiceLink therefore supports the
late-correlation rule in Section 10; playback never waits for look-ahead
delivery.

R2 emits a host-configured fixed maximum window, initially 12 units and 12,000
Unicode scalar values including the current unit. Thorium truncates only from
the tail to the smaller limits advertised by VoiceLink and never removes or
rewrites the current unit. VoiceLink then adaptively schedules an in-order
subset of the received future units. There is no feedback loop from VoiceLink's
runway estimator into R2 in protocol 1.

## 8. Thorium Adapter

Thorium is the integration boundary because it knows both the provider-neutral
R2 event and the configured local TTS integration.

On the first relevant event, and periodically after failures, Thorium calls:

```http
GET /v1/capabilities
```

If the response advertises look-ahead protocol version 1, Thorium forwards
snapshots using its Electron main process. Keeping the HTTP call out of the
webview avoids CORS and content-security-policy coupling and gives Thorium one
place to enforce loopback-only destinations, timeouts, size limits, and version
negotiation.

Forwarding is best-effort and non-blocking. A slow or failed call must never
delay `speechSynthesis.speak()`. Capability or transport failure disables the
optimization for a backoff period and leaves current speech unchanged.

Thorium sends stream deletion on normal teardown when practical, but correctness
does not depend on it because VoiceLink also expires streams by TTL.

## 9. VoiceLink Protocol

### 9.1 Capability discovery

```http
GET /v1/capabilities
```

The protocol-1 response and counting rules are normative in Section 7.2.
Absence, an error, or a response without protocol 1 means the feature is not
available.

### 9.2 Replace snapshot

```http
PUT /v1/tts/lookahead/{streamId}
Content-Type: application/json
```

The body is the normative Section 7.2 snapshot without `streamId`; the canonical
UUID exists only in the path. The operation replaces the server's desired
window for that stream and generation; it does not append commands.

Normative response codes and bodies are defined in Section 7.2. Thorium does not
retry stale, conflicting, invalid, or over-limit snapshots.

### 9.3 Cancel stream

```http
DELETE /v1/tts/lookahead/{streamId}
```

Cancellation removes pending speculative jobs and stream-specific associations.
Canonical cache entries may remain until ordinary cache eviction because they
contain only derived audio keyed independently of the stream.

### 9.4 Existing synthesis endpoint

`POST /v1/tts` remains backward-compatible in request and audio-response
semantics. A new VoiceLink bridge may add diagnostic correlation headers:

```text
X-VoiceLink-Bridge-Protocol: 2
X-VoiceLink-Engine-Instance: <random UUID created by the bridge>
X-VoiceLink-Engine-Process: <process identifier, if useful>
```

The bridge creates a fresh UUID for each `VoiceLinkEngine` COM object, not one
per DLL or process, and retains it for that object's lifetime. One object is not
assumed to equal one reader: the server still applies the concurrency and
ambiguity rules in Section 10. `X-VoiceLink-Bridge-Protocol: 2` promises that the
bridge understands prefix-silence timing headers. Older bridges omit these
headers and the server must continue to synthesize normally. None of the headers
attempts to transport the R2 stream ID through Web Speech or SAPI.

## 10. Conservative Correlation

The side-channel and authoritative SAPI request cannot carry a shared identifier
through the complete current stack. VoiceLink therefore treats correlation as a
best-effort optimization with strict ambiguity rules.

Bridge protocol 2 also sends
`X-VoiceLink-Request-Id: <UUID>` once per SAPI `Speak` call and reuses it only for
transport retries of that same call. This makes retry handling idempotent.

VoiceLink keeps two short-lived indexes: unclaimed snapshots and authoritative
request observations. An observation contains request ID, engine-instance ID,
arrival time, the exact request-body synthesis configuration, and text digests;
it expires after five seconds. This permits either arrival order:

- If the snapshot is already present, an authoritative request may claim it.
- If asynchronous forwarding makes the snapshot late, its arrival may claim one
  recent still-eligible request observation and then begin prefetching future
  units. It cannot accelerate that already-started current request, but it can
  prepare the next one.

For an initial claim, snapshot S and request observation R are eligible exactly
when all of these conditions hold:

- both are accepted, unexpired, and their arrival timestamps differ by no more
  than five seconds;
- S is unclaimed, R is unclaimed, and R has bridge protocol 2 instance and
  request IDs;
- S's current-unit `correlationTextDigest` equals R's digest;
- if S contains `synthesisHint`, every hint field exactly equals R's request
  route/body configuration; if the hint is absent, this check is neutral;
- neither the engine instance nor stream is already bound to a different
  active counterpart.

The protocol-1 `source` fields are never equality constraints because they are
provider-neutral and may use a different naming or rate scale. They are retained
for diagnostics and future negotiation only.

Claiming requires a mutually unique edge in the active eligibility graph: S has
exactly one eligible request observation, and R has exactly one eligible
snapshot. Unrelated pairs with different digests do not affect uniqueness. If
either endpoint has two eligible counterparts, none of those edges is claimed,
regardless of which item arrived first. This symmetric rule prevents arrival
order from choosing between simultaneous readers with identical text.

R2's `source` voice/rate/language values are correlation hints, not synthesis
configuration. When present, `synthesisHint` participates in matching only
because Thorium has asserted exact equivalence. The first authoritative request
that claims a stream freezes the actual configuration from its `/v1/tts` body:
route/model, voice, speed, format, backend version, and any other
synthesis-affecting option. All speculative units for that claim use this frozen
configuration. A later authoritative configuration mismatch suspends the claim,
serves normally, and requires a unique new-generation claim before prefetch is
used again. Thus no Web Speech-to-SAPI rate conversion is guessed by VoiceLink.

Candidate discovery may use a separately named `correlationTextDigest` based on
a narrowly defined normalization (Unicode NFC, CRLF-to-LF, and collapse of
Unicode whitespace runs to one ASCII space, with no case or punctuation
folding). This digest only locates candidates. It never authorizes audio reuse.
Every reusable PCM entry and in-flight synthesis job uses a
`synthesisInputDigest` over the exact UTF-8 text received by `/v1/tts` or stored
in the snapshot plus the frozen synthesis configuration. If the exact key does
not match, the authoritative text is synthesized on demand even when
correlation succeeds.

The binding state machine is normative:

1. `unclaimed`: a snapshot has no engine association. Claim it only if exactly
   one globally eligible snapshot/observation pair matches. Zero or multiple
   pairs remain unclaimed.
2. `bound`: associate `(engineInstance, streamId, generation)` with the last
   accepted sequence, next expected sequence, and frozen synthesis config.
3. `advance`: a new request advances only when it is the sole non-retry request
   on that engine instance, matches the exact expected sequence's
   `synthesisInputDigest`, and no competing unclaimed snapshot makes the same
   request globally ambiguous.
4. `retry`: the same request ID attaches to the same job/result and never
   advances sequence twice.
5. `suspended`: an unexpected text/configuration, competing candidate,
   concurrent different request ID, generation discontinuity, or sequence gap
   immediately disables prefetched reuse. The authoritative request follows the
   legacy path. A later unique claim may rebind.
6. `released`: DELETE, stop/TTL expiry, engine TTL expiry, or document teardown
   removes the association and cancels stream-specific speculative jobs.

One engine instance may have at most one bound stream and one non-retry
authoritative request in flight. The server does not assume that a COM object is
dedicated to one reader; overlap causes suspension and fallback. A higher
generation on the same stream may replace the binding only when its current
unit uniquely matches the next authoritative request.

Requests from an old bridge without protocol-2 instance and request IDs never
create an ordered stream binding and never receive structural prefix silence.
They may use an already existing canonical cache entry only when their exact
synthesis key matches; otherwise they follow the legacy path. Correctness takes
priority over hit rate.

Full text is not written to logs. Diagnostic records use digests, lengths,
sequence numbers, and reason codes such as `unique-match`, `ambiguous`,
`mismatch`, or `expired`.

## 11. Synthesis Scheduler and Cache

### 11.1 Worker model and authoritative isolation

Speculative inference must never occupy the only capacity able to serve a new
authoritative `/v1/tts` miss. A queue priority alone is insufficient because it
cannot preempt a long inference already running.

Protocol-1 enablement therefore requires two execution lanes:

1. An authoritative lane reserved for `/v1/tts` misses.
2. A lower-OS-priority speculative lane with its own cancellable worker process
   and model instance.

The two lanes share only job metadata and the bounded PCM cache, not a mutable
model object. If the machine cannot load the second model instance within its
configured memory/CPU budget, VoiceLink advertises no look-ahead capability and
uses the legacy authoritative lane only. Speculative CPU concurrency is capped
and benchmarked so enabling it does not materially regress authoritative
first-byte latency; the release gate is no more than 5% p95 regression versus
look-ahead disabled under the same workload.

An authoritative miss may reuse an identical completed cache entry. It may also
adopt an identical in-flight speculative job only when the backend can promote
or complete that job within the authoritative latency budget; otherwise it
synthesizes independently in the reserved lane and the first valid completion
wins cache insertion. Newer generations cancel the speculative process job when
safe and remove stale queued jobs. Results that finish after invalidation are
discarded from the stream association, though a valid exact-key canonical
result may still enter the bounded content cache.

### 11.2 Canonical cache key

Canonical synthesized speech PCM is keyed by all inputs that affect speech,
including at least:

```text
route + engine/model version + voice + effective speed + format
+ language/backend options + exact UTF-8 synthesis text
```

Structural silence is not included in canonical speech PCM. Keeping speech and
boundary pauses separate avoids duplicate cache entries for identical text at
different document boundaries.

### 11.3 Adaptive runway

VoiceLink tracks an exponentially weighted moving average of real-time factor:

```text
RTF = synthesis wall time / generated speech duration
```

The desired runway is enough predicted audio to cover expected synthesis time
plus a safety margin. A conceptual rule is:

```text
target_runway_seconds = clamp(
  minimum_runway,
  recent_RTF * next_expected_generation_cost + safety_margin,
  maximum_runway
)
```

The actual selection walks future units in order until the target is met, while
also enforcing hard caps. The exact estimator should be tuned from measured
telemetry rather than fixed in the protocol.

Hard bounds include:

- maximum units per snapshot and per active stream;
- maximum characters per unit and snapshot;
- maximum predicted PCM seconds and bytes;
- maximum queued speculative work;
- per-stream and global cache budgets;
- stream and cache TTLs.

When `RTF >= 1` for a sustained period, prefetch cannot guarantee continuous
playback. The scheduler still minimizes gaps but does not grow without bound.

## 12. Structure-Aware Pauses

### 12.1 Semantic policy

R2 supplies semantic boundaries. VoiceLink maps them through a configurable
`ProsodyPolicy`. Defaults should be conservative and tuned by listening tests,
not embedded in the cross-project protocol.

An initial ordering is:

```text
sentence < paragraph/list-item < heading-to-body < section/document
```

Different block kinds may refine the mapping. Code, verse-like preformatted
text, tables, and captions should avoid aggressive interpretation until tested.
Unknown structure uses ordinary sentence behavior.

Version 1 primarily realizes structure as silence. The policy abstraction may
later select model-specific style or prosody controls. Audible announcements of
structure are a separate future accessibility option and are off by default.

### 12.2 Pause placement

Boundary silence is prefixed to the following unit's response and represents
that unit's `boundaryBefore`. Prefix placement is preferable to appending silence
to the prior response because, when the next request arrives, VoiceLink knows
whether it incurred synthesis wait time and can avoid compounding that wait with
the full desired pause.

VoiceLink adds prefix silence only to a safely correlated request carrying
`X-VoiceLink-Bridge-Protocol: 2`. Old bridges receive no structural prefix
because they cannot offset SAPI boundary/progress events. Prefetch and canonical
exact-key cache hits remain independently usable without adding silence.

Let:

```text
desired_gap = ProsodyPolicy(boundaryBefore)
request_wait = elapsed time spent waiting for audio after the next request
prefix_silence = max(0, desired_gap - request_wait)
```

On a cache hit, `request_wait` is near zero and the full structural gap is
included. On a miss, synthesis latency already creates part or all of the
perceived gap, so only the remainder is added. The measurement begins when the
authoritative request reaches VoiceLink; it is therefore an approximation of
wall-clock inter-utterance delay, not sample-accurate presentation timing.

If actual request-to-playback buffering proves materially variable, the policy
must remain conservative or the bridge can later report timing telemetry. That
enhancement is not required for the initial protocol.

For bridge protocol 2, the response includes additive timing headers measured in
PCM bytes:

```text
X-Audio-Prefix-Bytes: <integer bytes of leading silence>
X-Speech-Audio-Length: <integer PCM bytes excluding prefix silence>
X-Audio-Length: <integer total PCM bytes returned>
```

New bridge code offsets boundary and progress events by the prefix duration and
uses speech length for word-timing calculations. Duration is derived from the
existing sample-rate, sample-width, and channel headers. Old bridges receive no
prefix, ignore the new headers if present, and continue to receive legacy PCM.

A safely correlated cache miss still receives
`max(0, desired_gap - request_wait)` silence under bridge protocol 2. In this
document, “legacy fallback” means byte/timing-equivalent old output when the
request is uncorrelated, ambiguous, from an old bridge, or otherwise unsafe.
A failed or evicted speculative job alone does not remove structure when the
stream correlation and protocol-2 timing support remain valid; it changes a hit
into on-demand synthesis plus the residual safe pause.

## 13. Failure Handling

The following cases always preserve the old behavior:

- R2 host installs no look-ahead listener.
- Thorium does not contain the adapter.
- VoiceLink is absent, unreachable, slow, or reports no compatible capability.
- The VoiceLink bridge is old and supplies no engine-instance header.
- A snapshot is late, stale, malformed, or over limits.
- Correlation has zero or multiple candidates.
- The expected sequence or text does not match the authoritative request.
- A prefetched job fails, is canceled, or is evicted.
- Structure is absent or unknown.

Look-ahead transport and synthesis errors are observable through bounded,
privacy-preserving diagnostics but never fail current speech solely because an
optimization failed.

## 14. Security and Privacy

- All VoiceLink endpoints bind to loopback only by default.
- Thorium forwards only to an explicitly configured loopback VoiceLink origin.
- Stream and block identifiers are random, opaque, and session-local.
- Snapshots contain no book path, title, author, account, or persistent document
  identifier.
- Logs omit full text and record only digests, lengths, timing, and reason codes.
- Requests have strict byte, character, unit, and nesting limits.
- Active streams, jobs, and PCM have per-client and global budgets plus TTLs.
- Capability and look-ahead calls use short timeouts and do not block playback.
- If VoiceLink later permits non-loopback binding, authentication and transport
  security must be designed separately before this endpoint is exposed.

## 15. Compatibility and Versioning

The look-ahead payload has an explicit protocol version. Capability negotiation
prevents Thorium from sending unsupported shapes. Additive fields may be ignored
within a protocol version; incompatible semantic changes require a new version.

Compatibility matrix:

| R2 | Thorium adapter | VoiceLink support | Result |
|---|---|---|---|
| old | any | any | Existing playback |
| new | absent | any | Existing playback |
| new | present | absent/old | Existing playback after capability failure |
| new | present | new, old bridge | Exact-key cache only; no binding or structured prefix |
| new | present | new, new bridge | Prefetch and structured pauses when uniquely correlated |

No existing R2 listener, Web Speech utterance, SAPI method, or `/v1/tts`
request body changes meaning.

## 16. Repository and Pull Request Boundaries

This feature should be proposed as three independently reviewable changes.

### 16.1 R2 Navigator JS PR

- Add provider-neutral look-ahead event types and listener plumbing.
- Convert existing queue items into bounded structured snapshots.
- Add generation and lifecycle handling.
- Preserve no-listener behavior as a no-op.
- Document the host contract without mentioning VoiceLink as a dependency.

### 16.2 Thorium Reader PR

- Listen for the optional R2 event.
- Add capability discovery and backoff.
- Forward snapshots and cancellation through the main process.
- Restrict forwarding to configured loopback destinations.
- Ensure failures never delay or alter ordinary TTS.

### 16.3 VoiceLink PR

- Add capabilities, snapshot replacement, and cancellation endpoints.
- Add conservative stream correlation and optional bridge-instance headers.
- Introduce priority scheduling, in-flight reuse, adaptive runway, and bounded
  canonical PCM caching.
- Add semantic pause policy and prefix-silence response metadata.
- Add limits, TTLs, diagnostics, and privacy safeguards.

The R2 PR can land without the other two because it is dormant without a
listener. Thorium should feature-detect VoiceLink. VoiceLink can land its API
before Thorium begins sending snapshots.

## 17. Testing Strategy

### 17.1 R2 Navigator tests

- Maps headings, paragraphs, list items, blockquotes, captions, table cells,
  preformatted content, EPUB semantics, ARIA roles, language, and direction.
- Produces correct sentence order and `indexInBlock` flags.
- Derives boundaries correctly across sentences, blocks, and headings.
- Emits bounded current-plus-future snapshots.
- Emits synchronously before the matching `speak()` call and increments revision
  monotonically within a generation.
- Increments generation for seek, voice/rate change, and queue rebuild.
- Emits cancellation on stop/close and uses a new stream ID on restart.
- Produces identical before/after values for every adjacent-unit boundary and
  applies the normative boundary precedence.
- Performs no extra work or behavior change without a listener.

### 17.2 Thorium tests

- Enables forwarding only after compatible capability discovery.
- Adds an exact `synthesisHint` only when token/rate mapping is guaranteed;
  otherwise omits it.
- Tail-truncates to negotiated limits without removing the current unit.
- Omits the snapshot when the current unit alone exceeds a limit.
- Uses the Electron main process and only allowed loopback origins.
- Coalesces or drops obsolete snapshots without blocking playback.
- Applies timeout and retry backoff when VoiceLink is unavailable.
- Sends cancellation on teardown when possible.
- Leaves ordinary TTS unchanged on all adapter failures.

### 17.3 VoiceLink unit and integration tests

- Accepts valid bounded snapshots and rejects malformed, stale, or oversized
  input.
- An identical revision is idempotent; lower/out-of-order revisions and
  same-revision conflicts receive the normative responses.
- A 429 leaves prior state unchanged, and a later newer revision is accepted
  after client backoff.
- Higher generations cancel stale queued work and cannot return stale audio.
- Both snapshot-first and request-first arrival can claim uniquely within the
  observation TTL; zero and multiple matches do not claim.
- Mutual uniqueness is arrival-order independent for simultaneous identical
  readers.
- Correlation normalization never permits reuse unless the exact synthesis key
  also matches.
- The first authoritative request freezes synthesis configuration; later config
  mismatches suspend the binding.
- Bound engine instances advance only on exact expected sequence and text.
- Duplicate request IDs do not double-advance; concurrent different request IDs
  suspend the binding.
- A reserved authoritative lane remains available while speculative work runs,
  and p95 first-byte latency stays within the 5% release gate.
- An authoritative request reuses an identical in-flight or cached job.
- Cache keys include every synthesis-affecting input and exclude structure.
- Cache, queue, stream, and TTL limits are enforced.
- Adaptive runway responds to measured RTF but respects hard caps.
- Pause policy maps semantic boundaries and synthesizes correct prefix silence.
- Miss wait time reduces prefix silence without producing a negative duration.
- Audio-length headers distinguish prefix, speech, and total duration.
- Logs contain no raw book text.
- Old clients and bridge requests continue through the existing path.

### 17.4 End-to-end tests

- Read a long chapter with no skipped, duplicated, or reordered sentence.
- Confirm future synthesis overlaps current playback and later requests hit the
  cache under normal load.
- Seek backward and forward while speculative work is active.
- Change voice and rate during playback.
- Pause and resume without unbounded prefetch growth.
- Use identical sentences in two simultaneous readers and confirm ambiguity
  disables optimization rather than cross-wiring streams.
- Kill or restart VoiceLink and confirm Thorium continues with existing speech
  behavior.
- Compare sentence, paragraph, list-item, heading-to-body, and section pauses in
  listening tests.
- Exercise books with large chapters and resource-heavy layouts to confirm hard
  resource limits.

## 18. Observability and Acceptance Criteria

Useful local metrics are:

- look-ahead snapshots accepted, rejected, stale, and expired;
- correlation outcomes by reason;
- cache and in-flight reuse hit rates;
- current-request first-byte latency;
- synthesis RTF and available runway;
- speculative jobs completed, canceled, and discarded;
- prefix silence selected by boundary kind;
- current and peak cache/queue sizes.

The first release is successful when:

1. Existing playback is behaviorally unchanged when look-ahead is disabled or
   unavailable.
2. With compatible components, the next sentence is normally synthesized before
   the authoritative request arrives on supported hardware where average
   synthesis RTF is below one.
3. No test can produce skipped, duplicated, reordered, or cross-stream speech.
4. Ambiguous correlation always falls back rather than guessing.
5. Paragraph and heading boundaries are audibly distinguishable without adding
   excessive delay after a cache miss.
6. Memory, queued work, and retained text/audio remain within configured bounds.
7. No persistent document identity or raw text is written to diagnostic logs.

## 19. Rollout

1. Land the dormant provider-neutral R2 event and tests.
2. Land VoiceLink capability and look-ahead endpoints behind a disabled-by-
   default experimental setting.
3. Add bridge instance headers, scheduler/cache, and correlation tests.
4. Add the Thorium adapter behind a feature flag and validate fallback paths.
5. Measure first-byte latency, RTF, hit rate, memory, and listening quality on a
   representative book set.
6. Tune default window bounds and `ProsodyPolicy` from evidence.
7. Enable by default only after compatibility and ambiguity tests pass across
   supported Windows and Thorium versions.

## 20. Alternatives Considered

### Queue several Web Speech utterances

Rejected as the primary solution. Chromium retains future utterances and does
not expose their text to the active SAPI engine early enough for VoiceLink
prefetch.

### Send a multi-sentence utterance

Rejected. It changes navigation, cancellation, highlighting, and boundary event
semantics, and makes large synthesis latency worse before playback begins.

### Add only `appendNextSentence`

Rejected. An imperative append stream is fragile under duplicate/dropped
messages, seeks, restarts, and changing window size. A versioned replacement
snapshot is idempotent and self-healing.

### Put VoiceLink HTTP calls directly in R2

Rejected. R2 is a reusable provider-neutral navigator and should not depend on a
specific local service or encounter renderer CORS/CSP concerns.

### Pass structure as fixed pause milliseconds from R2

Rejected. Document semantics are stable across engines; timing preferences and
model behavior are not. VoiceLink owns the policy that converts semantics to
audio treatment.

### Require a stream ID inside SAPI text

Rejected. Injecting control markers risks speaking them, altering synthesis,
breaking normalization, or coupling generic Web Speech content to one provider.
Conservative side-channel correlation preserves the existing contract.

## 21. Open Implementation Decisions

These decisions are intentionally deferred to implementation planning and
measurement because they do not change the architecture:

- final public names for R2 events and TypeScript types;
- default PCM/cache budgets and TTLs other than the normative five-second
  initial-correlation window;
- concrete EWMA constants and runway safety margin;
- default pause durations per semantic boundary;
- feature-flag names and telemetry presentation.

All such choices must preserve the invariant that an optimization miss or
ambiguity produces the same authoritative speech behavior as the existing
system.
