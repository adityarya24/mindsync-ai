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
import uuid
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

_INSERT_FACTS_SQL = '''INSERT INTO facts (fact_id, timestamp, agent, entity, attribute, text, source, confidence)
   VALUES (:fact_id, :timestamp, :agent, :entity, :attribute, :text, :source, :confidence)
   ON CONFLICT(fact_id) DO UPDATE SET
   timestamp=excluded.timestamp,
   agent=excluded.agent,
   entity=excluded.entity,
   attribute=excluded.attribute,
   text=excluded.text,
   source=excluded.source,
   confidence=excluded.confidence
'''


def _quarantine_raw_line(jsonl_path: Path, raw_line: str, reason: str) -> None:
    """Malformed raw lines are quarantined, never silently dropped."""
    dead_letter_path = jsonl_path.parent / "dead_letter.jsonl"
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "error": reason,
        "raw_record": raw_line,
    }
    try:
        with open(dead_letter_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _parse_claimed_facts(claimed_path: Path) -> list[dict]:
    facts: list[dict] = []
    with open(claimed_path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                fact = json.loads(line)
            except json.JSONDecodeError:
                _quarantine_raw_line(claimed_path, line, "malformed_json")
                continue
            if not isinstance(fact, dict):
                _quarantine_raw_line(claimed_path, line, "not_an_object")
                continue

            if not fact.get("fact_id"):
                content = f"{fact.get('agent')}:{fact.get('entity')}:{fact.get('attribute')}:{fact.get('timestamp')}:{fact.get('text')}"
                fact["fact_id"] = hashlib.sha256(content.encode("utf-8")).hexdigest()

            # Fill in any missing columns so one under-populated (but
            # otherwise valid JSON) record can't blow up the whole
            # transaction -- normalize instead of aborting.
            facts.append(
                {
                    "fact_id": fact["fact_id"],
                    "timestamp": fact.get("timestamp") or datetime.now(timezone.utc).isoformat(),
                    "agent": fact.get("agent") or "",
                    "entity": fact.get("entity") or "",
                    "attribute": fact.get("attribute") or "",
                    "text": fact.get("text") or "",
                    "source": fact.get("source") or "",
                    "confidence": float(fact.get("confidence", 1.0) or 1.0),
                }
            )
    return facts


def _migrate_claimed_file(conn: sqlite3.Connection, claimed_path: Path) -> None:
    """Migrate one already-claimed (renamed-aside) JSONL file into SQLite.

    The claimed file is only deleted AFTER the DB transaction commits. On
    any failure it is left in place under its `.migrating-*` name for a
    later call to retry via `_recover_orphan_migrations` -- it is never
    renamed back onto the live path, which could by then have fresh
    concurrent appends that a blind overwrite would destroy.
    """
    facts = _parse_claimed_facts(claimed_path)

    if not facts:
        # Nothing usable (empty, or everything was quarantined) -- safe to
        # remove now, the malformed content already lives in dead_letter.jsonl.
        claimed_path.unlink(missing_ok=True)
        return

    with conn:
        conn.executemany(_INSERT_FACTS_SQL, facts)
    claimed_path.unlink(missing_ok=True)


def _recover_orphan_migrations(conn: sqlite3.Connection, jsonl_path: Path) -> None:
    """Retry any `.migrating-*` files left behind by a prior run that
    claimed a file but crashed/failed before completing the migration."""
    for claimed_path in sorted(jsonl_path.parent.glob(f"{jsonl_path.name}.migrating-*")):
        try:
            _migrate_claimed_file(conn, claimed_path)
        except Exception:
            # Leave it for the next run; don't let one bad orphan block others.
            continue


def _migrate_jsonl(conn: sqlite3.Connection, jsonl_path: Path) -> None:
    """Migrate a legacy JSONL fact file into the SQLite store.

    Safety properties:
    (a) the source is claimed via an atomic rename BEFORE it is read, so we
        never read a file a concurrent writer might still be appending to;
    (b) because the claim is a rename (not a copy/truncate), anything that
        appends to the original path afterwards starts a fresh file that a
        LATER call picks up -- concurrent appends are never lost;
    (c) malformed raw lines are quarantined to dead_letter.jsonl instead of
        aborting the whole migration or being silently dropped;
    (d) the claimed file is deleted only after the DB transaction commits;
        on failure it is left in place for a later retry rather than being
        renamed back onto (and clobbering) the live path.
    """
    _recover_orphan_migrations(conn, jsonl_path)

    if not jsonl_path.exists() or jsonl_path.stat().st_size == 0:
        return

    claimed_path = jsonl_path.with_name(f"{jsonl_path.name}.migrating-{uuid.uuid4().hex}")
    try:
        os.rename(jsonl_path, claimed_path)
    except OSError:
        return  # Nothing to claim (already claimed/rotated by a concurrent run).

    _migrate_claimed_file(conn, claimed_path)

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
