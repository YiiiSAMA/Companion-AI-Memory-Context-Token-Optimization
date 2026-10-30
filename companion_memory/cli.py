"""Command line interface for the memory proxy."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .config import Config
from .curator import MemoryCurator
from .db import MemoryStore
from .markdown import MarkdownMirror
from .self_continuity import SelfContinuity
from .server import run


def load_dotenv(path: str = ".env") -> None:
    dotenv = Path(path)
    if not dotenv.is_file():
        return
    for raw in dotenv.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _config(args: argparse.Namespace) -> Config:
    load_dotenv(args.env)
    config = Config.from_env()
    if args.db:
        config.db_path = args.db
    if args.upstream_base_url:
        config.upstream_base_url = args.upstream_base_url
    if args.upstream_api_key:
        config.upstream_api_key = args.upstream_api_key
    if args.upstream_model:
        config.upstream_model = args.upstream_model
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="companion-memory")
    parser.add_argument("--env", default=".env", help="path to .env file")
    parser.add_argument("--db", help="override MEMORY_DB")
    parser.add_argument("--upstream-base-url", help="override UPSTREAM_BASE_URL")
    parser.add_argument("--upstream-api-key", help="override UPSTREAM_API_KEY")
    parser.add_argument("--upstream-model", help="override UPSTREAM_MODEL")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the HTTP proxy")
    serve.add_argument("--host", help="override MEMORY_HOST")
    serve.add_argument("--port", type=int, help="override MEMORY_PORT")

    stats = sub.add_parser("stats", help="print memory and token-savings statistics")
    stats.add_argument("--conversation-id", default=None)

    search = sub.add_parser("search", help="search long-term memories")
    search.add_argument("query")
    search.add_argument("--conversation-id", default="default")
    search.add_argument("--limit", type=int, default=8)

    export = sub.add_parser("export", help="export one conversation as JSON")
    export.add_argument("--conversation-id", default="default")

    export_md = sub.add_parser("export-md", help="render active memories as Markdown cards")
    export_md.add_argument("--conversation-id", default="default")
    export_md.add_argument("--output", default=None, help="override MEMORY_MARKDOWN_DIR")

    consolidate = sub.add_parser("consolidate", help="compress old unconsolidated turns")
    consolidate.add_argument("--conversation-id", default="default")

    curate = sub.add_parser("curate", help="review and adjust memory scores")
    curate.add_argument("--conversation-id", default="default")
    curate.add_argument("--limit", type=int, default=30)
    curate.add_argument("--dry-run", action="store_true")

    self_note = sub.add_parser("self-note", help="write yesterday's first-person self note")
    self_note.add_argument("--conversation-id", default="default")
    self_note.add_argument("--hours", type=int, default=24)
    self_note.add_argument("--dry-run", action="store_true")

    sub.add_parser("rebuild-fts", help="rebuild the FTS index from stored memories")

    args = parser.parse_args(argv)
    if args.command is None:
        args.command = "serve"
    config = _config(args)

    if args.command == "serve":
        if getattr(args, "host", None):
            config.host = args.host
        if getattr(args, "port", None):
            config.port = args.port
        run(config)
        return 0

    store = MemoryStore(config.db_path)
    try:
        if args.command == "stats":
            print(json.dumps(store.stats(args.conversation_id), ensure_ascii=False, indent=2))
        elif args.command == "search":
            hits = store.search_memories(
                args.conversation_id,
                args.query,
                limit=args.limit,
                max_tokens=config.max_memory_tokens,
            )
            print(json.dumps(hits, ensure_ascii=False, indent=2))
        elif args.command == "export":
            print(json.dumps(store.export_conversation(args.conversation_id), ensure_ascii=False, indent=2))
        elif args.command == "export-md":
            mirror = MarkdownMirror(store, config)
            print(json.dumps(mirror.export(args.conversation_id, args.output), ensure_ascii=False, indent=2))
        elif args.command == "consolidate":
            result = store.consolidate(
                args.conversation_id,
                threshold=config.consolidation_threshold,
                batch_size=config.consolidation_batch,
                keep_recent=max(4, config.recent_turns * 2),
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "rebuild-fts":
            print(json.dumps({"indexed": store.rebuild_fts()}))
        elif args.command == "curate":
            result = MemoryCurator(store, config).run(
                args.conversation_id,
                limit=args.limit,
                dry_run=args.dry_run,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "self-note":
            result = SelfContinuity(store, config).generate(
                args.conversation_id,
                hours=args.hours,
                dry_run=args.dry_run,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
