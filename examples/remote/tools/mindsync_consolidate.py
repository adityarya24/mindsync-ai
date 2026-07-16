#!/usr/bin/env python3
"""Sample consolidator — groups facts into compiled-truth/*.md."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    store = root / "data" / "facts.jsonl"
    out_dir = root / "compiled-truth"
    out_dir.mkdir(parents=True, exist_ok=True)

    by_entity: dict[str, list[dict]] = defaultdict(list)
    if store.exists():
        for line in store.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            entity = str(rec.get("entity") or "unknown")
            by_entity[entity].append(rec)

    for entity, rows in by_entity.items():
        path = out_dir / f"{entity}.md"
        lines = [f"# {entity}", ""]
        for rec in rows[-50:]:
            attr = rec.get("attribute", "")
            text = rec.get("text", "")
            agent = rec.get("agent", "")
            lines.append(f"- **{attr}** ({agent}): {text}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"Wrote {len(by_entity)} truth file(s) to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
