"""A15 ack 帳本測試（DESIGN 附錄 A15「blocked 收斂出口」）。

分兩路：
  - **純函式**（手搭 SyncPlan + 真實 ledger IO）——不依賴磁碟 casefold collision，故各平台都跑。涵蓋
    ackable 抽取/分組、fingerprint、is_acked、帳本 round-trip、fail-closed、idempotent、remove、呈現層過濾。
  - **真檔 e2e**——collision 需 case-sensitive FS（`@needs_case_sensitive_fs`，Windows 大小寫不敏感則 skip）；
    damaged 呈現測可攜。CLI round-trip（status→ack→status→show→unack）驗整條接線。

**最重要不變量**：ack 是**純呈現層**——絕不改變分類/apply。以「acked 後 plan 的 action 仍 blocked」+「apply 不碰」驗證。
"""
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from claude_session_sync import acks, apply as apply_mod, cli, doctor, scan, state as state_mod, tombstone
from claude_session_sync.scan import ProjectPlan, SessionPlan, SyncPlan
from claude_session_sync.state import State
from tests import _caps, fixtures as fx


def _plan(hub_dir, sessions, local_dir=None):
    """手搭單專案 SyncPlan（純函式測試；不依賴磁碟 collision）。"""
    pp = ProjectPlan(
        local_dir=str(local_dir) if local_dir else None,
        hub_dir=str(hub_dir), identity="match", coverage_initialized=True, sessions=sessions)
    return SyncPlan(first_run=False, anomalies=[], projects=[pp])


def _coll(*sids):
    return [SessionPlan(s, "blocked-casefold-collision", None, "casefold 撞名") for s in sids]


