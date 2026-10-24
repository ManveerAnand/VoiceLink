# Adaptive TTS Look-Ahead and Prefetch Design

**Status:** Proposed for implementation

**Date:** 2026-10-24

**Primary project:** VoiceLink

**Integration project:** `edrlab/r2-navigator-js` (consumed by Thorium Reader)

## Intent and rationale

VoiceLink currently introduces a pause at every Thorium read-aloud boundary. Thorium submits one sentence to the Web Speech API, waits for that utterance to end, and only then submits the next sentence. VoiceLink cannot synthesize the next sentence during current playback because SAPI exposes only the text in the current `ISpTTSEngine::Speak` call.

The current VoiceLink server adds a second latency source: `/v1/tts` collects every Kokoro PCM chunk before returning HTTP headers or audio. On the reference CPU installation, measured time to first byte was 1.11 seconds for 12 characters, 1.85 seconds for 39 characters, and 7.24 seconds for 232 characters. Time to first byte was effectively equal to total request time.

The intended result is continuous read-aloud: the current segment starts as soon as it is ready, while a bounded background producer prepares future segments. Text highlighting, cancellation, navigation, and compatibility with existing SAPI clients must remain correct.

## Goals

- Remove synthesis-sized gaps between consecutive Thorium sentences when the model can generate faster than playback consumes audio.
- Let the text owner, Readium, provide a bounded look-ahead window rather than asking VoiceLink to guess future text.
- Separate synthesis from playback through a bounded producer/consumer pipeline.
- Preserve accurate SAPI word and sentence events using the exact PCM length of each generated segment.
- Adapt the buffered audio runway to observed model throughput instead of relying only on a fixed sentence count.
- Cancel stale work promptly after stop, seek, page navigation, voice change, rate change, or a new read-aloud session.
- Retain the existing `/v1/tts` contract for old VoiceLink bridges and non-look-ahead clients.
- Structure the work so the VoiceLink and Readium changes can be reviewed independently and submitted upstream.

## Non-goals

- Pre-rendering a whole book or chapter.
- Predicting text that the reader has not supplied.
- Running multiple concurrent Kokoro inference calls against the same model instance.
- Providing phoneme-level alignment. Word timing remains an estimate within an exact segment duration.
- Replacing SAPI or the Web Speech API with a VoiceLink-only media player.
- Solving sustained playback for a model whose effective real-time factor is at or above 1.0. Buffering can absorb variation, but it cannot make an unsustainable producer keep up indefinitely.
- Making Qwen3 use this pipeline in the first implementation. The protocol should not prevent later adoption, but Kokoro/Thorium is the initial target.

## Anti-goals

The change is considered a failure if it:

- silently skips or repeats text at batch boundaries;
- lets highlighting advance independently of the audio presentation position;
- generates an unbounded second audiobook in RAM or on disk;
- keeps synthesizing an old location after the user navigates elsewhere;
- requires all SAPI clients to adopt the new protocol simultaneously;
- reduces stop/skip responsiveness compared with the current bridge;
- fixes latency by removing boundary events or materially degrading Thorium navigation.

## Constraints and findings

### Ownership of future text

Readium constructs a document-wide TTS queue and flattens sentence fragments into global queue indexes. Its current player creates one `SpeechSynthesisUtterance` for the current global item and calls the next item only from `utterance.onend`.

Chromium also serializes Web Speech utterances: additional calls to `speechSynthesis.speak()` are stored in Chromium's queue while a native utterance is active. Chromium does not forward all queued utterances to the Windows voice in advance.

SAPI calls `ISpTTSEngine::Speak` with only the `SPVTEXTFRAG` list belonging to the current input stream. Consequently, no VoiceLink-only implementation can pre-render Thorium's next sentence unless Readium first includes that sentence in the active native utterance or uses a separate provider API.

### Existing VoiceLink timing contract

