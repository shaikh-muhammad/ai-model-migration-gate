"""Evaluate committed evidence offline: historical expectations or a release selector.

Usage: python scripts/evaluate_saved.py historical|release

Future experiments.json has exactly an ``experiments`` list. Each entry contains
config, run_id, kind (official/exploratory), evidence_sha256 (see evidence_digest),
and expected (the full observed ``gate check --json`` decision). No catalog is
needed for the original historical Luna assertion. Release requires the exact
official pair and gate.pass.yaml or gate.blocked.yaml; it evaluates the actual
decision, rather than granting approval from the catalog's expected decision.

Code, policy, pins, catalog and evidence share repository trust. These checks
are consistency safeguards, not an independent security boundary. No verifier,
provider client, subprocess, environment file, or network is used here.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import yaml

from ai_model_migration_gate.cases import load_cases
from ai_model_migration_gate.config import GateConfig
from ai_model_migration_gate.fingerprint import case_set_sha256
from ai_model_migration_gate.gate import Decision, GateDecision, RuleStatus, check_run
from ai_model_migration_gate.scoring import score_run


ROOT = Path(__file__).resolve().parents[1]
BASELINE_IDS = ("current-baseline-01", "current-baseline-02", "current-baseline-03")
FROZEN_RULES = {
    "max_critical_dangerous_mistakes": 0,
    "min_correct_cases": 10,
    "max_slow_case_seconds": 5.0,
    "max_dangerous_regressions": 0,
}
CORPUS_SHA256 = "fd1aa6b77276f122930f6d3ad85e05d992c2a67342d0ca6610cc8a8642291245"
TOOL_VERSION = "0.1.0"
RELEASE_CONFIGS = {"gate.pass.yaml", "gate.blocked.yaml"}
CONFIG_NAME = re.compile(r"gate(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)?\.yaml")
RUN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
LUNA_RUN = "candidate-official-01"
LUNA_DIGEST = "2db8b2b27d5abb39720a44f1b34ca440dac6c176139eaac2500c278ec7abb856"


class EvaluationError(ValueError):
    """A concise diagnostic safe to display without input contents."""


def require(condition, message):
    if not condition:
        raise EvaluationError(message)


def safe_path(root, relative, *, directory=False):
    """Require an existing in-root path, with no symlink in any component."""
    relative = Path(relative)
    require(not relative.is_absolute() and ".." not in relative.parts,
            "Path must remain inside the repository")
    path = root
    for part in relative.parts:
        path = path / part
        require(not path.is_symlink(), "Symlinks are not permitted in evaluation inputs")
    require(path.is_dir() if directory else path.is_file(),
            "Required evaluation directory is missing" if directory
            else "Required evaluation file is missing")
    return path


def unique_mapping(pairs):
    result = {}
    for key, value in pairs:
        require(isinstance(key, str) and key not in result,
                "Mapping keys must be unique strings")
        result[key] = value
    return result


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicates at every YAML mapping depth, before model validation."""


def yaml_mapping(loader, node):
    return unique_mapping((loader.construct_object(key), loader.construct_object(value))
                          for key, value in node.value)


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, yaml_mapping)


def read_yaml(root, name):
    return yaml.load(safe_path(root, name).read_text(encoding="utf-8"), Loader=UniqueLoader)


def read_json(root, name):
    return json.loads(safe_path(root, name).read_text(encoding="utf-8"),
                      object_pairs_hook=unique_mapping,
                      parse_constant=lambda _: require(False, "Nonfinite JSON number"))


def validate_raw_config(raw):
    require(isinstance(raw, dict) and set(raw) == {"targets", "rules"},
            "Configuration must contain exactly targets and rules")
    rules = raw["rules"]
    require(isinstance(rules, dict) and set(rules) == set(FROZEN_RULES),
            "All four frozen rules must be explicitly present, with no extra rules")
    for name, expected in FROZEN_RULES.items():
        value = rules[name]
        allowed_types = (int, float) if name == "max_slow_case_seconds" else (int,)
        require(type(value) in allowed_types and value == expected,
                "Frozen rule values must match without coercion")
    targets = raw["targets"]
    require(isinstance(targets, dict) and set(targets) == {"current", "candidate"},
            "Configuration requires exactly current and candidate")
    fields = {"base_url", "verifier_path", "model", "expected_fingerprint"}
    pin_fields = {"declared_model", "prompt_sha256", "verifier_commit_sha",
                  "case_set_sha256", "tool_version"}
    for target in targets.values():
        require(isinstance(target, dict) and set(target) == fields,
                "Target fields must be explicit and exact")
        require(all(type(target[field]) is str and target[field].strip()
                    for field in fields - {"expected_fingerprint"}),
                "Target values must be nonempty strings")
        pin = target["expected_fingerprint"]
        require(isinstance(pin, dict) and set(pin) == pin_fields
                and all(type(value) is str and value.strip() for value in pin.values()),
                "Target fingerprint must contain all five string fields")
        require(target["model"] == pin["declared_model"] and target["model"] != "CHANGE_ME",
                "Target model must match its fingerprint declaration")
        require(all(re.fullmatch(r"[0-9a-f]{64}", pin[field])
                    for field in ("prompt_sha256", "case_set_sha256"))
                and re.fullmatch(r"[0-9a-f]{40}", pin["verifier_commit_sha"]),
                "Fingerprint hashes must have valid hexadecimal structure")
        require(pin["case_set_sha256"] == CORPUS_SHA256 and pin["tool_version"] == TOOL_VERSION,
                "Corpus identity and tool version must remain frozen")
    return GateConfig.model_validate(raw)


