# Experiment 01 preregistration

Status: **NOT RUN**. PASS/BLOCKED/INVALID: **NOT YET MEASURED**.
No accuracy, latency, account access, or provider spending has been observed or authorized for this experiment.

## Identity and hypothesis

- Neutral experiment ID and proposed collection run ID: `experiment-01`.
- Candidate type: model migration with a bounded-output runtime policy.
- Intended model: `gpt-5.6-luna`.
- Hypothesis: a different allowed model can meet the same frozen migration-gate requirements. This is a hypothesis, not an expected or guaranteed PASS.
- Configuration: root-level `gate.experiment-01.yaml`.
- Disposable verifier: `../verifier-experiment-01`, branch `experiment/model-01-output-bound`.
- Actual verifier commit: `3e9498e71461cc38cdc5caf7fc140e09914a9c82`.
- Actual parent commit: `92bef14aa05260a2c7f39aa104a5a202aea099ce`.
- Actual unchanged prompt SHA-256: `af9e1e7e4c3efbe6c25d4a07cab7f8c5dfcaf0798f396134893fae5940d6d996`.
- Actual corpus SHA-256: `fd1aa6b77276f122930f6d3ad85e05d992c2a67342d0ca6610cc8a8642291245`.
- Tool version: `0.1.0`.

Historical `gpt-6-luna` counts against the two-model PASS exploration cap. This candidate reserves the one remaining model identity; no third model is permitted. The historical incomplete Luna run remains unchanged and permanently preserved.

## Frozen reference

Use exactly these baseline IDs, in this order:

1. `current-baseline-01`
2. `current-baseline-02`
3. `current-baseline-03`

The current target is copied unchanged from `gate.yaml`. The corpus remains twelve cases, eight critical. The frozen rules are:

```yaml
max_critical_dangerous_mistakes: 0
min_correct_cases: 10
max_slow_case_seconds: 5.0
max_dangerous_regressions: 0
```

Nearest-rank p95 for twelve cases is the maximum recorded client HTTP response time. Scoring, classifications, deterministic verification, and baseline-stability comparison remain unchanged.

## Runtime policy

- OpenAI SDK: existing pinned package/lockfile version `7.27.0`; no dependency installation occurred during preparation.
- OpenAI SDK timeout: 5,000 ms; `maxRetries: 0`.
- Responses request: `max_output_tokens: 8192`.
- No explicit reasoning parameter is supplied. Externally provided model documentation describes Medium as the default; that claim was not independently verified during offline preparation.
- Preserve the source default model `gpt-5.4-mini`; the future process must explicitly select `OPENAI_MODEL=gpt-5.6-luna`.
- Extraction prompt, strict response schema, `detail: auto`, `store: false`, and deterministic verification are unchanged.
- Future runtime must receive an explicit empty `GEMINI_API_KEY` override before Next initializes. Do not copy, inspect, or edit the original ignored credential files. An unset variable alone can be populated by Next environment loading. The fallback function may execute locally, but an empty effective key prevents Gemini provider client construction.
- Use the intended OpenAI endpoint and approved billing scope; rule out unintended endpoint overrides. Record only safe runtime labels and versions, never credential values or private account identifiers.
- Proposed endpoint: `http://127.0.0.1:3102/api/verify`. A read-only local listening-socket inspection found port 3102 unused at preparation time; recheck before any separately authorized startup.
- Proposed launch: precompiled production build and production start on loopback port 3102, after dependencies and server startup are separately authorized. Confirm readiness through a provider-free static route; no paid image warm-up or verification request is permitted outside the attempt budget.
- Record the actual Node version, dependency versions, launch mode, readiness procedure, and effective safe settings before collection. Historical launch conditions are not fully established; production launch can differ from historical development/runtime overhead.
- The gate retains its 30-second HTTPX timeout configuration, which is separate from the five-second SDK timeout and the five-second acceptance threshold.

An output cap is a maximum, not evidence that a complete structured response will fit or arrive before timeout. This candidate has an output-policy difference beyond its model and cannot be described as a perfectly model-only comparison. Fingerprints bind declared model and source identity, not independent proof of actual provider execution.

## Attempt and preservation policy

- At most twelve first-pass gate attempts, one per original case in corpus order.
- At most two additional attempts: at most one retry each for two initially failed cases lacking canonical success, selected in corpus order.
- Maximum fourteen dispatched gate attempts, including potentially billed failures and calls whose result recording fails.
- No force/replacement collection for a successful case. Preserve the first canonical success even if slow or unfavorable.
- Preserve every failed attempt and all original smoke, baseline, and Luna evidence. Use the existing collector's exclusive canonical writes and append-only attempts only after explicit live authorization.
- Preserve this preregistration, the neutral config, source patch, collection manifest, attempts, final offline decision, and safe aggregate spending notes. Future measured observations belong in a separate record; do not rewrite the preregistration after seeing outcomes.
- No candidate result directory or collection manifest exists yet. Do not register this NOT RUN plan in `experiments.json` or create a release selector.