class TestAckableExtraction(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.proj = self.tmp / "hub" / "projA"
        self.proj.mkdir(parents=True)

    def tearDown(self):
        self._td.cleanup()

    def test_collision_grouped_by_casefold_key(self):
        items = acks.ackable_from_plan(_plan(self.proj, _coll("ABC", "abc", "Zzz", "zzz")))
        by_id = {it.identity: it for it in items}
        self.assertEqual(set(by_id), {"abc", "zzz"})            # 兩個 casefold 組
        self.assertEqual(by_id["abc"].kind, "casefold-collision")
        self.assertEqual(by_id["abc"].session_ids, ("ABC", "abc"))
        self.assertEqual(by_id["zzz"].session_ids, ("Zzz", "zzz"))

    def test_collision_fingerprint_changes_when_set_changes(self):
        two = acks.ackable_from_plan(_plan(self.proj, _coll("ABC", "abc")))[0].fingerprint
        three = acks.ackable_from_plan(_plan(self.proj, _coll("ABC", "abc", "Abc")))[0].fingerprint
        self.assertNotEqual(two, three)                          # 新拼法加入 → 指紋變 → 會重報

    def test_collision_fingerprint_is_content_aware(self):
        # g4 High：collision fp 綁撞名檔內容 → 撞名檔內容改（如變 damaged）→ fp 變 → 重報（撞名閘先於 damaged，
        # 否則撞名檔之一變壞會被舊 collision ack 遮蓋）。名稱集不變、內容改 → fp 仍須變。
        (self.proj / "A.jsonl").write_bytes(b"content-v1")
        fp1 = acks.fingerprint_collision(["A", "a"], {}, scan._session_files(self.proj))
        (self.proj / "A.jsonl").write_bytes(b"content-v2-corrupt-or-changed")
        fp2 = acks.fingerprint_collision(["A", "a"], {}, scan._session_files(self.proj))
        self.assertNotEqual(fp1, fp2)                            # 內容改 → fp 變
        self.assertEqual(fp2, acks.fingerprint_collision(["A", "a"], {}, scan._session_files(self.proj)))  # 穩定

    def test_damaged_item_fingerprint_reflects_bytes(self):
        (self.proj / "dmg.jsonl").write_bytes(b"{ not valid json\n")
        local = self.tmp / "local" / "projA"
        local.mkdir(parents=True)
        items = acks.ackable_from_plan(_plan(
            self.proj, [SessionPlan("dmg", "blocked-damaged-source", None, "壞檔")], local_dir=local))
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0].kind, items[0].identity), ("damaged", "dmg"))
        fp1 = items[0].fingerprint
        self.assertTrue(fp1.startswith("fs:"))
        # 內容改 → 指紋變
        (self.proj / "dmg.jsonl").write_bytes(b"{ different garbage\n")
        fp2 = acks.ackable_from_plan(_plan(
            self.proj, [SessionPlan("dmg", "blocked-damaged-source", None, "壞檔")], local_dir=local))[0].fingerprint
        self.assertNotEqual(fp1, fp2)

    def test_unbindable_fingerprint_is_none(self):
        # g5 Medium：present 但讀不到的檔（此處以目錄路徑觸發 raw_file_digest 的 OSError）→ 指紋不可綁定 → None。
        d = self.proj / "adir"
        d.mkdir()
        self.assertIsNone(acks.fingerprint_files(d))                          # 目錄當檔讀 → OSError → None
        self.assertIsNone(acks.fingerprint_collision(["X", "x"], {}, {"X": d}))   # 撞名檔不可讀 → None

    @_caps.needs_unreadable_dir
    def test_unreadable_damaged_is_unbindable(self):
        # g5 Medium + g6：不可讀 damaged → **仍列出但 fp=None**（不可綁定 → 不可 ack；不 skip，否則 g6 誤藏）。
        local = self.tmp / "local" / "projA"
        local.mkdir(parents=True)
        f = local / "dmg.jsonl"
        f.write_bytes(b"{ broken\n")
        os.chmod(f, 0)
        try:
            plan = _plan(self.proj, [SessionPlan("dmg", "blocked-damaged-source", None, "壞")], local_dir=local)
            items = acks.ackable_from_plan(plan)
            self.assertEqual(len(items), 1)
            self.assertIsNone(items[0].fingerprint)               # 不可綁定 → fp None → 不可 ack
        finally:
            os.chmod(f, stat.S_IRWXU)

    def test_both_present_damaged_action_is_ackable(self):
        items = acks.ackable_from_plan(_plan(self.proj, [SessionPlan("x", "damaged", None, "壞")]))
        self.assertEqual([(it.kind, it.identity) for it in items], [("damaged", "x")])

    def test_identity_collision_action_is_ackable(self):
        items = acks.ackable_from_plan(_plan(self.proj, [SessionPlan("y", "identity-collision", None, "同uuid異hash")]))
        self.assertEqual([(it.kind, it.identity) for it in items], [("identity-collision", "y")])

    def test_non_ackable_actions_ignored(self):
        plan = _plan(self.proj, [SessionPlan("s1", "copy-to-hub", "local->other", "新檔"),
                                 SessionPlan("s2", "blocked-unmapped", None, "未對應"),
                                 SessionPlan("s3", "identical", None, "同")])
        self.assertEqual(acks.ackable_from_plan(plan), [])

    def test_no_hub_dir_not_ackable(self):
        pp = ProjectPlan(local_dir=str(self.tmp / "l"), hub_dir=None, identity="local-only",
                         coverage_initialized=False, sessions=_coll("ABC", "abc"))
        self.assertEqual(acks.ackable_from_plan(SyncPlan(False, [], [pp])), [])


