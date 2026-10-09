"""CI shell tests use explicitly synthetic saved runs in temporary projects.

No fixture is measured model evidence, and no installation/provider step runs.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from test_config_invariants import ROOT, saved_project
from test_saved_evaluation import release_project, record_observation, mutate_result


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def step(name, needle):
    steps = next(iter(workflow(name)["jobs"].values()))["steps"]
    return next(item for item in steps if needle in item.get("name", ""))


def ci_environment(root):
    # Supply only test/runtime necessities, never inherit provider credentials.
    return {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
            "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
            "OPENAI_API_KEY": "", "GEMINI_API_KEY": "", "HOME": str(root)}


def run_evaluation_step(root, mode):
    (root / "scripts").mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "scripts/evaluate_saved.py", root / "scripts/evaluate_saved.py")
    command = step("release.yml", "Evaluate")["run"] if mode == "release" else step(
        "ci.yml", "historical")["run"]
    return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], cwd=root,
                          env=ci_environment(root), capture_output=True, text=True)


def synthetic_outcome(root, decision):
    """Change only clearly named offline-fixture-candidate data under tmp_path."""
    if decision == "BLOCKED":
        mutate_result(root, "offline-fixture-candidate", "05-warning-title-case", status="pass")
    elif decision == "INVALID":
        (root / "results/candidate/offline-fixture-candidate/09-glare.json").unlink()
    record_observation(root)


@pytest.mark.parametrize("decision,code", [("PASS", 0), ("BLOCKED", 1), ("INVALID", 2)])
def test_synthetic_release_ci_propagates_real_evaluator_exit(release_project, decision, code):
    synthetic_outcome(release_project, decision)
    result = run_evaluation_step(release_project, "release")
    assert result.returncode == code and result.stderr == ""
    observed = json.loads(result.stdout)
    assert observed["decision"] == decision and observed["run_id"] == "offline-fixture-candidate"
    if decision != "INVALID":
        assert len(observed["comparison"]["cases"]) == 12
        assert observed["comparison_required"] is False
        assert all(rule["status"] != "not_evaluated" for rule in observed["rules"])


@pytest.mark.parametrize("decision", ["PASS", "BLOCKED", "INVALID"])
def test_synthetic_expected_history_remains_green_for_all_outcomes(release_project, decision):
    synthetic_outcome(release_project, decision)
    result = run_evaluation_step(release_project, "historical")
    assert result.returncode == 0 and result.stderr == ""
    assert f"Historical experiment: {decision}/" in result.stdout
    assert "Historical Luna: INVALID/2" in result.stdout


@pytest.mark.parametrize("missing", ["release.json", "experiments.json"])
def test_release_ci_missing_inputs_fail_closed(release_project, missing):
    (release_project / missing).unlink()
    result = run_evaluation_step(release_project, "release")
    assert result.returncode == 2 and "INVALID/2" in result.stderr
    assert "Traceback" not in result.stderr


def test_release_ci_unregistered_pair_is_not_approved(release_project):
    (release_project / "experiments.json").write_text('{"experiments":[]}')
    result = run_evaluation_step(release_project, "release")
    assert result.returncode == 2 and "not approved" in result.stderr


def test_release_ci_catalog_pass_label_cannot_hide_blocked_evidence(release_project):
    mutate_result(release_project, "offline-fixture-candidate", "05-warning-title-case", status="pass")
    # Keep the old synthetic PASS label but update its digest. The real gate wins.
    from test_config_invariants import evaluation
    path = release_project / "experiments.json"
    catalog = json.loads(path.read_text())
    catalog["experiments"][0]["evidence_sha256"] = evaluation.evidence_digest(
        release_project, "candidate", "offline-fixture-candidate")
    path.write_text(json.dumps(catalog))
    result = run_evaluation_step(release_project, "release")
    assert result.returncode == 1 and json.loads(result.stdout)["decision"] == "BLOCKED"
    history = run_evaluation_step(release_project, "historical")
    assert history.returncode == 2  # Historical outcomes must match their recorded observation.


def test_workflows_use_helper_directly_and_read_only_permissions():
    for name in ("ci.yml", "release.yml"):
        data = workflow(name)
        assert set(data["on"]) == {"push", "pull_request", "workflow_dispatch"}
        assert data["permissions"] == {"contents": "read"}
        job = next(iter(data["jobs"].values()))
        assert job["env"]["OPENAI_API_KEY"] == job["env"]["GEMINI_API_KEY"] == ""
        assert not job.get("continue-on-error", False)
        assert all(not item.get("continue-on-error", False) for item in job["steps"])
        checkout = next(item for item in job["steps"] if "checkout@" in item.get("uses", ""))
        assert checkout["with"]["persist-credentials"] is False
    assert step("ci.yml", "historical")["run"] == "python scripts/evaluate_saved.py historical"
    release = step("release.yml", "Evaluate")
    assert release["run"] == "python scripts/evaluate_saved.py release"
    assert release["if"] == "steps.selection.outputs.evaluate == 'true'"
    assert next(item for item in workflow("release.yml")["jobs"]["release-check"]["steps"]
                if "checkout@" in item.get("uses", ""))["with"]["fetch-depth"] == 0


@pytest.mark.parametrize("event,head,base_selector,base_commit,expected", [
    ("pull_request", "absent", False, True, False),
    ("push", "absent", False, True, False),
    ("workflow_dispatch", "absent", False, True, True),
    ("pull_request", "file", False, True, True),
    ("push", "file", False, True, True),
    ("pull_request", "absent", True, True, True),  # Deleted selector still requires evaluation.
    ("pull_request", "broken_symlink", False, True, True),
    ("pull_request", "directory", False, True, True),
    ("pull_request", "absent", False, False, None),  # Unknown base cannot silently skip.
])
def test_release_intent_shell_fails_closed(tmp_path, event, head, base_selector, base_commit, expected):
    root = tmp_path / "synthetic-ci-root"; root.mkdir()
    if head == "file": (root / "release.json").write_text('{}')
    elif head == "broken_symlink": (root / "release.json").symlink_to(root / "nonexistent")
    elif head == "directory": (root / "release.json").mkdir()
    # Fake git only answers two local object-presence questions; no checkout/fetch/network.
    bin_dir = root / "fake-bin"; bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text('''#!/bin/sh
case "$3" in
  *'^{commit}') [ "$BASE_COMMIT_PRESENT" = yes ] ;;
  *':release.json') [ "$BASE_HAS_SELECTOR" = yes ] ;;
  *) exit 2 ;;
esac
''')
    git.chmod(0o755)
    output = root / "outputs"
    env = {**ci_environment(root), "PATH": str(bin_dir) + os.pathsep + os.defpath,
           "RELEASE_EVENT_NAME": event, "RELEASE_BASE_SHA": "a" * 40,
           "GITHUB_OUTPUT": str(output), "BASE_COMMIT_PRESENT": "yes" if base_commit else "no",
           "BASE_HAS_SELECTOR": "yes" if base_selector else "no"}
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c",
                             step("release.yml", "Determine")["run"]],
                            cwd=root, env=env, capture_output=True, text=True)
    if expected is None:
        assert result.returncode != 0 and not output.exists()
    else:
        assert result.returncode == 0
        assert output.read_text().strip() == f"evaluate={str(expected).lower()}"
        if not expected:
            assert "no migration approval evaluated" in result.stdout


def test_committed_original_history_still_reproduces_without_local_experiment(saved_project):
    result = run_evaluation_step(saved_project, "historical")
    assert result.returncode == 0 and "Historical Luna: INVALID/2" in result.stdout
    assert not (saved_project / "results/candidate/experiment-01").exists()
