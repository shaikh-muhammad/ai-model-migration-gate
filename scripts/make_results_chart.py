"""Generate docs/results.png from the committed evidence catalog (experiments.json).

Every bar comes from saved gate output. Nothing is typed in by hand except the
display names of the runs. INVALID runs have no complete metrics, so they are
not drawn.

Usage:  python scripts/make_results_chart.py   (needs matplotlib)
"""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "experiments.json"
OUT = ROOT / "docs" / "results.png"

NAMES = {
    "experiment-02": "Luna (Exp 02)",
    "experiment-03": "Luna (Exp 03)",
    "experiment-04": "GPT-4.1 Mini (Exp 04)",
    "control-01": "Same-model control 1",
    "control-02": "Same-model control 2 (calibrated)",
}

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
PASS_C = "#0ca30c"   # status: good
BLOCK_C = "#d03b3b"  # status: critical


def reason(failed):
    if "max_critical_dangerous_mistakes" in failed or "max_dangerous_regressions" in failed:
        return "unsafe"
    if "max_slow_case_seconds" in failed:
        return "too slow"
    if "min_correct_cases" in failed:
        return "accuracy"
    return "rule failed"


def load_rows():
    rows = []
    for entry in json.loads(CATALOG.read_text())["experiments"]:
        run_id = entry["run_id"]
        expected = entry["expected"]
        metrics = expected["metrics"]
        if run_id not in NAMES or metrics is None:
            continue
        failed = [r["name"] for r in expected["rules"] if r["status"] == "fail"]
        limit_ms = next(r["threshold"] for r in expected["rules"] if r["name"] == "max_slow_case_seconds")
        rows.append(
            {
                "label": NAMES[run_id],
                "seconds": metrics["p95_response_time_ms"] / 1000,
                "limit": limit_ms / 1000,
                "decision": expected["decision"],
                "why": "calibrated rules" if expected["decision"] == "PASS" else reason(failed),
                "correct": metrics["correct_cases"],
                "total": metrics["total_cases"],
                "dangerous": metrics["dangerous_cases"],
            }
        )
    return rows


def main():
    rows = load_rows()
    limit = rows[0]["limit"]
    fig, ax = plt.subplots(figsize=(10, 4.6), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    ys = list(range(len(rows)))[::-1]
    for y, r in zip(ys, rows):
        color = PASS_C if r["decision"] == "PASS" else BLOCK_C
        ax.barh(y, r["seconds"], height=0.46, color=color, zorder=3)
        ax.text(r["seconds"] + 0.08, y, f'{r["seconds"]:.1f} s', va="center", ha="left",
                fontsize=10, color=INK, zorder=4)
        ax.text(8.15, y + 0.12, f'{r["decision"]} \u00b7 {r["why"]}', va="center", ha="left",
                fontsize=10, fontweight="bold", color=INK)
        ax.text(8.15, y - 0.20, f'{r["correct"]}/{r["total"]} correct \u00b7 {r["dangerous"]} dangerous',
                va="center", ha="left", fontsize=9, color=INK_2)

    ax.axvline(limit, color=INK, linestyle=(0, (4, 3)), linewidth=1.2, zorder=2)
    ax.text(limit + 0.06, len(rows) - 0.42, f"limit: {limit:.0f} s", fontsize=9, color=INK, va="bottom")

    ax.set_yticks(ys)
    ax.set_yticklabels([r["label"] for r in rows], fontsize=10, color=INK)
    ax.set_xlim(0, 8)
    ax.set_ylim(-0.6, len(rows) - 0.1)
    ax.set_xlabel("Slowest response among 12 labels (seconds)", fontsize=9, color=INK_2)
    ax.tick_params(axis="x", colors=INK_2, labelsize=9, length=0)
    ax.tick_params(axis="y", length=0)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8, zorder=0)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)

    fig.text(0.02, 0.96, "Every complete run, against the frozen 5-second limit", fontsize=12,
             fontweight="bold", color=INK, va="top")
    fig.text(0.02, 0.905, "Runs with missing responses (INVALID) have no complete result and are not drawn.",
             fontsize=9, color=INK_2, va="top")
    fig.subplots_adjust(left=0.26, right=0.70, top=0.84, bottom=0.14)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, facecolor=SURFACE)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