The server currently pre-generates all PCM to produce `X-Audio-Length`. The bridge uses that total length to distribute word-boundary events proportionally across the entire input. This improves total-duration knowledge at the cost of delaying every byte.

The exact total length of the whole look-ahead batch is unnecessary. Exact length per segment is sufficient and produces a better estimate: each word is placed proportionally within its own sentence or clause rather than across an entire multi-sentence passage.

### Reference designs

- `readio` uses separate render and playback threads, a bounded queue, a hard sentence cap, generation epochs for cancellation, a content-addressed cache, and an audio-runway target adjusted by an EWMA of real-time factor.
- `paperreader` generates and caches sentence clips ahead of playback and derives word timings from actual clip duration.
- `PageCast` uses a bounded duration target, limited concurrent preparation, caching, and cancellation of obsolete work.
- `readaloud` pre-generates future chunks and keeps canonical cached audio independent of playback tempo.

These systems share one invariant: the document owner supplies future text to a bounded producer before the player needs it.

## Approaches considered

### 1. Restore streaming in VoiceLink only

Return a `StreamingResponse` directly from the Kokoro generator and omit the whole-response `X-Audio-Length`.

**Advantages**

- Smallest VoiceLink change.
- Improves time to first audio for clients that submit several sentences or a paragraph.

**Trade-offs**

- Thorium normally submits one sentence, so there is no next sentence to generate.
- A Kokoro chunk is approximately a sentence; a one-sentence request still waits for that sentence's inference.
- Whole-utterance timing falls back to a rough estimate.

**Decision:** retain as a compatibility improvement where practical, but reject as the complete solution.

### 2. Combine a fixed number of Readium queue items

Readium could concatenate the next three to five sentences into one Web Speech utterance. VoiceLink would receive the larger input and stream Kokoro's chunks.

**Advantages**

- Avoids a custom document-provider API.
- Relatively small first prototype.

**Trade-offs**

- Sentence length and synthesis speed vary widely, so a count is a poor buffer measure.
- Boundary-to-DOM mapping, skip, previous/next, and cancellation must still be redesigned.
- Raw PCM streaming does not carry exact per-segment text and audio metadata.

**Decision:** useful as a spike, but reject as the final algorithm.

### 3. Batched Readium utterance plus an adaptive VoiceLink synthesis session

Readium supplies a bounded batch with a mapping back to its original queue items. The SAPI bridge creates a short-lived server session. The server returns one exact segment at a time and prepares later segments in the background. The bridge consumes segments in order and queues exact per-segment SAPI events.

**Advantages**

- Gives the synthesizer the future text it actually needs.
- Keeps the standard Web Speech and SAPI playback path.
- Provides explicit cancellation, exact segment metadata, backpressure, bounded memory, and compatibility.
- Allows model throughput to determine the runway.

**Trade-offs**

- Requires coordinated changes in VoiceLink and Readium.
- Requires stateful, short-lived local sessions.
- Readium must map boundary indexes for a combined utterance back to original DOM ranges.

**Decision:** selected. This is the only approach that addresses both ownership of future text and a robust bounded generation pipeline.

## System architecture

```text
Readium document TTS queue
        |
        | builds a same-language look-ahead batch and span map
        v
SpeechSynthesisUtterance(batch text)
        |
        | Chromium forwards the single active utterance
        v
VoiceLink SAPI bridge
        |
        | POST session, then request ordered segments
        v
VoiceLink session coordinator
        |
        +--> urgent current-segment job
        +--> lower-priority prefetch jobs
        v
single Kokoro model worker --> bounded ready queue/cache
        |
        | exact segment metadata + PCM
        v
SAPI bridge queues boundary events, then writes paced PCM
        |
        v
Thorium receives boundary events and maps charIndex to DOM spans
```

The document queue, synthesis queue, and playback queue are distinct:

- Readium owns textual order and navigation.
- The VoiceLink server owns inference scheduling and cached PCM.
- SAPI owns presentation and reports playback-position events to Readium.

