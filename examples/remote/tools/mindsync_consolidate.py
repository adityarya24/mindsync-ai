#!/usr/bin/env python3
"""Sample consolidator — groups facts into compiled-truth/*.md."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    store = root / "data" / "facts.db"
    out_dir = root / "compiled-truth"
    out_dir.mkdir(parents=True, exist_ok=True)

    by_entity: dict[str, list[dict]] = defaultdict(list)
    if store.exists():
        try:
            conn = sqlite3.connect(store)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM facts ORDER BY timestamp ASC").fetchall()
            for row in rows:
                rec = dict(row)
                entity = str(rec.get("entity") or "unknown")
                by_entity[entity].append(rec)
        except sqlite3.Error:
            pass

    for entity, rows in by_entity.items():
        path = out_dir / f"{entity}.md"
        lines = [f"# {entity}", ""]
        for rec in rows[-50:]:
            attr = rec.get("attribute", "")
            text = rec.get("text", "")
            agent = rec.get("agent", "")
            lines.append(f"- **{attr}** ({agent}): {text}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps({"ok": True, "message": f"Wrote {len(by_entity)} truth file(s) to {out_dir}"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
