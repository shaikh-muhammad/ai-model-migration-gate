# Experiment 04 preregistration

Status: **PREPARED, NOT RUN**. Preparation is authorized; live execution and the proposed $0.35 budget require later authorization. No Experiment 04 observations exist. This registration precedes all observations.

## Identity and request policy

- Run ID: `experiment-04`; config: `gate.experiment-04.yaml`.
- Gate preparation branch: `experiment/04-preparation`, from local `origin/main` at `0b153dee379fdea165a3d5a384f29048653dcda6`.
- Isolated verifier: `../verifier-experiment-04`, branch `experiment/04-gpt-4.1-mini`.
- Parent verifier: `065cde5889c16ea87267262ce8a89b42fb885a85`.
- Pinned verifier: `7974b3fc3d0e8ba9147c0850f9e5d0d620f3546d`.
- Candidate: `gpt-4.1-mini-2025-04-14`, selected explicitly through `OPENAI_MODEL`. The inherited source fallback default is unchanged; the launch command must supply the pinned model.
- Prompt SHA-256: `af9e1e7e4c3efbe6c25d4a07cab7f8c5dfcaf0798f396134893fae5940d6d996`.
- Corpus SHA-256: `fd1aa6b77276f122930f6d3ad85e05d992c2a67342d0ca6610cc8a8642291245`; tool version: `0.1.0`.
- Endpoint: `http://127.0.0.1:3105/api/verify`.
- Remove the entire reasoning property; output cap: `2048`; SDK timeout: `20000` ms; SDK retries: `0`.
- Preserve Responses API, strict structured-output schema, prompt, original image bytes, data URLs, `detail: auto`, `store: false`, sanitized diagnostics and deterministic verification.
- Effective `GEMINI_API_KEY` must be explicitly empty. The unchanged Gemini helper rejects an empty key before client construction, disabling paid fallback.
- Use a production build, official `OPENAI_BASE_URL=https://api.openai.com/v1`, `OPENAI_LOG=off`, and loopback port 3105. Credentials remain in the operator's environment, never in evidence or commands.

## Independent offline review and hypothesis

SDK 7.27.0 recognizes the exact snapshot in Responses model types. The generated schema is strict, with eleven required root fields and five required nested fields, additional properties forbidden, and basic nullable strings, enums and booleans. The existing SDK accepts image data URLs. These local checks identify no material compatibility blocker; they do not establish current model/account access or provider acceptance. Known model capabilities and pricing were not fetched or independently verified online.

The non-reasoning candidate may provide sufficiently accurate extraction with lower latency. This is an unmeasured hypothesis. Experiment 02 and 03 both reached 10/12 correct but were BLOCKED by p95/max latency of 5639.530 and 6884.823 ms. Their smaller medians do not establish reliable five-second completion. Saved extraction objects are at most 901 compact UTF-8 bytes (979 with local indentation); provider token usage and original output formatting were not saved. A 2048-token cap provides plausible headroom, but free-length schema strings mean future completion is not guaranteed. Reducing a cap does not necessarily accelerate already-short output.

## Frozen evaluation and collection

Use all twelve original cases in corpus order, including the eight critical cases, and the three original baselines in order: `current-baseline-01`, `current-baseline-02`, `current-baseline-03`. Current target, baselines, prompt, schema, cases, scoring matrix and comparison logic remain frozen.

```yaml
max_critical_dangerous_mistakes: 0
min_correct_cases: 10
max_slow_case_seconds: 5.0
max_dangerous_regressions: 0
```

For twelve canonical successes nearest-rank p95 equals the maximum full client HTTP latency; equality at 5000 ms passes. The 20-second SDK timeout and 30-second gate HTTP timeout permit recording slow results without relaxing acceptance.

Collect twelve first-pass attempts, then retry at most two failed cases lacking canonical success, in corpus order, once each: fourteen total verifier dispatches maximum. Do not force, replace or resample a success, including slow or incorrect results. Keep every attempt and the first canonical success. The existing gate CLI performs collection; no controller or paid compatibility probe is added.

`evaluate-once.sh` checks readiness using only GET `/`, validates frozen inputs and fingerprints, then atomically creates `experiments/experiment-04/live-evaluation.started` before any collection. The permanent guard is never removed. After interruption, uncertain dispatch, recording failure or an unexpected runtime/cost condition, stop for review and do not relaunch. Per-invocation caps and this guarded fixed sequence bound the batch; they are not a provider spending cutoff.

Missing or invalid canonical evidence yields INVALID/2. Complete evidence failing any frozen rule yields BLOCKED/1. Only complete evidence passing every rule and the baseline comparison yields PASS/0. Preserve all prior evidence, guards, ledgers and smoke runs. No Experiment 04 manifest, result, catalog entry, release selector or measured decision is created during preparation.

## Proposed conditional budget

Proposed separate ceiling: **$0.35**, including failed or timed-out calls. Use standard uncached input at $0.40/million and output at $1.60/million, at most 46,000 total input tokens per attempt including image/schema, and the enforced 2048 output-token cap. These are conditional assumptions, not verified account pricing, usage or charges. No cache discounts, free failures or Gemini calls are assumed. Do not reuse the previous Luna rates.

```text
per attempt = (46000 * 0.40 + 2048 * 1.60) / 1000000 = $0.0216768
12 attempts = $0.2601216
14 attempts = $0.3034752
headroom under $0.35 = $0.0465248
```

The smaller image-based estimate is $0.1158752 for fourteen attempts if non-image input is at most 10,000 tokens and image input at most 2500 tokens. That allowance uses the known model-specific 1536-patch ceiling and 1.62 multiplier, neither verified online here. The conservative 46,000-token allowance is the operational estimate. Applicable pricing, access, input bounds and available funds remain prerequisites for later authorized execution. Timeouts may be charged. Stop if these assumptions fail; there is no provider-side dollar cutoff.

The pinned verifier patch, offline validation record and later launch instructions are saved alongside this registration. Freeze registration and configuration before observations; record later outcomes separately.
