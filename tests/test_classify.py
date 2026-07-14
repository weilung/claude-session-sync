import tempfile
import unittest
from pathlib import Path

from claude_session_sync import classify as classify_mod
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

    # ── smaller 側「落後指標」（回合中途快照）：`_lagging_tip` ────────────────────
    # 實機 a9f7f783（2026-07-14）：hub 是回合中途的快照（助理已回、使用者未送下一則）→ 其 last-prompt
    # 指向已有子節點的 user 行 → 非葉 → 舊規則判 tip 無效 → 純延伸被誤擋成 superset-branch，每次
    # sync 重報且逼人工。smaller 無歧義時應回退取其唯一 genuine leaf。

    def test_lagging_pointer_smaller_still_ff(self):
        c = classify(self._shape(fx.mid_turn_continued()), self._shape(fx.mid_turn_snapshot()))
        self.assertEqual(c.klass, Klass.FAST_FORWARD)
        self.assertEqual(c.direction, "local->hub")

    def test_cursor_on_tool_result_line_still_ff(self):
        # 護欄：實機真實形狀——游標指向一條帶 toolUseResult 的**工具結果行**（fan-out，非葉候選但**是**
        # 真對話節點、在葉的祖先鏈上）。守衛 2 若連 fan-out 都排除（codex g8 的建議），這個本塊要修的
        # 真實案例會被反過來擋死。此測試把那條界線釘住。
        c = classify(self._shape(fx.mid_turn_cursor_on_tool_result_continued()),
                     self._shape(fx.mid_turn_cursor_on_tool_result()))
        self.assertEqual(c.klass, Klass.FAST_FORWARD)
        self.assertEqual(c.direction, "local->hub")

    def test_lagging_pointer_smaller_still_ff_reverse(self):
        # 反向（hub 較新、local 是回合中途快照）→ ff hub->local（apply 走 keep-both、不覆蓋 local）
        c = classify(self._shape(fx.mid_turn_snapshot()), self._shape(fx.mid_turn_continued()))
        self.assertEqual(c.klass, Klass.FAST_FORWARD)
        self.assertEqual(c.direction, "hub->local")

    def test_lagging_pointer_does_not_bypass_title_guard(self):
        # 落後指標的新路徑**不得**繞過「ff 覆蓋 hub 會丟 hub 端標題」守衛（codex r19）
        c = classify(self._shape(fx.mid_turn_continued()), self._shape(fx.mid_turn_snapshot_with_title()))
        self.assertEqual(c.klass, Klass.NEEDS_DECISION)

    def test_lagging_pointer_two_leaves_blocks_ff(self):
        # 條件 1：smaller 有兩個 genuine leaf → 哪一枝是活的有歧義 → 不回退（bigger 側條件全過，
        # 故擋下 ff 的確實是這條）
        c = classify(self._shape(fx.mid_turn_two_leaves_continued()), self._shape(fx.mid_turn_two_leaves()))
        self.assertEqual(c.klass, Klass.SUPERSET_BRANCH)

    def test_lagging_pointer_dangling_blocks_ff(self):
        # 條件 2：指標指向檔外 uuid（根的缺席父親）→ dangling → 不回退。
        # 若少了「在本檔 uuid 集內」的明檢，is_ancestor 會沿 parent_map 走到根的檔外父親而誤判 True。
        c = classify(self._shape(fx.mid_turn_ghost_root_parent_continued()),
                     self._shape(fx.mid_turn_ghost_root_parent()))
        self.assertEqual(c.klass, Klass.SUPERSET_BRANCH)

    def test_lagging_pointer_to_sidechain_blocks_ff(self):
        # 條件 2：指標指向 sidechain 行（非對話主鏈真行）→ 不可信 → 不回退
        c = classify(self._shape(fx.mid_turn_pointer_off_branch_continued()),
                     self._shape(fx.mid_turn_pointer_off_branch()))
        self.assertEqual(c.klass, Klass.SUPERSET_BRANCH)

    def test_lagging_pointer_to_meta_ancestor_blocks_ff(self):
        # 條件 2（codex g8 的 High）：指標指向**帶 uuid 的揮發 meta 行**，且該行是唯一真葉的祖先、
        # 行序也在葉之前 → 守衛 1/3/4 全過 → 僅「主鏈真行」判定擋得住。
        c = classify(self._shape(fx.mid_turn_pointer_to_meta_ancestor_continued()),
                     self._shape(fx.mid_turn_pointer_to_meta_ancestor()))
        self.assertNotEqual(c.klass, Klass.FAST_FORWARD)

    def test_lagging_pointer_off_real_branch_blocks_ff(self):
        # 條件 3（隔離）：指標指向一條**真對話行**（過得了守衛 2），但它在另一條枝上、不在唯一真葉的
        # 祖先鏈上 → 非單純落後 → 不回退。
        c = classify(self._shape(fx.mid_turn_pointer_off_real_branch_continued()),
                     self._shape(fx.mid_turn_pointer_off_real_branch()))
        self.assertEqual(c.klass, Klass.SUPERSET_BRANCH)

    def test_lagging_pointer_to_fanout_blocks_ff(self):
        # 條件 3（fan-out 口味，codex R1 指出的涵蓋缺口）：指標指向工具 fan-out 行（存在於檔內、
        # 非 genuine leaf、不在唯一葉的祖先鏈上）→ 非單純落後 → 不回退
        c = classify(self._shape(fx.mid_turn_pointer_to_fanout_continued()),
                     self._shape(fx.mid_turn_pointer_to_fanout()))
        self.assertEqual(c.klass, Klass.SUPERSET_BRANCH)

    def test_deliberate_rewind_blocks_ff(self):
        # 條件 4（codex g1 的 High 反例）：smaller 的 last-prompt 寫在葉子**之後** → 語意是「使用者
        # 刻意 rewind 回祖先、放棄該葉」，不是落後指標。圖形與 mid_turn_snapshot 完全相同、只差行序。
        # 誤放行會 ff local->hub 覆蓋 hub、靜默改掉那個刻意的游標。
        c = classify(self._shape(fx.mid_turn_continued()), self._shape(fx.rewound_to_ancestor()))
        self.assertEqual(c.klass, Klass.SUPERSET_BRANCH)

    def test_rewind_and_midturn_differ_only_in_line_order(self):
        # 釘住條件 4 的判準本身：兩個 fixture 的「內容行」完全相同（同 uuid/parent/指標/唯一葉），
        # 差別**只有** last-prompt 的行序 → 一個可 ff、一個不可。若哪天 lineset 不再保序即會紅。
        mid, rew = self._shape(fx.mid_turn_snapshot()), self._shape(fx.rewound_to_ancestor())
        self.assertEqual({ln.uuid for ln in mid.lines if ln.uuid},
                         {ln.uuid for ln in rew.lines if ln.uuid})
        self.assertEqual(mid.active_tip, rew.active_tip)
        self.assertEqual([lf.uuid for lf in mid.genuine_leaves], [lf.uuid for lf in rew.genuine_leaves])
        self.assertEqual(classify(self._shape(fx.mid_turn_continued()), mid).klass, Klass.FAST_FORWARD)
        self.assertEqual(classify(self._shape(fx.mid_turn_continued()), rew).klass, Klass.SUPERSET_BRANCH)

    def test_lastprompt_after_leaf_without_leafuuid_blocks_ff(self):
        # 條件 4b（codex g6 線索）：葉子之後又有 last-prompt，但缺 leafUuid → 對 active_tip_index 隱形。
        # 「葉子存在後游標被碰過」即無法證明指標只是落後 → fail-closed。
        c = classify(self._shape(fx.mid_turn_continued()),
                     self._shape(fx.rewind_marker_without_leafuuid()))
        self.assertEqual(c.klass, Klass.SUPERSET_BRANCH)

    def test_metadata_only_extension_is_not_ff(self):
        # codex g5：唯一的「新增」是一條帶 uuid 的 last-prompt 簿記行 → 對話內容其實相同、只有游標不同
        # → 不得當 ff 覆蓋對側（那會繞過「內容同、active_tip 異 → needs-decision」的游標守衛）。
        c = classify(self._shape(fx.metadata_only_extension()), self._shape(fx.linear_no_lastprompt()))
        self.assertNotEqual(c.klass, Klass.FAST_FORWARD)

    def test_sidechain_only_extension_still_ff(self):
        # 反向保護：sidechain（子代理）行是**真內容**，不是簿記 → 只多 sidechain、主鏈 tip 未前進的
        # 合法無損延伸必須**仍可 ff**（否則「延伸須含真內容」的修法會誤傷、製造新噪音）。
        c = classify(self._shape(fx.sidechain_only_extension()), self._shape(fx.linear()))
        self.assertEqual(c.klass, Klass.FAST_FORWARD)
        self.assertEqual(c.direction, "local->hub")

    def test_meta_node_as_dag_link_blocks_ff(self):
        # codex g9：ff 的「純延伸」證明（is_ancestor）穿過一條**帶 uuid 的簿記行**（我們自己宣告它不是
        # 對話節點、union 還會丟掉它）→ 證明建立在一個不存在的連結上 → 結構異常，fail-closed 交人。
        c = classify(self._shape(fx.meta_node_as_dag_link()), self._shape(fx.meta_node_as_dag_link_smaller()))
        self.assertEqual(c.klass, Klass.NEEDS_DECISION)
        self.assertNotEqual(c.klass, Klass.FAST_FORWARD)

    def test_parent_cycle_blocks_ff(self):
        # codex g4 反例①：self-parent 環。四道 _lagging_tip 守衛全過（is_ancestor 對環回 True），
        # 但 ff 的整套推理建立在 DAG 上 → classify 須先拒環，fail-closed 交人。
        c = classify(self._shape(fx.self_parent_cycle_continued()), self._shape(fx.self_parent_cycle()))
        self.assertEqual(c.klass, Klass.NEEDS_DECISION)
        self.assertNotEqual(c.klass, Klass.FAST_FORWARD)

    def test_cursor_line_being_the_leaf_blocks_ff(self):
        # 條件 4 的邊界（codex g2 的 High）：last-prompt 行自己帶 uuid → 游標行**就是**葉子行
        # （active_tip_index == leaf_first）→ 相等不證明「游標寫下時葉子不存在」→ 不可 ff。
        c = classify(self._shape(fx.cursor_line_is_the_leaf_continued()),
                     self._shape(fx.cursor_line_is_the_leaf()))
        self.assertNotEqual(c.klass, Klass.FAST_FORWARD)

    def test_volatile_meta_line_is_not_a_genuine_leaf(self):
        # lineset 收緊（codex g2）：揮發 meta 行（即使畸形地帶了 uuid）不得成為對話 tip。
        # 此檔的 meta 行 uuid=u4、parent=u3 → u4 被排除、u3 因被當 parent 而非葉 → 零 genuine leaf。
        s = self._shape(fx.cursor_line_is_the_leaf())
        self.assertNotIn("u4", [lf.uuid for lf in s.genuine_leaves])
        self.assertEqual(s.genuine_leaves, [])

    def test_lagging_tip_fails_closed_if_cursor_line_became_a_leaf(self):
        # 條件 4 的**縱深防禦**：lineset 收緊後，「游標行本身被當成葉子」已結構上不可能（last-prompt 不再
        # 算 genuine leaf）。此測試繞過該收緊、直接把游標行塞回 genuine_leaves，證明即使 leaf 抽取邏輯
        # 日後放寬，「葉子首現之後不得有 last-prompt 行」仍會 fail-closed 擋下（葉子行自己就是 last-prompt）。
        s = self._shape(fx.cursor_line_is_the_leaf())
        meta_line = next(ln for ln in s.lines if ln.type == "last-prompt")
        s.genuine_leaves = [meta_line]                       # 模擬「meta 行又被當成葉子」的回歸
        self.assertIsNone(classify_mod._lagging_tip(s))      # fail-closed，不回退

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
