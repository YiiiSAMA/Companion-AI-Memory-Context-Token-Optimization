"""SQLite-backed episodic + semantic memory store.

Design notes
------------
* Raw turns are stored as *episodic* messages.
* Stable facts/preferences are stored as *semantic* memories and can supersede
  older values (e.g. a changed job).
* Old turns are consolidated into compact *summary* memories so the proxy can
  drop raw history without losing the thread.
* FTS5 indexes a CJK-friendly, character-segmented view of memory text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .tokens import compact_whitespace, estimate_tokens, text_of

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_SYSTEM_REMINDER_RE = re.compile(r"<system_reminder>.*?(?:</system_reminder>|$)", re.S)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

_EXTRACT_PATTERNS: list[tuple[str, str, re.Pattern[str], float, int]] = [
    ("identity", "name", re.compile(r"(?:我叫|我的名字是|叫我)([^\s，。！？,.!?\n]{1,16})"), 0.95, 1),
    (
        "identity",
        "birthday",
        re.compile(
            r"(?:我的生日是|生日是|我生日)([0-9]{1,4}[年/\-][0-9]{1,2}[月/\-][0-9]{1,2}日?|[0-9]{1,2}月[0-9]{1,2}日)"
        ),
        0.9,
        1,
    ),
    ("preference", "like", re.compile(r"我(?:很|最|特别)?(?:喜欢|爱)([^，。！？,.!?\n]{1,40})"), 0.75, 0),
    ("preference", "dislike", re.compile(r"我(?:不喜欢|讨厌|受不了|怕)([^，。！？,.!?\n]{1,40})"), 0.75, 0),
    ("fact", "location", re.compile(r"我(?:住在|家在)([^，。！？,.!?\n]{1,30})"), 0.8, 0),
    ("fact", "job", re.compile(r"我(?:在|做)([^，。！？,.!?\n]{1,30})(?:工作|上班|这行)"), 0.7, 0),
    ("fact", "explicit", re.compile(r"(?:记住|记一下|帮我记着)[：:，, ]*(.{1,100})"), 0.85, 0),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def date_of(value: str) -> str:
    return (value or "")[:10]


def clean_text(value: str) -> str:
    """Remove AstrBot's injected timestamps and collapse whitespace."""
    return compact_whitespace(_SYSTEM_REMINDER_RE.sub("", value or ""))


