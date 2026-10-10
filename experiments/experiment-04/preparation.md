# Experiment 04 offline preparation record

Status: **READY for a later authorized bounded evaluation; NOT RUN**.

The interrupted preparation was resumed from existing work. Research, the successful verifier tests and production build were not repeated. Gate branch `experiment/04-preparation` remains based on `origin/main` at `0b153dee379fdea165a3d5a384f29048653dcda6`; new gate preparation files are uncommitted. No provider calls, server startup, installation, experiment execution, push or PR creation occurred.

## Pinned verifier and completed work

- Isolated checkout: `../verifier-experiment-04`; branch: `experiment/04-gpt-4.1-mini`.
- Parent: `065cde5889c16ea87267262ce8a89b42fb885a85`.
- One new local commit: `7974b3fc3d0e8ba9147c0850f9e5d0d620f3546d`; working tree clean.
- Production delta is restricted to `lib/openai.ts`: remove reasoning and change output cap from 8192 to 2048. The corresponding existing request tests check the cap, absence of reasoning and explicit pinned model selection. No other tracked verifier files changed.
- Runtime model: `gpt-4.1-mini-2025-04-14`; inherited source fallback default remains unchanged. The launch command explicitly supplies the pinned model and an empty Gemini key.
- Prompt, schema, image bytes/detail, 20-second timeout, zero SDK retries, diagnostics and deterministic verification remain unchanged.
- Existing dependencies were copied locally, without installation or copying credential files.
- Astra's completed checks: 184 targeted mocked-provider tests passed; the production Webpack build completed, including TypeScript, static generation and traces, with empty provider keys and telemetry disabled. The resume verified BUILD_ID, the built verification route, source/build timestamps and a clean verifier; it did not rebuild.

## Fingerprint and essential resume checks

The existing fingerprint calculator produced exactly the config pin:

```json
{
  "declared_model": "gpt-4.1-mini-2025-04-14",
  "prompt_sha256": "af9e1e7e4c3efbe6c25d4a07cab7f8c5dfcaf0798f396134893fae5940d6d996",
  "verifier_commit_sha": "7974b3fc3d0e8ba9147c0850f9e5d0d620f3546d",
  "case_set_sha256": "fd1aa6b77276f122930f6d3ad85e05d992c2a67342d0ca6610cc8a8642291245",
  "tool_version": "0.1.0"
}
```

- Existing config validation confirmed frozen rules, current target and corpus identity. The plan-only gate command selected all twelve cases in corpus order, with no `--yes` and no dispatch.
- Historical evaluation returned 0 and reproduced original Luna INVALID/2, Experiment 01 INVALID/2, and Experiments 02/03 BLOCKED/1.
- The adapted one-shot script passed `bash -n`, compilation of its three embedded Python blocks and structural inspection. It has exactly two collection commands capped at 12 and 2, the correct config/run/port, exact config and preregistration byte hashes, fingerprint validation, and atomic creation of a permanent Experiment 04 guard before collection. It has no force flag, shell collection loop, guard deletion or baseline dispatch. Successful canonical results are skipped by the existing gate runner; only failed cases lacking canonical success can be selected for the retry invocation.
- The script was not executed. Experiment 04 results, decision, manifest and started guard do not exist. The guard is created only when later collection begins.
- All 235 pre-existing preservation-snapshot file hashes matched, including raw evidence, ledgers, prior scripts and smoke results. Prior replay guards remain intact. Local demo branch remains `d913aaccccd9cfb51fb6d3a4c2ecf56ee0aba7ce`; GitHub PR #2 was not accessed or changed.
- Exact decimal cost arithmetic verified $0.0216768 per attempt, $0.2601216 for twelve, and $0.3034752 for fourteen, with $0.0465248 headroom under the proposed $0.35 ceiling.

## Remaining execution prerequisites

No preparation item remains missing. READY does not establish model/account access, current pricing, actual usage, actual spending, server readiness, five-second completion or 10/12 extraction accuracy. Live execution and its proposed budget need later authorization and the preregistered assumptions must hold. Output truncation remains possible because schema strings are unbounded. The budget is conditional, not a provider-side cutoff.

Exact later launch and one-shot commands are in `runbook.md`. Keep the preregistration and config frozen; subsequent observations must be recorded separately.
