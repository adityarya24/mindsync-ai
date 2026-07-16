#!/usr/bin/env python3
"""Sample remote fact writer — replace storage with your database of choice."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="MindSync remote fact writer (sample)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    write = sub.add_parser("write")
    write.add_argument("--agent", required=True)
    write.add_argument("--entity", required=True)
    write.add_argument("--attribute", required=True)
    write.add_argument("--text", required=True)
    write.add_argument("--source", required=True)
    write.add_argument("--confidence", type=float, default=1.0)
    args = parser.parse_args()

    if args.cmd != "write":
        parser.error("unknown command")

    root = Path(__file__).resolve().parents[1]
    store = root / "data" / "facts.jsonl"
    store.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": args.agent,
        "entity": args.entity,
        "attribute": args.attribute,
        "text": args.text,
        "source": args.source,
        "confidence": args.confidence,
    }
    with store.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps({"ok": True, "stored": str(store)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
