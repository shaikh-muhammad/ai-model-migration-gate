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
    shutil.copytree(ROOT / c.DIRECTORY, root / c.DIRECTORY)
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
                                "verification": {"overall": {"status": "Pass"}}})
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
    assert output["conditional_per_attempt_usd"] == "0.0190304"
    assert output["conditional_fourteen_attempts_usd"] == "0.2664256"
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