## Readium look-ahead batching

### Batch construction

Starting at global queue index `N`, Readium collects subsequent `ITtsQueueItemReference` values until the first applicable limit:

- language or direction changes;
- document boundary;
- configured hard sentence cap;
- configured hard character cap;
- estimated audio-runway cap.

The initial hard limits are implementation safeguards, not tuning claims. Recommended starting points are six sentence items, 1,200 characters, and approximately 30 seconds of estimated speech. Benchmarks and tests may lower them.

Readium creates:

```text
TtsLookAheadBatch
  startGlobalIndex
  endGlobalIndexExclusive
  combinedText
  spans[]
    globalIndex
    combinedStartUtf16
    combinedLengthUtf16
    queueItemReference
```

Separators inserted between items are included in the span accounting. UTF-16 units are used because Web Speech `charIndex` and SAPI text offsets are based on the Windows/JavaScript string representation.

Separators are explicit non-item spans rather than being assigned to either neighboring item. Batch construction inserts one ASCII space only when the existing boundary does not already contain whitespace. A boundary event inside a separator span is ignored and the current highlight remains in place until an event reaches the following item. The bridge's word parser must not normally emit an event for whitespace, but representing separators explicitly keeps binary-search and local-offset behavior defined for malformed or platform-specific events.

### Highlighting and navigation

On a word boundary, Readium binary-searches `spans` for the reported `charIndex`, switches the active queue reference when necessary, converts the index to the item's local offset, and uses the existing highlight path.

Normal `onend` advances to `endGlobalIndexExclusive`, not `N + 1`.

Explicit next/previous, click-to-read, seek, stop, pause-restart, language change, or voice change cancels the active Web Speech utterance and constructs a fresh batch at the requested global index. A manual next command advances one logical queue item, not one whole batch.

### Activation and upstream compatibility

The `r2-navigator-js` API gains a configurable look-ahead policy. The default remains one item until the multi-item boundary mapping is validated across supported platforms. The host can select `adaptive` or a numeric hard cap.

Thorium adoption is a small integration step after the navigator change: update the dependency and enable the policy for voices/configurations where boundary events are available. If upstream maintainers do not want a new UI control, the navigator option can initially be enabled by Thorium configuration while retaining the current default for other consumers.

This means there are two substantive implementations and potentially a third, small adoption PR in Thorium itself. The navigator change remains the correct upstream home for queue batching.

## VoiceLink session protocol

The existing `POST /v1/tts` remains supported. A versioned session API is added for bridges that advertise look-ahead capability.

### Create session and obtain the first segment

```text
POST /v1/tts/sessions
Content-Type: application/json

{
  "text": "combined look-ahead text",
  "voice": "af_heart",
  "speed": 1.0,
  "format": "pcm_24k_16bit",
  "protocol": 1
}
```

The response body is the first segment's PCM. Response headers include:

```text
X-VoiceLink-Protocol: 1
X-VoiceLink-Session: <opaque id>
X-Segment-Index: 0
X-Segment-Text-Start-UTF16: <offset>
X-Segment-Text-Length-UTF16: <length>
X-Segment-Audio-Length: <PCM bytes>
X-Segment-Flags: sentence-start[,sentence-end][,final]
```

Returning PCM in the creation response avoids a second round trip before initial playback. The response is sent as soon as segment zero is complete; it does not wait for future segments.

### Obtain subsequent segments

```text
GET /v1/tts/sessions/{session_id}/segments/next
```

The request waits until the next ordered segment is ready, fails, is cancelled, or reaches a bounded timeout. It returns PCM with the same segment headers. End-of-session is an explicit `204 No Content` or a `final` segment followed by idempotent `204`; the implementation plan must select and test one consistent rule.

Local HTTP round-trip cost is negligible compared with inference and gives the server an acknowledgement boundary: requesting segment `K + 1` means the bridge has finished submitting segment `K` to SAPI.

