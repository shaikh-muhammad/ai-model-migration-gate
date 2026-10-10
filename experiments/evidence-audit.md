# Saved evidence acceptance audit

**Migration PASS available: NO.** Experiments 02 and 03 provide genuine complete, comparison-backed BLOCKED demonstrations. No saved candidate provides a complete migration PASS under the frozen five-second gate.

## Acceptance requirements and sources

This audit uses the repository's recorded acceptance contract: `gate.yaml` as frozen in baseline commit `6770682`, the decision/scoring and fingerprint contract in `README.md` at `83c90d4`, and Experiment 01's pre-observation preregistration. No separate original user brief is stored in the repository. The documented difference between that brief and the frozen Expected Needs Review + Actual Fail classification remains unchanged.

A final migration PASS requires twelve valid canonical successes with matching five-field fingerprints, exactly the three frozen current baselines in order, all four evaluated rules and a complete comparison. The frozen thresholds are zero critical dangerous mistakes, at least ten correct cases, nearest-rank p95 client latency at most 5000 ms, and zero dangerous regressions. For twelve cases, p95 is the maximum. A model migration must differ from the current model: the current-model baselines and a hypothetical no-change control do not demonstrate successful migration.

The original software acceptance gap was genuine PASS and BLOCKED evidence, rather than synthetic test outcomes or green CI. BLOCKED is now demonstrated; PASS remains missing. The audit changes no policy, classifications, expectations, timings or raw evidence and uses only the existing offline evaluator.

## All saved real runs

| Run | Canonical / corpus | Attempts / HTTP errors | Correct | P95 ms | Finding |
|---|---:|---:|---:|---:|---|
| current-baseline-01 | 12/12 | 12/0 | 10 | 3419.673 | Absolute thresholds pass; current-model control |
| current-baseline-02 | 12/12 | 12/0 | 10 | 2688.779 | Absolute thresholds pass; current-model control |
| current-baseline-03 | 12/12 | 12/0 | 10 | 2898.546 | Absolute thresholds pass; current-model control |
| current-smoke-20261007-001 | 1/12 | No saved attempt history | — | — | No manifest; incomplete developmental evidence |
| current-smoke-20261007-002 | 1/12 | No saved attempt history | — | — | No manifest; incomplete developmental evidence |
| candidate-smoke-01 | 1/12 | 1/0 | — | — | Incomplete developmental evidence; excluded from release |
| candidate-official-01 | 11/12 | 14/3 | — | — | INVALID/2; missing 09-glare |
| experiment-01 | 7/12 | 12/5 | — | — | INVALID/2; missing 02, 03, 09, 10, 11 |
| experiment-02 | 12/12 | 12/0 | 10 | 5639.530 | BLOCKED/1; latency rule fails |
| experiment-03 | 12/12 | 12/0 | 10 | 6884.823 | BLOCKED/1; latency rule fails |

Incomplete runs have no complete aggregate metrics. Every available canonical record equals its first successful saved attempt. No case was imported from another run. All manifest-backed runs match their original declared pins; current/candidate source fingerprints recomputed from the existing clean verifier checkouts also match their configured pins. The two current smoke runs lack manifests and cannot establish fingerprint validity.

Experiment 02 has one minor and one false block, with no dangerous mistakes or regressions. Its slow cases are 09-glare (5639.530 ms) and 10-angled (5109.447 ms). Experiment 03 has two minor cases, with no false blocks, dangerous mistakes or regressions. Its slow case is 07-warning-missing (6884.823 ms). Both pass all three non-latency rules. Existing saved decision JSON for both complete experiments reproduces exactly.

These observations do not establish the cause of historical 502s or pure reasoning-policy causality. Source identity is not independent provider attestation. Reported budgets are conditional reservations, not observed provider charges.

## Minimal catalog

`experiments.json` contains exactly three entries under the existing evaluator schema: the neutral configuration/run pair, kind, evidence digest and full verified observed decision. Experiment 01 is exploratory INVALID; Experiments 02 and 03 are official measured BLOCKED demonstrations. Official status does not mean approved migration. The original Luna remains independently checked by the evaluator's existing historical assertion; smoke/control runs are not cataloged as migrations.

| Run | Verifier commit | Evidence SHA-256 |
|---|---|---|
| experiment-01 | 3e9498e71461cc38cdc5caf7fc140e09914a9c82 | f7c0ab8183ca2fcd261b73f9caf2f72507234f92424b6dd36d023889fc80b679 |
| experiment-02 | 718808b1fffc07d7e565c959fbe650bb7b02dd1d | 29c1d9712a7bb11596e75ec6662d9351a3832c5ce0e4af11bb823f44eb5a0e24 |
| experiment-03 | 065cde5889c16ea87267262ce8a89b42fb885a85 | 0fccf9793febba9b5a2427dcbcb0b3fe011e33158d88126b0422a16c73d4092c |

The digest uses the existing `evidence_digest`: sorted relative filenames and SHA-256 file bytes for the complete candidate run directory, including its manifest, canonical responses and every attempt. It does not substitute for the separately preserved accounting files or guards.

## Release evaluator verification

The existing release evaluator accepts only `gate.pass.yaml` or `gate.blocked.yaml` selectors. Each BLOCKED run was independently checked with `evaluate_saved.main(['release'], root=view)` in a temporary `/tmp` view. The view contained byte-identical copies of the existing corpus, baseline evidence and candidate evidence; all evidence digests were checked against the original directories.

For each view, the selected neutral configuration was copied unchanged to `gate.blocked.yaml`, its catalog pair used that alias, and a selector named the original run ID. The evaluator returned **BLOCKED/1** and its full output equaled the original saved decision for Experiment 02, then Experiment 03. No result identities or raw files changed. These are copies of genuine observations, not synthetic test cases, new experiments or new model observations. Temporary selector/configuration aliases were removed with their views; no root release selector or outcome-labeled configuration was created.

Local historical verification reproduced INVALID/2, BLOCKED/1 and BLOCKED/1 from the catalog. The 206 existing targeted tests for saved evaluation, configuration invariants, release CI and final checks passed. No evaluator, workflow, scoring code or tests were edited.

## Preservation and remaining blockers

All pre-existing raw results, corpus files, configurations, preregistrations, verifier patches, ledgers, replay guards, implementation files, tests and workflows were preserved. Original accounting file hashes remain:

- Experiment 01 ledger: `b76ba88cb246ff7c30095531b309fdd752650bebf033148f5eab178c4c873971`.
- Experiment 01 lock anchors: `69db5219645dd08b3d8ea954934eb83ec88b405bebad894a08ea06ffd04c5926`.

Remaining blockers are concrete:

1. No complete candidate migration PASS exists. The complete candidates exceed five seconds; the incomplete candidates cannot support a final decision. Meeting the frozen threshold would require qualifying genuine evidence unavailable in this offline task.
2. Experiments 01–03 raw evidence remains local and untracked. This documentation/configuration-only commit does not package it. A clean-checkout view containing only committed files plus the new metadata returned historical INVALID/2 for missing cataloged evidence. Reproducible fresh-clone/CI validation therefore requires a separately reviewed way to distribute the unchanged evidence; the catalog alone is insufficient.
3. No release selector exists. Direct root release evaluation remains INVALID/2. A complete PASS and an exact reviewed selector would be required for migration approval; the BLOCKED verification above establishes rejection behavior only.

This audit made no API calls, started no servers, installed nothing, and created no new experiments. Only safe documentation/configuration metadata was committed locally; nothing was pushed and no PR was opened.
