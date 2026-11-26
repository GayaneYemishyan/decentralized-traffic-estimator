#!/usr/bin/env python3
"""End-to-end acceptance checks (P4).

    python scripts/integration_test.py                # pipeline only: no Docker, no Go
    python scripts/integration_test.py --cluster      # pipeline + the running compose cluster
    python scripts/integration_test.py --cluster-only

Pipeline mode feeds Vika's synthetic generator through the real microservice
(Lilit's inference, our triggers, a real ledger) at 5 s intervals and checks the
agreed acceptance table. A failing row is calibration feedback, not a test bug:
the detail column shows the numbers.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "orchestration")]

STEPS, INTERVAL_S, LANES = 40, 5.0, 4
WARMUP = 3          # steps ignored while smoothing settles
JAM_START = 5       # cv_module's "jam" scenario switches at step 5
JAM_WINDOW = 6      # steps (30 s) to reach score >= 0.80 and confidence >= 0.65
MIN_SCORE, MIN_CONF = 0.80, 0.65
SCENARIOS = ("free", "rising", "congested", "jam")


@dataclass
class Step:
    seq: int
    local_state: str
    score: float      # regional score: what the triggers see
    confidence: float
    actions: list[str]


Check = tuple[str, bool, str]


# --------------------------------------------------------------------------- pipeline
def run_scenario(scenario: str, seed: int) -> tuple[list[Step | None], bool, list[str]]:
    from cv_module.synthetic import generate_synthetic_metrics
    from src.inference_adapter import LilitInference
    from src.ledger import Ledger
    from src.microservice import Microservice, ServiceConfig
    from src.serializer import utc_now_iso

    random.seed(seed)
    t0 = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
    now = [t0]
    steps: list[Step | None] = []
    with tempfile.TemporaryDirectory() as tmp, Ledger(Path(tmp) / "it.db") as ledger:
        svc = Microservice(ServiceConfig(ledger_path=str(Path(tmp) / "it.db"), node_id="node-it"),
                           bridge=None, ledger=ledger, inference=LilitInference(),
                           clock=lambda: now[0], handlers=[])
        svc.resolve_identity()
        for seq in range(STEPS):
            start = t0 + timedelta(seconds=seq * INTERVAL_S)
            end = start + timedelta(seconds=INTERVAL_S)
            now[0] = end
            metrics = {
                "schema_version": "0.2.0", "node_id": "node-it", "seq": seq,
                "interval_start": utc_now_iso(start), "interval_end": utc_now_iso(end), "source": "synthetic",
                "lanes": [{"lane_id": f"lane-{i}", **generate_synthetic_metrics(scenario, INTERVAL_S, step=seq)}
                          for i in range(1, LANES + 1)],
            }
            res = svc.handle_metrics(metrics)
            if res is None or res.regional is None:
                steps.append(None)
                continue
            steps.append(Step(seq, res.local["state"], res.regional["congestion_score"],
                              res.regional["confidence"], [a.kind for a in res.actions]))
        chain = ledger.verify_chain()
        recorded = [e.payload["kind"] for e in ledger.iter_entries() if e.entry_type == "action"]
    return steps, bool(chain), recorded


def pipeline_checks(scenario: str, seed: int) -> list[Check]:
    steps, chain_ok, recorded = run_scenario(scenario, seed)
    out: list[Check] = []

    def add(label: str, ok: bool, detail: str = "") -> None:
        out.append((f"{scenario}: {label}", ok, detail))

    good = [s for s in steps if s is not None]
    add("every interval produced a state", len(good) == STEPS, f"{len(good)}/{STEPS}")
    add("ledger chain verifies", chain_ok)
    if not good:
        return out
    fired = [(s.seq, k) for s in good for k in s.actions]
    add("ledger records exactly the fired actions", recorded == [k for _, k in fired], f"{recorded}")
    peak = max(good, key=lambda s: s.score)
    summary = f"peak score {peak.score:.3f} (step {peak.seq}), conf at peak {peak.confidence:.3f}"

    if scenario == "free":
        add("state stays free", all(s.local_state == "free" for s in good),
            f"states {sorted({s.local_state for s in good})}")
        add("no alert", not fired, summary)
    elif scenario == "rising":
        first = lambda label: next((s.seq for s in good if s.local_state == label), None)  # noqa: E731
        moderate, congested = first("moderate"), first("congested")
        add("crosses moderate", moderate is not None, summary)
        add("then reaches congested", congested is not None and moderate is not None and moderate <= congested,
            f"moderate at {moderate}, congested at {congested}")
    elif scenario == "congested":
        late = [s for s in good if s.seq >= WARMUP]
        share = sum(s.score >= 0.70 for s in late) / max(1, len(late))
        add("score >= 0.70 for most steps (>= 60%)", share >= 0.60, f"{share:.0%} of steps; {summary}")
    elif scenario == "jam":
        window = [s for s in good if JAM_START <= s.seq < JAM_START + JAM_WINDOW]
        hit = next((s for s in window if s.score >= MIN_SCORE and s.confidence >= MIN_CONF), None)
        add(f"score >= {MIN_SCORE} and confidence >= {MIN_CONF} within {JAM_WINDOW} steps of the jam", hit is not None,
            f"first at step {hit.seq}" if hit else summary)
        alerts = [seq for seq, kind in fired if kind == "congestion_alert"]
        add("alert fires within window + 2 steps", bool(alerts) and alerts[0] <= JAM_START + JAM_WINDOW + 1,
            f"alert steps {alerts}")
        add("no alert before the jam starts", not any(a < JAM_START for a in alerts))
    return out


# --------------------------------------------------------------------------- cluster
_SNIPPET = """
import json
from src.ledger import Ledger
with Ledger("/data/ledger.db") as led:
    r = led.verify_chain()
    es = list(led.iter_entries())
