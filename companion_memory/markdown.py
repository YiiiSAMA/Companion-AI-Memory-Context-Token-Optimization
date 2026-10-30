"""Markdown mirror for the memory store.

SQLite remains the source of truth.  This module renders a human-readable,
version-control-friendly snapshot of active memories as small Markdown cards.
It intentionally does not replace retrieval or consolidation.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path

from .config import Config
from .db import MemoryStore
from .tokens import estimate_tokens

MEMORY_ENCODER_PROMPT = """你是知言的记忆编码器。

把输入对话压缩成记忆卡，只能使用以下类型：
FACT / PREF / EPISODE / PROMISE / RELATION

规则：
1. 每条只写一个可验证的命题。
2. 必须有 source 引用。
3. 推断内容标 confidence < 0.8。
4. 不确定的内容写进“待确认”，不要写成事实。
5. 与旧记忆冲突时，输出 supersedes。
6. 每条不超过 80 个中文字符。
输出 YAML frontmatter + Markdown 列表。
"""

_KIND_LABEL = {
    "identity": "身份",
    "preference": "偏好",
    "fact": "事实",
    "episode": "经历",
    "summary": "摘要",
    "commitment": "承诺",
    "relationship": "关系",
    "self": "自我",
}

_KIND_TAG = {
    "identity": "IDENT",
    "preference": "PREF",
    "fact": "FACT",
    "episode": "EPISODE",
    "summary": "SUMMARY",
    "commitment": "PROMISE",
    "relationship": "REL",
    "self": "SELF",
}


def _yaml(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_yaml(x) for x in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def _source_ids(memory: dict) -> list[int]:
    raw = memory.get("source_message_ids")
    if not raw:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return [int(x) for x in value]
    except Exception:
        return []


def _date_of(memory: dict) -> str:
    value = str(memory.get("created_at") or memory.get("updated_at") or "")
    return value[:10] if len(value) >= 10 else datetime.now().strftime("%Y-%m-%d")


def _week_of(memory: dict) -> str:
    value = str(memory.get("created_at") or memory.get("updated_at") or "")
    try:
        dt = datetime.strptime(value[:10], "%Y-%m-%d")
    except Exception:
        dt = datetime.now()
    return f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"


def _target_file(memory: dict) -> str:
    kind = str(memory.get("kind") or "fact")
    if kind == "identity":
        return "identity.md"
    if kind == "preference":
        return "preferences.md"
    if kind == "summary":
        return f"summaries/{_week_of(memory)}.md"
    if kind == "commitment":
        return "open-loops.md"
    if kind == "relationship":
        return "relationships.md"
    if kind == "self":
        return f"self/{_date_of(memory)}.md"
    if kind == "episode":
        return f"episodes/{_date_of(memory)}.md"
    return "facts.md"


def render_card(memory: dict) -> str:
    memory_id = f"mem_{int(memory['id']):06d}" if memory.get("id") is not None else "mem_unknown"
    kind = str(memory.get("kind") or "fact")
    tag = _KIND_TAG.get(kind, "FACT")
    content = str(memory.get("content") or "").strip()
    if kind == "summary":
        content = content.replace(" - ", "\n- ")
    front = [
        "---",
        f"id: {memory_id}",
        f"type: {_yaml(kind)}",
        f"key: {_yaml(memory.get('key'))}",
        f"importance: {_yaml(round(float(memory.get('importance') or 0), 3))}",
        f"urgency: {_yaml(round(float(memory.get('urgency') or 0), 3))}",
        f"confidence: {_yaml(round(float(memory.get('confidence') or 0), 3))}",
        f"due: {_yaml(memory.get('due_at'))}",
        f"status: {_yaml(memory.get('status') or 'active')}",
        f"pinned: {_yaml(bool(memory.get('pinned')))}",
        f"created_at: {_yaml(memory.get('created_at'))}",
        f"updated_at: {_yaml(memory.get('updated_at'))}",
        f"source_messages: {_yaml(_source_ids(memory))}",
        "---",
        "",
        f"### {_KIND_LABEL.get(kind, '记忆')} · {memory_id}",
        "",
        f"`[{tag}|{memory_id}|{float(memory.get('importance') or 0):.2f}|{float(memory.get('urgency') or 0):.2f}]` {content}",
        "",
    ]
    if memory.get("superseded_by"):
        front.extend([f"> 已被 `mem_{int(memory['superseded_by']):06d}` 替代。", ""])
    return "\n".join(front).rstrip() + "\n"


class MarkdownMirror:
    def __init__(self, store: MemoryStore, config: Config):
        self.store = store
        self.config = config
        self._lock = threading.Lock()

    def export(self, conversation_id: str, output_dir: str | None = None) -> dict:
        with self._lock:
            return self._export_locked(conversation_id, output_dir)

    def _export_locked(self, conversation_id: str, output_dir: str | None = None) -> dict:
        data = self.store.export_conversation(conversation_id)
        memories = data.get("memories", [])
        base = Path(output_dir or self.config.markdown_dir)
        tmp = base.with_name(base.name + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True, exist_ok=True)

        groups: dict[str, list[str]] = {}
        total_tokens = 0
        for memory in memories:
            if memory.get("status") != "active":
                continue
            card = render_card(memory)
            total_tokens += estimate_tokens(card)
            groups.setdefault(_target_file(memory), []).append(card)

        for rel, cards in sorted(groups.items()):
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(cards), encoding="utf-8")

        # Always create core files so the layout is predictable.
        for rel in ("identity.md", "preferences.md", "facts.md", "open-loops.md", "relationships.md", "self.md"):
            path = tmp / rel
            if not path.exists():
                path.write_text("# 暂无记录\n", encoding="utf-8")

        self_cards = [m for m in memories if m.get("kind") == "self" and m.get("status") == "active"]
        if self_cards:
            latest = max(self_cards, key=lambda m: (str(m.get("created_at") or ""), int(m.get("id") or 0)))
            (tmp / "self.md").write_text(render_card(latest), encoding="utf-8")

        manifest = {
            "conversation_id": conversation_id,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "memory_count": sum(len(v) for v in groups.values()),
            "files": sorted(groups.keys()),
            "estimated_tokens": total_tokens,
            "source": "companion-memory SQLite",
        }
        (tmp / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        index_lines = [
            "# 知言记忆索引",
            "",
            f"- 会话：`{conversation_id}`",
            f"- 生成时间：{manifest['generated_at']}",
            f"- 活跃记忆：{manifest['memory_count']} 条",
            f"- 卡片估算：{total_tokens} tokens",
            "",
            "| 文件 | 说明 |",
            "|---|---|",
            "| `identity.md` | 名字、生日等 pinned 身份 |",
            "| `preferences.md` | 喜欢 / 不喜欢 |",
            "| `facts.md` | 其他稳定事实 |",
            "| `episodes/` | 按日期归档的事件 |",
            "| `summaries/` | 按周归档的摘要 |",
            "| `open-loops.md` | 未完成承诺 / 约定 |",
            "| `relationships.md` | 关系锚点、相处规则、边界 |",
            "| `self.md` | 最新一份“昨天的我” |",
            "| `self/` | 按日期归档的自我笔记 |",
            "",
            "> SQLite 是事实来源；这些文件是镜像，自动导出会覆盖手工修改。",
        ]
        (tmp / "index.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

        readme = f"""# 知言记忆镜像

