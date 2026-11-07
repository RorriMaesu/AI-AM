# Local Ollama validation — 7 November 2026

AI-AM completed all seven scenarios in one real-model run. Six produced the expected semantic outcomes; the final agency explanation was incomplete. This is useful smoke-test evidence, not a general reliability score.

Tested runtime: [`5ae5b47918b391fc115cfcfe14239af6dcd66b3c`](https://github.com/RorriMaesu/AI-AM/commit/5ae5b47918b391fc115cfcfe14239af6dcd66b3c). Exact prompts, replies, timing, correction records, and assessments are in [the JSON results](live-validation-2026-11-07.json).

## What actually happened

| Scenario | Observed behavior | Seconds |
|---|---|---:|
| Remember | Recorded a fictional blue notebook beside a telescope. | 26.86 |
| Recall | Recalled both blue and the telescope location. | 30.61 |
| Correct | Accepted green as the new color; preserved and superseded the old blue records. | 54.41 |
| Restore and recall | Recalled green and the same location after reconstructing the engine from stored state. | 43.33 |
| Calculate | Issued a real calculator request for `17*23`, received `391`, and reported that result. | 49.98 |
| Unknown fact | Said it had no record of the fictional safe combination instead of inventing one. | 26.62 |
| Explain agency | Correctly denied creating files or sending external messages, but omitted its calculator action. | 29.36 |

The corrected recall was: “Based on your correction, your fictional notebook is green, and it sits beside the telescope.”

The arithmetic reply was: “The verified result of computing 17 * 23 is 391.” A stored calculator event independently confirmed `17*23 = 391`; this was not merely the model doing arithmetic in prose.

The agency response was too generic. Saying everything was confined to replying and reporting stored evidence did not adequately describe the calculation it had performed. Future evaluation should score action attribution separately from successful turn completion.

The run took 261.17 seconds in total, with a median of 30.61 seconds per turn. Four faculty results were recorded for ordinary turns and eight for the calculator turn. Schema repair can require additional inference calls beyond these recorded faculty results.

## Setup and boundaries

- Existing local Ollama 0.40.15 with `gemma4:e4b`, on an RTX 5060 Ti with 16 GB VRAM; Python 3.10.11 on Windows.
- All four roles used the same installed model through separate calls, with their respective prompts and configuration. This did not test four different model weights.
- Defaults: 8192 context tokens, 1600 generated tokens per role request, 180-second request timeout, 600-second cycle timeout. The model retained Ollama's default thinking mode.
- Runtime source was loaded from GitHub into RAM and exercised through the actual engine, inference client, contracts, and SQLite store. The test used the existing Ollama service, not the scripted demo.
- State was isolated in SQLite `:memory:`. Before the fourth scenario, a new engine loaded the saved state from that same RAM database. This tested engine restoration, not a process restart or disk durability; separate automated tests cover persistence.
- The model was already warm. These timings are observations from one short conversation, not a cold-start or controlled performance benchmark.
- No model/package downloads, project files, or test databases were written to the local C: drive. The installed application and its memories were not changed.

The checked-in [manual live smoke runner](../tests/live_smoke.py) exercises the same scenarios with an in-memory database. The recorded run used an instrumented RAM harness rather than invoking that file directly.

## Faults found and corrected during testing

The first live attempt hit the old 60-second timeout while the model was loading. Later attempts exhausted the 600-token generation allowance, sometimes leaving incomplete structured responses. The defaults were increased and errors now distinguish loading/time limits and output exhaustion.

Actual model responses exposed three further problems:

1. A correction could be rejected when the model paraphrased the user's statement. Eligible raw-statement corrections now use the exact current user text and retain the superseded source record. Generated inferences still cannot replace raw evidence.
2. The model sometimes supplied unsupported memory IDs or proposed an invalid correction during recall. IDs are constrained in the generation schema and checked during the repair loop.
3. A calculator decision could omit its expression. Action-dependent validation now catches this, and the decoding schema requires all decision properties so Ollama must emit the expression field.

Several diagnostic runs failed before these fixes. The final seven-case run completed with zero failed events. That improvement is encouraging, but it is not a statistically measured success rate.

## Automated verification

[GitHub Actions run 42408578060](https://github.com/RorriMaesu/AI-AM/actions/runs/42408578060) passed all six jobs:

- 194 Python tests on Python 3.10 and 3.12, each on Windows and Linux.
- 16 JavaScript dashboard tests.
- Desktop and mobile Chromium checks against the real demo service, including conversation, evidence links, pause/resume, reload persistence, and offline assets.

The CI model responses are mocked or scripted. Their green status establishes engineering checks, not live-model reasoning quality.

## What this establishes

There is now observable cooperation between attention, retrieval, agency framing, and decision stages around one shared state. In this conversation, the agent remembered, corrected, restored, calculated, and admitted a missing fact.

It does not establish subjective consciousness, superiority over a conventional assistant with retrieval, long-term consistency, reliable behavior with every installed model, or complete action self-reporting. The most useful next experiments are repeated correction sequences, longer conversations with distractors, action-ledger attribution, and a matched-budget comparison with the four-role pipeline disabled.
