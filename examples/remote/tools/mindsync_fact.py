#!/usr/bin/env python3
"""Sample remote fact writer — replace storage with your database of choice."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path

# Safe identifiers matching bridge.py validator
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}$")

def validate_id(label: str, value: str) -> str:
    if not value or not _SAFE_ID.match(value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value

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

def _migrate_jsonl(conn: sqlite3.Connection, jsonl_path: Path) -> None:
    if not jsonl_path.exists() or jsonl_path.stat().st_size == 0:
        return

    facts = []
    with open(jsonl_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                fact = json.loads(line)
                if isinstance(fact, dict):
                    # Ensure it has a fact_id
                    if not fact.get("fact_id"):
                        content = f"{fact.get('agent')}:{fact.get('entity')}:{fact.get('attribute')}:{fact.get('timestamp')}:{fact.get('text')}"
                        fact["fact_id"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    facts.append(fact)
            except Exception:
                continue

    if not facts:
        try:
            jsonl_path.unlink()
        except OSError:
            pass
        return

    temp_jsonl = jsonl_path.with_suffix(".jsonl.migrating")
    try:
        os.rename(jsonl_path, temp_jsonl)
    except OSError:
        return  # Already migrating or locked

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
        temp_jsonl.unlink()
    except Exception:
        try:
            os.rename(temp_jsonl, jsonl_path)
        except OSError:
            pass
        raise

def main() -> int:
    parser = argparse.ArgumentParser(description="MindSync remote fact writer (sample)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    write = sub.add_parser("write")
    write.add_argument("--fact_id", required=False) # Optional for backward compatibility
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
    jsonl_store = root / "data" / "facts.jsonl"
    
    try:
        conn = _init_db(store)
        _migrate_jsonl(conn, jsonl_store)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"DB init or migration failed: {exc}"}))
        return 1

    if args.cmd == "write":
        try:
            agent = validate_id("agent", args.agent)
            entity = validate_id("entity", args.entity)
            attribute = validate_id("attribute", args.attribute)
            source = validate_id("source", args.source)
            fid = args.fact_id
            if not fid:
                content = f"{agent}:{entity}:{attribute}::{args.text}"
                fid = hashlib.sha256(content.encode("utf-8")).hexdigest()
            fid = validate_id("fact_id", fid)
            conf = float(args.confidence)
            if not 0.0 <= conf <= 1.0:
                raise ValueError("confidence must be between 0.0 and 1.0")
        except (ValueError, TypeError) as exc:
            print(json.dumps({"ok": False, "error": f"Validation failed: {exc}"}))
            return 1

        fact = {
            "fact_id": fid,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": agent,
            "entity": entity,
            "attribute": attribute,
            "text": args.text,
            "source": source,
            "confidence": conf,
        }
        try:
            with conn:
                conn.execute(
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
                    fact
                )
            print(json.dumps({"ok": True, "stored": str(store), "count": 1, "success_ids": [fid]}))
            return 0
        except sqlite3.Error as exc:
            print(json.dumps({"ok": False, "error": f"DB write failed: {exc}"}))
            return 1

    elif args.cmd == "batch":
        try:
            payload_json = args.payload
            raw_facts = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            print(json.dumps({"ok": False, "error": f"Batch JSON decode failed: {exc}"}))
            return 1

        success_ids = []
        failed = []
        now = datetime.now(timezone.utc).isoformat()

        # Validate and insert individually inside a transaction to record per-item outcome
        try:
            with conn:
                for idx, f in enumerate(raw_facts):
                    if not isinstance(f, dict):
                        failed.append({"index": idx, "error": "Fact is not a dictionary"})
                        continue
                    
                    fid = f.get("fact_id")
                    if not fid:
                        # Fallback generation
                        content = f"{f.get('agent')}:{f.get('entity')}:{f.get('attribute')}::{f.get('text')}"
                        fid = hashlib.sha256(content.encode("utf-8")).hexdigest()

                    try:
                        agent = validate_id("agent", f.get("agent", ""))
                        entity = validate_id("entity", f.get("entity", ""))
                        attribute = validate_id("attribute", f.get("attribute", ""))
                        source = validate_id("source", f.get("source", ""))
                        validate_id("fact_id", fid)
                        conf = float(f.get("confidence", 1.0))
                        if not 0.0 <= conf <= 1.0:
                            raise ValueError("confidence must be between 0.0 and 1.0")
                    except (ValueError, TypeError) as exc:
                        failed.append({"fact_id": fid, "error": f"Validation failed: {exc}"})
                        continue

                    fact = {
                        "fact_id": fid,
                        "timestamp": f.get("timestamp") or now,
                        "agent": agent,
                        "entity": entity,
                        "attribute": attribute,
                        "text": f.get("text", ""),
                        "source": source,
                        "confidence": conf,
                    }

                    try:
                        conn.execute(
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
                            fact
                        )
                        success_ids.append(fid)
                    except sqlite3.Error as exc:
                        failed.append({"fact_id": fid, "error": f"Database error: {exc}"})

            print(json.dumps({"ok": True, "stored": str(store), "success_ids": success_ids, "failed": failed}))
            return 0
        except sqlite3.Error as exc:
            print(json.dumps({"ok": False, "error": f"Batch transaction failed: {exc}"}))
            return 1

    return 0

if __name__ == "__main__":
    sys.exit(main())
