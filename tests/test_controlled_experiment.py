"""Offline controller tests: temporary evidence and fake collectors only."""

from datetime import datetime, timedelta, timezone
import fcntl
import importlib.util
import json
import multiprocessing
from pathlib import Path
import shutil
import socket
import subprocess
from decimal import Decimal
from types import SimpleNamespace

import pytest
import yaml

from ai_model_migration_gate.config import FingerprintIdentity
from ai_model_migration_gate.manifest import RunManifest, create_manifest
from ai_model_migration_gate.results import CaseResult, write_attempt, write_result


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("controlled", ROOT / "scripts/controlled_experiment.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"; root.mkdir()
    for name in ["gate.yaml", c.CONFIG]:
        shutil.copyfile(ROOT / name, root / name)
    shutil.copytree(ROOT / "cases", root / "cases")
    (root / c.DIRECTORY).mkdir(parents=True)
    for name in ["preregistration.md", "verifier.patch"]:
        shutil.copyfile(ROOT / c.DIRECTORY / name, root / c.DIRECTORY / name)
    for run_id in c.saved.BASELINE_IDS:
        shutil.copytree(ROOT / "results/current" / run_id, root / "results/current" / run_id)
    monkeypatch.setattr(c, "calculate_fingerprint", lambda *args: FingerprintIdentity(**c.PIN))
    # No test can accidentally use the real collector, provider or verifier.
    def forbidden(*args, **kwargs):
        raise AssertionError("Real subprocess/network forbidden in controller tests")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    return root


def approval(count=0, **changes):
    return {"pricing_confirmed": True, "runtime_confirmed": True,
            "spent_usd": "0", "budget_usd": "0.50", "through": count,
            "as_of": datetime.now(timezone.utc).isoformat(), **changes}


def fake_collector(root, *, failed=False, slow=False, error_code="EXTRACTION_SERVICE_ERROR", callback=None):
    def run(command, **kwargs):
        assert kwargs == {"cwd": root, "check": False, "stdout": subprocess.DEVNULL,
                          "stderr": subprocess.DEVNULL}
        assert command[:4] == [c.sys.executable, "-m", "ai_model_migration_gate.cli", "run"]
        assert command.count("--case") == 1 and command.count("--max-calls") == 1
        assert command[command.index("--max-calls") + 1] == "1"
        assert command[command.index("--run-id") + 1] == c.EXPERIMENT
        assert command[command.index("--config") + 1] == str(root / c.CONFIG)
        assert command[command.index("--target") + 1] == "candidate"
        assert "--force" not in command and "--yes" in command
        ledger, lock = c.accounting_paths(root)
        records = [c.decode(line) for line in ledger.read_bytes().splitlines()]
        assert records[-1]["event"]["type"] == "reserve"
        assert lock.read_bytes().splitlines()[-1] == records[-1]["hash"].encode()
        case = command[command.index("--case") + 1]
        if callback:
            callback(command)
        result = CaseResult(case_id=case, target="candidate", status="error" if failed else "success",
                            http_status=502 if failed else 200, response_time_ms=9000 if slow else 1000,
                            error_code=error_code if failed else None,
                            raw_response={"error": {"code": error_code}} if failed else {
                                "verification": {"overall": {"status": "pass"}}})
        run_dir = root / "results/candidate" / c.EXPERIMENT
        if not (run_dir / "manifest.json").exists():
            create_manifest(root / "results", RunManifest(
                run_id=c.EXPERIMENT, target="candidate", created_at=datetime.now(timezone.utc),
                fingerprint=FingerprintIdentity(**c.PIN)))
        directory = run_dir / "attempts" / case
        number = len(list(directory.glob("*.json"))) + 1
        write_attempt(root / "results", c.EXPERIMENT, number, result)
        if not failed:
            write_result(root / "results", c.EXPERIMENT, result)
        return SimpleNamespace(returncode=0)
    return run


def dispatch_next(root, **fake_options):
    plan = c.status(root)
    return c.dispatch(root, case_id=plan["next_case"], allow_live=True,
                      approval=approval(plan["reservations"]), confirm=lambda phrase: True,
                      run=fake_collector(root, **fake_options))


def records(root):
    ledger, _ = c.accounting_paths(root)
    return [c.decode(line) for line in ledger.read_bytes().splitlines()]


def rewrite(root, values, *, anchors=True):
    """Test-only malformed/tampered records; never touch real accounting files."""
    previous = c.ZERO
    for record in values:
        record["previous"] = previous
        record["hash"] = c.sha(c.encode({k: v for k, v in record.items() if k != "hash"}))
        previous = record["hash"]
    ledger, lock = c.accounting_paths(root)
    ledger.write_bytes(b"".join(c.encode(r) + b"\n" for r in values))
    if anchors:
        lock.write_bytes(b"".join(r["hash"].encode() + b"\n" for r in values))


def test_default_is_read_only_and_no_paid_action(project, capsys):
    before = {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    assert c.main([], root=project) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["next_case"] == "01-perfect" and output["reservations"] == 0
    assert output["pricing_version"] == 2
    assert output["conditional_per_attempt_usd"] == "0.0213304"
    assert output["conditional_fourteen_attempts_usd"] == "0.2986256"
    assert before == {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}


def test_one_durable_reservation_before_one_dispatch(project, monkeypatch):
    calls = []; syncs = []
    original = c.os.fsync
    monkeypatch.setattr(c.os, "fsync", lambda fd: (syncs.append(fd), original(fd))[-1])
    def observe(command):
        assert len(syncs) >= 3  # ledger, anchor and directory, all before launch
        calls.append(command)
    assert dispatch_next(project, callback=observe) == 0
    assert len(calls) == 1 and len(records(project)) == 2
    assert c.status(project)["reservations"] == 1


def test_twelve_first_passes_no_replay_after_completion(project):
    for _ in range(12):
        dispatch_next(project)
    plan = c.status(project)
    assert plan["reservations"] == 12 and plan["next_case"] is None
    assert [r["event"]["kind"] for r in records(project)[::2]] == ["first-pass"] * 12
    with pytest.raises(ValueError, match="eligible"):
        c.dispatch(project, case_id="01-perfect", allow_live=True, approval=approval(12),
                   confirm=lambda p: True, run=fake_collector(project))


def test_two_retries_fourteen_total_one_per_initial_failure_in_order(project):
    for index in range(12):
        dispatch_next(project, failed=index in (0, 2, 4))
    assert c.status(project)["next_case"] == "01-perfect"
    dispatch_next(project, failed=True)
    assert c.status(project)["next_case"] == "03-wrong-volume"
    dispatch_next(project)
    plan = c.status(project)
    assert plan["reservations"] == 14 and plan["next_case"] is None
    reserved = [r["event"] for r in records(project)[::2]]
    assert sum(r["kind"] == "retry" for r in reserved) == 2
    assert reserved[-2]["case_id"] == "01-perfect" and reserved[-1]["case_id"] == "03-wrong-volume"
    with pytest.raises(ValueError, match="eligible"):
        c.dispatch(project, case_id="05-warning-title-case", allow_live=True, approval=approval(14),
                   confirm=lambda p: True, run=fake_collector(project))


def test_slow_success_is_immutable_and_never_reselected(project):
    dispatch_next(project, slow=True)
    canonical = project / "results/candidate/experiment-01/01-perfect.json"
    before = canonical.read_bytes()
    with pytest.raises(ValueError, match="eligible"):
        c.dispatch(project, case_id="01-perfect", allow_live=True, approval=approval(1),
                   confirm=lambda p: True, run=fake_collector(project))
    assert canonical.read_bytes() == before
    assert c.status(project)["next_case"] == "02-wrong-abv"


def test_failed_attempt_counts_and_checkpoint_must_cover_it(project):
    dispatch_next(project, failed=True)
    assert c.status(project)["conditional_reserved_usd"] == str(c.PER_ATTEMPT)
    assert not (project / "results/candidate/experiment-01/01-perfect.json").exists()
    with pytest.raises(ValueError, match="every previous"):
        c.dispatch(project, case_id="02-wrong-abv", allow_live=True, approval=approval(0),
                   confirm=lambda p: True, run=fake_collector(project))


@pytest.mark.parametrize("failure", ["exception", "nonzero", "no_record", "attempt_without_canonical"])
def test_ambiguous_collector_completion_blocks_recovery(project, failure):
    def run(command, **kwargs):
        assert len(records(project)) == 1
        if failure == "exception":
            raise RuntimeError("fake crash")
        if failure == "attempt_without_canonical":
            real_fake = fake_collector(project); result = real_fake(command, **kwargs)
            (project / "results/candidate/experiment-01/01-perfect.json").unlink()
            return result
        return SimpleNamespace(returncode=1 if failure == "nonzero" else 0)
    with pytest.raises((RuntimeError, ValueError)):
        c.dispatch(project, case_id="01-perfect", allow_live=True, approval=approval(),
                   confirm=lambda p: True, run=run)
    assert len(records(project)) == 1
    with pytest.raises(ValueError, match="Unresolved"):
        c.status(project)
    assert c.main([], root=project) == 2


def test_two_consecutive_service_failures_pause(project):
    dispatch_next(project, failed=True)
    dispatch_next(project, failed=True)
    with pytest.raises(ValueError, match="pause"):
        c.status(project)


@pytest.mark.parametrize("damage", ["partial", "json", "duplicate_key", "hash", "truncate", "missing_ledger", "missing_lock", "partial_anchor"])
def test_damaged_accounting_fails_closed_without_repair(project, damage):
    dispatch_next(project)
    ledger, lock = c.accounting_paths(project)
    if damage == "partial": ledger.write_bytes(ledger.read_bytes()[:-1])
    elif damage == "json": ledger.write_bytes(b"garbage\n")
    elif damage == "duplicate_key": ledger.write_bytes(b'{"event":{},"event":{}}\n')
    elif damage == "hash": ledger.write_bytes(ledger.read_bytes().replace(b'"first-pass"', b'"retry"'))
    elif damage == "truncate": ledger.write_bytes(b"")
    elif damage == "missing_ledger": ledger.unlink()
    elif damage == "missing_lock": lock.unlink()
    else: lock.write_bytes(lock.read_bytes()[:-1])
    before = ledger.read_bytes() if ledger.exists() else None
    assert c.main([], root=project) == 2
    assert (ledger.read_bytes() if ledger.exists() else None) == before


@pytest.mark.parametrize("field", ["config", "run_id", "experiment", "fingerprint"])
def test_ledger_identity_mismatch_rejected(project, field):
    dispatch_next(project); values = records(project)
    values[0]["identity"][field] = {**c.PIN, "declared_model": "other"} if field == "fingerprint" else "other"
    rewrite(project, values)
    with pytest.raises(ValueError, match="identity"):
        c.status(project)


@pytest.mark.parametrize("change", ["case", "kind", "duplicate_id", "extra_reservation"])
def test_out_of_order_and_duplicate_reservations_rejected(project, change):
    dispatch_next(project); dispatch_next(project); values = records(project)
    if change == "case": values[0]["event"]["case_id"] = "unapproved"
    elif change == "kind": values[0]["event"]["kind"] = "retry"
    elif change == "duplicate_id": values[2]["event"]["dispatch_id"] = values[0]["event"]["dispatch_id"]
    else: values.extend(values[:2])
    rewrite(project, values)
    with pytest.raises(ValueError):
        c.status(project)


@pytest.mark.parametrize("damage", ["no_ledger", "extra_attempt", "missing_attempt", "modified_canonical", "bad_manifest", "symlink", "extra_directory"])
def test_evidence_must_match_ledger(project, damage, tmp_path):
    dispatch_next(project)
    run = project / "results/candidate/experiment-01"
    if damage == "no_ledger":
        ledger, lock = c.accounting_paths(project); ledger.unlink(); lock.unlink()
    elif damage == "extra_attempt":
        shutil.copyfile(run / "attempts/01-perfect/0001.json", run / "attempts/01-perfect/0002.json")
    elif damage == "missing_attempt": (run / "attempts/01-perfect/0001.json").unlink()
    elif damage == "modified_canonical":
        p=run / "01-perfect.json"; p.write_bytes(p.read_bytes()+b" ")
    elif damage == "bad_manifest":
        p=run / "manifest.json"; d=json.loads(p.read_text());d["run_id"]="other";p.write_text(json.dumps(d))
    elif damage == "symlink":
        (run / "escape").symlink_to(tmp_path)
    else: (run / "unexpected").mkdir()
    with pytest.raises(ValueError):
        c.status(project)


@pytest.mark.parametrize("path", ["dispatch.lock", "dispatch-ledger.jsonl"])
def test_accounting_symlinks_rejected(project, tmp_path, path):
    outside=tmp_path / "outside";outside.write_text("data")
    (project / c.DIRECTORY / path).symlink_to(outside)
    assert c.main([], root=project) == 2
    assert outside.read_text() == "data"


@pytest.mark.parametrize("changes", [
    {"pricing_confirmed": False}, {"pricing_confirmed": 1}, {"runtime_confirmed": False},
    {"spent_usd": None}, {"spent_usd": "NaN"}, {"spent_usd": "-1"},
    {"as_of": None}, {"as_of": "2026-01-01T00:00:00"},
    {"as_of": (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()},
    {"as_of": (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()},
    {"through": True}, {"through": 1}, {"budget_usd": "0.51"}, {"budget_usd": "0"},
    {"budget_usd": "0.01"}, {"spent_usd": "0.49"},
])
def test_unverified_stale_or_excessive_spending_blocks_dispatch(project, changes):
    with pytest.raises((ValueError, ArithmeticError)):
        c.dispatch(project, case_id="01-perfect", allow_live=True, approval=approval(**changes),
                   confirm=lambda p: True, run=fake_collector(project))
    assert not any(p.exists() for p in c.accounting_paths(project))


def test_reservation_budget_can_be_reached_but_not_exceeded(project):
    budget=str(c.PER_ATTEMPT)
    c.dispatch(project, case_id="01-perfect", allow_live=True, approval=approval(budget_usd=budget),
               confirm=lambda p: True, run=fake_collector(project))
    with pytest.raises(ValueError, match="exceed"):
        c.dispatch(project, case_id="02-wrong-abv", allow_live=True,
                   approval=approval(1,budget_usd=budget), confirm=lambda p: True, run=fake_collector(project))
    assert len(records(project)) == 2


@pytest.mark.parametrize("allow_live,confirmed", [(False,True),(True,False)])
def test_live_flag_and_confirmation_both_required(project, allow_live, confirmed):
    with pytest.raises(ValueError):
        c.dispatch(project, case_id="01-perfect", allow_live=allow_live, approval=approval(),
                   confirm=lambda p: confirmed, run=fake_collector(project))
    assert not any(p.exists() for p in c.accounting_paths(project))


def test_terminal_confirmation_cannot_be_unattended(monkeypatch):
    monkeypatch.setattr(c.sys.stdin,"isatty",lambda:False)
    with pytest.raises(ValueError,match="terminal"):
        c.terminal_confirmation("test")


def hold_lock(path, ready, release):
    with open(path,"r+b") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX);ready.set();release.wait(10)


def test_concurrent_process_lock_blocks_second_controller(project):
    dispatch_next(project)
    _,path=c.accounting_paths(project);ctx=multiprocessing.get_context("fork")
    ready=ctx.Event();release=ctx.Event();process=ctx.Process(target=hold_lock,args=(path,ready,release))
    process.start()
    try:
        assert ready.wait(5)
        with pytest.raises(ValueError,match="locked"):
            c.status(project)
    finally:
        release.set();process.join(5)
        if process.is_alive(): process.terminate();process.join()
    assert process.exitcode==0
    assert c.status(project)["reservations"]==1


def test_concurrent_reservation_cannot_happen_during_collector(project):
    def during(command):
        with pytest.raises(ValueError,match="locked"):
            c.status(project)
    dispatch_next(project,callback=during)
    assert len(records(project))==2


@pytest.mark.parametrize("mutation", ["fingerprint","url","path","baseline","rules","case"])
def test_real_prerequisite_validation_cannot_be_bypassed(project, mutation, monkeypatch):
    if mutation=="baseline":
        (project / "results/current/current-baseline-01/01-perfect.json").unlink()
    elif mutation=="case":
        p = project / "cases/cases.jsonl"
        lines = p.read_text().splitlines(); first = json.loads(lines[0]); first["critical"] = not first["critical"]
        lines[0] = json.dumps(first); p.write_text("\n".join(lines) + "\n")
    elif mutation=="fingerprint":
        monkeypatch.setattr(c,"calculate_fingerprint",lambda *a:FingerprintIdentity(**{**c.PIN,"prompt_sha256":"a"*64}))
    else:
        p=project / c.CONFIG;d=yaml.safe_load(p.read_text())
        if mutation=="rules":d["rules"]["min_correct_cases"]=9
        else:d["targets"]["candidate"]["base_url" if mutation=="url" else "verifier_path"]="http://127.0.0.1:3999" if mutation=="url" else "../wrong"
        p.write_text(yaml.safe_dump(d))
    assert c.main([],root=project)==2


@pytest.mark.parametrize("artifact",[c.CONFIG,"preregistration.md","verifier.patch"])
def test_pinned_preparation_bytes_cannot_change_after_reservation(project,artifact):
    dispatch_next(project)
    path=project / (artifact if artifact==c.CONFIG else c.DIRECTORY / artifact)
    path.write_bytes(path.read_bytes()+b"\n")
    with pytest.raises(ValueError,match="identity"):
        c.status(project)


def test_fake_dispatch_through_real_cli_interface(project, monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_collector(project))
    phrases = []
    monkeypatch.setattr(c, "terminal_confirmation", lambda phrase: (phrases.append(phrase), True)[1])
    assert c.main(["dispatch", "--case", "01-perfect", "--allow-live", "--confirm-pricing",
                   "--confirm-runtime", "--billing-spent", "0", "--billing-through", "0",
                   "--billing-as-of", datetime.now(timezone.utc).isoformat()], root=project) == 0
    assert phrases == ["DISPATCH experiment-01 01-perfect MAY INCUR PROVIDER CHARGES"]
    assert len(records(project)) == 2


def test_main_dispatch_without_opt_in_is_controlled_failure(project):
    assert c.main(["dispatch", "--case", "01-perfect"], root=project) == 2
    assert not any(path.exists() for path in c.accounting_paths(project))


def test_fifteenth_reservation_and_third_retry_have_no_eligible_case(project):
    cases, _ = c.inputs(project)
    first = [(dict(case_id=case.id), dict(status="error")) for case in cases]
    assert c.next_case(cases, first) == ("01-perfect", "retry")
    assert c.next_case(cases, first + first[:2]) == (None, None)
    with pytest.raises(ValueError, match="ceiling"):
        c.next_case(cases, first + first[:3])


def test_reordered_corpus_is_rejected_even_if_set_hash_is_same(project):
    path = project / "cases/cases.jsonl"
    lines = path.read_text().splitlines(); lines[0], lines[1] = lines[1], lines[0]
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="order"):
        c.status(project)


def test_future_root_configuration_is_discovered_and_validated(project):
    path = project / "gate.experiment-02.yaml"
    path.write_bytes((project / c.CONFIG).read_bytes())
    configs, cases = c.saved.validate_configs(project)
    assert set(configs) == {"gate.yaml", c.CONFIG, path.name}
    for config in configs.values():
        c.saved.validate_baselines(project, config, cases)
    path.write_text("invalid configuration")
    with pytest.raises(ValueError):
        c.status(project)


@pytest.mark.parametrize("field,value", [("returncode", 1), ("returncode", False),
                                         ("status", "unknown"), ("dispatch_id", "other"),
                                         ("evidence_sha256", "invalid"), ("error_code", 7)])
def test_invalid_completion_records_fail_closed(project, field, value):
    dispatch_next(project); values = records(project)
    values[1]["event"][field] = value; rewrite(project, values)
    assert c.main([], root=project) == 2


def test_checkpoint_cannot_move_backward_or_disprove_cost_bound(project):
    dispatch_next(project)
    c.dispatch(project, case_id="02-wrong-abv", allow_live=True, approval=approval(1, spent_usd="0.01"),
               confirm=lambda p: True, run=fake_collector(project))
    with pytest.raises(ValueError, match="backward"):
        c.dispatch(project, case_id="03-wrong-volume", allow_live=True, approval=approval(2, spent_usd="0"),
                   confirm=lambda p: True, run=fake_collector(project))
    with pytest.raises(ValueError, match="conditional bounds"):
        c.dispatch(project, case_id="03-wrong-volume", allow_live=True, approval=approval(2, spent_usd="0.1"),
                   confirm=lambda p: True, run=fake_collector(project))
    assert len(records(project)) == 4


def test_empty_preexisting_ledger_is_not_reset(project):
    ledger, lock = c.accounting_paths(project)
    ledger.touch(); lock.touch()
    with pytest.raises(ValueError, match="Empty ledger"):
        c.status(project)


def test_nonregular_accounting_file_is_rejected(project):
    ledger, _ = c.accounting_paths(project)
    ledger.mkdir()
    assert c.main([], root=project) == 2


def test_anchor_interruption_blocks_dispatch_without_repair(project):
    dispatch_next(project)
    _, lock = c.accounting_paths(project)
    lock.write_bytes(b"")
    with pytest.raises(ValueError, match="Missing ledger or anchors"):
        c.status(project)


def test_configured_budget_cannot_be_raised_by_later_invocation(project):
    c.dispatch(project, case_id="01-perfect", allow_live=True, approval=approval(budget_usd="0.03"),
               confirm=lambda p: True, run=fake_collector(project))
    with pytest.raises(ValueError, match="budget cannot increase"):
        c.dispatch(project, case_id="02-wrong-abv", allow_live=True, approval=approval(1),
                   confirm=lambda p: True, run=fake_collector(project))
    assert len(records(project)) == 2


@pytest.fixture
def historical_project(project, monkeypatch):
    """Synthetic old-format fixture; never depend on untracked real observations."""
    dispatch_next(project)  # Fake collector and isolated tmp_path only.
    values = records(project)
    del values[0]["event"]["pricing_version"]
    values[0]["event"]["reserved_usd"] = str(c.LEGACY_PER_ATTEMPT)
    rewrite(project, values)
    # Production allows exactly its real historical hash. This fixture substitutes
    # only its synthetic counterpart; the real ledger is validated read-only.
    monkeypatch.setattr(c, "LEGACY_RESERVATION_HASH", values[0]["hash"])
    return project


def test_exact_old_reservation_and_integrity_remain_valid(historical_project):
    root = historical_project
    ledger, anchor = c.accounting_paths(root)
    before = (ledger.read_bytes(), anchor.read_bytes())
    state = c.status(root)
    assert state["reservations"] == 1 and state["next_case"] == "02-wrong-abv"
    assert state["recorded_reserved_usd"] == "0.0190304"
    assert state["conditional_reserved_usd"] == "0.0213304"
    assert state["conditional_fourteen_attempts_usd"] == "0.2986256"
    old = records(root)[0]
    assert old["hash"] == c.LEGACY_RESERVATION_HASH
    assert "pricing_version" not in old["event"]
    assert old["event"]["reserved_usd"] == str(c.LEGACY_PER_ATTEMPT)
    assert before == (ledger.read_bytes(), anchor.read_bytes())


def test_new_reservation_uses_explicit_cache_write_policy(project):
    dispatch_next(project)
    reservation = records(project)[0]["event"]
    assert reservation["pricing_version"] == 2
    assert reservation["reserved_usd"] == "0.0213304"
    assert c.PER_ATTEMPT == (Decimal(46000) * Decimal("0.25")
                             + Decimal(8192) * Decimal("1.20")) / Decimal(1000000)
    assert c.MAX_BUDGET == Decimal("0.50")


def test_mixed_ledger_preserves_exact_old_records_and_canonical(historical_project):
    root = historical_project; ledger, anchor = c.accounting_paths(root)
    original = (ledger.read_bytes(), anchor.read_bytes())
    canonical = root / "results/candidate/experiment-01/01-perfect.json"
    before = canonical.read_bytes()
    dispatch_next(root)
    state = c.status(root)
    assert state["reservations"] == 2 and state["next_case"] == "03-wrong-volume"
    assert state["recorded_reserved_usd"] == "0.0403608"
    assert state["conditional_reserved_usd"] == "0.0426608"
    assert ledger.read_bytes().startswith(original[0]) and anchor.read_bytes().startswith(original[1])
    assert canonical.read_bytes() == before
    assert records(root)[2]["event"]["pricing_version"] == 2


def test_repriced_headroom_rejects_sum_of_original_estimates(historical_project):
    root = historical_project; before = records(root)
    underestimated_budget = str(c.LEGACY_PER_ATTEMPT + c.PER_ATTEMPT)
    with pytest.raises(ValueError, match="exceed"):
        c.dispatch(root, case_id="02-wrong-abv", allow_live=True,
                   approval=approval(1, budget_usd=underestimated_budget),
                   confirm=lambda p: True, run=fake_collector(root))
    assert records(root) == before


def test_mixed_ledger_exact_conservative_budget_boundary(historical_project):
    root = historical_project; budget = str(c.PER_ATTEMPT * 2)
    c.dispatch(root, case_id="02-wrong-abv", allow_live=True,
               approval=approval(1, budget_usd=budget, spent_usd="0.020"),
               confirm=lambda p: True, run=fake_collector(root))
    assert c.status(root)["conditional_reserved_usd"] == budget
    with pytest.raises(ValueError, match="exceed"):
        c.dispatch(root, case_id="03-wrong-volume", allow_live=True,
                   approval=approval(2, budget_usd=budget, spent_usd="0.020"),
                   confirm=lambda p: True, run=fake_collector(root))
    assert len(records(root)) == 4


@pytest.mark.parametrize("mutation", ["old_amount", "arbitrary_amount", "old_version", "future_version",
                                      "bool_version", "string_version", "no_version", "extra_price"])
def test_new_pricing_cannot_be_forged_or_downgraded(historical_project, mutation):
    root = historical_project; dispatch_next(root); values = records(root)
    event = values[2]["event"]
    if mutation == "old_amount": event["reserved_usd"] = str(c.LEGACY_PER_ATTEMPT)
    elif mutation == "arbitrary_amount": event["reserved_usd"] = "0.00001"
    elif mutation == "no_version":
        del event["pricing_version"]; event["reserved_usd"] = str(c.LEGACY_PER_ATTEMPT)
    elif mutation == "extra_price": event["input_rate"] = "0.20"
    else:
        event["pricing_version"] = {"old_version": 1, "future_version": 3,
                                    "bool_version": True, "string_version": "2"}[mutation]
    rewrite(root, values)  # Even a self-consistent rehash must fail policy checks.
    assert c.main([], root=root) == 2


@pytest.mark.parametrize("mutation", ["amount", "checkpoint", "dispatch_id"])
def test_legacy_allowance_requires_exact_original_reservation(historical_project, mutation):
    root = historical_project; values = records(root)
    event = values[0]["event"]
    if mutation == "amount": event["reserved_usd"] = "0.019"
    elif mutation == "checkpoint": event["checkpoint"]["budget_usd"] = "0.49"
    else: event["dispatch_id"] = "00000000-0000-4000-8000-000000000000"
    rewrite(root, values)
    with pytest.raises(ValueError, match="legacy pricing"):
        c.status(root)


def test_new_run_cannot_invent_unversioned_history(project):
    dispatch_next(project); values = records(project)
    del values[0]["event"]["pricing_version"]
    values[0]["event"]["reserved_usd"] = str(c.LEGACY_PER_ATTEMPT)
    rewrite(project, values)
    assert c.main([], root=project) == 2


def test_old_success_still_cannot_be_replayed(historical_project):
    root = historical_project; before = records(root)
    with pytest.raises(ValueError, match="eligible"):
        c.dispatch(root, case_id="01-perfect", allow_live=True, approval=approval(1),
                   confirm=lambda p: True, run=fake_collector(root))
    assert records(root) == before


def test_unresolved_new_reservation_after_legacy_still_blocks(historical_project):
    root = historical_project
    def crash(*args, **kwargs):
        assert records(root)[2]["event"]["reserved_usd"] == "0.0213304"
        raise RuntimeError("fake crash after durable reservation")
    with pytest.raises(RuntimeError):
        c.dispatch(root, case_id="02-wrong-abv", allow_live=True, approval=approval(1),
                   confirm=lambda p: True, run=crash)
    assert len(records(root)) == 3
    with pytest.raises(ValueError, match="Unresolved"):
        c.status(root)


def test_legacy_hash_or_anchor_corruption_is_not_accepted(historical_project):
    root = historical_project; ledger, _ = c.accounting_paths(root)
    ledger.write_bytes(ledger.read_bytes().replace(b'"0.0190304"', b'"0.0213304"', 1))
    with pytest.raises(ValueError, match="hash chain"):
        c.status(root)


def session_approval(**changes):
    return {"pricing_confirmed": True, "runtime_confirmed": True,
            "credit_confirmed": True, "monitor_confirmed": True,
            "available_usd": "0.50", "budget_usd": "0.50", "through": 1,
            "as_of": datetime.now(timezone.utc).isoformat(), **changes}


def run_session(root, **options):
    return c.session(root, allow_live=True, approval=session_approval(),
                     confirm=lambda phrase: True, run=fake_collector(root, **options))


def test_session_one_approval_eleven_durable_requests_preserves_history(historical_project, monkeypatch):
    root = historical_project
    ledger, anchor = c.accounting_paths(root)
    original = ledger.read_bytes(), anchor.read_bytes()
    canonical = root / "results/candidate/experiment-01/01-perfect.json"
    first = canonical.read_bytes()
    confirmations = []; calls = []; syncs = []
    original_sync = c.os.fsync
    monkeypatch.setattr(c.os, "fsync", lambda fd: (syncs.append(fd), original_sync(fd))[-1])
    def observe(command):
        assert len(syncs) >= 3 * (2 * len(calls) + 1)
        calls.append(command[command.index("--case") + 1])
        with pytest.raises(ValueError, match="lock"):
            c.status(root)  # Lock spans the entire session, including every collector.
    assert c.session(root, allow_live=True, approval=session_approval(),
                     confirm=lambda phrase: (confirmations.append(phrase) or True),
                     run=fake_collector(root, callback=observe)) == 0
    assert len(confirmations) == 1 and "ELEVEN FIRST-PASS" in confirmations[0]
    assert calls == list(c.CASE_IDS[1:])
    assert ledger.read_bytes().startswith(original[0]) and anchor.read_bytes().startswith(original[1])
    assert canonical.read_bytes() == first
    values = records(root)
    sessions = [r["event"] for r in values[2::2]]
    assert len({r["session_id"] for r in sessions}) == 1
    assert all(r["reserved_usd"] == "0.0213304" and r["kind"] == "first-pass" for r in sessions)
    state = c.status(root)
    assert state["reservations"] == 12 and state["next_case"] is None
    assert state["conditional_reserved_usd"] == "0.2559648"
    assert state["recorded_reserved_usd"] == "0.2536648"
    with pytest.raises(ValueError, match="partial sessions"):
        run_session(root)


@pytest.mark.parametrize("change", [
    {"pricing_confirmed": False}, {"runtime_confirmed": False},
    {"credit_confirmed": False}, {"monitor_confirmed": False},
    {"credit_confirmed": 1}, {"through": 0}, {"through": True},
    {"available_usd": "0.49"}, {"available_usd": "NaN"},
    {"budget_usd": "0.51"}, {"budget_usd": "0.25"},
    {"as_of": "2026-01-01T00:00:00"}, {"available_usd": None},
])
def test_session_invalid_start_approval_never_reserves(historical_project, change):
    root = historical_project; before = records(root)
    with pytest.raises((ValueError, TypeError)):
        c.session(root, allow_live=True, approval=session_approval(**change),
                  confirm=lambda phrase: True, run=fake_collector(root))
    assert records(root) == before


@pytest.mark.parametrize("seconds", [-1, 901])
def test_session_fresh_credit_check_required(historical_project, seconds):
    root = historical_project; before = records(root)
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with pytest.raises(ValueError, match="expired|future"):
        c.session(root, allow_live=True, approval=session_approval(as_of=stamp),
                  confirm=lambda phrase: True, run=fake_collector(root))
    assert records(root) == before


@pytest.mark.parametrize("allow,confirm", [(False, True), (True, False)])
def test_session_live_flag_and_personal_approval_required(historical_project, allow, confirm):
    root = historical_project; before = records(root)
    with pytest.raises(ValueError):
        c.session(root, allow_live=allow, approval=session_approval(),
                  confirm=lambda phrase: confirm, run=fake_collector(root))
    assert records(root) == before


def test_session_requires_first_success_and_no_additional_cases(project):
    with pytest.raises(ValueError, match="completed 01-perfect"):
        run_session(project)
    dispatch_next(project, failed=True)
    with pytest.raises(ValueError, match="immutable first success"):
        run_session(project)
    dispatch_next(project)
    with pytest.raises(ValueError, match="partial sessions"):
        run_session(project)


def test_session_exact_conservative_budget_boundary(historical_project):
    root = historical_project; budget = str(c.PER_ATTEMPT * 12)
    assert c.session(root, allow_live=True, approval=session_approval(budget_usd=budget),
                     confirm=lambda phrase: True, run=fake_collector(root)) == 0
    assert c.status(root)["conditional_reserved_usd"] == budget


def test_session_failed_request_recorded_and_no_retry(historical_project):
    root = historical_project
    with pytest.raises(ValueError, match="stopped on failed"):
        run_session(root, failed=True)
    assert len(records(root)) == 4
    assert c.status(root)["reservations"] == 2
    assert not (root / "results/candidate/experiment-01/02-wrong-abv.json").exists()
    with pytest.raises(ValueError, match="partial sessions"):
        run_session(root)


@pytest.mark.parametrize("failure", ["crash", "nonzero", "no_evidence"])
def test_session_ambiguous_reservation_stops_and_remains_charged(historical_project, failure):
    root = historical_project
    def fake(*args, **kwargs):
        if failure == "crash": raise RuntimeError("synthetic interruption")
        return SimpleNamespace(returncode=1 if failure == "nonzero" else 0)
    with pytest.raises((ValueError, RuntimeError)):
        c.session(root, allow_live=True, approval=session_approval(), confirm=lambda phrase: True, run=fake)
    assert len(records(root)) == 3
    with pytest.raises(ValueError, match="Unresolved"):
        c.status(root)


def test_session_slow_success_preserved_without_replacement(historical_project):
    root = historical_project
    assert run_session(root, slow=True) == 0
    assert len(records(root)) == 24
    canonical = root / "results/candidate/experiment-01/02-wrong-abv.json"
    assert json.loads(canonical.read_bytes())["response_time_ms"] == 9000
    assert canonical.read_bytes() == (canonical.parent / "attempts/02-wrong-abv/0001.json").read_bytes()


@pytest.mark.parametrize("mutation", ["session_id", "approval", "price", "version", "retry", "strip_session"])
def test_session_records_strict_validation(historical_project, mutation):
    root = historical_project; run_session(root); values = records(root)
    event = values[4]["event"]
    if mutation == "session_id": event["session_id"] = str(c.uuid.uuid4())
    elif mutation == "approval": event["checkpoint"]["available_usd"] = "0.60"
    elif mutation == "price": event["reserved_usd"] = str(c.LEGACY_PER_ATTEMPT)
    elif mutation == "version": event["pricing_version"] = 1
    elif mutation == "retry": event["kind"] = "retry"
    else: del event["session_id"]
    rewrite(root, values)
    assert c.main([], root=root) == 2


@pytest.mark.parametrize("damage", ["identity", "ledger", "evidence", "expiry"])
def test_session_rechecks_between_requests(historical_project, monkeypatch, damage):
    root = historical_project
    original_inputs = c.inputs; calls = []
    def changed_inputs(path):
        cases, identity = original_inputs(path)
        if calls and damage == "identity": identity = {**identity, "patch_sha256": "0" * 64}
        return cases, identity
    monkeypatch.setattr(c, "inputs", changed_inputs)
    def callback(command):
        calls.append(command)
        if damage == "ledger":
            ledger, _ = c.accounting_paths(root); ledger.write_bytes(ledger.read_bytes() + b'{')
        elif damage == "evidence":
            (root / "results/candidate/experiment-01/01-perfect.json").write_bytes(b'{}')
        elif damage == "expiry":
            class Expired:
                @staticmethod
                def now(tz): return datetime.now(tz) + timedelta(minutes=16)
                fromisoformat = staticmethod(datetime.fromisoformat)
            monkeypatch.setattr(c, "datetime", Expired)
    with pytest.raises(ValueError):
        c.session(root, allow_live=True, approval=session_approval(), confirm=lambda phrase: True,
                  run=fake_collector(root, callback=callback))
    assert len(calls) == 1


def test_session_cli_only_mocked_and_no_case_override(historical_project, monkeypatch):
    root = historical_project; calls = []
    original = c.session
    def offline_session(*args, **kwargs):
        return original(*args, **kwargs, run=fake_collector(root, callback=lambda command: calls.append(command)))
    monkeypatch.setattr(c, "session", offline_session)
    monkeypatch.setattr(c, "terminal_confirmation", lambda phrase: True)
    argv = ["session", "--allow-live", "--confirm-pricing", "--confirm-runtime",
            "--confirm-credit", "--confirm-monitor", "--prepaid-available", "0.50",
            "--credit-as-of", datetime.now(timezone.utc).isoformat(), "--budget", "0.50"]
    assert c.main(argv + ["--case", "02-wrong-abv"], root=root) == 2
    assert not calls
    assert c.main(argv, root=root) == 0 and len(calls) == 11


def test_session_default_status_stays_read_only(historical_project):
    root = historical_project
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert c.main([], root=root) == 0
    assert before == {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_session_malformed_success_stops_without_next_request(historical_project):
    root = historical_project; calls = []
    fake = fake_collector(root)
    def malformed(command, **kwargs):
        calls.append(command)
        result = fake(command, **kwargs)
        case = command[command.index('--case') + 1]
        run_dir = root / 'results/candidate/experiment-01'
        for path in (run_dir / (case + '.json'), run_dir / 'attempts' / case / '0001.json'):
            data = json.loads(path.read_bytes())
            data['raw_response'] = {'unexpected': 'synthetic'}
            path.write_text(json.dumps(data))
        return result
    with pytest.raises(ValueError):
        c.session(root, allow_live=True, approval=session_approval(), confirm=lambda phrase: True, run=malformed)
    assert len(calls) == 1 and len(records(root)) == 4
    assert c.status(root)['reservations'] == 2


def test_session_operator_interrupt_preserves_reservation(historical_project):
    root = historical_project
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt('Operator reports unexpected costs or runtime change')
    with pytest.raises(KeyboardInterrupt):
        c.session(root, allow_live=True, approval=session_approval(), confirm=lambda phrase: True, run=interrupt)
    assert len(records(root)) == 3
    with pytest.raises(ValueError, match='Unresolved'):
        c.status(root)
