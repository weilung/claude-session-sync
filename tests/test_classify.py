import tempfile
import unittest
from pathlib import Path

from claude_session_sync.classify import Klass, classify
from claude_session_sync.lineset import analyze
from tests import fixtures as fx


class TestClassify(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self._n = 0

    def tearDown(self):
        self._td.cleanup()

    def _shape(self, objs):
        self._n += 1
        return analyze(fx.write_jsonl(objs, str(self.tmp / f"f{self._n}.jsonl")))

    def _k(self, a, b):
        return classify(self._shape(a), self._shape(b)).klass

    def test_identical(self):
        self.assertEqual(self._k(fx.linear(), fx.linear()), Klass.IDENTICAL)

    def test_fast_forward(self):
        c = classify(self._shape(fx.linear()), self._shape(fx.fast_forward_of_linear()))
        self.assertEqual(c.klass, Klass.FAST_FORWARD)
        self.assertEqual(c.direction, "hub->local")

    def test_fast_forward_symmetric_direction(self):
        c = classify(self._shape(fx.fast_forward_of_linear()), self._shape(fx.linear()))
        self.assertEqual(c.klass, Klass.FAST_FORWARD)
        self.assertEqual(c.direction, "local->hub")

    def test_ff_overwrite_dropping_hub_title_is_needs_decision(self):
        # codex r19：ff local->hub 會覆蓋 hub；hub 有 local 缺的 custom-title → 不可靜默丟 → needs-decision。
        c = classify(self._shape(fx.fast_forward_of_linear()), self._shape(fx.linear_with_title()))
        self.assertEqual(c.klass, Klass.NEEDS_DECISION)

    def test_ff_hub_to_local_with_title_still_ff(self):
        # 反向（hub->local 走 keep-both、不覆蓋）：local 有 title、hub 是其 ff → 仍 ff（不丟，keep-both 保留）。
        c = classify(self._shape(fx.linear_with_title()), self._shape(fx.fast_forward_of_linear()))
        self.assertEqual(c.klass, Klass.FAST_FORWARD)
        self.assertEqual(c.direction, "hub->local")

    def test_superset_branch(self):
        self.assertEqual(
            self._k(fx.linear(), fx.superset_branch_of_linear()), Klass.SUPERSET_BRANCH
        )

    def test_stale_rewind_not_ff(self):
        # 多出新枝（2 genuine leaf）→ 不可自動 ff（superset-branch）
        self.assertEqual(
            self._k(fx.linear(), fx.stale_rewind_of_linear()), Klass.SUPERSET_BRANCH
        )

    def test_two_new_genuine_leaves_not_ff(self):
        self.assertEqual(
            self._k(fx.linear(), fx.two_new_genuine_leaves()), Klass.SUPERSET_BRANCH
        )

    def test_active_tip_missing_blocks_ff(self):
        self.assertEqual(self._k(fx.linear(), fx.active_tip_missing()), Klass.NEEDS_DECISION)

    def test_active_tip_to_fanout_blocks_ff(self):
        self.assertEqual(self._k(fx.linear(), fx.active_tip_to_fanout()), Klass.NEEDS_DECISION)

    def test_cross_file_uuid_hash_conflict_is_damaged(self):
        self.assertEqual(self._k(fx.linear(), fx.linear_u2_rewritten()), Klass.DAMAGED)

    def test_active_tip_none_single_leaf_can_ff(self):
        # 記錄並固定意圖（codex r5）：無 last-prompt 但唯一 genuine leaf → tip 明確 → 允許 ff
        c = classify(self._shape(fx.linear_no_lastprompt()), self._shape(fx.ff_no_lastprompt()))
        self.assertEqual(c.klass, Klass.FAST_FORWARD)

    def test_fork(self):
        self.assertEqual(self._k(fx.linear(), fx.fork_of_linear()), Klass.FORK)

    def test_identity_collision(self):
        self.assertEqual(self._k(fx.linear(), fx.disjoint()), Klass.IDENTITY_COLLISION)

    def test_damaged_bad_line(self):
        a = fx.write_jsonl(fx.linear(), str(self.tmp / "good.jsonl"))
        b = self.tmp / "bad.jsonl"
        b.write_text('{"uuid":"u1","parentUuid":null,"type":"user"}\nNOT JSON\n', encoding="utf-8")
        self.assertEqual(classify(analyze(a), analyze(str(b))).klass, Klass.DAMAGED)

    def test_damaged_zero_byte(self):
        a = fx.write_jsonl(fx.linear(), str(self.tmp / "good2.jsonl"))
        z = self.tmp / "z.jsonl"
        z.write_bytes(b"")
        self.assertEqual(classify(analyze(a), analyze(str(z))).klass, Klass.DAMAGED)

    def test_disconnected_root_injection_not_ff(self):
        # 回歸測：superset 含新增非-system disconnected 根 → 絕不可 FAST_FORWARD
        c = classify(self._shape(fx.linear()), self._shape(fx.disconnected_root_injection()))
        self.assertEqual(c.klass, Klass.NEEDS_DECISION)

    def test_volatile_meta_excluded_from_compare(self):
        # 對話相同、只揮發 meta 不同 → 不得判 fork（應 identical 或 needs-decision，不是 fork）
        c = classify(self._shape(fx.linear()), self._shape(fx.linear_diff_volatile_only()))
        self.assertNotEqual(c.klass, Klass.FORK)
        self.assertIn(c.klass, {Klass.IDENTICAL, Klass.NEEDS_DECISION})

    def test_summary_only_diff_is_superset_branch_not_ff(self):
        # 只多一條內容性 summary（無新 uuid 行）→ 不可自動 ff（codex r4：嚴格 superset-branch）
        c = classify(self._shape(fx.linear()), self._shape(fx.linear_extra_summary()))
        self.assertEqual(c.klass, Klass.SUPERSET_BRANCH)

    def test_compact_superset_not_collision(self):
        # compact 新增 system 根，但與既有鏈共享 uuid → 不可誤判 collision
        k = self._k(fx.linear(), fx.compact_system_root())
        self.assertNotEqual(k, Klass.IDENTITY_COLLISION)
        self.assertIn(k, {Klass.FAST_FORWARD, Klass.SUPERSET_BRANCH})


if __name__ == "__main__":
    unittest.main()
