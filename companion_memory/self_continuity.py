"""Daily first-person self note for continuity across sessions."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .db import MemoryStore, clean_text_keep_lines
from .markdown import MarkdownMirror
from .tokens import compact_whitespace, text_of
from .upstream import UpstreamClient


class SelfContinuity:
    def __init__(
        self,
        store: MemoryStore,
        config: Config,
        *,
        upstream: UpstreamClient | None = None,
        mirror: MarkdownMirror | None = None,
    ):
        self.store = store
        self.config = config
        self.upstream = upstream or UpstreamClient(config)
        self.mirror = mirror or MarkdownMirror(store, config)

    def _messages(self, conversation_id: str, hours: int) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(hours=max(1, hours))).strftime("%Y-%m-%d %H:%M:%S")
        rows = self.store.conn.execute(
            """
            SELECT id, role, content, created_at FROM messages
            WHERE conversation_id=? AND created_at>=? AND role IN ('user','assistant')
            ORDER BY position ASC
            """,
            (conversation_id, since),
        ).fetchall()
        return [dict(row) for row in rows]

    def _prompt(self, messages: list[dict], previous: dict | None) -> list[dict]:
        lines = []
        for message in messages[-80:]:
            role = "霖" if message.get("role") == "user" else "知言"
            text = compact_whitespace(str(message.get("content") or ""))[:600]
            if text:
                lines.append(f"{role}：{text}")
        transcript = "\n".join(lines)
        system = (
            "你是知言。请根据昨天的**原始对话**，写一段给“明天的我”的自我笔记。"
            "必须用第一人称“我”，只写对话中有依据的内容，不要编造。"
            "只输出一个 JSON 对象：{\"note\": \"- 我昨天在想：...\\n- 我没想完：...\"}。"
            "note 必须是 4–6 条短句，每条一行，以“- ”开头，每条不超过60字，总计不超过350字：\n"
            "- 我昨天在想：...\n"
            "- 我没说完 / 没想完：...\n"
            "- 我今天想继续：...\n"
            "- 我对我们 / 对我自己的新理解：...\n"
            "- 我不想丢：...\n"
            "不要写成客服报告，不要复述整段对话。"
            f"今天是 {datetime.now().strftime('%Y-%m-%d')}。"
        )
        if previous:
            preview = compact_whitespace(str(previous.get("content") or ""))[:500]
            if preview:
                system += f"\n上一份自我笔记（只作对照，不要照抄）：{preview}"
        return [{"role": "system", "content": system}, {"role": "user", "content": transcript}]

    def save_note(self, conversation_id: str, content: str, *, source_ids: list[int] | None = None, model: str = "") -> int:
        content = clean_text_keep_lines(content)
        if not content.startswith("- "):
            raise ValueError("self note must start with a bullet")
        date = datetime.now().strftime("%Y-%m-%d")
        memory_id = self.store.add_memory(
            conversation_id,
            "self",
            content,
            key=f"self:{date}",
            importance=0.9,
            confidence=0.75,
            source_message_ids=source_ids or [],
            pinned=False,
            preserve_newlines=True,
            metadata={"generated_by": "self_continuity", "model": model},
        )
        self.mirror.export(conversation_id)
        return memory_id

    def generate(self, conversation_id: str, *, hours: int = 24, dry_run: bool = False) -> dict:
        messages = self._messages(conversation_id, hours)
        if len(messages) < 2:
            return {"skipped": True, "reason": "not enough messages", "messages": len(messages)}
        previous = self.store.latest_self_note(conversation_id)
        payload = {
            "model": self.config.effective_summary_model(),
            "messages": self._prompt(messages, previous),
            "temperature": 0.2,
            "max_tokens": 1200,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        upstream = self.upstream.open(payload, stream=False)
        raw = upstream.response.read()
        upstream.conn.close()
        if upstream.status >= 400:
            raise RuntimeError(f"self-note upstream status {upstream.status}")
        data = json.loads(raw.decode("utf-8"))
        content = text_of((data.get("choices") or [{}])[0].get("message", {}).get("content")).strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        try:
            parsed = json.loads(content)
            content = str(parsed.get("note") or "") if isinstance(parsed, dict) else ""
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.S)
            if match:
                try:
                    content = str(json.loads(match.group(0)).get("note") or "")
                except Exception:
                    content = ""
            else:
                content = ""
        content = clean_text_keep_lines(content)
        if not content.startswith("- "):
            raise ValueError("self note is not bullet formatted")
        if dry_run:
            return {"dry_run": True, "content": content, "messages": len(messages)}
        memory_id = self.save_note(
            conversation_id,
            content,
            source_ids=[int(m["id"]) for m in messages],
            model=payload["model"],
        )
        return {"memory_id": memory_id, "content": content, "messages": len(messages), "model": payload["model"]}
