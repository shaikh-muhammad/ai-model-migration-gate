"""Strict offline invariants; every mutation is confined to a temporary copy."""

import importlib.util
import json
from pathlib import Path
import shutil

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("evaluate_saved", ROOT / "scripts/evaluate_saved.py")
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


@pytest.fixture
def saved_project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    shutil.copyfile(ROOT / "gate.yaml", root / "gate.yaml")
    shutil.copytree(ROOT / "cases", root / "cases")
    for target, run_ids in [("current", evaluation.BASELINE_IDS),
                            ("candidate", [evaluation.LUNA_RUN])]:
        for run_id in run_ids:
            shutil.copytree(ROOT / "results" / target / run_id,
                            root / "results" / target / run_id)
    return root


def candidate_config(root):
    data = yaml.safe_load((root / "gate.yaml").read_text())
    candidate = data["targets"]["candidate"]
    candidate.update(model="offline-test-candidate", verifier_path="../offline-fixture", base_url="http://127.0.0.1:3999")
    candidate["expected_fingerprint"].update(declared_model=candidate["model"],
                                            prompt_sha256="a" * 64, verifier_commit_sha="b" * 40)
    return data


def save_config(root, data, name="gate.experiment-01.yaml"):
    (root / name).write_text(yaml.safe_dump(data), encoding="utf-8")


def test_original_config_and_actual_baselines_are_accepted():
    configs, cases = evaluation.validate_configs(ROOT)
    evaluation.validate_baselines(ROOT, configs["gate.yaml"], cases)
    assert set(configs) == {"gate.yaml"}
    assert len(cases) == 12


def test_matching_root_candidate_config_and_discovery(saved_project):
    save_config(saved_project, candidate_config(saved_project))
    configs, cases = evaluation.validate_configs(saved_project)
    assert set(configs) == {"gate.yaml", "gate.experiment-01.yaml"}
    for config in configs.values():
        evaluation.validate_baselines(saved_project, config, cases)
    nested = saved_project / "configs"
    nested.mkdir()
    (nested / "gate.pass.yaml").write_text("invalid yaml")
    assert set(evaluation.validate_configs(saved_project)[0]) == set(configs)


@pytest.mark.parametrize("rule", evaluation.FROZEN_RULES)
@pytest.mark.parametrize("mutation", ["changed", "omitted", "null", "bool", "string"])
def test_each_rule_rejects_deviations_before_defaults_and_coercion(saved_project, rule, mutation):
    data = candidate_config(saved_project)
    if mutation == "omitted":
        del data["rules"][rule]
    else:
        data["rules"][rule] = {
            "changed": data["rules"][rule] + 1, "null": None,
            "bool": False, "string": str(data["rules"][rule]),
        }[mutation]
    save_config(saved_project, data)
    with pytest.raises(evaluation.EvaluationError, match="rule"):
        evaluation.validate_configs(saved_project)


@pytest.mark.parametrize("mutation", ["missing_rules", "extra_rule", "extra_target", "missing_pin",
                                      "extra_pin", "bad_prompt_hash", "bad_commit", "unknown_name"])
def test_exact_structure_and_hashes(saved_project, mutation):
    data = candidate_config(saved_project)
    pin = data["targets"]["candidate"]["expected_fingerprint"]
    name = "gate.experiment-01.yaml"
    if mutation == "missing_rules":
        del data["rules"]
    elif mutation == "extra_rule":
        data["rules"]["extra"] = 0
    elif mutation == "extra_target":
        data["targets"]["third"] = data["targets"]["candidate"]
    elif mutation == "missing_pin":
        data["targets"]["candidate"]["expected_fingerprint"] = None
    elif mutation == "extra_pin":
        pin["extra"] = "unused"
    elif mutation == "bad_prompt_hash":
        pin["prompt_sha256"] = "invalid"
    elif mutation == "bad_commit":
        pin["verifier_commit_sha"] = "invalid"
    else:
        name = "gate.BAD.yaml"
    save_config(saved_project, data, name)
    with pytest.raises(evaluation.EvaluationError):
        evaluation.validate_configs(saved_project)


