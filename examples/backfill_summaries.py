"""Rewrite old rule-based summaries as Zhiyan first-person summaries.

This is a deliberate, opt-in batch operation.  It reads the source messages
referenced by each existing summary, asks the configured upstream model to
rewrite it in Zhiyan's first person, updates the memory row in place, and
refreshes the FTS index.  Run a small --limit first.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime

from companion_memory.config import Config
from companion_memory.db import MemoryStore, clean_text
from companion_memory.server import ProxyHandler
from companion_memory.upstream import UpstreamClient


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--limit", type=int, default=0, help="0 means all candidates")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--delay", type=float, default=0.4)
    args = parser.parse_args()

    config = Config.from_env()
    if config.consolidation_mode != "llm":
        raise SystemExit("MEMORY_CONSOLIDATION_MODE must be llm; aborting")
    store = MemoryStore(config.db_path)
    con = store.conn
    holder = type("Holder", (), {})()
    holder.store = store
    holder.config = config
    holder.upstream = UpstreamClient(config)

    rows = con.execute(
        "select id, content, source_message_ids, metadata, key from memories "
        "where conversation_id=? and kind='summary' order by id",
        (args.conversation_id,),
    ).fetchall()
    targets = []
    for row in rows:
        content = str(row["content"] or "")
        if content.lstrip().startswith("- "):
            continue
        try:
            ids = json.loads(row["source_message_ids"] or "[]")
            ids = [int(x) for x in ids]
        except Exception:
            ids = []
        if not ids:
            continue
        q = ",".join("?" for _ in ids)
        messages = con.execute(
            f"select id, role, content, created_at from messages where id in ({q}) order by id",
            ids,
        ).fetchall()
        if len(messages) < len(ids):
            continue
        targets.append((row, [dict(m) for m in messages]))

    print(f"candidates={len(targets)} limit={args.limit or 'all'} dry_run={args.dry_run}")
    if args.dry_run:
        store.close()
        return 0

    selected = targets[: args.limit] if args.limit > 0 else targets
    ok = skipped = failed = 0
    for index, (row, messages) in enumerate(selected, 1):
        try:
            summary = ProxyHandler._summarize_batch(holder, messages)
            summary = clean_text(summary)
            if not summary or not summary.startswith("- ") or "<system_reminder>" in summary:
                print(f"SKIP id={row['id']} reason=not_a_bullet_summary")
                skipped += 1
                continue
            metadata = json.loads(row["metadata"] or "{}")
            metadata["backfill"] = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "model": config.effective_summary_model(),
                "from": "rule-based",
            }
            con.execute(
                "update memories set content=?, metadata=? where id=?",
                (summary, json.dumps(metadata, ensure_ascii=False), row["id"]),
            )
            store._fts_delete(int(row["id"]))
            store._fts_add(int(row["id"]), summary, row["key"])
            con.commit()
            ok += 1
            print(f"OK {index}/{len(selected)} id={row['id']} chars={len(summary)}")
        except Exception as exc:
            failed += 1
            print(f"ERROR id={row['id']} {type(exc).__name__}: {exc}")
        time.sleep(max(0.0, args.delay))

    print(f"done ok={ok} skipped={skipped} failed={failed}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
