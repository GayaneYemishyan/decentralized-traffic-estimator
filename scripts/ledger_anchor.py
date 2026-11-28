#!/usr/bin/env python3
"""Ledger anchors.

  save   --db DB [--out FILE]      print the current head as one JSON line (also appended to FILE)
  verify --db DB [--anchors FILE]  verify the chain and the newest anchor ('-' reads stdin)

Keep anchors OUTSIDE the ledger's host or volume, or they protect nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestration"))
from src.ledger import Ledger  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("save", "verify"))
    ap.add_argument("--db", required=True)
    ap.add_argument("--out")
    ap.add_argument("--anchors")
    a = ap.parse_args()
    if not Path(a.db).exists():
        print(f"no ledger at {a.db}", file=sys.stderr)
        return 2

    with Ledger(a.db) as led:
        if a.cmd == "save":
            seq, head = led.head()
            line = json.dumps({"seq": seq, "hash": head,
                               "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            print(line)
            if a.out:
                with open(a.out, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            return 0

        anchor = None
        if a.anchors:
            text = sys.stdin.read() if a.anchors == "-" else Path(a.anchors).read_text("utf-8")
            rows = [json.loads(x) for x in text.splitlines() if x.strip()]
            if rows:
                best = max(rows, key=lambda r: r["seq"])  # the newest anchor covers all older entries
                anchor = (best["seq"], best["hash"])
        res = led.verify_chain(anchor=anchor)
        print(f"{'OK' if res else 'TAMPERED'}: {res.entries_checked} entries checked"
              + (f", anchor seq {anchor[0]}" if anchor else ", no anchor")
              + ("" if res else f" | bad seq {res.bad_seq}: {res.error}"))
        return 0 if res else 1


if __name__ == "__main__":
    sys.exit(main())