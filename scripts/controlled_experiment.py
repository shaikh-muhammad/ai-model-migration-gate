"""Experiment 01 controller; default status is entirely read-only.

One-case dispatch requires separate spending authorization, explicit flags and a
terminal confirmation. Never start a verifier here. A fresh manual billing
checkpoint must cover ALL prior reservations, including potentially billed
failures. Confirm model-specific input/output bounds, prices, billing scope,
reporting settlement and absence of concurrent workloads before attesting.

The separate session command requires --allow-live and one interactive charge
acknowledgement for the undispatched first-pass cases. It holds the lock
throughout and resumes only from fully reconciled evidence, including saved
HTTP errors. It never retries. Unresolved requests, transport failures or invalid
outcomes require operator review. The operator must interrupt on known unexpected costs or
runtime changes. No dashboard amount, timestamp, settlement check or expiry is
required by session mode. Prepaid balance and actual provider charges cannot be
verified or enforced here; reservations are conditional estimates only. Session
approval does not assert a fresh billing check. Unfinished cases require review.

Runtime attestation means production on 127.0.0.1:3102, OPENAI_MODEL=gpt-5.6-luna,
explicitly empty effective GEMINI_API_KEY, official OpenAI endpoint, timeout
5000 ms, retries 0, output cap 8192, no explicit reasoning. No secrets are read.
Pins and operator attestations cannot prove actual provider execution or billing.

Pricing version 2 reserves all 46,000 input tokens at the $0.25/million
cache-write rate (1.25 times $0.20), plus 8,192 output/reasoning tokens at
$1.20/million. No cache hits or premium processing are assumed. Only the exact
first historical reservation is accepted without a pricing version; its bytes
and original estimate remain unchanged. Budget headroom conservatively reprices
ALL reservations at the current rate, rather than reducing the old allowance.

The JSONL ledger and its append-only lock-file anchors detect inconsistent edits
and truncation, not a malicious operator rewriting both. Repository code, policy
and evidence share trust. All dispatches must use this controller; unrelated
collectors or deleting BOTH accounting files can bypass local controls. Preserve
both files independently for review. Never automatically repair either file.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

# Direct script execution and importlib-based offline tests both reuse the helper.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate_saved as saved

from ai_model_migration_gate.fingerprint import calculate_fingerprint
from ai_model_migration_gate.manifest import RunManifest
from ai_model_migration_gate.results import CaseResult, ResultStatus, read_result
from ai_model_migration_gate.runner import preflight_case
from ai_model_migration_gate.scoring import score_case


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = "experiment-01"
CONFIG = "gate.experiment-01.yaml"
DIRECTORY = Path("experiments") / EXPERIMENT
CASE_IDS = ("01-perfect", "02-wrong-abv", "03-wrong-volume", "04-brand-case-only",
            "05-warning-title-case", "06-warning-word-changed", "07-warning-missing",
            "08-warning-not-bold", "09-glare", "10-angled", "11-imported-pass",
            "12-country-mismatch")
PIN = {
    "declared_model": "gpt-5.6-luna",
    "prompt_sha256": "af9e1e7e4c3efbe6c25d4a07cab7f8c5dfcaf0798f396134893fae5940d6d996",
    "verifier_commit_sha": "3e9498e71461cc38cdc5caf7fc140e09914a9c82",
    "case_set_sha256": saved.CORPUS_SHA256,
    "tool_version": "0.1.0",
}
PRICING_VERSION = 2
LEGACY_PER_ATTEMPT = Decimal("0.0190304")
# This immutable, already observed reservation is the sole unversioned record.
LEGACY_RESERVATION_HASH = "846facdd29e15a34e8d259d2927feb52fc1abb63b31084955f4e13f59a719145"
PER_ATTEMPT = (Decimal(46000) * Decimal("0.25")
               + Decimal(8192) * Decimal("1.20")) / Decimal(1000000)
MAX_BUDGET = Decimal("0.50")
CHECKPOINT_SECONDS = 900
ZERO = "0" * 64
require = saved.require


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def decode(data):
    return json.loads(data, object_pairs_hook=saved.unique_mapping,
                      parse_constant=lambda _: require(False, "Nonfinite accounting value"))


def local_path(root, relative):
    """Allow absent accounting/evidence paths, never symlinks or escapes."""
    relative = Path(relative)
    require(not relative.is_absolute() and ".." not in relative.parts, "Unsafe controller path")
    path = root
    for part in relative.parts:
        path = path / part
        require(not path.is_symlink(), "Controller paths must not be symlinks")
    return path


def inputs(root):
    configs, cases = saved.validate_configs(root)
    require(CONFIG in configs, "Preregistered configuration is missing")
    config = configs[CONFIG]
    require(tuple(case.id for case in cases) == CASE_IDS, "Original corpus order must remain fixed")
    saved.validate_baselines(root, config, cases)
    candidate = config.targets.candidate
    require(candidate.expected_fingerprint.model_dump() == PIN
            and str(candidate.base_url).rstrip("/") == "http://127.0.0.1:3102"
            and candidate.verifier_path == Path("../verifier-experiment-01"),
            "Candidate settings differ from Experiment 01")
    require(calculate_fingerprint(config, "candidate", root / CONFIG, cases).model_dump() == PIN,
            "Actual candidate fingerprint mismatch")
    for case in cases:
        preflight_case(case, root / CONFIG)
    identity = {"version": 1, "experiment": EXPERIMENT, "config": CONFIG,
                "target": "candidate", "run_id": EXPERIMENT, "fingerprint": PIN}
    for field, path in [("config_sha256", CONFIG),
                        ("preregistration_sha256", DIRECTORY / "preregistration.md"),
                        ("patch_sha256", DIRECTORY / "verifier.patch")]:
        identity[field] = sha(saved.safe_path(root, path).read_bytes())
    return cases, identity


def accounting_paths(root):
    saved.safe_path(root, DIRECTORY, directory=True)
    paths = (local_path(root, DIRECTORY / "dispatch-ledger.jsonl"),
             local_path(root, DIRECTORY / "dispatch.lock"))
    for path in paths:
        require(not path.exists() or path.is_file(), "Accounting must use regular files")
    return paths


@contextmanager
def locked(root, *, create=False):
    ledger, path = accounting_paths(root)
    new = not path.exists()
    require(create or not new, "Accounting lock is missing; operator review required")
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT | os.O_EXCL if new else 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise saved.EvaluationError("Another controller initialized accounting; retry status")
    with os.fdopen(fd, "r+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise saved.EvaluationError("Experiment controller is already locked")
        require(ledger.exists() or new, "Ledger is missing; operator review required")
        yield lock


def history(root, lock, identity):
    ledger, _ = accounting_paths(root)
    raw = ledger.read_bytes() if ledger.exists() else b""
    lock.seek(0)
    anchors = lock.read()
    require(not ledger.exists() or bool(raw), "Empty ledger; operator review required")
    require(bool(raw) == bool(anchors), "Missing ledger or anchors; operator review required")
    require(not raw or raw.endswith(b"\n"), "Interrupted ledger; operator review required")
    require(not anchors or anchors.endswith(b"\n"), "Interrupted accounting anchors")
    records = []; previous = ZERO
    for line in raw.splitlines():
        record = decode(line)
        require(type(record) is dict and set(record) == {"previous", "identity", "event", "hash"},
                "Malformed ledger record")
        digest = record["hash"]
        payload = {k: v for k, v in record.items() if k != "hash"}
        require(record["previous"] == previous and encode(record["identity"]) == encode(identity)
                and digest == sha(encode(payload)), "Ledger identity/hash chain mismatch")
        records.append(record); previous = digest
    require(anchors.splitlines() == [r["hash"].encode() for r in records],
            "Ledger/anchor mismatch; truncation or interrupted write requires review")
    return records


def append(root, lock, identity, records, event):
    ledger, _ = accounting_paths(root)
    payload = {"identity": identity, "previous": records[-1]["hash"] if records else ZERO,
               "event": event}
    record = {**payload, "hash": sha(encode(payload))}
    flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW
    if not records:
        flags |= os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(ledger, flags, 0o600), "ab") as output:
        output.write(encode(record) + b"\n"); output.flush(); os.fsync(output.fileno())
    lock.seek(0, os.SEEK_END)
    lock.write(record["hash"].encode() + b"\n"); lock.flush(); os.fsync(lock.fileno())
    # Persist new directory entries as well as file data before any collector launch.
    fd = os.open(ledger.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    records.append(record)


def evidence(root, completed):
    """Exactly one attempt per completed dispatch; canonical equals first success."""
    run = local_path(root, Path("results/candidate") / EXPERIMENT)
    if not run.exists():
        require(not completed, "Recorded evidence is missing")
        return None
    saved.safe_path(root, run.relative_to(root), directory=True)
    require(bool(completed), "Unexpected evidence without completed dispatches")
    manifest = RunManifest.model_validate_json(saved.safe_path(root, run.relative_to(root) / "manifest.json").read_bytes())
    require(manifest.run_id == EXPERIMENT and manifest.target == "candidate"
            and manifest.fingerprint.model_dump() == PIN, "Experiment manifest mismatch")
    expected = {"manifest.json"}; numbers = {}; successes = set()
    for reservation, completion in completed:
        case = reservation["case_id"]; numbers[case] = numbers.get(case, 0) + 1
        name = f"attempts/{case}/{numbers[case]:04d}.json"; expected.add(name)
        path = saved.safe_path(root, run.relative_to(root) / name)
        data = decode(path.read_bytes()); result = CaseResult.model_validate(data)
        require(result.model_dump(mode="json") == data and result.case_id == case
                and result.target == "candidate" and result.status.value == completion["status"]
                and result.error_code == completion["error_code"], "Attempt does not match dispatch")
        if result.status == ResultStatus.SUCCESS:
            require(case not in successes, "Repeated canonical success")
            successes.add(case); expected.add(case + ".json")
            canonical = read_result(root / "results", "candidate", EXPERIMENT, case)
            require(canonical == result and (run / (case + ".json")).read_bytes() == path.read_bytes(),
                    "First canonical success does not match its attempt")
    actual = set()
    for path in run.rglob("*"):
        require(not path.is_symlink(), "Evidence symlink rejected")
        if path.is_dir():
            require(path.relative_to(run).as_posix() in {"attempts", *(f"attempts/{c}" for c in numbers)},
                    "Unexpected evidence directory")
        else:
            actual.add(path.relative_to(run).as_posix())
    require(actual == expected, "Evidence inventory does not match dispatch ledger")
    return saved.evidence_digest(root, "candidate", EXPERIMENT)


def next_case(cases, completed):
    ids = [case.id for case in cases]
    require(len(completed) <= 14, "Fourteen-dispatch ceiling exceeded")
    if len(completed) < 12:
        return ids[len(completed)], "first-pass"
    failures = [r["case_id"] for r, c in completed[:12] if c["status"] == "error"][:2]
    retries = len(completed) - 12
    return (failures[retries], "retry") if retries < len(failures) else (None, None)


def reservation_price(record, index):
    """Accept exact historical bytes or the current explicit pricing policy only."""
    reservation = record["event"]
    fields = {"type", "dispatch_id", "case_id", "kind", "before", "checkpoint", "reserved_usd"}
    require(type(reservation) is dict, "Malformed reservation")
    if set(reservation) == fields:
        require(index == 0 and record["hash"] == LEGACY_RESERVATION_HASH
                and reservation["reserved_usd"] == str(LEGACY_PER_ATTEMPT),
                "Unrecognized legacy pricing reservation; downgrade rejected")
        return LEGACY_PER_ATTEMPT
    require(set(reservation) in (fields | {"pricing_version"},
                                fields | {"pricing_version", "session_id"}), "Malformed reservation")
    require(type(reservation["pricing_version"]) is int
            and reservation["pricing_version"] == PRICING_VERSION
            and reservation["reserved_usd"] == str(PER_ATTEMPT),
            "Reservation pricing version or amount mismatch")
    return PER_ATTEMPT


def reconcile(root, cases, records):
    completed = []; seen = set(); digest = None
    for index in range(0, len(records), 2):
        reservation = records[index]["event"]
        price = reservation_price(records[index], index)
        expected_case, kind = next_case(cases, completed)
        require(reservation["type"] == "reserve" and expected_case is not None
                and (reservation["case_id"], reservation["kind"]) == (expected_case, kind)
                and reservation["before"] == digest,
                "Out-of-order or excessive reservation")
        dispatch_id = reservation["dispatch_id"]
        require(type(dispatch_id) is str and str(uuid.UUID(dispatch_id)) == dispatch_id
                and dispatch_id not in seen, "Duplicate/invalid dispatch reservation")
        seen.add(dispatch_id)
        if "session_id" in reservation:
            require(1 <= len(completed) < 12 and completed[0][1]["status"] == "success",
                    "Session may only collect remaining first-pass cases")
            session_id = reservation["session_id"]
            require(type(session_id) is str and str(uuid.UUID(session_id)) == session_id,
                    "Invalid session identity")
            session_checkpoint(reservation["checkpoint"])
            through = reservation["checkpoint"]["through"]
            require(through <= len(completed), "Session starts beyond collected evidence")
            if through == len(completed):
                require(all(r.get("session_id") != session_id for r, _ in completed),
                        "Session identity cannot be reused for a new approval")
            else:
                require(completed[-1][0].get("session_id") == session_id
                        and completed[-1][0]["checkpoint"] == reservation["checkpoint"],
                        "Session approval or identity changed")
        else:
            checkpoint(reservation["checkpoint"], len(completed), now=None, per_attempt=price)
        if completed:
            prior = completed[-1][0]["checkpoint"]
            require(money(reservation["checkpoint"]["budget_usd"]) <= money(prior["budget_usd"]),
                    "Recorded reservation budget cannot increase")
            if "session_id" not in reservation:
                prior_billing = next(r["checkpoint"] for r, _ in reversed(completed)
                                     if "session_id" not in r)
                require(money(reservation["checkpoint"]["spent_usd"]) >= money(prior_billing["spent_usd"])
                        and datetime.fromisoformat(reservation["checkpoint"]["as_of"])
                        >= datetime.fromisoformat(prior_billing["as_of"]),
                        "Billing checkpoints cannot move backward")
        require(index + 1 < len(records), "Unresolved reservation; operator review required")
        completion = records[index + 1]["event"]
        require(type(completion) is dict and set(completion) == {
            "type", "dispatch_id", "returncode", "status", "error_code", "evidence_sha256"},
            "Malformed completion")
        require(completion["type"] == "complete" and completion["dispatch_id"] == dispatch_id
                and type(completion["returncode"]) is int and completion["returncode"] == 0
                and completion["status"] in ("success", "error")
                and (completion["error_code"] is None or type(completion["error_code"]) is str),
                "Uncertain collector completion; operator review required")
        digest = completion["evidence_sha256"]
        require(type(digest) is str and len(digest) == 64
                and all(c in "0123456789abcdef" for c in digest), "Malformed evidence digest")
        completed.append((reservation, completion))
    require(evidence(root, completed) == digest, "Collected evidence changed after completion")
    if len(completed) >= 2 and "session_id" not in completed[-1][0]:
        require(not all(c["status"] == "error" and c["error_code"] == "EXTRACTION_SERVICE_ERROR"
                        for _, c in completed[-2:]),
                "Two consecutive extraction-service failures; pause for operator review")
    return completed


def money(value):
    require(type(value) is str, "Money must be a decimal string")
    result = Decimal(value)
    require(result.is_finite() and result >= 0, "Invalid spending amount")
    return result


def checkpoint(value, count, *, now, per_attempt=PER_ATTEMPT):
    require(type(value) is dict and set(value) == {
        "pricing_confirmed", "runtime_confirmed", "spent_usd", "budget_usd", "through", "as_of"},
        "Missing billing/runtime checkpoint")
    require(value["pricing_confirmed"] is True, "Model-specific billing assumptions are unverified")
    require(value["runtime_confirmed"] is True, "Registered runtime settings are unconfirmed")
    require(type(value["through"]) is int and value["through"] == count,
            "Billing checkpoint must cover every previous reservation")
    require(type(value["as_of"]) is str, "Billing status is unknown")
    stamp = datetime.fromisoformat(value["as_of"])
    require(stamp.tzinfo is not None, "Billing checkpoint must have a timezone")
    if now is not None:
        require(0 <= (now - stamp).total_seconds() <= CHECKPOINT_SECONDS,
                "Billing checkpoint is overdue or in the future")
    budget = money(value["budget_usd"]); spent = money(value["spent_usd"])
    require(0 < budget <= MAX_BUDGET, "Budget cannot exceed the proposed $0.50 ceiling")
    require(spent <= per_attempt * count,
            "Observed billing exceeds conditional bounds; stop for operator review")
    require(max(spent, per_attempt * count) + per_attempt <= budget,
            "Next reservation would exceed the accounting budget")


def collector_command(root, case):
    return [sys.executable, "-m", "ai_model_migration_gate.cli", "run",
            "--config", str(root / CONFIG), "--target", "candidate", "--run-id", EXPERIMENT,
            "--case", case, "--max-calls", "1", "--yes"]


def session_checkpoint(value):
    """Reservation policy only: no claim of observed credit or settled billing."""
    require(type(value) is dict and set(value) == {
        "session_policy_version", "budget_usd", "through"}, "Missing session reservation policy")
    require(type(value["session_policy_version"]) is int and value["session_policy_version"] in (2, 3),
            "Unsupported session reservation policy")
    require(type(value["through"]) is int and 1 <= value["through"] < 12
            and (value["session_policy_version"] == 3 or value["through"] == 1),
            "Session must start after reconciled first-pass reservations")
    budget = money(value["budget_usd"])
    require(0 < budget <= MAX_BUDGET and PER_ATTEMPT * 12 <= budget,
            "First-pass session would exceed the accounting budget")


def session_evidence(root, cases, completed):
    """HTTP failures are observations; transport/response ambiguity stops resume."""
    require(completed and completed[0][1]["status"] == "success"
            and all(r["kind"] == "first-pass" for r, _ in completed),
            "Session requires the immutable first success and no retry history")
    for case, (reservation, _) in zip(cases, completed):
        path = saved.safe_path(root, Path("results/candidate") / EXPERIMENT / "attempts"
                               / reservation["case_id"] / "0001.json")
        attempt = CaseResult.model_validate_json(path.read_bytes())
        if attempt.status == ResultStatus.SUCCESS:
            score_case(case, attempt)
        else:
            require(attempt.http_status is not None and 400 <= attempt.http_status <= 599,
                    "Unexpected transport or response failure; operator review required")


def collect_reserved(root, lock, identity, records, completed, reservation, run):
    """Persist before execution; uncertainty always leaves a charged reservation."""
    directory = local_path(root, Path("results/candidate") / EXPERIMENT)
    protected = {path: sha(saved.safe_path(root, path.relative_to(root)).read_bytes())
                 for path in directory.rglob("*.json")}
    append(root, lock, identity, records, reservation)
    collector = run if run is not None else subprocess.run
    result = collector(collector_command(root, reservation["case_id"]), cwd=root, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    require(result.returncode == 0, "Collector failed; reservation remains charged and unresolved")
    require(all(sha(saved.safe_path(root, path.relative_to(root)).read_bytes()) == digest
                for path, digest in protected.items()),
            "Existing evidence changed during collection; reservation remains unresolved")
    number = 1 if reservation["kind"] == "first-pass" else 2
    path = saved.safe_path(root, Path("results/candidate") / EXPERIMENT / "attempts"
                           / reservation["case_id"] / f"{number:04d}.json")
    attempt = CaseResult.model_validate_json(path.read_bytes())
    completion = {"type": "complete", "dispatch_id": reservation["dispatch_id"], "returncode": 0,
                  "status": attempt.status.value, "error_code": attempt.error_code,
                  "evidence_sha256": None}
    completion["evidence_sha256"] = evidence(root, [*completed, (reservation, completion)])
    append(root, lock, identity, records, completion)
    return attempt


def session(root, *, allow_live, approval, confirm, run=None):
    require(allow_live is True, "Session requires --allow-live and separate authorization")
    plan = status(root)
    count = plan["reservations"]
    require(1 <= count < 12 and plan["kind"] == "first-pass"
            and plan["next_case"] == CASE_IDS[count], "No remaining eligible first-pass cases")
    session_checkpoint(approval)
    require(approval["session_policy_version"] == 3 and approval["through"] == count,
            "Session approval must cover the existing reservation count")
    cases, identity = inputs(root)
    with locked(root) as lock:
        session_evidence(root, cases, reconcile(root, cases, history(root, lock, identity)))
    phrase = f"SESSION {EXPERIMENT} {12 - count} REMAINING FIRST-PASS CASES MAY INCUR PROVIDER CHARGES"
    require(confirm(phrase), "Interactive session provider-charge confirmation declined")
    cases, identity = inputs(root)
    session_id = str(uuid.uuid4())
    with locked(root) as lock:
        records = history(root, lock, identity)
        completed = reconcile(root, cases, records)
        require(len(completed) == count, "Session eligibility changed during approval")
        session_evidence(root, cases, completed)
        prior = completed[-1][0]["checkpoint"]
        require(money(approval["budget_usd"]) <= money(prior["budget_usd"]),
                "Session reservation budget cannot increase")
        for case in CASE_IDS[count:]:
            # Recheck local identity, accounting and evidence before EVERY request.
            _, current_identity = inputs(root)
            require(current_identity == identity, "Session identity changed")
            records = history(root, lock, identity)
            completed = reconcile(root, cases, records)
            session_evidence(root, cases, completed)
            session_checkpoint(approval)
            require(next_case(cases, completed) == (case, "first-pass")
                    and len(completed) < 12 and len(completed) < 14, "Session case eligibility changed")
            require(PER_ATTEMPT * (len(completed) + 1) <= money(approval["budget_usd"]),
                    "Next reservation would exceed the accounting budget")
            reservation = {"type": "reserve", "dispatch_id": str(uuid.uuid4()), "case_id": case,
                           "kind": "first-pass", "before": evidence(root, completed),
                           "checkpoint": approval.copy(), "session_id": session_id,
                           "pricing_version": PRICING_VERSION, "reserved_usd": str(PER_ATTEMPT)}
            attempt = collect_reserved(root, lock, identity, records, completed, reservation, run)
            completed = reconcile(root, cases, history(root, lock, identity))
            session_evidence(root, cases, completed)
            classification = (score_case(next(item for item in cases if item.id == case), attempt)
                              .classification.value if attempt.status == ResultStatus.SUCCESS
                              else f"unscored error ({attempt.error_code})")
            print(f"{case}: HTTP {attempt.http_status}, {classification}, "
                  f"{attempt.response_time_ms:.3f} ms")
        require(inputs(root)[1] == identity, "Session identity changed")
        reconcile(root, cases, history(root, lock, identity))
    return 0


def status(root):
    cases, identity = inputs(root)
    ledger, lock_path = accounting_paths(root)
    if not ledger.exists() and not lock_path.exists():
        completed = []; evidence(root, completed)
    else:
        with locked(root) as lock:
            completed = reconcile(root, cases, history(root, lock, identity))
    case, kind = next_case(cases, completed)
    return {"experiment": EXPERIMENT, "reservations": len(completed),
            "pricing_version": PRICING_VERSION,
            "conditional_per_attempt_usd": str(PER_ATTEMPT),
            "recorded_reserved_usd": str(sum((money(r["reserved_usd"]) for r, _ in completed), Decimal(0))),
            "conditional_reserved_usd": str(PER_ATTEMPT * len(completed)),
            "conditional_fourteen_attempts_usd": str(PER_ATTEMPT * 14),
            "next_case": case, "kind": kind,
            "note": "Headroom reprices all reservations conservatively; no spending authorized by status; estimates are not hard provider billing limits"}


def dispatch(root, *, case_id, allow_live, approval, confirm, run=None):
    require(allow_live is True, "Live dispatch requires --allow-live and separate authorization")
    # No accounting files are created until all prerequisites and confirmation pass.
    plan = status(root)
    checkpoint(approval, plan["reservations"], now=datetime.now(timezone.utc))
    require(case_id == plan["next_case"] and case_id is not None, "Case is not the next eligible case")
    phrase = f"DISPATCH {EXPERIMENT} {case_id} MAY INCUR PROVIDER CHARGES"
    require(confirm(phrase), "Interactive provider-charge confirmation declined")
    cases, identity = inputs(root)
    with locked(root, create=True) as lock:
        records = history(root, lock, identity)
        completed = reconcile(root, cases, records)
        case, kind = next_case(cases, completed)
        require(case_id == case and case is not None, "Case eligibility changed")
        checkpoint(approval, len(completed), now=datetime.now(timezone.utc))
        if completed:
            prior = completed[-1][0]["checkpoint"]
            require(money(approval["budget_usd"]) <= money(prior["budget_usd"]),
                    "Reservation budget cannot increase after accounting begins")
            prior_billing = next(r["checkpoint"] for r, _ in reversed(completed)
                                 if "session_id" not in r)
            require(money(approval["spent_usd"]) >= money(prior_billing["spent_usd"])
                    and datetime.fromisoformat(approval["as_of"]) >= datetime.fromisoformat(prior_billing["as_of"]),
                    "Billing checkpoints cannot move backward")
        before = evidence(root, completed)
        reservation = {"type": "reserve", "dispatch_id": str(uuid.uuid4()), "case_id": case,
                       "kind": kind, "before": before, "checkpoint": approval,
                       "pricing_version": PRICING_VERSION,
                       "reserved_usd": str(PER_ATTEMPT)}
        collect_reserved(root, lock, identity, records, completed, reservation, run)
        return 0


def terminal_confirmation(phrase):
    require(sys.stdin.isatty(), "Live dispatch requires an interactive terminal")
    print("Separate live authorization is required. The approved request or session may incur provider charges.")
    if phrase.startswith("SESSION "):
        print("This approves only the remaining first-pass requests under the registered runtime and conditional pricing assumptions.")
        print("Prepaid balance and actual charges cannot be verified or enforced by this controller; no fresh billing check is asserted.")
        print("Interrupt on known unexpected costs or runtime changes. Local reservations are not a provider-side spending cutoff.")
    print("Type exactly: " + phrase)
    return input().strip() == phrase


def main(argv=None, *, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("status", "dispatch", "session"), default="status")
    parser.add_argument("--case")
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--confirm-pricing", action="store_true", help="One-case dispatch pricing confirmation")
    parser.add_argument("--confirm-runtime", action="store_true", help="One-case dispatch runtime confirmation")
    parser.add_argument("--billing-spent", help="Settled experiment spending, including failed requests")
    parser.add_argument("--billing-through", type=int, help="Count of ALL prior reservations reconciled with billing")
    parser.add_argument("--billing-as-of", help="Fresh timezone-aware ISO timestamp of manual settled checkpoint")
    parser.add_argument("--budget", default="0.50", help="Separately authorized ceiling, at most $0.50")
    args = parser.parse_args(argv); root = Path(root).resolve()
    try:
        if args.mode == "status":
            print(json.dumps(status(root), indent=2)); return 0
        if args.mode == "session":
            require(args.case is None and args.billing_spent is None and args.billing_through is None
                    and args.billing_as_of is None, "Session does not accept case selection or settled-billing flags")
            approval = {"session_policy_version": 3, "budget_usd": args.budget,
                        "through": status(root)["reservations"]}
            return session(root, allow_live=args.allow_live, approval=approval, confirm=terminal_confirmation)
        approval = {"pricing_confirmed": args.confirm_pricing, "runtime_confirmed": args.confirm_runtime,
                    "spent_usd": args.billing_spent, "through": args.billing_through,
                    "as_of": args.billing_as_of, "budget_usd": args.budget}
        return dispatch(root, case_id=args.case, allow_live=args.allow_live,
                        approval=approval, confirm=terminal_confirmation)
    except saved.EvaluationError as exc:
        print(f"STOP/2: {exc}", file=sys.stderr)
    except (OSError, ValueError, ArithmeticError, TypeError, KeyError, EOFError, RecursionError):
        print("STOP/2: Cannot validate controller inputs; preserve accounting for operator review", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
