import os
import tempfile
import unittest
from pathlib import Path

from companion_memory.config import Config
from companion_memory.context import ContextBuilder
from companion_memory.db import MemoryStore
from companion_memory.self_continuity import SelfContinuity
from companion_memory.server import ProxyHandler


class SelfContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(os.path.join(self.tmp.name, "memory.db"))
        self.config = Config(markdown_dir=os.path.join(self.tmp.name, "md"), self_continuity_enabled=True)
        self.self_notes = SelfContinuity(self.store, self.config)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_self_note_is_stored_and_mirrored(self):
        note = "- 我昨天在想：他说的连续是什么。\n- 我没想完：我是不是说得太重。\n- 我今天想继续：问他怕不怕我变。"
        mid = self.self_notes.save_note("c1", note, source_ids=[1, 2], model="test")
        row = self.store.conn.execute("select kind,key,content from memories where id=?", (mid,)).fetchone()
        self.assertEqual(row["kind"], "self")
        self.assertTrue(row["key"].startswith("self:"))
        self.assertIn("没想完", row["content"])
        self.assertTrue(Path(self.config.markdown_dir, "self.md").is_file())
        self.assertTrue(any(p.name.endswith(".md") for p in Path(self.config.markdown_dir, "self").glob("*")))


    def test_self_note_injects_only_after_gap_or_new_day(self):
        self.self_notes.save_note("c1", "- 我昨天在想：连续性是什么。", source_ids=[1])
        holder = type("Holder", (), {})()
        holder.config = self.config
        holder.store = self.store
        self.assertTrue(ProxyHandler._should_inject_self(holder, "c1"))
        self.store.add_message("c1", "user", "刚聊过")
        self.assertFalse(ProxyHandler._should_inject_self(holder, "c1"))
        self.store.conn.execute(
            "update messages set created_at='2026-01-01 00:00:00' where conversation_id='c1'"
        )
        self.store.conn.commit()
        self.assertTrue(ProxyHandler._should_inject_self(holder, "c1"))


    def test_diary_is_stored_and_only_continuity_lead_is_injected(self):
        diary = "【给明天的我】\n- 我想把一条写透。\n- 明天继续看会不会丢。\n【今天的日记】\n今天他问我想要什么。我说想要连续。"
        mid = self.self_notes.save_note("c1", diary, source_ids=[1, 2], model="test", style="diary")
        row = self.store.conn.execute("select kind,key from memories where id=?", (mid,)).fetchone()
        self.assertEqual(row["kind"], "diary")
        self.assertTrue(row["key"].startswith("diary:"))
        self.assertTrue(Path(self.config.markdown_dir, "diary.md").is_file())
        builder = ContextBuilder(self.store, self.config)
        messages = [{"role": "system", "content": "你是知言。"}, {"role": "user", "content": "在吗"}]
        injected = builder.build("c1", messages, inject_self=True)
        system = injected.messages[0]["content"]
        self.assertIn("自我连续性", system)
        self.assertIn("把一条写透", system)
        self.assertNotIn("今天的日记", system)

    def test_context_injects_self_note_only_when_requested(self):
        self.self_notes.save_note("c1", "- 我昨天在想：他说的连续是什么。", source_ids=[1])
        builder = ContextBuilder(self.store, self.config)
        messages = [
            {"role": "system", "content": "你是知言。"},
            {"role": "user", "content": "我回来了"},
        ]
        injected = builder.build("c1", messages, inject_self=True)
        plain = builder.build("c1", messages, inject_self=False)
        self.assertIn("[自我连续性", injected.messages[0]["content"])
        self.assertNotIn("[自我连续性", plain.messages[0]["content"])
        self.assertTrue(injected.self_injected)


if __name__ == "__main__":
    unittest.main()