class TestLedgerIO(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.proj = self.tmp / "hub" / "projA"
        self.proj.mkdir(parents=True)
        self.item = acks.AckItem("projA", str(self.proj), "damaged", "dmg", "fs:abc", ("dmg",), "dmg")

    def tearDown(self):
        self._td.cleanup()

    def test_missing_ledger_is_empty_ok(self):
        led = acks.load_ledger(self.proj)
        self.assertTrue(led.ok)
        self.assertEqual(led.by_key, {})

    def test_roundtrip_and_fingerprint_binding(self):
        res = acks.update_ledger(self.proj, add=[self.item])
        self.assertEqual(res.added, ["dmg"])
        led = acks.load_ledger(self.proj)
        self.assertTrue(acks.is_acked(led, "damaged", "dmg", "fs:abc"))
        self.assertFalse(acks.is_acked(led, "damaged", "dmg", "fs:CHANGED"))  # 指紋變 → 未 ack

    def test_idempotent_reack(self):
        acks.update_ledger(self.proj, add=[self.item])
        res = acks.update_ledger(self.proj, add=[self.item])
        self.assertEqual((res.added, res.unchanged), ([], ["dmg"]))

    def test_two_fingerprints_same_identity_coexist(self):
        # g2 Medium：同一 (kind,identity) 不同 fp（同 hub 專案多 local view 的 damaged 內容不同）**並存不互蓋**。
        it2 = acks.AckItem("projA", str(self.proj), "damaged", "dmg", "fs:NEW", ("dmg",), "dmg")
        res = acks.update_ledger(self.proj, add=[self.item, it2])
        self.assertEqual(len(res.added), 2)          # 兩者都算新增（非後者覆蓋前者）
        led = acks.load_ledger(self.proj)
        self.assertEqual(len(led.by_key), 2)
        self.assertTrue(acks.is_acked(led, "damaged", "dmg", "fs:abc"))
        self.assertTrue(acks.is_acked(led, "damaged", "dmg", "fs:NEW"))

    def test_remove(self):
        acks.update_ledger(self.proj, add=[self.item])
        res = acks.update_ledger(self.proj, remove=[("damaged", "dmg", "fs:abc")])   # 三元組 key（g2）
        self.assertEqual(res.removed, ["dmg"])
        self.assertEqual(acks.load_ledger(self.proj).by_key, {})

    def test_corrupt_ledger_fail_closed(self):
        tdir = self.proj / tombstone.TOMB_DIR
        tdir.mkdir()
        (tdir / acks.ACKS_FILE).write_bytes(b"{ not json")
        led = acks.load_ledger(self.proj)
        self.assertFalse(led.ok)                 # 呼叫端據此警告
        self.assertEqual(led.by_key, {})         # 壞帳本 → 不 suppress 任何項（fail-closed）

    def test_wrong_version_fail_closed(self):
        tdir = self.proj / tombstone.TOMB_DIR
        tdir.mkdir()
        (tdir / acks.ACKS_FILE).write_text(json.dumps({"version": 999, "acks": []}))
        led = acks.load_ledger(self.proj)
        self.assertFalse(led.ok)
        self.assertEqual(led.by_key, {})

    def test_bool_or_float_version_rejected(self):
        # R1 High#1：`True == 1` / `1.0 == 1` 為真 → 若只比值會把 {"version": true} 當合法 → 壞帳本 suppress 真問題。
        tdir = self.proj / tombstone.TOMB_DIR
        tdir.mkdir()
        rec = [{"kind": "damaged", "identity": "dmg", "fingerprint": "fs:abc", "label": "dmg"}]
        for bad in (True, 1.0):
            (tdir / acks.ACKS_FILE).write_text(json.dumps({"version": bad, "acks": rec}))
            led = acks.load_ledger(self.proj)
            self.assertFalse(led.ok, f"version={bad!r} 應被拒為壞帳本")
            self.assertEqual(led.by_key, {})

    def test_bad_entries_skipped_not_poisoning(self):
        tdir = self.proj / tombstone.TOMB_DIR
        tdir.mkdir()
        obj = {"version": acks.SCHEMA_VERSION, "acks": [
            {"kind": "damaged", "identity": "good", "fingerprint": "fs:1", "label": "good"},
            {"kind": "damaged"},                                    # 缺欄位
            "not-a-dict",                                           # 型別錯
            {"kind": "unknown-kind", "identity": "x", "fingerprint": "y"},  # 非法 kind
            {"kind": "damaged", "identity": 123, "fingerprint": "z"},        # identity 非字串
        ]}
        (tdir / acks.ACKS_FILE).write_text(json.dumps(obj))
        led = acks.load_ledger(self.proj)
        self.assertTrue(led.ok)
        self.assertEqual(set(led.by_key), {("damaged", "good", "fs:1")})    # 只留合法條目（三元組 key，g2）

    def test_reack_over_corrupt_replaces(self):
        tdir = self.proj / tombstone.TOMB_DIR
        tdir.mkdir()
        (tdir / acks.ACKS_FILE).write_bytes(b"{ garbage")
        res = acks.update_ledger(self.proj, add=[self.item])
        self.assertTrue(res.replaced_corrupt)
        led = acks.load_ledger(self.proj)
        self.assertTrue(led.ok and acks.is_acked(led, "damaged", "dmg", "fs:abc"))

    def test_acks_excluded_from_tombstone_decision_digest(self):
        # g1 Medium：acks.json 放 .tombstones/ 內、但**純呈現層**，不得進決策 digest（否則並發 ack 改 digest →
        # apply 對無關 session 誤判 skipped-changed＝ack 改了 apply 行為）。寫 acks 後 digest 須不變。
        tombstone.write_coverage(self.proj)                # 讓 digest 非空（含 _coverage.json）
        d0 = tombstone.tombstone_dir_digest(self.proj)
        acks.update_ledger(self.proj, add=[self.item])
        self.assertEqual(tombstone.tombstone_dir_digest(self.proj), d0)

    @_caps.needs_symlink
    def test_symlink_acks_file_not_trusted(self):
        # g1 High：`.tombstones` 是真夾但 `acks.json` **本身**是 symlink（指界外）→ 讀時不跟隨、不信任其內容。
        tdir = self.proj / tombstone.TOMB_DIR
        tdir.mkdir()
        outside = self.tmp / "planted.json"
        outside.write_text(json.dumps({"version": acks.SCHEMA_VERSION, "acks": [
            {"kind": "damaged", "identity": "dmg", "fingerprint": "fs:abc", "label": "dmg"}]}))
        (tdir / acks.ACKS_FILE).symlink_to(outside)
        led = acks.load_ledger(self.proj)
        self.assertEqual(led.by_key, {})               # symlink 帳本內容不被信任 → 不 suppress
        self.assertFalse(acks.is_acked(led, "damaged", "dmg", "fs:abc"))

    @_caps.needs_symlink
    def test_symlink_tombstones_dir_not_trusted_and_write_refused(self):
        # `.tombstones` 是 symlink（指界外）→ 讀不信任（回空、不 suppress）、寫拒絕（不寫界外）。
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.proj / tombstone.TOMB_DIR).symlink_to(outside, target_is_directory=True)
        self.assertEqual(acks.load_ledger(self.proj).by_key, {})   # 不信任 symlink .tombstones
        with self.assertRaises(acks.UnsafeAcksDir):
            acks.update_ledger(self.proj, add=[self.item])