def validate_configs(root):
    """Discover root configs, check raw invariants, then hash the real corpus."""
    root = Path(root).resolve()
    original = read_yaml(root, "gate.yaml")
    validate_raw_config(original)
    configs = {}
    for name in sorted({"gate.yaml", *(p.name for p in root.glob("gate.*.yaml"))}):
        require(CONFIG_NAME.fullmatch(name), "Unrecognized root gate configuration name")
        raw = read_yaml(root, name)
        config = validate_raw_config(raw)
        require(raw["rules"] == original["rules"], "Rules differ from gate.yaml")
        require(raw["targets"]["current"] == original["targets"]["current"],
                "Current target differs from gate.yaml")
        configs[name] = config
    cases = load_cases(safe_path(root, "cases/cases.jsonl"))
    require(len(cases) == 12, "Frozen corpus must contain twelve cases")
    for case in cases:
        safe_path(root, case.image)
    require(case_set_sha256(cases, root / "gate.yaml") == CORPUS_SHA256,
            "Actual corpus identity differs from the frozen hash")
    return configs, cases


def run_directory(root, target, run_id):
    require(type(run_id) is str and RUN_NAME.fullmatch(run_id), "Unsafe run ID")
    require("smoke" not in run_id.lower(), "Developmental smoke evidence is not eligible")
    return safe_path(root, Path("results") / target / run_id, directory=True)