print(json.dumps({"ok": bool(r), "checked": r.entries_checked,
  "origins": sorted({e.origin_node_id for e in es if e.entry_type == "state" and e.message_id}),
  "actions": [e.payload["kind"] for e in es if e.entry_type == "action"]}))
"""


def node_report(n: int) -> dict | None:
    p = subprocess.run(["docker", "compose", "exec", "-T", f"node{n}", "python", "-c", _SNIPPET],
                       cwd=ROOT, capture_output=True, text=True, timeout=60,
                       env={**os.environ, "NODE1_ID": os.environ.get("NODE1_ID", "pending")})
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except ValueError:
        return None


def cluster_checks(nodes: int, wait_s: float) -> list[Check]:
    deadline = time.monotonic() + wait_s
    reports: dict[int, dict | None] = {}
    while True:
        reports = {n: node_report(n) for n in range(1, nodes + 1)}
        live = [r for r in reports.values() if r]
        converged = (len(live) == nodes and all(len(r["origins"]) >= nodes for r in live)
                     and any("congestion_alert" in r["actions"] for r in live))
        if converged or time.monotonic() > deadline:
            break
        time.sleep(5)

    live = {n: r for n, r in reports.items() if r}
    return [
        ("cluster: every node's ledger is readable", len(live) == nodes, f"{len(live)}/{nodes} answered"),
        ("cluster: every ledger chain verifies", bool(live) and all(r["ok"] for r in live.values()),
         ", ".join(f"node{n}={r['checked']} entries" for n, r in live.items())),
        ("cluster: every node recorded states from all nodes (gossip + identity checks work)",
         bool(live) and all(len(r["origins"]) >= nodes for r in live.values()),
         ", ".join(f"node{n}={len(r['origins'])} origins" for n, r in live.items())),
        ("cluster: at least one node raised a congestion_alert",
         any("congestion_alert" in r["actions"] for r in live.values()),
         ", ".join(f"node{n}={r['actions']}" for n, r in live.items())),
    ]


# --------------------------------------------------------------------------- main
def report(checks: list[Check]) -> bool:
    width = max(len(c[0]) for c in checks)
    for label, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {label.ljust(width)}  {detail}")
    failed = sum(not c[1] for c in checks)
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return failed == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cluster", action="store_true", help="also check the running docker compose cluster")
    ap.add_argument("--cluster-only", action="store_true")
    ap.add_argument("--nodes", type=int, default=3)
    ap.add_argument("--wait", type=float, default=150.0, help="seconds to wait for the cluster to converge")
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args()
    logging.basicConfig(level=logging.ERROR)

    checks: list[Check] = []
    if not args.cluster_only:
        try:
            for scenario in SCENARIOS:
                checks += pipeline_checks(scenario, args.seed)
        except ImportError as exc:
            print(f"cannot import a module ({exc}); run from the repo root with inference/ and cv_module/ present",
                  file=sys.stderr)
            return 2
    if args.cluster or args.cluster_only:
        checks += cluster_checks(args.nodes, args.wait)
    return 0 if report(checks) else 1


if __name__ == "__main__":
    sys.exit(main())