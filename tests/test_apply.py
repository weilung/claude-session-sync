import hashlib
import os
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

from claude_session_sync import anomaly, apply as apply_mod, atomicio, scan, state as state_mod, tombstone
from claude_session_sync.config import Config
from claude_session_sync.scan import SessionPlan
from claude_session_sync.snapshot import compute_decision_snapshot
from tests import _caps, fixtures as fx


def _name_match(local_dir, hub_dirs):
    for hd in hub_dirs:
        if hd.name == local_dir.name:
            return ("match", hd)
    return ("needs-map", None)


class TestApply(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.local = self.tmp / "local"
        self.hub = self.tmp / "hub"
        self.lA = self.local / "projA"
        self.hA = self.hub / "projA"
        self.lA.mkdir(parents=True)
        self.hA.mkdir(parents=True)
        self.state = self.tmp / "state.json"
        self.cfg = Config(own_hub=str(self.hub))
        # 已 bootstrap 的情境：磁碟 state 含本機對 projA 的 hub+local 基線（has_baseline / has_local_baseline=True）。
        state_mod.save(state_mod.State(known_sessions={"projA": set()},
                                       local_sessions={"projA": set()}), self.state)

    def tearDown(self):
        self._td.cleanup()

    def _w(self, path, objs):
        fx.write_jsonl(objs, str(path))

    def _plan(self, *, coverage=True):
        if coverage:
            tombstone.write_coverage(self.hA)
        return scan.build_plan(self.local, self.hub, None, identity_fn=_name_match)

    def _apply(self, plan, state=None):
        # 預設讀磁碟 state（setUp 寫的 projA 基線），使 plan-time 與鎖內重載一致（codex r18）。
        st = state if state is not None else state_mod.load_or_none(self.state)
        return apply_mod.apply_plan(
            plan, local_root=self.local, hub_root=self.hub, config=self.cfg,
            state=st, state_path=str(self.state),
        )

    def _result(self, report, sid):
        return next(o for o in report.outcomes if o.session_id == sid)

    # ── 自動套用 happy paths ────────────────────────────────────────────────

    def test_ff_local_to_hub_writes_hub(self):
        self._w(self.lA / "s1.jsonl", fx.fast_forward_of_linear())  # local 較新
        self._w(self.hA / "s1.jsonl", fx.linear())
        report = self._apply(self._plan())
        self.assertEqual(self._result(report, "s1").result, "applied-ff-hub")
        self.assertEqual((self.hA / "s1.jsonl").read_bytes(), (self.lA / "s1.jsonl").read_bytes())
        # state 記下 known
        self.assertIn("s1", state_mod.load_or_none(self.state).known_sessions["projA"])

    @_caps.needs_symlink
    def test_apply_blocks_escaping_local_dir_toctou(self):
        # e2e gate G-High：plan 時 local projA 安全（s1=copy-to-hub），apply 前被換成逃逸 symlink（指向界外含別的 s1）
        # → apply 寫入邊界重驗 → blocked-unsafe，不從界外讀 s1 寫進 hub。
        self._w(self.lA / "s1.jsonl", fx.linear())    # local-only s1 → copy-to-hub
        plan = self._plan()
        # 換成逃逸 symlink：移除實體 projA，改指向界外夾（界外有「私密」s1）
        import shutil
        shutil.rmtree(self.lA)
        outside = self.tmp / "outside"
        outside.mkdir()
        self._w(outside / "s1.jsonl", fx.fast_forward_of_linear())   # 界外私密 s1
        self.lA.symlink_to(outside, target_is_directory=True)
        report = self._apply(plan)
        self.assertEqual(self._result(report, "s1").result, "blocked-unsafe")
        self.assertFalse((self.hA / "s1.jsonl").exists())            # 界外 s1 未寫進 hub

    @_caps.needs_symlink
    def test_apply_blocks_leaf_symlink_session_toctou(self):
        # e2e gate3 #1：plan 時 s1 是正常檔（copy-to-hub），apply 前被換成指向界外的 symlink → apply 於 reclassify/
        # snapshot **前**的 leaf 檢查擋下 → skipped-changed，不讀/copy 界外內容進 hub。
        self._w(self.lA / "s1.jsonl", fx.linear())   # local-only → copy-to-hub
        plan = self._plan()
        (self.lA / "s1.jsonl").unlink()
        outside = self.tmp / "secret.jsonl"
        self._w(outside, fx.linear())
        (self.lA / "s1.jsonl").symlink_to(outside)
        report = self._apply(plan)
        self.assertEqual(self._result(report, "s1").result, "skipped-changed")
        self.assertFalse((self.hA / "s1.jsonl").exists())   # 界外內容未寫進 hub

    @_caps.needs_symlink
    def test_reparse_safe_symlink_names_cf_normalizes(self):
        # e2e gate7/gate8：alias 偵測核心——symlink 名以 `_name_key`（NFC+casefold）入集，故 casefold-alias `A.md`
        # 與 normalization-alias（NFD café）都能對到 tracked 的正規化鍵；一般檔不入集；缺夾 → 空集。
        d = self.tmp / "probe"; d.mkdir()
        outside = self.tmp / "t.md"; outside.write_text("x", encoding="utf-8")
        (d / "A.md").symlink_to(outside)                            # casefold-alias
        (d / unicodedata.normalize("NFD", "café.md")).symlink_to(outside)   # normalization-alias（NFD leaf）
        (d / "real.md").write_text("x", encoding="utf-8")           # 一般檔不入集
        names = apply_mod._reparse_safe_symlink_names_cf(d)          # reparse-aware wrapper（委派 scan._symlink_name_keys）
        self.assertIn(scan._name_key("a.md"), names)                # A.md → 正規化鍵
        self.assertIn(scan._name_key(unicodedata.normalize("NFC", "café.md")), names)  # NFD leaf 對到 NFC 查詢鍵
        self.assertNotIn(scan._name_key("real.md"), names)
        self.assertEqual(apply_mod._reparse_safe_symlink_names_cf(self.tmp / "nope"), set())  # 缺夾 → 空集

    def test_ff_hub_to_local_keeps_both_never_overwrites(self):
        self._w(self.lA / "s1.jsonl", fx.linear())                  # local 較舊
        self._w(self.hA / "s1.jsonl", fx.fast_forward_of_linear())  # hub 較新
        before = (self.lA / "s1.jsonl").read_bytes()
        report = self._apply(self._plan())
        self.assertEqual(self._result(report, "s1").result, "kept-both-local")
        self.assertEqual((self.lA / "s1.jsonl").read_bytes(), before)  # C3：local 原檔絕不被覆蓋
        kept = [p for p in self.lA.glob("*.jsonl") if p.name != "s1.jsonl"]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), (self.hA / "s1.jsonl").read_bytes())

    def test_copy_to_hub(self):
        self._w(self.lA / "only_local.jsonl", fx.linear())
        report = self._apply(self._plan())
        self.assertEqual(self._result(report, "only_local").result, "copied-to-hub")
        self.assertTrue((self.hA / "only_local.jsonl").exists())

    def test_copy_to_local(self):
        self._w(self.hA / "only_hub.jsonl", fx.linear())
        report = self._apply(self._plan())
        self.assertEqual(self._result(report, "only_hub").result, "copied-to-local")
        self.assertTrue((self.lA / "only_hub.jsonl").exists())

    def test_identical_no_write(self):
        self._w(self.lA / "s1.jsonl", fx.linear())
        self._w(self.hA / "s1.jsonl", fx.linear())
        report = self._apply(self._plan())
        self.assertEqual(self._result(report, "s1").result, "identical")

    def test_identical_commits_known(self):
        # codex r17：identical 也要記 known，否則該 session 日後在 hub 被刪時 known-deleted 閘抓不到。
        self._w(self.lA / "s1.jsonl", fx.linear())
        self._w(self.hA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        report = self._apply(self._plan())
        self.assertEqual(self._result(report, "s1").result, "identical")
        self.assertIn("s1", state_mod.load_or_none(self.state).known_sessions.get("projA", set()))

    def test_fork_reported_not_written(self):
        self._w(self.lA / "s1.jsonl", fx.fork_of_linear())
        self._w(self.hA / "s1.jsonl", fx.linear())
        hub_before = (self.hA / "s1.jsonl").read_bytes()
        report = self._apply(self._plan())
        self.assertEqual(self._result(report, "s1").result, "reported")
        self.assertEqual((self.hA / "s1.jsonl").read_bytes(), hub_before)

    def test_uninitialized_single_side_blocked_not_copied(self):
        self._w(self.lA / "only_local.jsonl", fx.linear())
        report = self._apply(self._plan(coverage=False))  # 未 bootstrap
        self.assertEqual(self._result(report, "only_local").result, "reported")
        self.assertFalse((self.hA / "only_local.jsonl").exists())

    def test_uninitialized_paired_ff_also_blocked(self):
        # codex r11-1：未 bootstrap 的專案，連 paired ff 都不可自動寫（信任邊界）。
        self._w(self.lA / "s1.jsonl", fx.fast_forward_of_linear())
        self._w(self.hA / "s1.jsonl", fx.linear())
        hub_before = (self.hA / "s1.jsonl").read_bytes()
        report = self._apply(self._plan(coverage=False))
        self.assertEqual(self._result(report, "s1").result, "blocked-uninitialized")
        self.assertEqual((self.hA / "s1.jsonl").read_bytes(), hub_before)  # hub 未被寫

    def test_corrupt_tombstone_blocks_copy(self):
        # codex r11-3：壞掉的 tombstone 必須 fail-closed（不可當「沒標記」而復活單邊檔）。
        self._w(self.lA / "secret.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        (tombstone.tombstones_dir(self.hA) / "secret.deleted.json").write_text("{ broken", encoding="utf-8")
        report = self._apply(self._plan(coverage=True))
        self.assertEqual(self._result(report, "secret").action, "blocked-tombstone-corrupt")
        self.assertFalse((self.hA / "secret.jsonl").exists())  # 關鍵：未復活

    def test_paired_ff_with_tombstone_not_resurrected(self):
        # codex r14-1：tombstoned session 即便兩側都在、且 local 是 hub 的 ff，也不可被當 ff 寫回 hub（復活）。
        # P1c 條件式：兩側內容分歧（local≠hub）→ 不可能都 ==base → conflict-delete-vs-update（仍不寫回 hub）。
        self._w(self.lA / "s1.jsonl", fx.fast_forward_of_linear())
        self._w(self.hA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        tombstone.write_session_tombstone(self.hA, "s1", base_hash="h")
        hub_before = (self.hA / "s1.jsonl").read_bytes()
        plan = scan.build_plan(self.local, self.hub, None, identity_fn=_name_match)
        report = apply_mod.apply_plan(plan, local_root=self.local, hub_root=self.hub,
                                      config=self.cfg, state=state_mod.State(), state_path=str(self.state))
        r = self._result(report, "s1")
        self.assertEqual(r.action, "conflict-delete-vs-update")
        self.assertEqual(r.result, "reported")                            # 非自動套用、未寫
        self.assertEqual((self.hA / "s1.jsonl").read_bytes(), hub_before)  # hub 未被 ff 覆蓋（不復活）

    def test_midflight_tombstone_conflict_surfaced(self):
        # codex r22：plan 後、鎖內 reclass 前才出現 tombstone（base≠local）→ _apply_session 重分類為
        # conflict-delete-vs-update，須**誠實 surface 該動作**（非泛用 suppressed），且不寫 hub。
        self._w(self.lA / "s1.jsonl", fx.linear())   # local-only → 計畫 copy-to-hub
        tombstone.write_coverage(self.hA)
        st = state_mod.State(known_sessions={"projA": set()})
        plan = scan.build_plan(self.local, self.hub, st, identity_fn=_name_match)

        orig = atomicio.FileLock.acquire_blocking
        box = {"done": False}

        def inject(self, **kw):  # 取鎖瞬間注入 tombstone（模擬 plan 後刪除標記出現）
            lock = orig(self, **kw)
            if not box["done"]:
                box["done"] = True
                tombstone.write_session_tombstone(self.lock_path.parent, "s1", base_hash="nope")
            return lock

        with mock.patch.object(atomicio.FileLock, "acquire_blocking", inject):
            report = self._apply(plan, state=st)
        r = self._result(report, "s1")
        self.assertEqual(r.action, "conflict-delete-vs-update")
        self.assertEqual(r.result, "reported")
        self.assertFalse((self.hA / "s1.jsonl").exists())  # 未復活/未寫 hub

    def test_lock_oserror_reported_not_crash(self):
        # codex g11：取鎖底層是 os.open(O_CREAT|O_EXCL)。碟片被拔/唯讀/權限不足 → 丟**純 OSError**（非
        # LockError），而取鎖在 try 之外 → 原本會 traceback 中止整個 sync。hub 常是可移除式 USB/網路碟 →
        # 必須逐檔回報 error（該檔失敗、其餘照跑、CLI 誠實非零），不可炸掉整份報告。
        self._w(self.lA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        st = state_mod.State(known_sessions={"projA": set()})
        plan = scan.build_plan(self.local, self.hub, st, identity_fn=_name_match)

        def boom(self, **kw):
            raise PermissionError(13, "The device is not ready")   # 模擬 USB 被拔/唯讀

        with mock.patch.object(atomicio.FileLock, "acquire_blocking", boom):
            report = self._apply(plan, state=st)                   # 不可 raise
        r = self._result(report, "s1")
        self.assertEqual(r.result, "error")
        self.assertIn("取鎖失敗", r.detail)
        self.assertTrue(report.had_error)                          # CLI 會回非零
        self.assertFalse((self.hA / "s1.jsonl").exists())          # 未寫入任何東西

    def test_paired_identical_to_base_suppressed_by_tombstone(self):
        # 兩側 byte-identical 且 == base → suppressed-deleted（不復活、不寫）。
        self._w(self.lA / "s1.jsonl", fx.linear())
        self._w(self.hA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        base = tombstone.raw_file_digest(self.hA / "s1.jsonl")
        tombstone.write_session_tombstone(self.hA, "s1", base_hash=base)
        plan = scan.build_plan(self.local, self.hub, None, identity_fn=_name_match)
        report = apply_mod.apply_plan(plan, local_root=self.local, hub_root=self.hub,
                                      config=self.cfg, state=state_mod.State(), state_path=str(self.state))
        self.assertEqual(self._result(report, "s1").action, "suppressed-deleted")

    def test_known_hub_deletion_below_threshold_not_resurrected(self):
        # codex r16：已知 session 在 hub 消失（單一、低於大量消失門檻、無 tombstone）→ 不可從 local 複製回去。
        self._w(self.lA / "s1.jsonl", fx.linear())   # local 還在
        tombstone.write_coverage(self.hA)             # hub 無 s1.jsonl（已被刪）
        st = state_mod.State(known_sessions={"projA": {"s1"}})  # state 知道 s1 曾在 hub
        plan = scan.build_plan(self.local, self.hub, st, identity_fn=_name_match)
        report = self._apply(plan, state=st)
        self.assertEqual(self._result(report, "s1").action, "blocked-known-deleted")
        self.assertFalse((self.hA / "s1.jsonl").exists())  # 未復活到 hub

    def test_copy_blocked_without_local_baseline(self):
        # codex r18：hub 已 cov（可能他機 bootstrap）但本機 state 無此專案基線 → 單邊 copy 不可進行。
        self._w(self.lA / "s1.jsonl", fx.linear())   # local-only
        tombstone.write_coverage(self.hA)             # cov true
        st = state_mod.State(known_sessions={"projB": set()})  # 本機只 bootstrap 過 projB，無 projA
        plan = scan.build_plan(self.local, self.hub, st, identity_fn=_name_match)
        report = self._apply(plan, state=st)
        self.assertEqual(self._result(report, "s1").action, "blocked-no-baseline")
        self.assertFalse((self.hA / "s1.jsonl").exists())

    def test_genuinely_new_local_still_copies(self):
        # 對照：未知的 local-only 新檔仍正常 copy-to-hub（不被刪除偵測誤擋）。
        self._w(self.lA / "fresh.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        st = state_mod.State(known_sessions={"projA": set()})  # fresh 不在 known
        plan = scan.build_plan(self.local, self.hub, st, identity_fn=_name_match)
        report = self._apply(plan, state=st)
        self.assertEqual(self._result(report, "fresh").result, "copied-to-hub")
        self.assertTrue((self.hA / "fresh.jsonl").exists())

    # ── local-presence 對稱刪除（P1c）───────────────────────────────────────

    def _save_state(self, known, local):
        st = state_mod.State(known_sessions={"projA": set(known)},
                             local_sessions={"projA": set(local)})
        state_mod.save(st, self.state)
        return st

    def test_local_deletion_writes_tombstone_not_resurrect(self):
        # 本機刪除 local s1（hub 仍在）→ 寫 hub tombstone（base=hub 現況）、**不刪 hub**、re-glob 後移出 local。
        self._w(self.hA / "s1.jsonl", fx.linear())   # hub 有；local 無（已刪）
        tombstone.write_coverage(self.hA)
        st = self._save_state({"s1"}, {"s1"})
        plan = scan.build_plan(self.local, self.hub, st, identity_fn=_name_match)
        report = self._apply(plan, state=st)
        o = self._result(report, "s1")
        self.assertEqual(o.action, "local-deleted")
        self.assertEqual(o.result, "tombstoned-local-deletion")
        self.assertTrue((self.hA / "s1.jsonl").exists())                       # A3：hub 歸檔保留
        tomb = tombstone.find_session_tombstone(self.hA, "s1")
        self.assertIsNotNone(tomb)
        self.assertEqual(tomb.base_hash, tombstone.raw_file_digest(self.hA / "s1.jsonl"))
        self.assertNotIn("s1", state_mod.load_or_none(self.state).local_sessions.get("projA", set()))

    @_caps.needs_case_sensitive_fs
    @_caps.needs_symlink
    def test_local_deleted_casefold_symlink_alias_not_tombstoned(self):
        # e2e gate7 finding2（session）：case-sensitive FS 上 tracked `abc` 的 local 檔被換成 casefold-alias symlink
        # `ABC.jsonl` → `_session_files` 略過、casefold 碰撞偵測只看**列出**名字亦漏 → 舊 exact-name guard 放行寫
        # tombstone。改 casefold 後須擋：skipped-changed、不寫 tombstone、hub 保留、abc 留 local 基線（fail-closed）。
        self._w(self.hA / "abc.jsonl", fx.linear())              # hub 真檔
        tombstone.write_coverage(self.hA)
        outside = self.tmp / "outside.jsonl"
        self._w(outside, fx.linear())
        (self.lA / "ABC.jsonl").symlink_to(outside)              # local casefold-alias symlink（大寫）
        st = self._save_state({"abc"}, {"abc"})
        plan = scan.build_plan(self.local, self.hub, st, identity_fn=_name_match)
        report = self._apply(plan, state=st)
        o = self._result(report, "abc")
        self.assertEqual(o.action, "local-deleted")
        self.assertEqual(o.result, "skipped-changed")
        self.assertIsNone(tombstone.find_session_tombstone(self.hA, "abc"))   # 無抑制 tombstone
        self.assertTrue((self.hA / "abc.jsonl").exists())                    # A3：hub 保留
        self.assertIn("abc", state_mod.load_or_none(self.state).local_sessions.get("projA", set()))

    @_caps.needs_unreadable_dir
    def test_unreadable_local_dir_no_tombstone(self):
        # e2e gate9 finding2：local 專案夾不可讀（權限）→ `_session_files` 的 glob **fail-open** 回空 → 單一 known
        # session 會被誤判 local-deleted。apply 的 `scan._dir_scannable` guard 須擋：skipped-unreadable、不寫 tombstone、
        # s1 留 local 基線（fail-closed，不把不可讀誤當刪除）。POSIX-only（Windows chmod 對夾無此效果 → skip）。
        self._w(self.hA / "s1.jsonl", fx.linear())
        self._w(self.lA / "s1.jsonl", fx.linear())          # local 有 s1（tracked）
        tombstone.write_coverage(self.hA)
        st = self._save_state({"s1"}, {"s1"})
        os.chmod(self.lA, 0)
        try:
            plan = scan.build_plan(self.local, self.hub, st, identity_fn=_name_match)
            report = self._apply(plan, state=st)
        finally:
            os.chmod(self.lA, 0o700)                         # 還原以便 tearDown 清理
        o = self._result(report, "s1")
        self.assertEqual(o.result, "skipped-unreadable")
        self.assertIsNone(tombstone.find_session_tombstone(self.hA, "s1"))   # 無抑制 tombstone
        self.assertIn("s1", state_mod.load_or_none(self.state).local_sessions.get("projA", set()))

    @_caps.needs_unreadable_dir
    def test_unreadable_hub_dir_no_write(self):
        # e2e gate10：hub 專案夾 write+execute 但不可讀（0o333）→ `_symlink_name_keys` 回空 → alias 偵測失效 → 可能
        # 把 casefold/normalization-alias 當 absent 寫出撞名檔。scannability guard 須擋：skipped-unreadable、不寫 hub。
        self._w(self.lA / "s1.jsonl", fx.linear())          # local-only → copy-to-hub
        tombstone.write_coverage(self.hA)
        st = self._save_state(set(), set())                 # projA 基線在、s1 為新
        os.chmod(self.hA, 0o333)                            # write+execute，不可讀
        try:
            plan = scan.build_plan(self.local, self.hub, st, identity_fn=_name_match)
            report = self._apply(plan, state=st)
        finally:
            os.chmod(self.hA, 0o700)
        o = self._result(report, "s1")
        self.assertEqual(o.result, "skipped-unreadable")
        self.assertFalse((self.hA / "s1.jsonl").exists())   # 未寫入 hub（fail-closed）

    def test_deletion_then_resync_suppressed_not_resurrected(self):
        # 端到端「止血」：run1 寫 tombstone；run2 該 sid 不被 copy-to-local 復活。
        self._w(self.hA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        st = self._save_state({"s1"}, {"s1"})
        self._apply(scan.build_plan(self.local, self.hub, st, identity_fn=_name_match), state=st)  # run1
        st2 = state_mod.load_or_none(self.state)
        report2 = self._apply(scan.build_plan(self.local, self.hub, st2, identity_fn=_name_match), state=st2)
        self.assertEqual(self._result(report2, "s1").action, "suppressed-deleted")
        self.assertFalse((self.lA / "s1.jsonl").exists())                     # 未復活到 local

    def test_bulk_local_deletion_blocked_no_tombstone(self):
        # 5 個 local_known 全消失（疑掛錯碟/被清）→ 全 blocked-bulk、零 tombstone、追蹤不被未受信任現況覆蓋。
        sids = [f"s{i}" for i in range(5)]
        for s in sids:
            self._w(self.hA / f"{s}.jsonl", fx.linear())   # hub 5 個；local 全空
        tombstone.write_coverage(self.hA)
        st = self._save_state(sids, sids)
        report = self._apply(scan.build_plan(self.local, self.hub, st, identity_fn=_name_match), state=st)
        for s in sids:
            self.assertEqual(self._result(report, s).action, "blocked-bulk-local-deletion")
        self.assertEqual(list(tombstone.tombstones_dir(self.hA).glob("*.deleted.json")), [])
        self.assertEqual(state_mod.load_or_none(self.state).local_sessions["projA"], set(sids))

    def test_copy_to_local_updates_local_presence(self):
        # 真新 hub 檔 → copy-to-local；專案末 re-glob 把它納入 local_sessions（之後刪除才偵測得到）。
        self._w(self.hA / "newhub.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        st = self._save_state(set(), set())
        report = self._apply(scan.build_plan(self.local, self.hub, st, identity_fn=_name_match), state=st)
        self.assertEqual(self._result(report, "newhub").result, "copied-to-local")
        self.assertIn("newhub", state_mod.load_or_none(self.state).local_sessions["projA"])

    def test_local_deleted_stale_plan_reclassified_on_reappear(self):
        # plan 算 local-deleted，但 apply 時 local 檔已復現 → 鎖內重分類 ≠ local-deleted → skipped、不寫 tombstone。
        self._w(self.hA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        st = self._save_state({"s1"}, {"s1"})
        self._w(self.lA / "s1.jsonl", fx.linear())   # 復現（與 hub 相同）
        sp = SessionPlan("s1", "local-deleted", None, "stale")
        snap = compute_decision_snapshot(
            session_id="s1", local_project_dir=self.lA, hub_project_dir=self.hA,
            config=self.cfg, state=st, project_key="projA", cwd=None)
        out = self._call(sp, snap)
        self.assertEqual(out.result, "skipped-changed")
        self.assertIsNone(tombstone.find_session_tombstone(self.hA, "s1"))

    def test_delete_last_session_empties_dir_still_tombstones(self):
        # codex r25：刪到空夾（local 無任何 session）仍能靠 local_dir_bindings 配對 → 寫 tombstone。
        self._w(self.hA / "s1.jsonl", fx.linear())   # hub 有；local 夾（self.lA）空
        tombstone.write_coverage(self.hA)
        st = state_mod.State(known_sessions={"projA": {"s1"}}, local_sessions={"projA": {"s1"}},
                             local_dir_bindings={self.lA.name: "projA"})
        state_mod.save(st, self.state)
        # 用預設 git 解析（非 _name_match）以驗證夾名綁定路徑真的接上空夾。
        plan = scan.build_plan(self.local, self.hub, st)
        report = self._apply(plan, state=st)
        o = self._result(report, "s1")
        self.assertEqual(o.action, "local-deleted")
        self.assertEqual(o.result, "tombstoned-local-deletion")
        self.assertIsNotNone(tombstone.find_session_tombstone(self.hA, "s1"))
        self.assertTrue((self.hA / "s1.jsonl").exists())   # A3：hub 保留

    def test_migration_no_local_baseline_blocks_hub_copy(self):
        # codex r24-1：舊 state（有 known、無 local_sessions[projA]）→ hub-only 檔 fail-closed（不自動 copy，
        # 避免靜默復活已刪）；且不可在專案末悄悄建立 local 基線（否則下次就復活）。須重 bootstrap。
        self._w(self.hA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        st = state_mod.State(known_sessions={"projA": {"s1"}})   # 無 local_sessions entry（migration）
        state_mod.save(st, self.state)
        report = self._apply(scan.build_plan(self.local, self.hub, st, identity_fn=_name_match), state=st)
        self.assertEqual(self._result(report, "s1").action, "blocked-no-local-baseline")
        self.assertFalse((self.lA / "s1.jsonl").exists())                       # 未復活
        self.assertNotIn("projA", state_mod.load_or_none(self.state).local_sessions)  # 未悄悄建基線

    def test_failed_tombstone_keeps_pending_no_resurrect(self):
        # tombstone 寫失敗（error）→ 該 sid 不可被悄悄移出 local_known，否則下次當「新 hub 檔」復活。
        self._w(self.hA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        st = self._save_state({"s1"}, {"s1"})
        plan = scan.build_plan(self.local, self.hub, st, identity_fn=_name_match)
        with mock.patch.object(apply_mod.tombstone, "write_session_tombstone",
                               side_effect=OSError("disk full")):
            report = self._apply(plan, state=st)
        self.assertEqual(self._result(report, "s1").result, "error")
        self.assertIsNone(tombstone.find_session_tombstone(self.hA, "s1"))    # 未寫成
        self.assertIn("s1", state_mod.load_or_none(self.state).local_sessions["projA"])  # pending 保留

    def test_reconcile_failure_flags_reconcile_failed(self):
        # codex 3b2-R1 #3（session 對稱）：local-presence reconcile 失敗 → reconcile_failed=True（CLI 非零）。
        self._w(self.hA / "newhub.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        st = self._save_state(set(), set())
        plan = scan.build_plan(self.local, self.hub, st, identity_fn=_name_match)
        with mock.patch.object(apply_mod.state_mod, "reconcile_local_presence", side_effect=OSError("boom")):
            report = self._apply(plan, state=st)
        self.assertEqual(self._result(report, "newhub").result, "copied-to-local")
        self.assertTrue(report.reconcile_failed)

    def test_damaged_single_side_not_copied(self):
        # codex r14-2：單邊 copy 來源若損壞/無身分（0-byte/壞行/空）→ 不可原樣散播。
        (self.lA / "zero.jsonl").write_bytes(b"")                                  # 0-byte
        (self.lA / "partial.jsonl").write_text('{"uuid":"u1"}\n{bad\n', encoding="utf-8")  # 壞行
        tombstone.write_coverage(self.hA)
        plan = scan.build_plan(self.local, self.hub, None, identity_fn=_name_match)
        report = self._apply(plan)
        self.assertEqual(self._result(report, "zero").action, "blocked-damaged-source")
        self.assertEqual(self._result(report, "partial").action, "blocked-damaged-source")
        self.assertFalse((self.hA / "zero.jsonl").exists())
        self.assertFalse((self.hA / "partial.jsonl").exists())

    def test_tombstone_filename_content_mismatch_blocks(self):
        # codex r12：`secret.deleted.json` 內容 target 卻寫別的 sid（或型別錯）→ 視為損壞、阻擋 secret，
        # 不可被當「沒有 secret 的 tombstone」而復活。
        self._w(self.lA / "secret.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        (tombstone.tombstones_dir(self.hA) / "secret.deleted.json").write_text(
            '{"kind": "session", "target": "other"}', encoding="utf-8")
        report = self._apply(self._plan(coverage=True))
        self.assertEqual(self._result(report, "secret").action, "blocked-tombstone-corrupt")
        self.assertFalse((self.hA / "secret.jsonl").exists())  # 未復活

    def test_cross_side_casefold_blocked(self):
        # codex r11-4：local ABC + hub abc（case-only）在 case-sensitive 機器上也要擋。
        self._w(self.lA / "ABC.jsonl", fx.linear())
        self._w(self.hA / "abc.jsonl", fx.linear())
        report = self._apply(self._plan(coverage=True))
        self.assertTrue(all(o.action == "blocked-casefold-collision" for o in report.outcomes))
        self.assertFalse(report.wrote_anything)

    def test_state_commit_failure_is_uncommitted_not_success(self):
        # codex r11-6：寫檔成功但 state 未提交 → committed=False、had_uncommitted、CLI 非零。
        self._w(self.lA / "only_local.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        held = atomicio.FileLock(self.state).acquire()  # 卡住 state 鎖
        try:
            report = apply_mod.apply_plan(
                self._plan(coverage=True), local_root=self.local, hub_root=self.hub,
                config=self.cfg, state=state_mod.State(known_sessions={"projA": set()}),
                state_path=str(self.state), lock_timeout_s=0.2,
            )
        finally:
            held.release()
        o = self._result(report, "only_local")
        self.assertEqual(o.result, "copied-to-hub")      # 檔確實寫了
        self.assertFalse(o.committed)                    # 但 state 沒提交
        self.assertTrue(report.had_uncommitted)
        self.assertTrue((self.hA / "only_local.jsonl").exists())

    # ── 交易守衛（直接打 _apply_session 精準注入）──────────────────────────

    def _session_snap(self, sid, action, direction):
        sp = SessionPlan(sid, action, direction, "test")
        snap = compute_decision_snapshot(
            session_id=sid, local_project_dir=self.lA, hub_project_dir=self.hA,
            config=self.cfg, state=None, project_key="projA", cwd=None,
        )
        return sp, snap

    def _call(self, sp, snap, *, base_fp=None):
        return apply_mod._apply_session(
            sp, local_dir=self.lA, hub_dir=self.hA, project_key="projA", cwd=None,
            plan_snap=snap, config=self.cfg, state_path=str(self.state), hub_root=self.hub,
            base_fp=base_fp if base_fp is not None else anomaly.hub_fingerprint(self.hub),
            machine="testhost", lock_timeout_s=0.2,
        )

    def test_stale_snapshot_aborts(self):
        self._w(self.lA / "s1.jsonl", fx.linear())
        self._w(self.hA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        sp, snap = self._session_snap("s1", "identical", None)
        self._w(self.hA / "s1.jsonl", fx.fast_forward_of_linear())  # plan 後 hub 變了
        out = self._call(sp, snap)
        self.assertEqual(out.result, "skipped-changed")

    def test_locked_session_skipped(self):
        self._w(self.lA / "s1.jsonl", fx.fast_forward_of_linear())
        self._w(self.hA / "s1.jsonl", fx.linear())
        sp, snap = self._session_snap("s1", "fast-forward", "local->hub")
        held = atomicio.FileLock(self.hA / "s1.jsonl").acquire()
        try:
            out = self._call(sp, snap)
        finally:
            held.release()
        self.assertEqual(out.result, "skipped-locked")
        # 未寫入（hub 仍為舊內容）
        self.assertEqual((self.hA / "s1.jsonl").read_bytes(), fx_bytes(fx.linear()))

    def test_fingerprint_change_mid_apply_halts(self):
        self._w(self.lA / "s1.jsonl", fx.fast_forward_of_linear())
        self._w(self.hA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        sp, snap = self._session_snap("s1", "fast-forward", "local->hub")
        out = self._call(sp, snap, base_fp="a-different-fingerprint")
        self.assertEqual(out.result, "halt")

    def test_stale_action_caught_by_reclassify(self):
        # codex r10-2：plan_snap 反映「現況已是 fork」（模擬 plan 後、快照前 hub 被獨立寫入），
        # 但動作仍是過期的 ff local->hub → 持鎖重新分類擋下，不覆蓋 hub。
        self._w(self.lA / "s1.jsonl", fx.fork_of_linear())
        self._w(self.hA / "s1.jsonl", fx.linear())  # classify(local,hub) = fork
        tombstone.write_coverage(self.hA)
        sp = SessionPlan("s1", "fast-forward", "local->hub", "stale")  # 過期決策
        snap = compute_decision_snapshot(
            session_id="s1", local_project_dir=self.lA, hub_project_dir=self.hA,
            config=self.cfg, state=None, project_key="projA", cwd=None,
        )
        hub_before = (self.hA / "s1.jsonl").read_bytes()
        out = self._call(sp, snap)
        self.assertEqual(out.result, "skipped-changed")
        self.assertEqual((self.hA / "s1.jsonl").read_bytes(), hub_before)  # hub 未被過期動作覆蓋

    def test_verified_bytes_binds_to_snapshot(self):
        # codex r10-3：寫出的 bytes 必須對得上快照 digest。
        p = self.lA / "x.jsonl"
        p.write_bytes(b"hello")
        good = "sha:" + hashlib.sha256(b"hello").hexdigest()
        self.assertEqual(apply_mod._verified_bytes(p, good), b"hello")
        self.assertIsNone(apply_mod._verified_bytes(p, "sha:deadbeef"))     # digest 不符
        self.assertIsNone(apply_mod._verified_bytes(self.lA / "nope.jsonl", good))  # 讀不到

    def test_copy_to_local_never_overwrites_raced_file(self):
        # C3：copy-to-local 用 O_EXCL；若 local 期間冒出同名檔，改 keep-both，不覆蓋。
        # 直接打 atomic_create_bytes 確認 no-clobber（apply 內 reclassify 多半已先擋下）。
        existing = self.lA / "raced.jsonl"
        existing.write_bytes(b"local-precious")
        with self.assertRaises(FileExistsError):
            atomicio.atomic_create_bytes(existing, b"incoming")
        self.assertEqual(existing.read_bytes(), b"local-precious")

    def test_copy_suppressed_by_tombstone_at_apply(self):
        # base 與 local 相符 → 鎖內 reclass = suppressed-deleted（不復活、不寫）。
        self._w(self.lA / "s2.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        base = tombstone.raw_file_digest(self.lA / "s2.jsonl")
        tombstone.write_session_tombstone(self.hA, "s2", base_hash=base)
        sp, snap = self._session_snap("s2", "copy-to-hub", "local->other")
        out = self._call(sp, snap)
        self.assertEqual(out.result, "suppressed")
        self.assertFalse((self.hA / "s2.jsonl").exists())

    def test_write_error_is_reported_not_raised(self):
        self._w(self.lA / "only_local.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        sp, snap = self._session_snap("only_local", "copy-to-hub", "local->other")
        with mock.patch.object(apply_mod.atomicio, "atomic_write_bytes", side_effect=OSError("disk full")):
            out = self._call(sp, snap)
        self.assertEqual(out.result, "error")

    def test_halt_anomaly_writes_nothing(self):
        self._w(self.lA / "s1.jsonl", fx.fast_forward_of_linear())
        self._w(self.hA / "s1.jsonl", fx.linear())
        plan = self._plan()
        # 用一個指紋不符的 state → apply 前檢 halt
        st = state_mod.State(hub_fingerprint="stale")
        report = apply_mod.apply_plan(
            plan, local_root=self.local, hub_root=self.hub, config=self.cfg,
            state=st, state_path=str(self.state),
        )
        self.assertTrue(report.halted)
        self.assertEqual((self.hA / "s1.jsonl").read_bytes(), fx_bytes(fx.linear()))


def fx_bytes(objs):
    import json
    return ("".join(json.dumps(o, ensure_ascii=False) + "\n" for o in objs)).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
