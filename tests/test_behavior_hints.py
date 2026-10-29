import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.memory import MemoryStore


class _OfflineClient:
    pass


class BehaviorHintsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MemoryStore(
            _OfflineClient(),
            path=str(Path(self.directory.name) / "memory.db"),
            embed_model="offline-test",
        )
        self.addCleanup(self.store.db.close)

    def hints_for(self, value):
        self.store.upsert_slots([{"entity": "年糕", "attr": "状态", "value": value}])
        return self.store.behavior_hints()

    def test_recovered_state_does_not_match_negated_gone_word(self):
        self.assertEqual(self.hints_for("已经找回来了，不再走失"), [])
        self.assertEqual(self.hints_for("没有走失，在家"), [])
        self.assertEqual(self.hints_for("不曾走失，一直在家"), [])
        self.assertEqual(self.hints_for("从未走失，一直在家"), [])
        self.assertEqual(self.hints_for("没再走失，一直在家"), [])
        self.assertEqual(self.hints_for("没有再走失，一直在家"), [])

    def test_positive_gone_word_still_produces_hint(self):
        hints = self.hints_for("走失中，仍未回家")
        self.assertEqual(len(hints), 1)
        self.assertIn("年糕", hints[0])

    def test_independent_gone_word_survives_another_negated_word(self):
        hints = self.hints_for("没有走失，但已送人")
        self.assertEqual(len(hints), 1)
        self.assertIn("年糕", hints[0])

    def test_double_negation_does_not_suppress_gone_word(self):
        hints = self.hints_for("不是没有走失，仍在外面")
        self.assertEqual(len(hints), 1)

    def test_gone_terms_starting_with_negation_are_positive_terms(self):
        self.assertEqual(len(self.hints_for("没回来")), 1)
        self.assertEqual(len(self.hints_for("未归")), 1)


if __name__ == "__main__":
    unittest.main()