### Cancel session

```text
DELETE /v1/tts/sessions/{session_id}
```

Cancellation is idempotent. Sessions also expire by TTL so a crashed SAPI host cannot leak memory or work indefinitely.

The lease is refreshed whenever a segment request begins or completes. A configurable idle TTL, initially 30 minutes, bounds abandoned paused sessions. If a generic SAPI client remains paused beyond the lease and later resumes, the bridge finishes any already accepted current-segment write, recreates a session for the first not-yet-submitted segment, and does not replay the current segment. Thorium's existing pause path cancels the active Web Speech utterance, so its resume naturally creates a new batch instead of depending on a long-lived server session.

### Errors

- Failure before the first response uses the existing HTTP error contract.
- Failure during a session is returned by the next-segment request with a stable error code and message.
- The bridge aborts the active SAPI `Speak` call on a terminal session error.
- Readium stops read-aloud, surfaces the error, and retains the first unplayed logical queue item as the retry position. It neither skips that item nor automatically replays an ambiguously consumed segment.
- If session creation is unsupported, the bridge falls back to `/v1/tts` without changing registered voice compatibility.

## Segmentation and exact event timing

The server segments the original request without losing original character ranges. It prefers sentence-final punctuation, then clause punctuation, then whitespace before a model/token safety limit. It never splits a UTF-16 surrogate pair.

Each segment records:

- Python source slice;
- source start and length converted to UTF-16 code units;
- whether it starts or ends a logical sentence;
- exact generated PCM byte length;
- synthesis wall time and audio duration.

Long sentences are allowed to become several inference segments. Only the first segment carries `sentence-start`; the bridge continues word events through later segments without inventing new sentence boundaries.

Before writing a segment's PCM, the bridge queues its SAPI events. Word offsets are distributed proportionally within that exact segment's PCM length and added to the cumulative audio byte offset. This obeys SAPI's requirement that events be queued before their associated audio and improves on whole-utterance proportional timing.

## Producer, scheduler, and backpressure

### Single model actor

Kokoro inference is owned by one long-lived worker/actor. Requests are serialized through it; multiple HTTP handlers never invoke the same model concurrently.

Jobs have two priorities:

1. `immediate`: the segment blocking audible playback;
2. `prefetch`: future segments in an active session.

Fairness prevents one long batch from starving a newly started or newly sought session. At segment boundaries the scheduler checks for newer immediate work before taking another prefetch job.

### Session state

```text
CREATING -> ACTIVE -> DRAINED -> CLOSED
                 \-> CANCELLED
                 \-> FAILED
```

Every session has a monotonically increasing generation/era token. Stop or replacement invalidates the old era before attempting model cancellation. Results produced for a stale era are discarded and never enter the ready queue.

Sessions are independent per client `Speak` call. Creating a session never globally supersedes sessions belonging to another SAPI object, process, or application. Replacement invalidates only the session owned by that bridge call. The model worker and priority scheduler are global to the server and arbitrate fairly across these independent sessions.

### Adaptive runway

For every cache miss, record:

```text
sample_rtf = synthesis_wall_seconds / generated_audio_seconds
```

Maintain a clipped EWMA per effective model/voice/speed identity:

```text
rtf = 0.75 * previous_rtf + 0.25 * sample_rtf
utilization = rtf
target_runway = base_runway / clamp(1 - utilization, min_headroom, 1)
```

`target_runway` is clamped by configuration and hard resource limits. A reasonable initial policy to validate is:

- base runway: 8 seconds;
- target range: 8 to 30 seconds;
- hard ready-segment cap: 6;
- hard PCM byte cap derived from 30 seconds at 24 kHz, 16-bit mono;
- minimum headroom: 0.2.

These are benchmark inputs, not immutable product values. Tests assert boundedness and adaptation, not a magic exact constant.