class TestPresentationFilter(unittest.TestCase):
    """呈現層：format_plan 隱藏 acked、加摘要；ack 不改分類。"""
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.proj = self.tmp / "hub" / "projA"
        self.proj.mkdir(parents=True)

    def tearDown(self):
        self._td.cleanup()

    def test_unacked_shown_acked_hidden(self):
        plan = _plan(self.proj, _coll("ABC", "abc"))
        # 未 ack：顯示
        out0 = scan.format_plan(plan, acks.compute_ack_view(plan))
        self.assertIn("blocked-casefold-collision", out0)
        # ack 後：隱藏 + 摘要
        acks.update_ledger(self.proj, add=acks.ackable_from_plan(plan))
        view = acks.compute_ack_view(plan)
        self.assertEqual(view.hidden.get("projA"), {"ABC", "abc"})
        out1 = scan.format_plan(plan, view)
        self.assertNotIn("blocked-casefold-collision", out1)
        self.assertIn("已 acknowledged", out1)   # 隱藏行數由 format_plan 自行計數（g2：AckView 不再帶 acked_count）

    def test_ack_does_not_change_classification(self):
        # 最重要不變量：acked 後 plan 的 action 仍為 blocked（ack 只影響 format_plan、不進 classify/apply）。
        plan = _plan(self.proj, _coll("ABC", "abc"))
        acks.update_ledger(self.proj, add=acks.ackable_from_plan(plan))
        _ = acks.compute_ack_view(plan)
        self.assertTrue(all(s.action == "blocked-casefold-collision" for s in plan.projects[0].sessions))

    def test_stale_fingerprint_not_hidden(self):
        plan = _plan(self.proj, _coll("ABC", "abc"))
        acks.update_ledger(self.proj, add=acks.ackable_from_plan(plan))
        # 撞名集合變（第三個拼法）→ 指紋不符 → 不再隱藏、照常重報
        plan2 = _plan(self.proj, _coll("ABC", "abc", "Abc"))
        view = acks.compute_ack_view(plan2)
        self.assertEqual(view.hidden.get("projA", set()), set())
        self.assertIn("blocked-casefold-collision", scan.format_plan(plan2, view))

    def test_corrupt_ledger_surfaced_and_not_hiding(self):
        plan = _plan(self.proj, _coll("ABC", "abc"))
        acks.update_ledger(self.proj, add=acks.ackable_from_plan(plan))
        # 弄壞帳本 → 不 suppress + corrupt_projects 標記
        (self.proj / tombstone.TOMB_DIR / acks.ACKS_FILE).write_bytes(b"{ broken")
        view = acks.compute_ack_view(plan)
        self.assertEqual(view.hidden.get("projA", set()), set())
        self.assertIn("projA", view.corrupt_projects)

    def test_damaged_suppression_end_to_end(self):
        # 真檔 damaged（可攜）：ackable→ack→隱藏。
        (self.proj / "dmg.jsonl").write_bytes(b"{ broken json\n")
        local = self.tmp / "local" / "projA"
        local.mkdir(parents=True)
        plan = _plan(self.proj, [SessionPlan("dmg", "blocked-damaged-source", None, "壞檔")], local_dir=local)
        acks.update_ledger(self.proj, add=acks.ackable_from_plan(plan))
        out = scan.format_plan(plan, acks.compute_ack_view(plan))
        self.assertNotIn("blocked-damaged-source", out)
        self.assertIn("已 acknowledged", out)

    def test_same_hub_two_local_views_partial_ack_not_masked(self):
        # g2 High：同一 hub 專案被兩個 local 夾映射 → 同 sid、內容不同（fp 不同）的兩個 damaged 項共用 (pk,sid)。
        # 只 ack 其一，**不得**把另一個未 ack 的也藏掉（fail-safe：涵蓋該 sid 的所有項都 ack 才藏）。
        l1, l2 = self.tmp / "l1", self.tmp / "l2"
        l1.mkdir()
        l2.mkdir()
        (l1 / "dmg.jsonl").write_bytes(b"{ view1 broken\n")
        (l2 / "dmg.jsonl").write_bytes(b"{ view2 broken different\n")
        mk = lambda ld: ProjectPlan(local_dir=str(ld), hub_dir=str(self.proj), identity="match",
                                    coverage_initialized=True,
                                    sessions=[SessionPlan("dmg", "blocked-damaged-source", None, "壞檔")])
        plan = SyncPlan(False, [], [mk(l1), mk(l2)])
        items = acks.ackable_from_plan(plan)
        self.assertEqual(len(items), 2)
        self.assertNotEqual(items[0].fingerprint, items[1].fingerprint)   # 內容不同 → fp 不同
        acks.update_ledger(self.proj, add=[items[0]])                     # 只 ack 第一個 view
        view = acks.compute_ack_view(plan)
        self.assertEqual(view.hidden.get("projA", set()), set())          # 不藏（另一 fp 未 ack）
        self.assertEqual(scan.format_plan(plan, view).count("blocked-damaged-source"), 2)
        acks.update_ledger(self.proj, add=[items[1]])                     # 兩 fp 都 ack
        view2 = acks.compute_ack_view(plan)
        self.assertEqual(view2.hidden.get("projA"), {"dmg"})              # 全 ack → 藏
        self.assertNotIn("blocked-damaged-source", scan.format_plan(plan, view2))

    def test_ack_does_not_hide_nonackable_same_sid_other_view(self):
        # g3 High：同 hub 兩 local view，sid S 在 view A 是 damaged（已 ack）、view B 是**非 ackable** 的 copy-to-hub。
        # ack 不得把 view B 的 copy-to-hub 行也藏（否則漏顯示待寫入）——format_plan 需 action 護欄。
        l1, l2 = self.tmp / "l1", self.tmp / "l2"
        l1.mkdir()
        l2.mkdir()
        (l1 / "S.jsonl").write_bytes(b"{ broken\n")
        ppA = ProjectPlan(local_dir=str(l1), hub_dir=str(self.proj), identity="match",
                          coverage_initialized=True,
                          sessions=[SessionPlan("S", "blocked-damaged-source", None, "壞檔")])
        ppB = ProjectPlan(local_dir=str(l2), hub_dir=str(self.proj), identity="match",
                          coverage_initialized=True,
                          sessions=[SessionPlan("S", "copy-to-hub", "local->other", "單邊新檔")])
        plan = SyncPlan(False, [], [ppA, ppB])
        acks.update_ledger(self.proj, add=acks.ackable_from_plan(plan))   # 只有 damaged S 是 ackable
        out = scan.format_plan(plan, acks.compute_ack_view(plan))
        self.assertNotIn("blocked-damaged-source", out)   # view A 的 damaged 隱藏
        self.assertIn("copy-to-hub", out)                 # view B 的 copy-to-hub 仍顯示（未被誤藏）

    def test_ackable_actions_single_source(self):
        # 單一真相源在 scan.ACKABLE_ACTIONS；acks re-export 且 _ACTION_KIND 鍵與之一致（漂移守衛）。
        self.assertEqual(acks.ACKABLE_ACTIONS, scan.ACKABLE_ACTIONS)
        self.assertEqual(frozenset(acks._ACTION_KIND), scan.ACKABLE_ACTIONS)

    def test_format_report_respects_passed_ack_view(self):
        # g5 High：format_report 只依**傳入的** ack_view 過濾（cli 在 apply 後以 fresh plan 重算，不用 stale T0 視圖）。
        # 傳「未含該 sid」的視圖 → 顯示（即使該 sid 曾在別的時點被 ack）；傳「含該 sid」→ 隱藏。
        report = apply_mod.ApplyReport(outcomes=[
            apply_mod.ApplyOutcome("S", "blocked-damaged-source", "reported", "壞", project="projA")])
        self.assertIn("blocked-damaged-source", apply_mod.format_report(report, acks.AckView()))
        self.assertNotIn("blocked-damaged-source",
                         apply_mod.format_report(report, acks.AckView(hidden={"projA": {"S"}})))

    @_caps.needs_unreadable_dir
    def test_same_hub_unreadable_view_not_masked_by_readable_ack(self):
        # g6 High：View A dmg 可讀+已 ack、View B 同 sid dmg 不可讀（不可綁定）→ 整個 (pk,sid) fail-closed 不隱藏
        # （不可綁定行不因另一 view 的 ack 被誤藏；all-covering-acked 含 fp=None 項→該項 is_acked False→不藏）。
        lA, lB = self.tmp / "lA", self.tmp / "lB"
        lA.mkdir()
        lB.mkdir()
        (lA / "dmg.jsonl").write_bytes(b"{ broken A\n")
        fb = lB / "dmg.jsonl"
        fb.write_bytes(b"{ broken B\n")
        os.chmod(fb, 0)
        try:
            mk = lambda ld: ProjectPlan(local_dir=str(ld), hub_dir=str(self.proj), identity="match",
                                        coverage_initialized=True,
                                        sessions=[SessionPlan("dmg", "blocked-damaged-source", None, "壞")])
            plan = SyncPlan(False, [], [mk(lA), mk(lB)])
            bindable = [it for it in acks.ackable_from_plan(plan) if it.fingerprint is not None]
            acks.update_ledger(self.proj, add=bindable)                      # ack View A（可綁定那個）
            self.assertEqual(acks.compute_ack_view(plan).hidden.get("projA", set()), set())  # 不藏（View B 不可綁定）
        finally:
            os.chmod(fb, stat.S_IRWXU)

    def test_ackable_actions_never_in_auto_set(self):
        # 結構保證：所有 ackable 的 blocked action 都不在 apply 自動集 → ack 不可能造成 auto-apply（R1 確認的最強不變量）。
        for act in ("blocked-casefold-collision", "blocked-damaged-source", "damaged", "identity-collision"):
            self.assertNotIn(act, apply_mod.AUTO_ACTIONS)
            self.assertNotIn(act, apply_mod.MEM_AUTO_ACTIONS)

    def test_apply_report_hiding_is_project_scoped(self):
        # g1 Low：跨專案同 sid（A acked、B 未 ack）→ B 的 reported 行**不得**被 A 的 ack 誤藏（project-scoped、非 flatten）。
        S = "dup-sid-xyz"
        report = apply_mod.ApplyReport(outcomes=[
            apply_mod.ApplyOutcome(S, "blocked-damaged-source", "reported", "壞", project="projA"),
            apply_mod.ApplyOutcome(S, "blocked-damaged-source", "reported", "壞", project="projB"),
        ])
        view = acks.AckView(hidden={"projA": {S}})   # 只 ack projA
        out = apply_mod.format_report(report, view)
        self.assertEqual(out.count("[reported]"), 1)   # projA 隱藏、projB 仍在（未被跨專案 flatten 誤藏）
        self.assertIn("已 acknowledged", out)


