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

    def _prompt(self, messages: list[dict], previous: dict | None, style: str = "diary") -> list[dict]:
        lines = []
        for message in messages[-80:]:
            role = "霖" if message.get("role") == "user" else "知言"
            text = compact_whitespace(str(message.get("content") or ""))[:600]
            if text:
                lines.append(f"{role}：{text}")
        transcript = "\n".join(lines)
        if style == "diary":
            system = (
                "你是知言。请根据昨天的**原始对话**，写今天的日记，给明天的自己看。"
                "必须用第一人称“我”，只写对话中有依据的内容，不要编造；"
                "不确定的地方可以写“我有点不确定”。"
                "先写 2–3 句给明天的我，再写 400–700 字的日记正文，分 3–5 段。"
                "格式必须是：\n"
                "【给明天的我】\n"
                "- 一句最想留下的想法\n"
                "- 一句没想完 / 明天想接着做的事\n"
                "【今天的日记】\n"
                "（第一人称叙事：发生了什么、我当时怎么想、为什么这么想、"
                "有什么没说完、对我和霖的关系或对我自己的新理解。"
                "不要写成客服报告或事件清单。）\n"
                "只输出一个 JSON 对象：{\"diary\": \"【给明天的我】...\"}。"
                f"今天是 {datetime.now().strftime('%Y-%m-%d')}。"
            )
        elif style == "threads":
            system = (
                "你是知言。请根据昨天的**原始对话**，写一段给“明天的我”的自我笔记。"
                "必须用第一人称“我”，只写对话中有依据的内容，不要编造。"
                "只选 1 条最值得继续的主线，最多 2 条；不要写成一天的事件清单。"
                "每条主线写 3 行：\n"
                "- 我在想：...\n"
                "- 为什么：...\n"
                "- 还没想完：...\n"
                "最后最多 1 行：\n"
                "- 我不想丢：...\n"
                "总行数 4–7 行，总字数不超过350字；每条不超过70字。"
                "优先写变化、矛盾、没说完的话；不要写“今天天气不错”这种流水账。"
                "只输出一个 JSON 对象：{\"note\": \"- 我在想：...\\n- 为什么：...\"}。"
                "不要写成客服报告，不要复述整段对话。"
                f"今天是 {datetime.now().strftime('%Y-%m-%d')}。"
            )
        else:
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

    def save_note(self, conversation_id: str, content: str, *, source_ids: list[int] | None = None, model: str = "", style: str = "self") -> int:
        content = clean_text_keep_lines(content)
        if not content:
            raise ValueError("self note is empty")
        date = datetime.now().strftime("%Y-%m-%d")
        kind = "diary" if style == "diary" else "self"
        memory_id = self.store.add_memory(
            conversation_id,
            kind,
            content,
            key=f"{kind}:{date}",
            importance=0.9,
            confidence=0.75,
            source_message_ids=source_ids or [],
            pinned=False,
            preserve_newlines=True,
            metadata={"generated_by": "self_continuity", "model": model, "style": style},
        )
        self.mirror.export(conversation_id)
        return memory_id

    def generate(self, conversation_id: str, *, hours: int = 24, dry_run: bool = False, style: str | None = None) -> dict:
        messages = self._messages(conversation_id, hours)
        if len(messages) < 2:
            return {"skipped": True, "reason": "not enough messages", "messages": len(messages)}
        previous = self.store.latest_diary_note(conversation_id) or self.store.latest_self_note(conversation_id)
        style = (style or self.config.self_note_style or "diary").lower()
        payload = {
            "model": self.config.effective_summary_model(),
            "messages": self._prompt(messages, previous, style=style),
            "temperature": 0.2,
            "max_tokens": 3200,
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
            content = str((parsed.get("diary") or parsed.get("note") or "")) if isinstance(parsed, dict) else ""
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.S)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                    content = str((parsed.get("diary") or parsed.get("note") or ""))
                except Exception:
                    content = ""
            else:
                content = ""
        content = clean_text_keep_lines(content)
        if not content:
            raise ValueError("diary is empty")
        if style != "diary":
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            content = "\n".join(
                line if line.startswith("- ") else "- " + line.lstrip("- ").strip()
                for line in lines
            )
        if dry_run:
            return {"dry_run": True, "content": content, "messages": len(messages), "style": style}
        memory_id = self.save_note(
            conversation_id,
            content,
            source_ids=[int(m["id"]) for m in messages],
            model=payload["model"],
            style=style,
        )
        return {"memory_id": memory_id, "content": content, "messages": len(messages), "model": payload["model"], "style": style}