If `utilization >= 1`, the system fills to the hard cap but reports that continuous playback is not sustainable at the selected model/rate. It does not allocate without bound.

The producer may start another segment only while the session is current and projected ready audio is below the target and all hard caps permit it. The next segment's duration is estimated from recent characters-per-audio-second until exact PCM exists.

### Backpressure

The next-segment request is the primary consumption acknowledgement. The server retains only the bounded ready queue. The bridge writes PCM in small chunks and checks `SPVES_ABORT`, `SPVES_SKIP`, rate, and volume actions between writes. It must not fetch unlimited future segments into the DLL.

## Cache design

Generated canonical PCM is keyed by a versioned digest of:

```text
model implementation/version
voice
synthesis speed
normalized exact segment text
audio format
segmentation protocol version
```

Unlike players that apply tempo after synthesis, VoiceLink passes speed into Kokoro, so speed is part of the cache identity.

The first implementation may use an in-memory LRU to minimize scope. A disk cache is optional later. Both entry count and total bytes are bounded. Partial, failed, or stale output is never cached.

Cache hits do not update model RTF because they contain no inference measurement.

## Cancellation and control behavior

- **Stop or purge:** invalidate the session era, issue `DELETE`, stop requesting segments, and return promptly from `Speak`.
- **Skip/click navigation:** acknowledge SAPI skip as today, cancel the session, and let Readium start a new batch at the chosen queue item.
- **Reader-originated voice change:** Readium cancels the active utterance and restarts from the active logical queue item using the new voice. The old bridge call only cancels its own session; it does not attempt to synthesize a suffix for a different voice.
- **Reader-originated rate change:** Readium follows the same cancel/restart rule so text ownership and the restart position remain with the reader.
- **SAPI runtime rate action without a replacement `Speak`:** finish the already-started PCM segment at its old rate, cancel prefetched suffix segments, and create a replacement session for the first not-yet-submitted text range at the new rate. Never restart a partially submitted segment. Volume changes remain local PCM scaling and do not invalidate synthesized audio.
- **Pause:** preserve the current session and bounded queue within its idle lease. Do not expand the runway while paused. Expiry recovery starts at the first not-yet-submitted segment as defined by the session lease rule.
- **Client crash:** TTL cleanup cancels pending work and releases PCM.
- **Server restart:** the bridge treats the lost session as a terminal request failure and may use its existing retry/fallback policy for a newly created session; it never replays an ambiguous partially consumed segment.

## Compatibility and rollout

1. Add tests and the session API behind protocol version 1 while leaving `/v1/tts` unchanged.
2. Add bridge support with automatic capability detection and fallback.
3. Validate VoiceLink batching with a small Readium spike.
4. Implement full Readium span mapping, navigation, and cancellation.
5. Submit the VoiceLink PR independently with generic multi-segment clients and integration tests.
6. Submit the navigator PR; then update Thorium's dependency/configuration in a small adoption PR if required by its release process.

Old server plus new bridge and new server plus old bridge must continue to speak through `/v1/tts`.

## Observability

Structured debug logs should expose enough data to diagnose gaps without logging full book text:

- session ID and era;
- segment index and UTF-16 range;
- cache hit/miss;
- synthesis milliseconds, audio milliseconds, sample RTF, EWMA RTF;
- ready runway seconds and queue count;
- time from request to first PCM;
- time the bridge waits for each next segment;
- cancellation reason and stale-result discards;
- underrun count and duration.

Text is represented only by length and a short digest in normal logs.

## Verification strategy

### VoiceLink server tests

- deterministic segmentation with ASCII, punctuation, paragraphs, emoji, combining characters, and surrogate pairs;
- session state transitions and idempotent cancellation;
- first response does not wait for the second synthetic segment;
- second segment is generated while a fake consumer plays the first;
- immediate work preempts queued prefetch work at a segment boundary;
- adaptive runway grows for slower RTF and faster consumption but never crosses hard caps;
- pause does not expand prefetch;
- stale-era results are discarded;
- cache keys include every synthesis-affecting input;
- errors after segment zero are observable and do not corrupt subsequent sessions;
- legacy endpoint behavior remains covered.

