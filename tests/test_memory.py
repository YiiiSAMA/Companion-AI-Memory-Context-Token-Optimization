import os
import tempfile
import unittest

from companion_memory.config import Config
from companion_memory.context import ContextBuilder, sanitize_tool_messages
from companion_memory.db import MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(os.path.join(self.tmp.name, "memory.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


    def test_orphan_tool_message_is_removed(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1", "type": "function"}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
            {"role": "tool", "tool_call_id": "call-orphan", "content": "bad"},
            {"role": "user", "content": "继续"},
        ]
        cleaned = sanitize_tool_messages(messages)
        self.assertEqual([m.get("role") for m in cleaned], ["system", "assistant", "tool", "user"])
        self.assertEqual(cleaned[2]["tool_call_id"], "call-1")

    def test_extracts_and_retrieves_facts(self):
        text = "我叫条猫，我喜欢喝冰美式，我讨厌香菜"
        self.store.upsert_messages("c1", [{"role": "user", "content": text}])
        self.store.extract_facts("c1", text)

        hits = self.store.search_memories("c1", "今天想喝冰美式", limit=6, max_tokens=800)
        contents = [h["content"] for h in hits]
        self.assertTrue(any("冰美式" in c for c in contents), contents)
        self.assertTrue(any("条猫" in c for c in contents), contents)

    def test_new_job_supersedes_old_one(self):
        self.store.extract_facts("c1", "我在阿里工作")
        self.store.extract_facts("c1", "我在字节工作")
        exported = self.store.export_conversation("c1")
        job_rows = [m for m in exported["memories"] if m["key"] == "fact:job"]
        self.assertEqual(len(job_rows), 2)
        active = [m for m in job_rows if m["status"] == "active"]
        self.assertEqual(len(active), 1)
        self.assertIn("字节", active[0]["content"])
        self.assertEqual([m for m in job_rows if m["status"] == "superseded"][0]["content"], "阿里")

    def test_context_budget_drops_old_turns_but_keeps_latest_user(self):
        config = Config(
            db_path=os.path.join(self.tmp.name, "memory.db"),
            max_prompt_tokens=2000,
            reserve_output_tokens=300,
            recent_turns=4,
            memory_top_k=6,
        )
        builder = ContextBuilder(self.store, config)
        messages = [{"role": "system", "content": "你是知言，一个自然的聊天伙伴。"}]
        for i in range(80):
            role = "user" if i % 2 == 0 else "assistant"
            messages.append({"role": role, "content": f"这是第{i}轮对话。" + "今天发生了一些很普通的事情，" * 3})
        messages.append({"role": "user", "content": "最后一句：我今天想喝冰美式。"})
        self.store.extract_facts("c1", "我喜欢喝冰美式。")
        prepared = builder.build("c1", messages)
        self.assertLessEqual(prepared.optimized_tokens, config.context_budget() + 30)
        self.assertGreater(prepared.saved_tokens, 0)
        self.assertTrue(prepared.messages[-1]["content"].endswith("我今天想喝冰美式。"))
        self.assertGreaterEqual(prepared.memory_count, 1)
        self.assertIn("冰美式", "\n".join(m["content"] for m in prepared.memories))

    def test_consolidation_creates_summary_and_marks_messages(self):
        for i in range(45):
            self.store.add_message("c1", "user" if i % 2 == 0 else "assistant", f"第{i}轮：今天聊了工作和晚饭。")
        result = self.store.consolidate("c1", threshold=30, batch_size=20, keep_recent=8)
        self.assertIsNotNone(result)
        self.assertGreater(result["messages"], 0)
        stats = self.store.stats("c1")
        self.assertGreaterEqual(stats["memory_kinds"].get("summary", 0), 1)
        remaining = self.store.unconsolidated_messages("c1")
        self.assertLess(len(remaining), 45)


if __name__ == "__main__":
    unittest.main()
