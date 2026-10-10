# Experiment 04: later authorized live evaluation

Preparation does not authorize execution. After explicit authorization for this one batch and its proposed $0.35 budget, confirm the preregistered pricing/input assumptions and available funds. Do not make an extra paid compatibility or warm-up call. Ensure port 3105 is free and use this prepared checkout.

In terminal 1, securely export `OPENAI_API_KEY` without recording its value. Start the already built verifier:

```bash
cd /home/sm/Projects/ai-model-migration-gate-workspace/verifier-experiment-04
: "${OPENAI_API_KEY:?Export OPENAI_API_KEY securely first}"
GEMINI_API_KEY= OPENAI_MODEL=gpt-4.1-mini-2025-04-14 \
OPENAI_BASE_URL=https://api.openai.com/v1 OPENAI_LOG=off \
NEXT_TELEMETRY_DISABLED=1 NODE_ENV=production \
node node_modules/next/dist/bin/next start --hostname 127.0.0.1 --port 3105 \
  2>&1 | tee -a ../ai-model-migration-gate/experiments/experiment-04/runtime.log
```

In terminal 2, run the fixed one-shot script exactly once:

```bash
cd /home/sm/Projects/ai-model-migration-gate-workspace/ai-model-migration-gate
bash experiments/experiment-04/evaluate-once.sh
```

The script checks provider-free readiness and pins, creates a permanent guard, collects at most twelve first-pass calls plus two failed-case retries, and evaluates against all three saved baselines. No baseline calls occur. Preserve all outcomes and the guard. Do not rerun after interruption or failure, remove the guard, retry successes, or exceed fourteen dispatches. After collection, stop the verifier with Ctrl-C and retain its sanitized diagnostics.
