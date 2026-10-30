"""Daily memory-score curator.

The curator looks at recent conversation turns and the currently active memory
cards, asks the model for a bounded set of score updates, validates every
change, writes it back, and keeps an audit log.  It never changes memory text
or deletes raw messages.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import Config
from .db import MemoryStore, clean_text
from .markdown import MarkdownMirror
from .tokens import compact_whitespace, text_of
from .upstream import UpstreamClient

MAX_CHANGES = 10
MAX_IMPORTANCE_DELTA = 0.15
MAX_URGENCY_DELTA = 0.25
PROTECTED_KINDS = {"identity", "commitment"}


def _clamp(value, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    return round(max(low, min(high, number)), 3)


def _due(value) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None


def _parse_json(text: str) -> dict:
    content = (text or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            raise ValueError("no JSON object in model response")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("model response is not a JSON object")
    return data


class MemoryCurator:
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

    def memory_cards(self, conversation_id: str, limit: int = 30) -> list[dict]:
        rows = self.store.conn.execute(
            """
            SELECT id, kind, key, content, importance, urgency, confidence,
                   pinned, due_at, updated_at
            FROM memories
            WHERE conversation_id=? AND status='active'
            ORDER BY pinned DESC, urgency DESC, importance DESC, updated_at DESC
            LIMIT ?
            """,
            (conversation_id, max(1, min(limit, 80))),
        ).fetchall()
        return [dict(row) for row in rows]

    def recent_messages(self, conversation_id: str, limit: int = 40) -> list[dict]:
        rows = self.store.conn.execute(
            """
            SELECT role, content, created_at FROM messages
            WHERE conversation_id=? AND role IN ('user','assistant')
            ORDER BY position DESC LIMIT ?
            """,
            (conversation_id, max(1, min(limit, 120))),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def _prompt(self, cards: list[dict], recent: list[dict]) -> list[dict]:
        card_lines = []
        for card in cards:
            content = compact_whitespace(clean_text(card.get("content") or ""))[:220]
            card_lines.append(
                "id={id} kind={kind} importance={importance:.2f} urgency={urgency:.2f} "
                "pinned={pinned} due={due} :: {content}".format(
                    id=card["id"],
                    kind=card["kind"],
                    importance=float(card.get("importance") or 0),
                    urgency=float(card.get("urgency") or 0),
                    pinned="true" if card.get("pinned") else "false",
                    due=card.get("due_at") or "null",
                    content=content,
                )
            )
        transcript = []
        for message in recent:
            role = "霖" if message.get("role") == "user" else "知言"
            text = compact_whitespace(clean_text(message.get("content") or ""))[:400]
            if text:
                transcript.append(f"{role}：{text}")

        system = (
            "你是知言的记忆校准器。根据最近的聊天，判断已有记忆卡片的 "
            "importance / urgency / pinned / due 是否需要调整。"
            "只输出一个 JSON 对象，不要 Markdown 代码块："
            '{"changes":[{"id":81,"importance":0.8,"urgency":0.2,'
            '"pinned":true,"due":null,"reason":"一句话依据"}]}。'
            "规则：最多改 10 条；没有证据就不要改；不要为了改而改；"
            "importance 单次最多调整 0.15；urgency 单次最多调整 0.25；"
            "身份(identity)、承诺(commitment)不能降低 importance，不能取消 pinned；"
            "任何已 pinned 的卡都不能取消 pinned；"
            "只有明确日期才写 YYYY-MM-DD，否则 due 写 null；"
            "不要修改记忆正文，不要编造聊天里没有的事。"
            f"今天是 {datetime.now().strftime('%Y-%m-%d')}。"
        )
        user = "当前记忆卡：\n" + "\n".join(card_lines) + "\n\n最近对话：\n" + "\n".join(transcript)
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def propose(self, conversation_id: str, limit: int = 30) -> dict:
        cards = self.memory_cards(conversation_id, limit)
        if not cards:
            return {"changes": [], "model": self.config.effective_summary_model(), "usage": {}, "error": "no memories"}
        recent = self.recent_messages(conversation_id)
        payload = {
            "model": self.config.effective_summary_model(),
            "messages": self._prompt(cards, recent),
            "temperature": 0.1,
            "max_tokens": 1800,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        upstream = self.upstream.open(payload, stream=False)
        raw = upstream.response.read()
        upstream.conn.close()
        if upstream.status >= 400:
            raise RuntimeError(f"curator upstream status {upstream.status}")
        data = json.loads(raw.decode("utf-8"))
        content = text_of((data.get("choices") or [{}])[0].get("message", {}).get("content"))
        parsed = _parse_json(content)
        changes = parsed.get("changes") or []
        if not isinstance(changes, list):
            changes = []
        return {
            "changes": changes[:MAX_CHANGES],
            "model": payload["model"],
            "usage": data.get("usage") or {},
        }

    def apply_changes(self, conversation_id: str, changes: list[dict], *, model: str = "", usage: dict | None = None) -> dict:
        applied: list[dict] = []
        rejected: list[dict] = []
        conn = self.store.conn
        for item in changes[:MAX_CHANGES]:
            if not isinstance(item, dict):
                rejected.append({"reason": "not an object"})
                continue
            try:
                memory_id = int(item.get("id"))
            except (TypeError, ValueError):
                rejected.append({"reason": "invalid id", "item": item})
                continue
            row = conn.execute(
                "SELECT id,kind,importance,urgency,pinned,due_at FROM memories "
                "WHERE id=? AND conversation_id=? AND status='active'",
                (memory_id, conversation_id),
            ).fetchone()
            if not row:
                rejected.append({"id": memory_id, "reason": "not found"})
                continue

            old_importance = float(row["importance"] or 0)
            old_urgency = float(row["urgency"] or 0)
            new_importance = (
                _clamp(item.get("importance"), 0.1, 0.95)
                if item.get("importance") is not None
                else old_importance
            )
            new_urgency = (
                _clamp(item.get("urgency"), 0.0, 1.0)
                if item.get("urgency") is not None
                else old_urgency
            )
            # Bound a single review's movement.
            new_importance = round(
                max(old_importance - MAX_IMPORTANCE_DELTA, min(old_importance + MAX_IMPORTANCE_DELTA, new_importance)),
                3,
            )
            new_urgency = round(
                max(old_urgency - MAX_URGENCY_DELTA, min(old_urgency + MAX_URGENCY_DELTA, new_urgency)),
                3,
            )

            kind = str(row["kind"] or "fact")
            old_pinned = bool(row["pinned"])
            new_pinned = bool(item.get("pinned", old_pinned))
            if kind in PROTECTED_KINDS:
                new_importance = max(old_importance, new_importance)
                new_pinned = True
            if old_pinned:
                new_pinned = True
            if new_pinned and new_importance < 0.75:
                new_pinned = False

            new_due = _due(item["due"]) if "due" in item else row["due_at"]
            changed = (
                new_importance != old_importance
                or new_urgency != old_urgency
                or new_pinned != old_pinned
                or new_due != row["due_at"]
            )
            if not changed:
                rejected.append({"id": memory_id, "reason": "no change"})
                continue
            conn.execute(
                "UPDATE memories SET importance=?, urgency=?, pinned=?, due_at=?, updated_at=? WHERE id=?",
                (new_importance, new_urgency, 1 if new_pinned else 0, new_due, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), memory_id),
            )
            applied.append(
                {
                    "id": memory_id,
                    "kind": kind,
                    "old": {"importance": old_importance, "urgency": old_urgency, "pinned": old_pinned, "due": row["due_at"]},
                    "new": {"importance": new_importance, "urgency": new_urgency, "pinned": new_pinned, "due": new_due},
                    "reason": str(item.get("reason") or "")[:200],
                }
            )
        conn.commit()
        prompt_tokens = int((usage or {}).get("prompt_tokens") or 0)
        completion_tokens = int((usage or {}).get("completion_tokens") or 0)
        self.store.log_review(
            conversation_id,
            model=model,
            proposed=len(changes),
            applied=len(applied),
            rejected=len(rejected),
            changes=applied,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return {"applied": applied, "rejected": rejected, "proposed": len(changes)}

    def run(self, conversation_id: str, *, limit: int = 30, dry_run: bool = False) -> dict:
        if not self.config.upstream_api_key:
            raise RuntimeError("UPSTREAM_API_KEY is empty")
        try:
            proposal = self.propose(conversation_id, limit=limit)
        except Exception as exc:
            self.store.log_review(conversation_id, error=str(exc))
            return {"error": str(exc), "applied": [], "rejected": [], "proposed": 0}
        if dry_run:
            return {"dry_run": True, **proposal}
        self._backup()
        result = self.apply_changes(
            conversation_id,
            proposal.get("changes") or [],
            model=proposal.get("model", ""),
            usage=proposal.get("usage") or {},
        )
        if result["applied"]:
            self.mirror.export(conversation_id)
        audit_dir = Path(self.config.db_path).parent / "memory-reviews"
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_dir.chmod(0o700)
        audit_path = audit_dir / f"review-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        audit_path.write_text(
            json.dumps({"proposal": proposal, "result": result}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        audit_path.chmod(0o600)
        return {**proposal, **result, "audit": str(audit_path)}

    def _backup(self) -> str:
        backup_dir = Path(self.config.db_path).parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"memory-before-curator-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        src = sqlite3.connect(self.config.db_path)
        dst = sqlite3.connect(str(target))
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        target.chmod(0o600)
        for old in sorted(backup_dir.glob("memory-before-curator-*.db"))[:-14]:
            old.unlink(missing_ok=True)
        return str(target)
