import os
import tempfile
import unittest
from pathlib import Path

from companion_memory.config import Config
from companion_memory.db import MemoryStore
from companion_memory.markdown import MarkdownMirror


class MarkdownMirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(os.path.join(self.tmp.name, "memory.db"))
        self.out = os.path.join(self.tmp.name, "memory-md")
        self.config = Config(markdown_dir=self.out, markdown_auto_export=False)
        self.mirror = MarkdownMirror(self.store, self.config)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_exports_cards_by_kind(self):
        self.store.add_memory(
            "c1", "identity", "名字：条猫", key="identity:name", importance=0.95, pinned=True,
            source_message_ids=[1],
        )
        self.store.add_memory(
            "c1", "preference", "喜欢喝冰美式", key="preference:like:coffee",
            importance=0.8, source_message_ids=[2],
        )
        self.store.add_memory(
            "c1", "summary", "2026-10-30：聊了工作、晚饭和周末计划。",
            importance=0.6, source_message_ids=[3, 4],
        )
        result = self.mirror.export("c1")
        self.assertEqual(result["memory_count"], 3)
        for name in ("identity.md", "preferences.md", "README.md", "index.md", "manifest.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.out, name)), name)
        identity = Path(self.out, "identity.md").read_text(encoding="utf-8")
        self.assertIn("mem_", identity)
        self.assertIn("[IDENT|", identity)
        self.assertIn("source_messages: [1]", identity)
        self.assertIn("名字：条猫", identity)
        prefs = Path(self.out, "preferences.md").read_text(encoding="utf-8")
        self.assertIn("[PREF|", prefs)
        self.assertIn("冰美式", prefs)
        # Summaries are grouped by ISO week, not dumped into a single file.
        self.assertTrue(any("summaries" in f for f in result["files"]), result["files"])


    def test_scores_and_due_are_rendered(self):
        self.store.add_memory(
            "c1", "fact", "明天十点考试", key="fact:exam",
            importance=0.9, urgency=0.8, due_at="2026-10-31",
            source_message_ids=[7],
        )
        self.mirror.export("c1")
        facts = Path(self.out, "facts.md").read_text(encoding="utf-8")
        self.assertIn("importance: 0.9", facts)
        self.assertIn("urgency: 0.8", facts)
        self.assertIn('due: "2026-10-31"', facts)
        self.assertIn("[FACT|", facts)
        self.assertIn("|0.90|0.80]", facts)


    def test_commitments_and_relationships_have_own_files(self):
        self.store.add_memory("c1", "commitment", "十一点睡，不喝酒。", key="commitment:pact", importance=0.95, pinned=True)
        self.store.add_memory("c1", "relationship", "情绪不对时先陪一句。", key="relationship:rule", importance=0.9, pinned=True)
        result = self.mirror.export("c1")
        self.assertTrue(os.path.isfile(os.path.join(self.out, "open-loops.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "relationships.md")))
        self.assertIn("open-loops.md", result["files"])
        self.assertIn("relationships.md", result["files"])

    def test_superseded_cards_are_not_exported(self):
        first = self.store.add_memory("c1", "fact", "在阿里工作", key="fact:job", importance=0.7)
        self.store.add_memory("c1", "fact", "在字节工作", key="fact:job", importance=0.7)
        self.mirror.export("c1")
        facts = Path(self.out, "facts.md").read_text(encoding="utf-8")
        self.assertIn("字节", facts)
        self.assertNotIn("阿里", facts)

    def test_encoder_prompt_is_in_readme(self):
        self.store.add_memory("c1", "identity", "名字：条猫", key="identity:name", pinned=True)
        self.mirror.export("c1")
        readme = Path(self.out, "README.md").read_text(encoding="utf-8")
        self.assertIn("记忆编码器", readme)
        self.assertIn("FACT / PREF / EPISODE", readme)


if __name__ == "__main__":
    unittest.main()