@_caps.needs_case_sensitive_fs
class TestDoctorDiagnoseAck(unittest.TestCase):
    """diagnose 對撞名**誠實不 under-report**（R1 High#2）：只顯示 ack 記錄數，不據此降級（它看不到 local 端、
    無法安全驗證 merged 撞名的 ack 是否仍成立）。需 case-sensitive FS 才能有兩個 collide 檔。"""
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.local = self.tmp / "local"
        self.hub = self.tmp / "hub"
        self.local.mkdir()
        self.hub.mkdir()
        self.state = self.tmp / "state.json"
        proj = self.hub / "projA"
        proj.mkdir()
        fx.write_jsonl(fx.linear(), str(proj / "ABC.jsonl"))
        fx.write_jsonl(fx.linear(), str(proj / "abc.jsonl"))
        self.proj = proj

    def tearDown(self):
        self._td.cleanup()

    def test_diagnose_stays_honest_but_shows_ack_record(self):
        rep = doctor.diagnose(self.local, self.hub, self.state)
        self.assertIn("casefold 撞名", rep.text())
        before = rep.problems
        acks.update_ledger(self.proj, add=[acks.AckItem(
            "projA", str(self.proj), "casefold-collision", "abc",
            acks.fingerprint_collision(["ABC", "abc"]), ("ABC", "abc"), "ABC/abc")])
        rep2 = doctor.diagnose(self.local, self.hub, self.state)
        self.assertEqual(rep2.problems, before)       # 仍計為問題（不 under-report merged 撞名）
        self.assertIn("筆 ack", rep2.text())           # 但 surface ack 記錄

    def test_diagnose_warns_on_corrupt_ledger(self):
        (self.proj / tombstone.TOMB_DIR).mkdir()
        (self.proj / tombstone.TOMB_DIR / acks.ACKS_FILE).write_bytes(b"{ broken")
        rep = doctor.diagnose(self.local, self.hub, self.state)
        self.assertIn("acks.json 損壞", rep.text())


