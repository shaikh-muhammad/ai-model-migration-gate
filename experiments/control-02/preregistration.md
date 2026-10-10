# control-02 preregistration

**policy v2 calibrated from observed noise; control, not a migration**

Status: PREPARED, NOT RUN. Gate branch: `experiment/control-02-policy-v2`, from main merge `b63d4744f83055e6554dc95fd9b64f85616ce06f`. Run ID: `control-02`; config: `gate.control-02.yaml`. This registration precedes control-02 observations and does not authorize live execution or a dollar budget.

## Calibration disclosure

The four observed same-model correct counts are **10, 10, 10, 9**: `current-baseline-01`, `current-baseline-02`, `current-baseline-03`, then `control-01`. Calibration uses observed results, not independent validation. Reducing the minimum to 9 accommodates the single observed flipped case; it does not establish a noise distribution, future stability, safety, or migration approval. Existing decisions are not rescored or relabeled. Any future PASS is a calibrated same-model control result only.

## Fixed identity and evaluation

Reuse `../verifier-control-01` at `729323791112f253ae3cac8cbd7933cc2d394bf4`, port 3106, model `gpt-5.4-mini`, original 5000-ms SDK timeout, zero retries and unchanged extraction prompt. Prompt SHA-256: `af9e1e7e4c3efbe6c25d4a07cab7f8c5dfcaf0798f396134893fae5940d6d996`. Corpus SHA-256: `fd1aa6b77276f122930f6d3ad85e05d992c2a67342d0ca6610cc8a8642291245`; tool version `0.1.0`. No verifier changes, reasoning parameter or output cap are introduced.

All twelve original cases, images, expectations, scoring and comparison logic are unchanged. Compare against exactly `current-baseline-01`, `current-baseline-02`, `current-baseline-03`, in that order. The sole configuration difference from control-01 is `min_correct_cases: 9` instead of 10. Critical dangerous mistakes and dangerous regressions remain limited to zero; p95 remains limited to 5000 ms. For twelve complete cases nearest-rank p95 is the slowest case. `gate.yaml` and historical configurations retain the original threshold of 10. Release approval remains unselected; PR #2 remains unchanged.

## One-shot collection after separate live authorization

The adapted existing script permits no arguments, force or replay. It checks the unstarted run, GET-only listener readiness, config and preregistration byte hashes, exact config invariance and a clean matching verifier fingerprint. It atomically creates `experiments/control-02/live-evaluation.started` before collection. Never remove that permanent guard or relaunch after failure or interruption.

Maximum twelve first-pass dispatches plus two failed-case retries, one per case in corpus order, fourteen total. Canonical successes, including incorrect or slow responses, are never retried or replaced. Preserve every attempt and outcome. Stop for review on transport failure, incomplete recording, interruption or uncertain execution. The existing gate check reports actual PASS/0, BLOCKED/1 or INVALID/2 under this explicitly calibrated control config; no result approves a migration.

No server is started during preparation. Later runtime must use the pinned checkout, explicit `OPENAI_MODEL=gpt-5.4-mini`, empty `GEMINI_API_KEY` to disable fallback, official OpenAI endpoint, logging off and telemetry disabled. Do not copy credentials or run a paid warm-up. There is no live spending authorization in this offline task. The original request has no explicit output cap; fourteen dispatches is not a dollar cutoff.

Preserve all historical evidence, ledgers, replay guards and smoke files. Do not add a release selector or catalog a result before observations.
