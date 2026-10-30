import os
import tempfile
import unittest

from companion_memory.config import Config
from companion_memory.curator import MemoryCurator
from companion_memory.db import MemoryStore


class CuratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(os.path.join(self.tmp.name, "memory.db"))
        self.config = Config(db_path=self.store.path, markdown_dir=os.path.join(self.tmp.name, "md"))
        self.curator = MemoryCurator(self.store, self.config)
        self.identity = self.store.add_memory(
            "c1", "identity", "他叫霖", key="identity:name", importance=0.99, pinned=True
        )
        self.pact = self.store.add_memory(
            "c1", "commitment", "十一点睡，不喝酒", key="commitment:pact", importance=0.96, pinned=True
        )
        self.fact = self.store.add_memory(
            "c1", "fact", "最近在准备考试", key="fact:exam", importance=0.6, urgency=0.0
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_protected_cards_cannot_be_downgraded_or_unpinned(self):
        result = self.curator.apply_changes(
            "c1",
            [
                {"id": self.identity, "importance": 0.2, "pinned": False, "reason": "test"},
                {"id": self.pact, "importance": 0.2, "pinned": False, "reason": "test"},
            ],
        )
        self.assertEqual(result["applied"], [])
        row = self.store.conn.execute(
            "select importance,pinned from memories where id=?", (self.identity,)
        ).fetchone()
        self.assertEqual(row["importance"], 0.99)
        self.assertEqual(row["pinned"], 1)
        row = self.store.conn.execute(
            "select importance,pinned from memories where id=?", (self.pact,)
        ).fetchone()
        self.assertEqual(row["importance"], 0.96)
        self.assertEqual(row["pinned"], 1)

    def test_change_is_clamped_and_logged(self):
        result = self.curator.apply_changes(
            "c1",
            [{"id": self.fact, "importance": 0.1, "urgency": 0.9, "due": "2026-11-01", "reason": "考试临近"}],
        )
        self.assertEqual(result["applied"][0]["new"]["importance"], 0.45)
        self.assertEqual(result["applied"][0]["new"]["urgency"], 0.25)
        row = self.store.conn.execute(
            "select importance,urgency,due_at from memories where id=?", (self.fact,)
        ).fetchone()
        self.assertEqual(row["importance"], 0.45)
        self.assertEqual(row["urgency"], 0.25)
        self.assertEqual(row["due_at"], "2026-11-01")
        review = self.store.conn.execute(
            "select applied,rejected,changes from memory_reviews order by id desc limit 1"
        ).fetchone()
        self.assertEqual(review["applied"], 1)


if __name__ == "__main__":
    unittest.main()
