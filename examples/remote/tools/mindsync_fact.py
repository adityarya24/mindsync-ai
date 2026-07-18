#!/usr/bin/env python3
"""Sample remote fact writer — replace storage with your database of choice."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def _init_db(store_path: Path) -> sqlite3.Connection:
    store_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(store_path)
    conn.execute('''CREATE TABLE IF NOT EXISTS facts (
        fact_id TEXT PRIMARY KEY,
        timestamp TEXT,
        agent TEXT,
        entity TEXT,
        attribute TEXT,
        text TEXT,
        source TEXT,
        confidence REAL
    )''')
    conn.commit()
    return conn

def main() -> int:
    parser = argparse.ArgumentParser(description="MindSync remote fact writer (sample)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    write = sub.add_parser("write")
    write.add_argument("--fact_id", required=True)
    write.add_argument("--agent", required=True)
    write.add_argument("--entity", required=True)
    write.add_argument("--attribute", required=True)
    write.add_argument("--text", required=True)
    write.add_argument("--source", required=True)
    write.add_argument("--confidence", type=float, default=1.0)
    
    batch = sub.add_parser("batch")
    batch.add_argument("--payload", required=True)
    
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    store = root / "data" / "facts.db"
    
    try:
        conn = _init_db(store)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"DB init failed: {exc}"}))
        return 1

    facts = []
    if args.cmd == "write":
        facts.append({
            "fact_id": args.fact_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": args.agent,
            "entity": args.entity,
            "attribute": args.attribute,
            "text": args.text,
            "source": args.source,
            "confidence": args.confidence,
        })
    elif args.cmd == "batch":
        try:
            payload_json = args.payload
            facts = json.loads(payload_json)
            # Add timestamp if missing
            now = datetime.now(timezone.utc).isoformat()
            for f in facts:
                if not f.get("timestamp"):
                    f["timestamp"] = now
        except json.JSONDecodeError as exc:
            print(json.dumps({"ok": False, "error": f"Batch JSON decode failed: {exc}"}))
            return 1
            
    try:
        with conn:
            conn.executemany(
                '''INSERT INTO facts (fact_id, timestamp, agent, entity, attribute, text, source, confidence)
                   VALUES (:fact_id, :timestamp, :agent, :entity, :attribute, :text, :source, :confidence)
                   ON CONFLICT(fact_id) DO UPDATE SET
                   timestamp=excluded.timestamp,
                   agent=excluded.agent,
                   entity=excluded.entity,
                   attribute=excluded.attribute,
                   text=excluded.text,
                   source=excluded.source,
                   confidence=excluded.confidence
                ''',
                facts
            )
    except sqlite3.Error as exc:
        print(json.dumps({"ok": False, "error": f"DB write failed: {exc}"}))
        return 1
        
    print(json.dumps({"ok": True, "stored": str(store), "count": len(facts)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