### Bridge tests

- protocol negotiation and legacy fallback;
- exact parsing of segment headers;
- cumulative audio offsets across segments;
- events queued before corresponding PCM;
- partial HTTP reads and malformed lengths;
- abort/skip while waiting, receiving, and writing;
- x64 and Win32 builds;
- bounded local buffering.

### Readium tests

- batch construction limits and language/document boundaries;
- UTF-16 span mapping, including emoji;
- boundary events crossing from one original queue item to the next;
- normal completion advances by the batch size;
- next/previous move one logical item;
- click-to-read and seek cancel/restart at the correct index;
- no skipped or duplicated text after cancellation races;
- single-item mode remains behaviorally unchanged.

### End-to-end acceptance

- Thorium reads a multi-paragraph EPUB continuously with Kokoro on the reference CPU system.
- After the first sentence starts, median inter-sentence dead air is below 150 ms when observed RTF is comfortably below 1; the test records underruns rather than hiding them.
- Highlighting selects the correct sentence and word throughout a batch.
- Stop and click-to-seek take effect within the existing responsiveness budget.
- Memory and cache remain within configured caps during a long chapter.
- A deliberately slow fake model demonstrates bounded, reported underruns without unbounded queue growth.

## Expected code areas

### VoiceLink

- `server/routers/tts.py`: versioned session endpoints and response metadata.
- `server/models/base.py`: a segment-oriented synthesis contract or adapter.
- `server/models/kokoro_model.py`: range-preserving segmentation and per-segment metrics.
- new server session/scheduler/cache modules with focused responsibilities.
- `sapi_bridge/src/http_client.*`: session lifecycle and per-segment requests.
- `sapi_bridge/src/tts_engine.cpp`: segment event mapping, paced writes, cancellation, and fallback.
- new Python tests and C++ protocol/unit tests where the build permits.

### Readium/Thorium

- `r2-navigator-js/src/electron/renderer/webview/readaloud.ts`: batch lifecycle and boundary routing.
- `r2-navigator-js/src/electron/renderer/common/dom-text-utils.ts`: look-ahead batch/span helpers.
- public read-aloud configuration types and tests.
- Thorium dependency/configuration adoption if the navigator option is not enabled by default.

## Builder discretion

The implementation plan may choose module names, concrete synchronization primitives, test libraries already compatible with each repository, and whether protocol headers use the exact names shown here. It must preserve the protocol semantics, UTF-16 ranges, boundedness, compatibility, and state transitions defined above.

The implementation plan should begin with a narrow VoiceLink protocol and scheduler slice using fake synthesis, then connect Kokoro, then update the bridge, and only then implement Readium batching. This keeps each failure attributable and allows the server/bridge behavior to be tested without Thorium.

## Source references

- Readium queue playback: <https://github.com/edrlab/r2-navigator-js/blob/develop/src/electron/renderer/webview/readaloud.ts>
- Readium queue flattening: <https://github.com/edrlab/r2-navigator-js/blob/develop/src/electron/renderer/common/dom-text-utils.ts>
- Chromium TTS queue: <https://github.com/chromium/chromium/blob/main/content/browser/speech/tts_controller_impl.cc>
- Microsoft SAPI `SPVTEXTFRAG`: <https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ms720504(v=vs.85)>
- Microsoft SAPI engine guide: <https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ms717037(v=vs.85)>
- readio producer/consumer implementation: <https://github.com/hrhrng/readio/blob/main/apps/tui/src/voice/mod.rs>
- paperreader: <https://github.com/dzimmerer/paperreader>
- PageCast: <https://github.com/flabanabba8/PageCast>
- readaloud: <https://github.com/YareyareSenpai/readaloud>