这些文件由 `companion-memory` 从 SQLite 导出，不是模型权重，也不会自动进入上下文。
系统每轮只按需检索少量卡片注入 prompt。

## 卡片格式

- 每张卡片有 YAML frontmatter：`id`、`type`、`importance`、`confidence`、`status`、`source_messages`。
- 正文中的紧凑行是给模型读的“记忆 DSL”：
  `[PREF|mem_000001|0.75] 用户喜欢喝冰美式`
- `self/` 是按日期归档的“昨天的我”，`self.md` 是最新一份。
- `source_messages` 对应原始消息 ID，事实出现分歧时以原始消息为准。

## 记忆编码器提示词

```text
{MEMORY_ENCODER_PROMPT}
```

## 注意

- `SQLite` 仍是事实来源，本目录是镜像。
- 自动导出会覆盖手工修改；如果要人工编辑，请先复制到另一个目录。
- 不要把本目录提交到 Public 仓库，里面是私密对话记忆。
"""
        (tmp / "README.md").write_text(readme, encoding="utf-8")

        for path in tmp.rglob("*"):
            if path.is_file():
                os.chmod(path, 0o600)
            elif path.is_dir():
                os.chmod(path, 0o700)
        os.chmod(tmp, 0o700)

        if base.exists():
            shutil.rmtree(base)
        os.replace(tmp, base)
        os.chmod(base, 0o700)
        return {
            "conversation_id": conversation_id,
            "path": str(base),
            "memory_count": manifest["memory_count"],
            "files": manifest["files"],
            "estimated_tokens": total_tokens,
            "generated_at": manifest["generated_at"],
        }