def clean_text_keep_lines(value: str) -> str:
    """Remove injected timestamps but preserve intentional Markdown lines."""
    raw = _SYSTEM_REMINDER_RE.sub("", value or "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def message_text(message: dict) -> str:
    return clean_text(text_of(message.get("content")))


def message_hash(message: dict) -> str:
    payload = json.dumps(
        {
            "role": message.get("role"),
            "content": message.get("content"),
            "tool_calls": message.get("tool_calls"),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def segment_for_fts(text: str) -> str:
    """Insert spaces around CJK characters so unicode61 can index them."""
    out: list[str] = []
    for char in (text or "").lower():
        if _CJK_RE.match(char):
            out.append(f" {char} ")
        elif char.isalnum() or char == "_":
            out.append(char)
        else:
            out.append(" ")
    return re.sub(r"\s+", " ", "".join(out)).strip()


def fts_terms(query: str, limit: int = 24) -> list[str]:
    seen: list[str] = []
    for term in segment_for_fts(query).split():
        if term not in seen:
            seen.append(term)
        if len(seen) >= limit:
            break
    return seen


class MemoryStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _create_schema(self) -> None:
        with self._lock, self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    tool_calls TEXT,
                    token_count INTEGER NOT NULL DEFAULT 0,
                    message_hash TEXT NOT NULL,
                    consolidated INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE (conversation_id, position)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_cid_position
                    ON messages (conversation_id, position);
                CREATE INDEX IF NOT EXISTS idx_messages_cid_consolidated
                    ON messages (conversation_id, consolidated, position);

                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    key TEXT,
                    content TEXT NOT NULL,
                    importance REAL NOT NULL DEFAULT 0.5,
                    confidence REAL NOT NULL DEFAULT 0.8,
                    urgency REAL NOT NULL DEFAULT 0.0,
                    due_at TEXT,
                    source_message_ids TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    superseded_by INTEGER,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_accessed_at TEXT,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_memories_cid_status
                    ON memories (conversation_id, status, importance);
                CREATE INDEX IF NOT EXISTS idx_memories_cid_key
                    ON memories (conversation_id, key, status);

                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                    USING fts5(memory_id UNINDEXED, search_text, tokenize='unicode61');

                CREATE TABLE IF NOT EXISTS memory_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    model TEXT,
                    proposed INTEGER NOT NULL DEFAULT 0,
                    applied INTEGER NOT NULL DEFAULT 0,
                    rejected INTEGER NOT NULL DEFAULT 0,
                    changes TEXT NOT NULL DEFAULT '[]',
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_memory_reviews_cid
                    ON memory_reviews (conversation_id, created_at);

                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    original_tokens INTEGER NOT NULL DEFAULT 0,
                    optimized_tokens INTEGER NOT NULL DEFAULT 0,
                    saved_tokens INTEGER NOT NULL DEFAULT 0,
                    memory_tokens INTEGER NOT NULL DEFAULT 0,
                    memory_count INTEGER NOT NULL DEFAULT 0,
                    dropped_messages INTEGER NOT NULL DEFAULT 0,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    model TEXT,
                    upstream_status INTEGER,
                    latency_ms INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_request_logs_cid
                    ON request_logs (conversation_id, created_at);
                """
            )
        self._ensure_memory_columns()

    def _ensure_memory_columns(self) -> None:
        with self._lock, self.conn:
            cols = {row[1] for row in self.conn.execute("PRAGMA table_info(memories)")}
            if "urgency" not in cols:
                self.conn.execute("ALTER TABLE memories ADD COLUMN urgency REAL NOT NULL DEFAULT 0.0")
            if "due_at" not in cols:
                self.conn.execute("ALTER TABLE memories ADD COLUMN due_at TEXT")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_cid_urgency "
                "ON memories (conversation_id, status, urgency)"
            )

    # ------------------------------------------------------------------
    # Conversations and raw turns
    # ------------------------------------------------------------------
    def ensure_conversation(self, conversation_id: str) -> None:
        now = now_iso()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO conversations (id, created_at, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at",
                (conversation_id, now, now),
            )

    def upsert_messages(self, conversation_id: str, messages: list[dict]) -> int:
        """Persist user/assistant/tool turns by their position in the request."""
        self.ensure_conversation(conversation_id)
        stored = 0
        now = now_iso()
        with self._lock, self.conn:
            for position, message in enumerate(messages):
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role") or "")
                if role == "system":
                    continue
                content = message_text(message)
                tool_calls = message.get("tool_calls")
                if not content and not tool_calls:
                    continue
                tool_json = json.dumps(tool_calls, ensure_ascii=False, default=str) if tool_calls else None
                digest = message_hash(message)
                self.conn.execute(
                    """
                    INSERT INTO messages
                        (conversation_id, position, role, content, tool_calls, token_count,
                         message_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(conversation_id, position) DO UPDATE SET
                        role=excluded.role,
                        content=excluded.content,
                        tool_calls=excluded.tool_calls,
                        token_count=excluded.token_count,
                        message_hash=excluded.message_hash
                    WHERE messages.message_hash != excluded.message_hash
                    """,
                    (
                        conversation_id,
                        position,
                        role,
                        content,
                        tool_json,
                        estimate_tokens(content) + (estimate_tokens(tool_calls) if tool_calls else 0),
                        digest,
                        now,
                    ),
                )
                stored += 1
        return stored

    def add_message(self, conversation_id: str, role: str, content: str, tool_calls: Any = None) -> int:
        self.ensure_conversation(conversation_id)
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(position), -1) AS p FROM messages WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            position = int(row["p"]) + 1
            cur = self.conn.execute(
                """
                INSERT INTO messages
                    (conversation_id, position, role, content, tool_calls, token_count,
                     message_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    position,
                    role,
                    compact_whitespace(content),
                    json.dumps(tool_calls, ensure_ascii=False, default=str) if tool_calls else None,
                    estimate_tokens(content),
                    hashlib.sha256(f"{role}\0{content}".encode("utf-8")).hexdigest(),
                    now_iso(),
                ),
            )
            return int(cur.lastrowid)

    def unconsolidated_messages(self, conversation_id: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, position, role, content, created_at FROM messages "
                "WHERE conversation_id=? AND consolidated=0 AND role IN ('user','assistant') "
                "ORDER BY position ASC",
                (conversation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_consolidated(self, message_ids: Iterable[int]) -> None:
        ids = [int(i) for i in message_ids]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self.conn:
            self.conn.execute(
                f"UPDATE messages SET consolidated=1 WHERE id IN ({placeholders})",
                ids,
            )

    # ------------------------------------------------------------------
    # Semantic memories
    # ------------------------------------------------------------------
    def _fts_add(self, memory_id: int, content: str, key: str | None) -> None:
        search_text = segment_for_fts(f"{key or ''} {content}")
        self.conn.execute(
            "INSERT INTO memories_fts (memory_id, search_text) VALUES (?, ?)",
            (memory_id, search_text),
        )

    def _fts_delete(self, memory_id: int) -> None:
        self.conn.execute("DELETE FROM memories_fts WHERE memory_id=?", (memory_id,))

    def add_memory(
        self,
        conversation_id: str,
        kind: str,
        content: str,
        *,
        key: str | None = None,
        importance: float = 0.5,
        confidence: float = 0.8,
        urgency: float = 0.0,
        due_at: str | None = None,
        source_message_ids: list[int] | None = None,
        pinned: bool = False,
        metadata: dict | None = None,
        preserve_newlines: bool = False,
    ) -> int:
        content = clean_text_keep_lines(content) if preserve_newlines else clean_text(content)
        if not content:
            raise ValueError("memory content is empty")
        self.ensure_conversation(conversation_id)
        now = now_iso()
        source_json = json.dumps(source_message_ids or [], ensure_ascii=False)
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        with self._lock, self.conn:
            old = None
            if key:
                old = self.conn.execute(
                    "SELECT id, content FROM memories WHERE conversation_id=? AND key=? "
                    "AND status='active' ORDER BY updated_at DESC LIMIT 1",
                    (conversation_id, key),
                ).fetchone()
                if old and compact_whitespace(old["content"]) == content:
                    self.conn.execute(
                        "UPDATE memories SET importance=MAX(importance, ?), "
                        "urgency=MAX(urgency, ?), due_at=COALESCE(?, due_at), "
                        "updated_at=?, access_count=access_count+1 WHERE id=?",
                        (importance, urgency, due_at, now, old["id"]),
                    )
                    return int(old["id"])
            cur = self.conn.execute(
                """
                INSERT INTO memories
                    (conversation_id, kind, key, content, importance, confidence,
                     urgency, due_at, source_message_ids, status, pinned, created_at,
                     updated_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    kind,
                    key,
                    content,
                    max(0.0, min(1.0, importance)),
                    max(0.0, min(1.0, confidence)),
                    max(0.0, min(1.0, urgency)),
                    due_at,
                    source_json,
                    1 if pinned else 0,
                    now,
                    now,
                    meta_json,
                ),
            )
            memory_id = int(cur.lastrowid)
            self._fts_add(memory_id, content, key)
            if old:
                self.conn.execute(
                    "UPDATE memories SET status='superseded', superseded_by=?, updated_at=? WHERE id=?",
                    (memory_id, now, old["id"]),
                )
            return memory_id

    def extract_facts(self, conversation_id: str, text: str, message_id: int | None = None) -> list[int]:
        """Rule-based, cheap fact extraction. Designed to be safe and predictable."""
        if not text:
            return []
        created: list[int] = []
        for kind, key_name, pattern, importance, pinned in _EXTRACT_PATTERNS:
            for match in pattern.finditer(text):
                content = compact_whitespace(match.group(1)).strip("。！？.!?，, ")
                if not content or len(content) > 80:
                    continue
                if kind == "preference":
                    digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:10]
                    key = f"preference:{key_name}:{digest}"
                elif key_name == "explicit":
                    digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:10]
                    key = f"fact:explicit:{digest}"
                else:
                    key = f"{kind}:{key_name}"
                memory_id = self.add_memory(
                    conversation_id,
                    kind,
                    content,
                    key=key,
                    importance=importance,
                    source_message_ids=[message_id] if message_id else [],
                    pinned=bool(pinned),
                )
                created.append(memory_id)
        return created

    def get_core_memories(self, conversation_id: str, limit: int = 12) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT * FROM memories
                WHERE conversation_id=? AND status='active'
                  AND (pinned=1 OR kind IN ('identity','preference','summary')
                       OR (urgency >= 0.6 AND due_at IS NOT NULL))
                ORDER BY pinned DESC, urgency DESC,
                         CASE kind WHEN 'summary' THEN 1 ELSE 0 END ASC,
                         importance DESC, updated_at DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_memories(
        self,
        conversation_id: str,
        query: str,
        *,
        limit: int = 6,
        max_tokens: int = 1100,
    ) -> list[dict]:
        hits: list[dict] = []
        terms = fts_terms(query)

        def query_fts(match: str) -> list[dict]:
            try:
                rows = self.conn.execute(
                    """
                    SELECT m.*, bm25(memories_fts) AS rank
                    FROM memories_fts f
                    JOIN memories m ON m.id = f.memory_id
                    WHERE memories_fts MATCH ?
                      AND m.conversation_id=? AND m.status='active'
                    ORDER BY rank ASC, (m.importance + 0.7 * m.urgency) DESC
                    LIMIT ?
                    """,
                    (match, conversation_id, max(limit * 4, 20)),
                ).fetchall()
                return [dict(row) for row in rows]
            except sqlite3.OperationalError:
                return []

        with self._lock:
            if terms:
                # Precision first: every term must appear.  Then widen to OR
                # for natural-language queries that do not match verbatim.
                match_and = " AND ".join(f'"{term}"' for term in terms)
                hits = query_fts(match_and)
                if len(hits) < limit:
                    match_or = " OR ".join(f'"{term}"' for term in terms)
                    seen_ids = {int(item["id"]) for item in hits}
                    for item in query_fts(match_or):
                        if int(item["id"]) not in seen_ids:
                            hits.append(item)
                            seen_ids.add(int(item["id"]))
            if not hits and query.strip():
                like = f"%{query.strip()[:80]}%"
                rows = self.conn.execute(
                    """
                    SELECT * FROM memories
                    WHERE conversation_id=? AND status='active'
                      AND (content LIKE ? OR key LIKE ?)
                    ORDER BY (importance + 0.7 * urgency) DESC, updated_at DESC
                    LIMIT ?
                    """,
                    (conversation_id, like, like, max(limit * 4, 20)),
                ).fetchall()
                hits = [dict(row) for row in rows]

        core = self.get_core_memories(conversation_id, limit=8)
        merged: list[dict] = []
        seen: set[int] = set()
        for item in core + hits:
            if int(item["id"]) in seen:
                continue
            seen.add(int(item["id"]))
            merged.append(item)
            if len(merged) >= max(limit, 8):
                break

        # Budget the final block. Core facts win when space is tight.
        selected: list[dict] = []
        used = 0
        for item in merged:
            cost = estimate_tokens(item.get("content", "")) + 8
            if selected and used + cost > max_tokens:
                continue
            if not selected and cost > max_tokens:
                item = dict(item)
                item["content"] = item.get("content", "")[: max(20, max_tokens * 2)]
                cost = estimate_tokens(item["content"]) + 8
            selected.append(item)
            used += cost

        if selected:
            ids = [int(item["id"]) for item in selected]
            placeholders = ",".join("?" for _ in ids)
            with self._lock, self.conn:
                self.conn.execute(
                    f"UPDATE memories SET access_count=access_count+1, last_accessed_at=? "
                    f"WHERE id IN ({placeholders})",
                    [now_iso(), *ids],
                )
        return selected

    def memory_line(self, item: dict) -> str:
        kind = item.get("kind") or "memory"
        key = item.get("key") or ""
        label = {
            "identity": "身份",
            "preference": "偏好",
            "fact": "事实",
            "summary": "摘要",
            "episode": "经历",
            "self": "自我",
        }.get(kind, "记忆")
        if key.startswith("preference:like"):
            label = "喜欢"
        elif key.startswith("preference:dislike"):
            label = "不喜欢"
        elif key.endswith(":name"):
            label = "名字"
        elif key.endswith(":birthday"):
            label = "生日"
        content = compact_whitespace(item.get("content", ""))
        urgency = float(item.get("urgency") or 0.0)
        due = item.get("due_at")
        prefix = "【急】" if urgency >= 0.7 else ""
        suffix = f"（截止 {due}）" if due else ""
        return f"- {prefix}{label}：{content}{suffix}"

    def latest_self_note(self, conversation_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM memories WHERE conversation_id=? AND kind='self' "
                "AND status='active' ORDER BY created_at DESC, id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def latest_diary_note(self, conversation_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM memories WHERE conversation_id=? AND kind='diary' "
                "AND status='active' ORDER BY created_at DESC, id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def last_activity_at(self, conversation_id: str) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT MAX(created_at) AS last_at FROM messages WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        return row["last_at"] if row and row["last_at"] else None

    # ------------------------------------------------------------------
    # Consolidation
    # ------------------------------------------------------------------
    def consolidate(
        self,
        conversation_id: str,
        *,
        threshold: int = 30,
        batch_size: int = 20,
        keep_recent: int = 16,
        summarizer: Callable[[list[dict]], str | dict] | None = None,
    ) -> dict | None:
        messages = self.unconsolidated_messages(conversation_id)
        if len(messages) < max(threshold, batch_size + keep_recent):
            return None
        older = messages[:-keep_recent] if keep_recent > 0 else messages
        batch = older[:batch_size]
        if len(batch) < 4:
            return None

        raw_result = summarizer(batch) if summarizer else self.extractive_summary(batch)
        graded = isinstance(raw_result, dict)
        if graded:
            result = raw_result
            summary = clean_text(str(result.get("summary") or ""))
            importance = _clamp(result.get("importance", 0.58), 0.1, 0.95)
            urgency = _clamp(result.get("urgency", 0.0), 0.0, 1.0)
            confidence = _clamp(result.get("confidence", 0.7), 0.0, 1.0)
            pinned = bool(result.get("pinned")) and importance >= 0.75
            due_at = _clean_due(result.get("due"))
        else:
            summary = compact_whitespace(str(raw_result or ""))
            importance, urgency, confidence, pinned, due_at = 0.58, 0.0, 0.7, False, None

        if not summary:
            return None
        if not summary.startswith("- "):
            # A plain paragraph is not a memory card; keep it out of the active
            # summary layer rather than silently accepting a bad format.
            return None

        memory_id = self.add_memory(
            conversation_id,
            "summary",
            summary,
            importance=importance,
            confidence=confidence,
            urgency=urgency,
            due_at=due_at,
            pinned=pinned,
            source_message_ids=[int(m["id"]) for m in batch],
            metadata={
                "from_position": batch[0]["position"],
                "to_position": batch[-1]["position"],
                "graded": graded,
            },
        )
        self.mark_consolidated([int(m["id"]) for m in batch])
        return {
            "memory_id": memory_id,
            "messages": len(batch),
            "summary": summary,
            "importance": importance,
            "urgency": urgency,
            "confidence": confidence,
            "pinned": pinned,
            "due_at": due_at,
            "graded": graded,
        }


    def extractive_summary(self, messages: list[dict], max_chars: int = 600) -> str:
        """Cheap fallback summary that preserves user-side facts and topics."""
        by_date: dict[str, list[str]] = {}
        for message in messages:
            if message.get("role") != "user":
                continue
            text = compact_whitespace(message.get("content", ""))
            if not text:
                continue
            date = date_of(message.get("created_at", ""))
            # Keep the first meaningful clause, not the whole essay.
            clause = re.split(r"[。！？!?]\s*", text, maxsplit=1)[0][:90]
            by_date.setdefault(date, []).append(clause)
        lines: list[str] = []
        for date in sorted(by_date)[-6:]:
            clauses = "；".join(by_date[date][:4])
            lines.append(f"- {date}：{clauses}")
        text = "\n".join(lines).strip()
        if not text:
            text = "（这段时间主要是日常闲聊，没有提取到稳定信息）"
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "…"
        return text

    # ------------------------------------------------------------------
    # Metrics and export
    # ------------------------------------------------------------------
    def log_request(
        self,
        conversation_id: str,
        *,
        original_tokens: int,
        optimized_tokens: int,
        memory_tokens: int,
        memory_count: int,
        dropped_messages: int,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        model: str = "",
        upstream_status: int = 0,
        latency_ms: int = 0,
    ) -> None:
        saved = max(0, int(original_tokens) - int(optimized_tokens))
        with self._lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO request_logs
                    (conversation_id, created_at, original_tokens, optimized_tokens, saved_tokens,
                     memory_tokens, memory_count, dropped_messages, prompt_tokens, completion_tokens,
                     model, upstream_status, latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    now_iso(),
                    int(original_tokens),
                    int(optimized_tokens),
                    saved,
                    int(memory_tokens),
                    int(memory_count),
                    int(dropped_messages),
                    int(prompt_tokens),
                    int(completion_tokens),
                    model,
                    int(upstream_status),
                    int(latency_ms),
                ),
            )

    def log_review(
        self,
        conversation_id: str,
        *,
        model: str = "",
        proposed: int = 0,
        applied: int = 0,
        rejected: int = 0,
        changes: list[dict] | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error: str | None = None,
    ) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO memory_reviews
                    (conversation_id, created_at, model, proposed, applied, rejected,
                     changes, prompt_tokens, completion_tokens, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    now_iso(),
                    model,
                    int(proposed),
                    int(applied),
                    int(rejected),
                    json.dumps(changes or [], ensure_ascii=False),
                    int(prompt_tokens),
                    int(completion_tokens),
                    error,
                ),
            )
            return int(cur.lastrowid)

    def stats(self, conversation_id: str | None = None) -> dict:
        where = "WHERE conversation_id=?" if conversation_id else ""
        params: tuple = (conversation_id,) if conversation_id else ()
        with self._lock:
            message_count = self.conn.execute(
                f"SELECT COUNT(*) AS n FROM messages {where}", params
            ).fetchone()["n"]
            if conversation_id:
                memory_count = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM memories WHERE conversation_id=? AND status='active'",
                    (conversation_id,),
                ).fetchone()["n"]
            else:
                memory_count = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM memories WHERE status='active'"
                ).fetchone()["n"]
            total_count = self.conn.execute(
                f"SELECT COUNT(*) AS n FROM memories {where}", params
            ).fetchone()["n"]
            logs = self.conn.execute(
                f"SELECT COUNT(*) AS n, COALESCE(SUM(saved_tokens),0) AS saved, "
                f"COALESCE(AVG(saved_tokens),0) AS avg_saved, "
                f"COALESCE(SUM(original_tokens),0) AS original, "
                f"COALESCE(SUM(optimized_tokens),0) AS optimized "
                f"FROM request_logs {where}",
                params,
            ).fetchone()
            last = self.conn.execute(
                f"SELECT * FROM request_logs {where} ORDER BY id DESC LIMIT 1", params
            ).fetchone()
            kinds = self.conn.execute(
                f"SELECT kind, COUNT(*) AS n FROM memories {where} "
                f"{'AND' if conversation_id else 'WHERE'} status='active' GROUP BY kind",
                params,
            ).fetchall()
        return {
            "conversation_id": conversation_id,
            "messages": int(message_count),
            "memories_active": int(memory_count),
            "memories_total": int(total_count),
            "memory_kinds": {row["kind"]: int(row["n"]) for row in kinds},
            "requests": int(logs["n"]),
            "original_tokens": int(logs["original"]),
            "optimized_tokens": int(logs["optimized"]),
            "saved_tokens": int(logs["saved"]),
            "avg_saved_tokens": round(float(logs["avg_saved"]), 1),
            "savings_ratio": round(float(logs["saved"]) / float(logs["original"]), 4)
            if logs["original"] else 0.0,
            "last_request": dict(last) if last else None,
            "db_path": os.path.abspath(self.path),
        }

    def recent_requests(self, conversation_id: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM request_logs WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                (conversation_id, max(1, min(limit, 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def export_conversation(self, conversation_id: str) -> dict:
        with self._lock:
            messages = self.conn.execute(
                "SELECT * FROM messages WHERE conversation_id=? ORDER BY position", (conversation_id,)
            ).fetchall()
            memories = self.conn.execute(
                "SELECT * FROM memories WHERE conversation_id=? ORDER BY id", (conversation_id,)
            ).fetchall()
        return {
            "conversation_id": conversation_id,
            "messages": [dict(row) for row in messages],
            "memories": [dict(row) for row in memories],
            "stats": self.stats(conversation_id),
        }

    def rebuild_fts(self) -> int:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM memories_fts")
            rows = self.conn.execute("SELECT id, content, key FROM memories").fetchall()
            for row in rows:
                self._fts_add(int(row["id"]), row["content"], row["key"])
        return len(rows)


def _clamp(value, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    return round(max(low, min(high, number)), 3)


def _clean_due(value) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    return None
