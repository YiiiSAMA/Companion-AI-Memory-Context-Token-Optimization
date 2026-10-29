"""Reproduce the README token-budget comparison without any network calls."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from companion_memory.config import Config
from companion_memory.context import ContextBuilder
from companion_memory.db import MemoryStore
from companion_memory.tokens import estimate_messages_tokens


def build_history(turns: int) -> list[dict]:
    messages = [
        {"role": "system", "content": "你是知言，一个自然的聊天伙伴。"},
        {"role": "user", "content": "我叫条猫，我喜欢喝冰美式，我讨厌香菜，我在阿里工作。"},
        {"role": "assistant", "content": "记住了。"},
    ]
    for i in range(turns):
        messages.append(
            {
                "role": "user",
                "content": f"第{i}轮：今天工作有点累，晚上吃了面，和同事聊了一会儿，回家路上有点冷。",
            }
        )
        messages.append({"role": "assistant", "content": "嗯，先休息一下。到家跟我说一声，别硬撑。"})
    messages.append({"role": "user", "content": "今天又想喝冰美式了，你觉得我该不该喝？"})
    return messages


def run(turns: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(os.path.join(tmp, "demo.db"))
        config = Config(
            max_prompt_tokens=6000,
            reserve_output_tokens=800,
            recent_turns=8,
            memory_top_k=8,
        )
        builder = ContextBuilder(store, config)
        messages = build_history(turns)
        store.extract_facts("demo", "我叫条猫，我喜欢喝冰美式，我讨厌香菜，我在阿里工作。")
        store.upsert_messages("demo", messages)
        if estimate_messages_tokens(messages) > config.context_budget():
            keep_recent = max(4, config.recent_turns * 2)
            pending = store.unconsolidated_messages("demo")
            if len(pending) > keep_recent + 4:
                store.consolidate(
                    "demo",
                    threshold=4,
                    batch_size=len(pending) - keep_recent,
                    keep_recent=keep_recent,
                )
        prepared = builder.build("demo", messages)
        print(
            f"turns={turns:4d}  original={prepared.original_tokens:6d}  "
            f"optimized={prepared.optimized_tokens:5d}  saved={prepared.saved_tokens:6d}  "
            f"ratio={prepared.compression_ratio:.1%}  recent={prepared.recent_count}  "
            f"dropped={prepared.dropped_messages}  memories={prepared.memory_count}"
        )
        store.close()


if __name__ == "__main__":
    run(60)
    run(1500)
