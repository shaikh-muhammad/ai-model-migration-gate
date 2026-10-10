# control-01: observed SAME-MODEL CONTROL outcome

The original gpt-5.4-mini behavior, with only a README comment changing its source commit, produced a genuine **BLOCKED/1** control result. This is not a model migration. All twelve first-pass attempts returned HTTP 200; all twelve canonical files equal their first attempts byte-for-byte. There were no retries. The saved decision reproduces exactly against current-baseline-01, current-baseline-02 and current-baseline-03.

The control has nine correct cases, two minor cases, one false block, zero dangerous mistakes and zero dangerous regressions. Its p95/max client latency is 2327.5635649988544 ms. Only min_correct_cases fails: nine correct against the frozen threshold of ten.

The single changed classification is **09-glare**. It was correct in every baseline: each returned Needs Review with governmentWarningBodyBold=uncertain. The control reports governmentWarningBodyBold=yes and returns Fail, with the warning-field reason "The warning text after the heading should not appear bold." Expected Needs Review plus actual Fail is false_block under the unchanged scoring matrix. This one flip reduces the baseline correct count of ten to nine. The unchanged verifier follows its explicit warning-formatting rule; this result does not prove an application bug or a model migration regression.

All 26 control JSON records were audited for sensitive values and metadata, including the original saved decision. No credentials, authorization headers, cookies, provider IDs/payloads, stack traces or account details were found. Content consists of the fixed synthetic label/application data, extraction observations, local verification explanations, timing/status fields, run identity, hashes and gate output. The original bytes were not sanitized or changed.

The minimum committed raw inventory contains 25 files: manifest, twelve canonical responses and twelve first attempts. The full saved decision is recorded in the existing catalog; the redundant original decision.json remains untouched locally. The catalog marks this same-model control exploratory, excluding it from official migration release selection. Runtime logs, replay guards, ledgers and smoke files are excluded.

Evidence SHA-256: `8ff2288959c9186d59af7fa6c8c2c204196484b2b355b76b8447f8aad1264bbb`. The manifest matches the preregistered fingerprint: gpt-5.4-mini, verifier commit 729323791112f253ae3cac8cbd7933cc2d394bf4, unchanged prompt/corpus hashes and tool version 0.1.0.

The accuracy threshold has no noise margin for this observed one-case flip. "Stable" means identical classifications across the three saved baseline runs, not guaranteed stability in later calls. Larger samples and a threshold derived from more runs are future work; no frozen rule, observed evidence, source behavior or release selector is changed here. The one-run authorization is consumed, and the permanent guard must remain intact.
