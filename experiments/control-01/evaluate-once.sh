#!/usr/bin/env bash
# SAME-MODEL CONTROL: fixed one-shot gate sequence for later authorized execution only.
set -eu
test "$#" -eq 0
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
(
  set -eu
  export PYTHONDONTWRITEBYTECODE=1
  test ! -e experiments/control-01/live-evaluation.started
  test ! -L experiments/control-01/live-evaluation.started
  test -f experiments/control-01/preregistration.md
  test ! -e results/candidate/control-01
  curl --noproxy '*' --fail --silent --show-error --max-time 10 http://127.0.0.1:3106/ -o /dev/null
  .venv/bin/python -B - <<'PY'
from pathlib import Path
import hashlib
from scripts.evaluate_saved import validate_configs
from ai_model_migration_gate.cases import load_cases
from ai_model_migration_gate.config import load_config
from ai_model_migration_gate.fingerprint import calculate_fingerprint
path = Path('gate.control-01.yaml').resolve()
assert hashlib.sha256(path.read_bytes()).hexdigest() == '68d3dcbbffe7b6b63b8ccd9959120bf5316f7ca5b0959d1a7dfa684b82e17c6e', 'Config bytes changed'
assert hashlib.sha256(Path('experiments/control-01/preregistration.md').read_bytes()).hexdigest() == '5c0897e0f8a62f3b9955e428da9cd1396a0b90d10230a57bf9ad98c282669b14', 'Preregistration bytes changed'
configs, cases = validate_configs(path.parent)
config = configs[path.name]
assert len(cases) == 12
assert config.targets.candidate.model == 'gpt-5.4-mini'
assert config.targets.candidate.expected_fingerprint.verifier_commit_sha == '729323791112f253ae3cac8cbd7933cc2d394bf4'
actual = calculate_fingerprint(config, 'candidate', path, cases)
assert actual == config.targets.candidate.expected_fingerprint, 'Verifier fingerprint mismatch'
print(actual.model_dump_json(indent=2))
PY
  mkdir experiments/control-01/live-evaluation.started
  .venv/bin/gate run --config gate.control-01.yaml --target candidate \
    --run-id control-01 --max-calls 12 --yes
  .venv/bin/python -B - <<'PY'
import json
from pathlib import Path
from ai_model_migration_gate.cases import load_cases
root = Path('results/candidate/control-01')
cases = load_cases('cases/cases.jsonl')
attempts = sorted((root / 'attempts').glob('*/*.json'))
assert len(attempts) == 12, 'Incomplete first pass; stop for review'
for case in cases:
    path = root / 'attempts' / case.id / '0001.json'
    value = json.loads(path.read_text())
    assert value['case_id'] == case.id and value['target'] == 'candidate'
    assert value.get('http_status') is not None, 'Transport failure; stop for review'
print('First pass recorded; at most two failed cases may be retried in corpus order.')
PY
  .venv/bin/gate run --config gate.control-01.yaml --target candidate \
    --run-id control-01 --max-calls 2 --yes
  .venv/bin/python -B - <<'PY'
from pathlib import Path
attempts = list(Path('results/candidate/control-01/attempts').glob('*/*.json'))
assert 12 <= len(attempts) <= 14, 'Attempt cap violated; stop for review'
assert all(int(path.stem) <= 2 for path in attempts), 'Repeated retry; stop for review'
print('SAME-MODEL CONTROL saved attempts:', len(attempts), '/ 14; no migration approval is established.')
PY
  set +e
  .venv/bin/gate check --config gate.control-01.yaml --target candidate \
    --run-id control-01 \
    --baseline-run-id current-baseline-01 \
    --baseline-run-id current-baseline-02 \
    --baseline-run-id current-baseline-03 --json \
    > experiments/control-01/decision.json
  decision_exit=$?
  set -e
  cat experiments/control-01/decision.json
  printf 'SAME-MODEL CONTROL gate exit: %s (PASS=0, BLOCKED=1, INVALID=2; not migration approval)\n' "$decision_exit"
  exit "$decision_exit"
)