@pytest.mark.parametrize("depth", ["root", "rules", "target", "pin"])
def test_duplicate_yaml_keys_at_every_depth(saved_project, depth):
    text = (saved_project / "gate.yaml").read_text()
    lines = {"root": "rules:", "rules": "  max_critical_dangerous_mistakes: 0",
             "target": "    model: gpt-6-luna", "pin": "      declared_model: gpt-6-luna"}
    line = lines[depth]
    (saved_project / "gate.experiment-01.yaml").write_text(text.replace(line, line + "\n" + line))
    with pytest.raises(evaluation.EvaluationError, match="unique"):
        evaluation.validate_configs(saved_project)


@pytest.mark.parametrize("field,value", [("model", "another-model"), ("verifier_path", "../other"),
                                         ("base_url", "http://127.0.0.1:4000")])
def test_complete_current_target_must_match(saved_project, field, value):
    data = candidate_config(saved_project)
    data["targets"]["current"][field] = value
    if field == "model":
        data["targets"]["current"]["expected_fingerprint"]["declared_model"] = value
    save_config(saved_project, data)
    with pytest.raises(evaluation.EvaluationError, match="Current target"):
        evaluation.validate_configs(saved_project)


@pytest.mark.parametrize("field,value", [("declared_model", "another-model"), ("prompt_sha256", "a" * 64),
                                         ("verifier_commit_sha", "b" * 40), ("case_set_sha256", "c" * 64),
                                         ("tool_version", "0.2.0")])
def test_every_current_fingerprint_field_is_frozen(saved_project, field, value):
    data = candidate_config(saved_project)
    data["targets"]["current"]["expected_fingerprint"][field] = value
    save_config(saved_project, data)
    with pytest.raises(evaluation.EvaluationError):
        evaluation.validate_configs(saved_project)


@pytest.mark.parametrize("field,value", [("declared_model", "different-model"),
                                         ("case_set_sha256", "c" * 64), ("tool_version", "0.2.0")])
def test_candidate_model_corpus_and_version_invariants(saved_project, field, value):
    data = candidate_config(saved_project)
    data["targets"]["candidate"]["expected_fingerprint"][field] = value
    save_config(saved_project, data)
    with pytest.raises(evaluation.EvaluationError):
        evaluation.validate_configs(saved_project)


@pytest.mark.parametrize("mutation", ["expectation", "image_bytes", "missing_case", "outside_image"])
def test_corpus_is_recomputed_from_records_and_image_bytes(saved_project, mutation):
    path = saved_project / "cases/cases.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    if mutation == "image_bytes":
        image = saved_project / records[0]["image"]
        image.write_bytes(image.read_bytes() + b"offline-test-change")
    else:
        if mutation == "expectation":
            records[0]["expected_outcome"] = "Fail"
        elif mutation == "missing_case":
            records.pop()
        else:
            records[0]["image"] = "../outside.png"
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    with pytest.raises(evaluation.EvaluationError):
        evaluation.validate_configs(saved_project)


@pytest.mark.parametrize("mutation", ["pin", "run_id", "target", "missing_manifest", "incomplete", "missing_run"])
def test_baseline_manifests_and_completeness(saved_project, mutation):
    directory = saved_project / "results/current" / evaluation.BASELINE_IDS[0]
    path = directory / "manifest.json"
    if mutation in ("pin", "run_id", "target"):
        data = json.loads(path.read_text())
        if mutation == "pin":
            data["fingerprint"]["prompt_sha256"] = "a" * 64
        else:
            data[mutation] = "other" if mutation == "run_id" else "candidate"
        path.write_text(json.dumps(data))
    elif mutation == "missing_manifest":
        path.unlink()
    elif mutation == "incomplete":
        (directory / "01-perfect.json").unlink()
    else:
        shutil.rmtree(directory)
    configs, cases = evaluation.validate_configs(saved_project)
    with pytest.raises(ValueError):
        evaluation.validate_baselines(saved_project, configs["gate.yaml"], cases)


def test_config_symlink_is_rejected(saved_project, tmp_path):
    outside = tmp_path / "outside.yaml"
    outside.write_bytes((saved_project / "gate.yaml").read_bytes())
    (saved_project / "gate.experiment-01.yaml").symlink_to(outside)
    with pytest.raises(evaluation.EvaluationError, match="Symlinks"):
        evaluation.validate_configs(saved_project)
