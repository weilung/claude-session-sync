import tempfile
import unittest
from pathlib import Path

from claude_session_sync.lineset import analyze, is_ancestor
from tests import fixtures as fx


class TestLineset(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _shape(self, objs, name="s.jsonl"):
        return analyze(fx.write_jsonl(objs, str(self.tmp / name)))

    def test_single_root_single_leaf(self):
        s = self._shape(fx.linear())
        self.assertEqual([r.uuid for r in s.roots], ["u1"])
        self.assertEqual([l.uuid for l in s.genuine_leaves], ["u3"])
        self.assertEqual(s.active_tip, "u3")
        self.assertFalse(s.is_damaged)

    def test_compact_adds_system_root(self):
        s = self._shape(fx.compact_system_root())
        root_uuids = {r.uuid for r in s.roots}
        self.assertIn("u1", root_uuids)
        self.assertIn("sysroot", root_uuids)
        # system 根存在但檔仍與既有鏈共享 u1..u3
        self.assertTrue({"u1", "u2", "u3"} <= s.uuids)

    def test_tool_fanout_excluded_from_leaves(self):
        s = self._shape(fx.with_tool_fanout())
        leaf_uuids = {l.uuid for l in s.genuine_leaves}
        self.assertIn("u5", leaf_uuids)
        self.assertNotIn("tr1", leaf_uuids)  # fan-out 葉不算 genuine tip
        self.assertEqual(s.active_tip, "u5")

    def test_active_tip_is_last_lastprompt(self):
        objs = fx.linear() + [fx.lastprompt("u2")]  # 後一條 last-prompt 覆蓋前者
        s = self._shape(objs, "lp.jsonl")
        self.assertEqual(s.active_tip, "u2")

    def test_same_uuid_diff_is_damage_signal(self):
        objs = [
            fx.umsg("u1", None, "user", 1),
            fx.umsg("u2", "u1", "assistant", 2, content="A"),
            fx.umsg("u2", "u1", "assistant", 2, content="B"),  # 同 uuid 不同內容
        ]
        s = self._shape(objs, "dup.jsonl")
        self.assertIn("u2", s.same_uuid_diff)
        self.assertTrue(s.is_damaged)

    def test_is_ancestor(self):
        s = self._shape(fx.fast_forward_of_linear())
        self.assertTrue(is_ancestor(s, "u3", "u4"))
        self.assertTrue(is_ancestor(s, "u1", "u4"))
        self.assertFalse(is_ancestor(s, "u4", "u1"))


if __name__ == "__main__":
    unittest.main()
