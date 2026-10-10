# control-01 preregistration: SAME-MODEL CONTROL

Status: **PREPARED, NOT RUN**. This is the single SAME-MODEL CONTROL, not a model migration. Local preparation and commits are authorized; this registration does not authorize provider calls. Any future PASS means this control passed the frozen gate, not that a model migration was approved. No results, catalog entry or release selector are created during preparation.

## Identity and unchanged behavior

- Gate branch: `experiment/same-model-control`, from `869f2066cb453865a06b72e84df645aca4944f8d`.
- Run ID: `control-01`; configuration: `gate.control-01.yaml`.
- Isolated verifier: `../verifier-control-01`, cloned locally from `../verifier-current` with `git clone --no-hardlinks --no-checkout`.
- Verifier branch: `experiment/control-01`.
- Original verifier: `92bef14aa05260a2c7f39aa104a5a202aea099ce`.
- Control verifier: `729323791112f253ae3cac8cbd7933cc2d394bf4`.
- Parent-to-control diff contains only one harmless README.md HTML comment, preceded by a blank line. All executable source is byte-identical to the original verifier.
- Declared model for current and control: `gpt-5.4-mini`. Supply `OPENAI_MODEL=gpt-5.4-mini` explicitly when later launching the control.
- Prompt SHA-256: `af9e1e7e4c3efbe6c25d4a07cab7f8c5dfcaf0798f396134893fae5940d6d996`.
- Corpus SHA-256: `fd1aa6b77276f122930f6d3ad85e05d992c2a67342d0ca6610cc8a8642291245`.
- Tool version: `0.1.0`; endpoint: `http://127.0.0.1:3106/api/verify`.
- Original SDK timeout: `5000` ms; SDK retries: `0`. No reasoning or max_output_tokens property is added. Responses API, strict schema, extraction prompt, image bytes, data URL handling, `detail: auto`, `store: false`, original error handling and deterministic verification remain unchanged.
- No ignored environment files or credentials are copied. Installed dependencies and a production build are not prepared in this task; only fingerprint, syntax and plan-only checks are performed.

## Hypothesis and frozen evaluation

This control measures the unchanged verifier behavior with the same declared model against the saved original baselines. Its documentation-only commit gives the isolated checkout a distinct source identity without changing executable behavior. It cannot establish a successful model migration. The declared model is an alias; fingerprints do not independently attest provider execution or identical provider weights across dates. Current runtime sampling and latency remain unmeasured.

Use all twelve original cases in corpus order, including the eight critical cases, and exactly these original baseline IDs in order:

1. `current-baseline-01`
2. `current-baseline-02`
3. `current-baseline-03`

The current target and its pins, cases, baselines, scoring matrix and comparison logic remain unchanged. Frozen rules are:

```yaml
max_critical_dangerous_mistakes: 0
min_correct_cases: 10
max_slow_case_seconds: 5.0
max_dangerous_regressions: 0
```

For twelve valid canonical responses, nearest-rank p95 is the maximum full client HTTP latency. Equality at 5000 ms passes. The original 5000-ms SDK timeout and 30-second gate HTTP timeout remain separate from acceptance; errors or missing canonical responses make evidence incomplete rather than successful.

## Bounded collection, after later authorization only

Use the existing gate CLI through `evaluate-once.sh`: twelve first-pass selections, then at most two failed-case retries in corpus order, one retry each. Maximum fourteen verifier dispatches. Never force, replace or retry a canonical success, including a slow or incorrect response. Preserve every attempt and the first canonical success.

Before later collection, launch only the pinned control checkout in production on loopback port 3106 with explicit `OPENAI_MODEL=gpt-5.4-mini`, effective `GEMINI_API_KEY=` to disable paid fallback, official `OPENAI_BASE_URL=https://api.openai.com/v1`, `OPENAI_LOG=off`, and telemetry disabled. The unchanged Gemini helper rejects an empty key before client construction. Credentials remain securely in the operator's environment. No paid compatibility probe or warm-up is part of this control.

The script checks readiness by GET `/` only, validates configuration/preregistration byte hashes, frozen inputs and the clean source fingerprint, then atomically creates `experiments/control-01/live-evaluation.started` before collection. This permanent replay guard is never removed. After failure, interruption, uncertain dispatch or recording failure, retain all evidence and stop for review; do not relaunch. Successful results are skipped by the existing runner; no force or collection loop is used. No baseline provider calls occur.

After the bounded sequence, the existing gate evaluates the actual control against all three saved baselines: complete passing evidence yields PASS/0; complete evidence failing a frozen rule yields BLOCKED/1; missing or invalid evidence yields INVALID/2. Every outcome remains labeled SAME-MODEL CONTROL. No outcome establishes model migration approval.

No live budget is specified or authorized by this offline task. Do not inherit Experiment 04's rates, output cap or cost estimate: this control preserves the original uncapped request policy. Provider charges, account access and a later execution budget are unverified. The fixed dispatch bound is not a provider dollar cutoff.

Preserve all existing raw evidence, configurations, ledgers, replay guards, smoke files and PR #2. Freeze this preregistration, configuration and verifier before observations; record future observations separately.