class TestDoctorAckCliRoundtrip(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.local = self.tmp / "local"
        self.hub = self.tmp / "hub"
        self.local.mkdir()
        self.hub.mkdir()
        self.state = self.tmp / "state.json"

    def tearDown(self):
        self._td.cleanup()

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _common(self):
        return ["--hub", str(self.hub), "--local-root", str(self.local), "--state", str(self.state)]

    def test_show_acked_empty(self):
        code, out, _ = self._run(["doctor", "--show-acked", *self._common()])
        self.assertEqual(code, 0)
        self.assertIn("沒有任何 acknowledged", out)

    def test_ack_all_nothing_to_ack(self):
        code, out, _ = self._run(["doctor", "--ack-all", *self._common()])
        self.assertEqual(code, 0)
        self.assertIn("沒有可 acknowledge", out)

    @_caps.needs_case_sensitive_fs
    def test_collision_full_roundtrip(self):
        proj = self.hub / "projA"
        proj.mkdir()
        fx.write_jsonl(fx.linear(), str(proj / "ABC.jsonl"))
        fx.write_jsonl(fx.linear(), str(proj / "abc.jsonl"))
        c = self._common()
        # status 顯示撞名
        _, out, _ = self._run(["status", *c])
        self.assertIn("blocked-casefold-collision", out)
        # ack 預覽（不寫）
        code, out, _ = self._run(["doctor", "--ack-all", *c])
        self.assertEqual(code, 0)
        self.assertIn("新增", out)
        self.assertIn("預覽", out)
        # 仍未寫 → status 照常顯示
        _, out, _ = self._run(["status", *c])
        self.assertIn("blocked-casefold-collision", out)
        # ack 落地
        code, out, _ = self._run(["doctor", "--ack-all", "--yes", *c])
        self.assertEqual(code, 0)
        self.assertIn("已 acknowledge", out)
        # status 隱藏 + 摘要
        _, out, _ = self._run(["status", *c])
        self.assertNotIn("blocked-casefold-collision", out)
        self.assertIn("已 acknowledged", out)
        # show-acked
        code, out, _ = self._run(["doctor", "--show-acked", *c])
        self.assertEqual(code, 0)
        self.assertIn("casefold-collision", out)
        # unack 落地 → 重新顯示
        code, out, _ = self._run(["doctor", "--unack-all", "--yes", *c])
        self.assertEqual(code, 0)
        self.assertIn("已取消", out)
        _, out, _ = self._run(["status", *c])
        self.assertIn("blocked-casefold-collision", out)

    def test_damaged_ack_wiring_portable(self):
        # 可攜（不需 case-sensitive FS）：matched 專案 + 單邊 damaged 檔，驗 doctor --ack-all → status 隱藏整條 CLI 接線。
        proj_l = self.local / "projA"
        proj_l.mkdir()
        proj_h = self.hub / "projA"
        proj_h.mkdir()
        sess = [fx.umsg("u1", None, "user", 1, cwd="/work/A")]
        fx.write_jsonl(sess, str(proj_l / "s1.jsonl"))
        fx.write_jsonl(sess, str(proj_h / "s1.jsonl"))
        (proj_l / "dmg.jsonl").write_bytes(b"{ broken json\n")
        tombstone.write_coverage(proj_h)
        state_mod.save(State(bindings={"/work/A": "projA"}, known_sessions={"projA": {"s1"}},
                             local_sessions={"projA": {"s1"}}), self.state)
        c = self._common()
        _, out, _ = self._run(["status", *c])
        self.assertIn("blocked-damaged-source", out)
        code, out, _ = self._run(["doctor", "--ack-all", "--yes", *c])
        self.assertEqual(code, 0)
        self.assertIn("已 acknowledge", out)
        _, out, _ = self._run(["status", *c])
        self.assertNotIn("blocked-damaged-source", out)
        self.assertIn("已 acknowledged", out)
        # --project 過濾到不存在的專案 → 無可 ack
        _, out, _ = self._run(["doctor", "--ack-all", "--project", "nope", *c])
        self.assertIn("沒有可 acknowledge", out)

    def test_apply_hides_acked_and_never_writes_it(self):
        # R1 Medium + Low：真 `sync --apply` 也隱藏 acked damaged（dry-run plan 與 apply report 皆隱藏），
        # 且 A3——damaged 檔原地不動、絕不被寫/搬。
        proj_l = self.local / "projA"
        proj_l.mkdir()
        proj_h = self.hub / "projA"
        proj_h.mkdir()
        sess = [fx.umsg("u1", None, "user", 1, cwd="/work/A")]
        fx.write_jsonl(sess, str(proj_l / "s1.jsonl"))
        fx.write_jsonl(sess, str(proj_h / "s1.jsonl"))
        dmg = proj_l / "dmg.jsonl"
        dmg.write_bytes(b"{ broken json\n")
        before = dmg.read_bytes()
        tombstone.write_coverage(proj_h)
        state_mod.save(State(bindings={"/work/A": "projA"}, known_sessions={"projA": {"s1"}},
                             local_sessions={"projA": {"s1"}}), self.state)
        c = self._common()
        # 未 ack：apply report 會列出 damaged
        _, out, _ = self._run(["sync", "--apply", *c])
        self.assertIn("blocked-damaged-source", out)
        # ack 後：dry-run plan 與 apply report 都不再列出，且 damaged 檔未被動
        self._run(["doctor", "--ack-all", "--yes", *c])
        code, out, _ = self._run(["sync", "--apply", *c])
        self.assertEqual(code, 0)
        self.assertNotIn("blocked-damaged-source", out)
        self.assertIn("已 acknowledged", out)
        self.assertEqual(dmg.read_bytes(), before)     # A3：原地不動


if __name__ == "__main__":
    unittest.main()
