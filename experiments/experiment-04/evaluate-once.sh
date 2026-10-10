#!/usr/bin/env bash
# Fixed one-shot sequence using the existing gate CLI; no provider calls until --yes.
set -eu
test "$#" -eq 0
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
(
  set -eu
  export PYTHONDONTWRITEBYTECODE=1
  test ! -e experiments/experiment-04/live-evaluation.started
  test ! -L experiments/experiment-04/live-evaluation.started
  test -f experiments/experiment-04/preregistration.md
  test ! -e results/candidate/experiment-04
  curl --noproxy '*' --fail --silent --show-error --max-time 10 http://127.0.0.1:3105/ -o /dev/null
  .venv/bin/python -B - <<'PY'
from pathlib import Path
import hashlib
from scripts.evaluate_saved import validate_configs
from ai_model_migration_gate.cases import load_cases
from ai_model_migration_gate.config import load_config
from ai_model_migration_gate.fingerprint import calculate_fingerprint
path = Path('gate.experiment-04.yaml').resolve()
assert hashlib.sha256(path.read_bytes()).hexdigest() == '01e1c9f8eec5749bbcd5c78be4db1d024630ac45f961377461d117f4086aa8eb', 'Config bytes changed'
assert hashlib.sha256(Path('experiments/experiment-04/preregistration.md').read_bytes()).hexdigest() == 'a27f1f0cedf3c94d9c3e44538e4f04e9581eabcf22ed6b06a677237f6630d1ed', 'Preregistration bytes changed'
configs, cases = validate_configs(path.parent)
config = configs[path.name]
assert len(cases) == 12
assert config.targets.candidate.model == 'gpt-4.1-mini-2025-04-14'
assert config.targets.candidate.expected_fingerprint.verifier_commit_sha == '7974b3fc3d0e8ba9147c0850f9e5d0d620f3546d'
actual = calculate_fingerprint(config, 'candidate', path, cases)
assert actual == config.targets.candidate.expected_fingerprint, 'Verifier fingerprint mismatch'
print(actual.model_dump_json(indent=2))
PY
  mkdir experiments/experiment-04/live-evaluation.started
  .venv/bin/gate run --config gate.experiment-04.yaml --target candidate \
    --run-id experiment-04 --max-calls 12 --yes
  .venv/bin/python -B - <<'PY'
import json
from pathlib import Path
from ai_model_migration_gate.cases import load_cases
root = Path('results/candidate/experiment-04')
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
  .venv/bin/gate run --config gate.experiment-04.yaml --target candidate \
    --run-id experiment-04 --max-calls 2 --yes
  .venv/bin/python -B - <<'PY'
from pathlib import Path
attempts = list(Path('results/candidate/experiment-04/attempts').glob('*/*.json'))
assert 12 <= len(attempts) <= 14, 'Attempt cap violated; stop for review'
assert all(int(path.stem) <= 2 for path in attempts), 'Repeated retry; stop for review'
print('Saved attempts:', len(attempts), '/ 14; conditional maximum reservation: $0.3034752 / $0.35.')
PY
  set +e
  .venv/bin/gate check --config gate.experiment-04.yaml --target candidate \
    --run-id experiment-04 \
    --baseline-run-id current-baseline-01 \
    --baseline-run-id current-baseline-02 \
    --baseline-run-id current-baseline-03 --json \
    > experiments/experiment-04/decision.json
  decision_exit=$?
  set -e
  cat experiments/experiment-04/decision.json
  printf 'Gate exit: %s (PASS=0, BLOCKED=1, INVALID=2)\n' "$decision_exit"
  exit "$decision_exit"
)