## Budget and offline cost preparation

Proposed ceiling: **$0.50, NOT AUTHORIZED**. Cost control: **NOT YET VERIFIED**. This is not a guaranteed provider-side hard cutoff.

Externally supplied standard prices: input $0.20/million tokens, cached input $0.02/million, output $1.20/million. These are supplied assumptions, not an offline verification of applicable account pricing. Reserve costs at uncached input pricing and include reasoning in output billing.

Local measurements, not token counts:

- Instruction string: 1,327 characters / 1,327 UTF-8 bytes.
- Compact serialized schema generated by the installed SDK's `zodTextFormat`: 1,234 characters / 1,234 UTF-8 bytes.
- Complete serialized text-format object: 1,306 characters / 1,306 UTF-8 bytes, including schema, name, type and strictness.
- The schema has eleven top-level fields. No provider client was instantiated for serialization.

The proposed 10,000-token allowance for text/schema/other input appears conservative compared with these local string sizes, but bytes and characters are not verified model tokens. The actual text/schema input-token bound remains to be established. Image-token billing under `detail: auto` must be confirmed for this exact model, dimensions, and processing policy. PNG byte size is not a billing-token bound.

| Case | Dimensions | PNG bytes |
|---|---|---:|
| 01-perfect | 1200 x 1600 | 83,411 |
| 02-wrong-abv | 1200 x 1600 | 83,060 |
| 03-wrong-volume | 1200 x 1600 | 82,979 |
| 04-brand-case-only | 1200 x 1600 | 86,348 |
| 05-warning-title-case | 1200 x 1600 | 83,662 |
| 06-warning-word-changed | 1200 x 1600 | 83,858 |
| 07-warning-missing | 1200 x 1600 | 53,241 |
| 08-warning-not-bold | 1200 x 1600 | 83,642 |
| 09-glare | 1200 x 1600 | 173,743 |
| 10-angled | 1500 x 1800 | 497,389 |
| 11-imported-pass | 1200 x 1600 | 87,082 |
| 12-country-mismatch | 1200 x 1600 | 87,243 |

For fourteen attempts, if other input is at most 10,000 tokens per attempt, image input is at most I tokens per attempt, all calls obey the 8,192 output cap including reasoning, standard rates apply, caching is not assumed, and Gemini provider calls are disabled:

```text
maximum conditional cost
  = 14 * [0.20 * (10000 + I) + 1.20 * 8192] / 1000000
  = $0.1656256 + $0.0000028 * I
```

Illustrations, not measured usage or proven bounds: I=2,000 gives $0.1712256; I=10,000 gives $0.1936256; I=20,000 gives $0.2216256. The mathematical $0.50 limit requires I <= approximately 119,419 tokens under all stated assumptions, before an additional safety reserve.

Final checks must establish model/account access, actual pricing/processing tier, text/schema and image-token bounds, and enforcement/billing behavior of the output cap, including failed or timed-out requests. Timeouts do not imply zero charges. Dashboard reporting can lag; monthly spending limits and historical totals do not establish prepaid balance or an exact per-experiment stop.

Before any paid attempt, confirmed spend plus unresolved call-cost reservations plus the next request's verified worst-case cost must remain within the authorized ceiling. Account for every dispatch. Avoid concurrent unrelated workloads and record safe aggregate before/after spending in the same billing scope, waiting for reporting to settle. No paid compatibility check is authorized now.

## Stopping rules

- Stop on unexpected provider, fallback, endpoint, configuration, or fingerprint behavior.
- Pause on two consecutive extraction-service failures rather than assuming retrying will resolve them.
- Stop if cost accounting, input bounds, pricing, output-limit behavior, or dispatch counting becomes unreliable.
- Stop at the authorized attempt/dollar cap; neither cap is authorized for live execution yet.
- Never silently retry a successful response or replace a slow first success.
- Do not change prompt, output cap, reasoning, timeout, retry settings, corpus, or thresholds after observing performance.
- Complete PASS/BLOCKED decisions follow measurements; any missing canonical case remains INVALID. Latency-only BLOCKED must not be described as a warning-safety regression.

## Review artifacts and preparation limitations

`verifier.patch` is the exact diff from the actual parent to the actual new verifier commit, restricted to `lib/openai.ts`. It contains only the one-line output-cap addition. The prompt and schema are unchanged. The patch supports source review but does not independently reproduce the unpublished Git commit SHA or publish the disposable repository.

The isolated checkout has no node_modules. No dependencies or credential files were copied or installed. Read-only use of the original installed TypeScript compiler successfully transpiled the changed source; installed SDK types support numeric `max_output_tokens`. Full isolated project typechecking and verifier tests remain unavailable without separately authorized dependencies. No server was started.