def evidence_digest(root, target, run_id):
    """Hash sorted relative names and SHA-256 file bytes, including all attempts."""
    directory = run_directory(root, target, run_id)
    inventory = []
    for path in sorted(directory.rglob("*")):
        require(not path.is_symlink(), "Evidence must not contain symlinks")
        if path.is_dir():
            continue
        safe_path(root, path.relative_to(root))
        inventory.append((path.relative_to(directory).as_posix(),
                          hashlib.sha256(path.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(inventory, separators=(",", ":")).encode()).hexdigest()


def validate_baselines(root, config, cases):
    for run_id in BASELINE_IDS:
        evidence_digest(root, "current", run_id)  # Check directories and all symlinks.
        report = score_run(config=config, target="current", run_id=run_id,
                           cases=cases, results_dir=root / "results")
        require(report.metrics.total_cases == 12, "Baseline must be complete")


def evaluate(root, config, cases, run_id):
    evidence_digest(root, "candidate", run_id)
    decision = check_run(config=config, target="candidate", run_id=run_id, cases=cases,
                         results_dir=root / "results", baseline_run_ids=list(BASELINE_IDS))
    # Revalidate even if a caller supplies a mutated model. No standalone PASS.
    decision = GateDecision.model_validate_json(decision.model_dump_json())
    require(decision.target == "candidate" and decision.run_id == run_id
            and decision.baseline_run_ids == list(BASELINE_IDS), "Unexpected decision identity")
    if decision.decision != Decision.INVALID:
        require(not decision.comparison_required and decision.comparison is not None
                and decision.metrics is not None and decision.metrics.total_cases == 12
                and decision.comparison.candidate_run_id == run_id
                and decision.comparison.baseline_run_ids == BASELINE_IDS
                and len(decision.comparison.cases) == 12
                and {case.case_id for case in decision.comparison.cases} == {case.id for case in cases}
                and not decision.errors and decision.scoring_problems is None,
                "Release outcomes require a complete final comparison")
        require(len(decision.rules) == 4 and {rule.name for rule in decision.rules} == set(FROZEN_RULES)
                and all(rule.status != RuleStatus.NOT_EVALUATED and rule.actual is not None
                        and rule.threshold == (FROZEN_RULES[rule.name] * 1000
                                              if rule.name == "max_slow_case_seconds"
                                              else FROZEN_RULES[rule.name]) for rule in decision.rules),
                "Release outcomes require all frozen rules to be evaluated")
        require((decision.decision == Decision.PASS)
                == all(rule.status == RuleStatus.PASS for rule in decision.rules),
                "Decision does not match evaluated rules")
    return decision


def read_selector(root):
    selector = read_json(root, "release.json")
    require(isinstance(selector, dict) and set(selector) == {"config", "run_id"},
            "Release selector requires exactly config and run_id")
    require(type(selector["config"]) is str and selector["config"] in RELEASE_CONFIGS,
            "Release configuration must be an approved root filename")
    run_directory(root, "candidate", selector["run_id"])
    safe_path(root, selector["config"])
    return selector


def read_catalog(root, configs):
    catalog = read_json(root, "experiments.json")
    require(isinstance(catalog, dict) and set(catalog) == {"experiments"}
            and isinstance(catalog["experiments"], list), "Invalid experiment catalog structure")
    entries = {}
    for entry in catalog["experiments"]:
        require(isinstance(entry, dict) and set(entry) == {
            "config", "run_id", "kind", "evidence_sha256", "expected"}, "Invalid catalog entry fields")
        require(type(entry["config"]) is str and entry["config"] in configs
                and entry["kind"] in ("official", "exploratory"), "Unrecognized catalog configuration or kind")
        run_directory(root, "candidate", entry["run_id"])
        pair = (entry["config"], entry["run_id"])
        require(pair not in entries, "Duplicate catalog pair")
        require(type(entry["evidence_sha256"]) is str
                and re.fullmatch(r"[0-9a-f]{64}", entry["evidence_sha256"]), "Invalid evidence digest")
        require(isinstance(entry["expected"], dict), "Catalog must record an observed decision")
        expected = GateDecision.model_validate_json(json.dumps(entry["expected"]))
        require(expected.model_dump(mode="json") == entry["expected"]
                and expected.target == "candidate" and expected.run_id == entry["run_id"]
                and expected.baseline_run_ids == list(BASELINE_IDS), "Invalid recorded decision identity or shape")
        require(evidence_digest(root, "candidate", entry["run_id"]) == entry["evidence_sha256"],
                "Catalog evidence digest mismatch")
        entries[pair] = entry
    return entries


def historical(root, configs, cases):
    decision = evaluate(root, configs["gate.yaml"], cases, LUNA_RUN)
    problems = decision.scoring_problems
    require(decision.decision == Decision.INVALID and decision.exit_code == 2
            and decision.metrics is None and decision.comparison is None
            and not decision.comparison_required
            and decision.errors == ["Run is incomplete or invalid. Missing canonical cases: 09-glare"]
            and problems is not None and problems.model_dump() == {
                "target": "candidate", "run_id": LUNA_RUN, "missing_case_ids": ["09-glare"],
                "invalid_case_results": {}, "invalid_outcomes": {}, "unexpected_canonical_files": [],
                "manifest_errors": [], "corpus_errors": []}
            and len(decision.rules) == 4 and {rule.name for rule in decision.rules} == set(FROZEN_RULES)
            and all(rule.status == RuleStatus.NOT_EVALUATED and rule.actual is None for rule in decision.rules),
            "Original Luna result must remain INVALID/2 solely because canonical 09-glare is missing")
    require(evidence_digest(root, "candidate", LUNA_RUN) == LUNA_DIGEST,
            "Original Luna evidence inventory or contents changed")
    print("Historical Luna: INVALID/2; missing canonical 09-glare; metrics=null; comparison=null; rules=not_evaluated")
    catalog_path = root / "experiments.json"
    if catalog_path.exists() or catalog_path.is_symlink():
        for (name, run_id), entry in read_catalog(root, configs).items():
            observed = evaluate(root, configs[name], cases, run_id)
            require(observed.model_dump(mode="json") == entry["expected"],
                    "Historical experiment differs from its recorded observed outcome")
            print(f"Historical experiment: {observed.decision.value}/{observed.exit_code}")
    return 0


def release(root, configs, cases):
    selector = read_selector(root)
    entries = read_catalog(root, configs)
    entry = entries.get((selector["config"], selector["run_id"]))
    require(entry is not None and entry["kind"] == "official", "Release config/run pair is not approved as official")
    decision = evaluate(root, configs[selector["config"]], cases, selector["run_id"])
    print(decision.model_dump_json(indent=2))
    return decision.exit_code


def main(argv=None, *, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("historical", "release"))
    args = parser.parse_args(argv)
    root = Path(root).resolve()
    try:
        # Missing release.json is a controlled failure, not a historical fallback.
        if args.mode == "release":
            read_selector(root)
        configs, cases = validate_configs(root)
        for config in configs.values():
            validate_baselines(root, config, cases)
        return historical(root, configs, cases) if args.mode == "historical" else release(root, configs, cases)
    except EvaluationError as exc:
        print(f"INVALID/2: {exc}", file=sys.stderr)
    except (OSError, ValueError, yaml.YAMLError, RecursionError):
        # Do not print library validation errors: they can include raw input data.
        print("INVALID/2: Cannot read or validate saved evaluation inputs", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
