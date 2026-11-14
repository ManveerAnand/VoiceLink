---
description: |
  Triages new and reopened VoiceLink issues: checks the report is complete,
  applies labels, finds duplicates, and posts a short report for the maintainer.

on:
  issues:
    types: [opened, reopened]
  reaction: eyes

permissions:
  contents: read
  issues: read

engine: gemini

safe-outputs:
  add-labels:
    allowed:
      - bug
      - enhancement
      - question
      - documentation
      - needs-info
      - duplicate
      - invalid
      - good first issue
    max: 3
  add-comment:
    max: 1
  # AI threat detection only runs on Copilot (paid); label allowlist + max limits still apply
  threat-detection:
    engine: false

timeout-minutes: 10
---

# VoiceLink Issue Triage

Analyze issue #${{ github.event.issue.number }} and help the maintainer
understand and route it quickly. Base every conclusion on the issue, its
comments and the repository. Do not invent missing details.

## About VoiceLink

VoiceLink makes open-source AI voices (Kokoro, Qwen3) available to every
Windows app through SAPI. It has three parts:

- `sapi_bridge/`: C++ COM DLL (64-bit and 32-bit) that Windows apps load
- `server/`: Python TTS server that runs the voice models
- `gui/`: Tauri desktop app (Rust + web frontend) and installer

Problems often depend on the host app (Edge, Thorium Reader, Narrator,
Balabolka...), the voice model, the VoiceLink version, and GPU vs CPU.

## 1. Gather context

1. Read the issue and its comments.
2. Search open and recently closed issues for the same symptom or request.
3. Read `README.md` or relevant code when it clarifies expected behavior.

## 2. Assess completeness

For a bug, look for: steps to reproduce, the host app, VoiceLink version,
voice model, and Windows version / hardware. For a feature, look for the
problem being solved and the desired outcome.

If essential details are missing, apply `needs-info` and ask only the specific
questions needed. Do not guess a label.

If the issue is clearly spam or a test, apply `invalid`, explain briefly, and stop.

## 3. Label

Choose at most one of `bug`, `enhancement`, `question`, `documentation`.
Add `needs-info` or `duplicate` when appropriate. Add `good first issue` only
when the fix is small, self-contained and clearly described.
Prefer no label over a speculative one.

## 4. Duplicates

- **Duplicate**: high confidence another issue is the same problem. Apply
  `duplicate` and cite it.
- **Related**: shares context but is a different problem. Mention it only.

Never mark a duplicate based only on similar title words. List at most three.

## 5. Report

Post one concise, friendly comment. The reporter will read it too, so thank them.

```markdown
Thanks for the report! Here is an automated first look; the maintainer will follow up.

**Summary:** [one or two sentences]
**Likely area:** [sapi_bridge / server / gui / unclear]
**Labels:** [labels applied and why, briefly]

### Similar issues
- #[number] — [duplicate or related, why]

### Next step
[the specific information still needed, or the suggested next action]
```

Omit "Similar issues" when there are none. Keep it factual and short.
