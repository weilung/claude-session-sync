import tempfile
import unittest
from pathlib import Path

from claude_session_sync.canonical import canon_hash, load
from claude_session_sync.lineset import analyze, analyze_result
from claude_session_sync.session_merge import (
    MergeOutcome,
    merge_sessions,
    render_jsonl,
)
from tests import fixtures as fx


class TestSessionMerge(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self._n = 0

    def tearDown(self):
        self._td.cleanup()

    def _shape(self, objs):
        self._n += 1
        return analyze(fx.write_jsonl(objs, str(self.tmp / f"f{self._n}.jsonl")))

    def _uuids(self, objs):
        return [o.get("uuid") for o in objs if o.get("uuid")]

    # ── 基本 union ────────────────────────────────────────────────────────────
    def test_fork_union_keeps_all_branch_lines(self):
        r = merge_sessions(self._shape(fx.linear()), self._shape(fx.fork_of_linear()))
        self.assertEqual(r.outcome, MergeOutcome.MERGED)
        # 共同前綴 u1,u2 各一次；兩枝 tip u3 / u4 都留。
        self.assertEqual(self._uuids(r.objs), ["u1", "u2", "u3", "u4"])
        # 結尾為新 last-prompt，指最新葉 u4（ts 4 > 3）。
        self.assertEqual(r.objs[-1]["type"], "last-prompt")
        self.assertEqual(r.chosen_tip, "u4")
        self.assertEqual(r.objs[-1]["leafUuid"], "u4")
        self.assertEqual({l.uuid for l in r.leaves}, {"u3", "u4"})

    def test_common_prefix_emitted_once(self):
        r = merge_sessions(self._shape(fx.linear()), self._shape(fx.fork_of_linear()))
        # u1/u2 各只出現一次（共同前綴不重複）。
        self.assertEqual(self._uuids(r.objs).count("u1"), 1)
        self.assertEqual(self._uuids(r.objs).count("u2"), 1)

    def test_superset_branch_union(self):
        # superset_branch：含 linear 全部 + 從 u2 長新枝 u9。
        r = merge_sessions(self._shape(fx.linear()), self._shape(fx.superset_branch_of_linear()))
        self.assertEqual(r.outcome, MergeOutcome.MERGED)
        self.assertEqual(set(self._uuids(r.objs)), {"u1", "u2", "u3", "u9"})
        self.assertEqual({l.uuid for l in r.leaves}, {"u3", "u9"})
        self.assertEqual(r.chosen_tip, "u9")  # ts 9 最新

    # ── 揮發 meta 不保留、結尾才補新 last-prompt ─────────────────────────────────
    def test_volatile_meta_not_carried(self):
        # 兩側皆含 last-prompt/mode/title；union 後輸入的揮發 meta 一律不保留。
        r = merge_sessions(self._shape(fx.linear_with_title()), self._shape(fx.fork_of_linear()))
        self.assertEqual(r.outcome, MergeOutcome.MERGED)
        types = [o["type"] for o in r.objs]
        # 只有結尾一條 last-prompt；無 custom-title / mode 殘留。
        self.assertEqual(types.count("last-prompt"), 1)
        self.assertEqual(types[-1], "last-prompt")
        self.assertNotIn("custom-title", types)
        self.assertNotIn("mode", types)

    def test_fresh_lastprompt_ignores_stale_input_tip(self):
        # stale_rewind：last-prompt 落後指 u3，但 u9 較新。union 重算 → 指 u9，不沿用 stale tip（B2）。
        r = merge_sessions(self._shape(fx.linear()), self._shape(fx.stale_rewind_of_linear()))
        self.assertEqual(r.outcome, MergeOutcome.MERGED)
        self.assertEqual(r.chosen_tip, "u9")
        self.assertEqual(r.objs[-1]["leafUuid"], "u9")

    # ── no-uuid 內容行：跨段不去重 ──────────────────────────────────────────────
    def test_summary_lines_kept_from_both_branches(self):
        # 兩枝各帶一條 summary（即使內容相同也各留：屬不同枝）。
        a = [
            fx.umsg("u1", None, "user", 1),
            fx.umsg("u2", "u1", "assistant", 2),
            fx.umsg("u3", "u2", "user", 3),
            fx.summary("同摘要"),
            fx.lastprompt("u3"),
        ]
        b = [
            fx.umsg("u1", None, "user", 1),
            fx.umsg("u2", "u1", "assistant", 2),
            fx.umsg("u4", "u2", "user", 4),
            fx.summary("同摘要"),
            fx.lastprompt("u4"),
        ]
        r = merge_sessions(self._shape(a), self._shape(b))
        self.assertEqual(r.outcome, MergeOutcome.MERGED)
        summaries = [o for o in r.objs if o.get("type") == "summary"]
        self.assertEqual(len(summaries), 2)  # 跨段同 hash 仍各留

    def test_summary_in_common_prefix_emitted_once(self):
        # summary 在共同前綴（divergence 之前）→ 只留一次。
        a = [
            fx.umsg("u1", None, "user", 1),
            fx.summary("前綴摘要"),
            fx.umsg("u2", "u1", "assistant", 2),
            fx.umsg("u3", "u2", "user", 3),
            fx.lastprompt("u3"),
        ]
        b = [
            fx.umsg("u1", None, "user", 1),
            fx.summary("前綴摘要"),
            fx.umsg("u2", "u1", "assistant", 2),
            fx.umsg("u4", "u2", "user", 4),
            fx.lastprompt("u4"),
        ]
        r = merge_sessions(self._shape(a), self._shape(b))
        summaries = [o for o in r.objs if o.get("type") == "summary"]
        self.assertEqual(len(summaries), 1)

    # ── uuid 去重 ──────────────────────────────────────────────────────────────
    def test_uuid_dedup_no_duplicate_lines(self):
        r = merge_sessions(self._shape(fx.linear()), self._shape(fx.fork_of_linear()))
        us = self._uuids(r.objs)
        self.assertEqual(len(us), len(set(us)))  # 無重複 uuid

    # ── chosen tip ────────────────────────────────────────────────────────────
    def test_explicit_chosen_tip_honored(self):
        r = merge_sessions(
            self._shape(fx.linear()), self._shape(fx.fork_of_linear()), chosen_tip="u3"
        )
        self.assertEqual(r.outcome, MergeOutcome.MERGED)
        self.assertEqual(r.chosen_tip, "u3")
        self.assertEqual(r.objs[-1]["leafUuid"], "u3")

    def test_invalid_chosen_tip_needs_decision(self):
        r = merge_sessions(
            self._shape(fx.linear()), self._shape(fx.fork_of_linear()), chosen_tip="u2"
        )
        # u2 是內部節點、非 genuine leaf → needs-decision（不寫 stale/錯 tip）。
        self.assertEqual(r.outcome, MergeOutcome.NEEDS_DECISION)
        self.assertIsNone(r.objs)

    def test_nonexistent_chosen_tip_needs_decision(self):
        r = merge_sessions(
            self._shape(fx.linear()), self._shape(fx.fork_of_linear()),
            chosen_tip="zzzzzzzz-0000-0000-0000-000000000000",
        )
        self.assertEqual(r.outcome, MergeOutcome.NEEDS_DECISION)

    def _fork_pair(self, t3, t4):
        """共享 u1,u2；A tip=u3@t3、B tip=u4@t4。t 為 None 則移除 timestamp。"""
        def _tip(uid, t):
            o = fx.umsg(uid, "u2", "user", t or 0)
            if t is None:
                del o["timestamp"]
            return o
        a = [fx.umsg("u1", None, "user", 1), fx.umsg("u2", "u1", "assistant", 2),
             _tip("u3", t3), fx.lastprompt("u3")]
        b = [fx.umsg("u1", None, "user", 1), fx.umsg("u2", "u1", "assistant", 2),
             _tip("u4", t4), fx.lastprompt("u4")]
        return a, b

    # ── 自動 tip 選擇：只在「唯一最新」拍板（codex r21）──────────────────────────
    def test_missing_timestamp_auto_tip_needs_decision(self):
        a, b = self._fork_pair(None, None)  # 兩葉皆無 ts
        r = merge_sessions(self._shape(a), self._shape(b))
        self.assertEqual(r.outcome, MergeOutcome.NEEDS_DECISION)
        self.assertIsNone(r.objs)
        self.assertEqual({l.uuid for l in r.leaves}, {"u3", "u4"})  # 仍給候選供互動挑選

    def test_tied_timestamp_auto_tip_needs_decision(self):
        a, b = self._fork_pair(4, 4)  # 並列最新 ts
        r = merge_sessions(self._shape(a), self._shape(b))
        self.assertEqual(r.outcome, MergeOutcome.NEEDS_DECISION)

    def test_ambiguous_tip_resolves_with_explicit_choice(self):
        # 無 ts 自動不可選，但使用者明確指定 → 可 MERGE。
        a, b = self._fork_pair(None, None)
        r = merge_sessions(self._shape(a), self._shape(b), chosen_tip="u4")
        self.assertEqual(r.outcome, MergeOutcome.MERGED)
        self.assertEqual(r.chosen_tip, "u4")

    # ── 安全：退回挑選 ──────────────────────────────────────────────────────────
    def test_damaged_falls_back(self):
        # u2 跨檔被改寫（同 uuid 異 hash）→ 兩檔分別 analyze 不一定 damaged，但内容不同 → 用單檔壞行測。
        bad = [
            fx.umsg("u1", None, "user", 1),
            fx.umsg("u2", "u1", "assistant", 2),
            fx.umsg("u2", "u1", "assistant", 2, content="X"),  # 同檔同 uuid 異 hash → damaged
            fx.lastprompt("u2"),
        ]
        r = merge_sessions(self._shape(fx.linear()), self._shape(bad))
        self.assertEqual(r.outcome, MergeOutcome.FALLBACK)

    def test_collision_falls_back(self):
        # 零共同 uuid → 無共同祖先 → 退回挑選。
        r = merge_sessions(self._shape(fx.linear()), self._shape(fx.disjoint()))
        self.assertEqual(r.outcome, MergeOutcome.FALLBACK)

    def test_disconnected_root_injection_falls_back(self):
        # 一側含非-system disconnected 根（不相關對話）→ union 後多非-system 根 → 退回挑選。
        r = merge_sessions(
            self._shape(fx.linear()), self._shape(fx.disconnected_root_injection())
        )
        self.assertEqual(r.outcome, MergeOutcome.FALLBACK)

    # ── 決定性 / commutative ───────────────────────────────────────────────────
    def test_deterministic_same_input(self):
        s1, s2 = self._shape(fx.linear()), self._shape(fx.fork_of_linear())
        r1 = merge_sessions(s1, s2)
        r2 = merge_sessions(s1, s2)
        self.assertEqual(render_jsonl(r1.objs), render_jsonl(r2.objs))

    def test_commutative_bytes(self):
        # union(local,hub) 與 union(hub,local) 產生**相同 bytes**（與標籤無關）。
        lin, frk = fx.linear(), fx.fork_of_linear()
        r_lh = merge_sessions(self._shape(lin), self._shape(frk))
        r_hl = merge_sessions(self._shape(frk), self._shape(lin))
        self.assertEqual(render_jsonl(r_lh.objs), render_jsonl(r_hl.objs))

    # ── render：canonical bytes、reload 一致 ─────────────────────────────────────
    def test_render_reload_roundtrip_classifies_consistently(self):
        r = merge_sessions(self._shape(fx.linear()), self._shape(fx.fork_of_linear()))
        data = render_jsonl(r.objs)
        out = self.tmp / "merged.jsonl"
        out.write_bytes(data)
        reshape = analyze(str(out))
        # 重載後：uuid 集合一致、active_tip = 我們寫的 chosen、兩 genuine leaf。
        self.assertEqual(reshape.uuids, {"u1", "u2", "u3", "u4"})
        self.assertEqual(reshape.active_tip, "u4")
        self.assertEqual({l.uuid for l in reshape.genuine_leaves}, {"u3", "u4"})
        self.assertFalse(reshape.is_damaged)

    def test_render_lines_canonical_idempotent(self):
        # render 出的每行 canon_hash 與原 obj 一致（canonical 序列化 idempotent）。
        r = merge_sessions(self._shape(fx.linear()), self._shape(fx.fork_of_linear()))
        out = self.tmp / "m.jsonl"
        out.write_bytes(render_jsonl(r.objs))
        reshape = analyze_result(load(str(out)))
        for ln in reshape.lines:
            if ln.obj is not None:
                self.assertEqual(ln.canon_hash, canon_hash(ln.obj))


if __name__ == "__main__":
    unittest.main()
