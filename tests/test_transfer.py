import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_session_sync import atomicio, tombstone, transfer
from tests import _caps, fixtures as fx


def _name_match(local_dir, remote_dirs):
    for rd in remote_dirs:
        if rd.name == local_dir.name:
            return ("match", rd)
    return ("needs-map", None)


class TestTransfer(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.local = self.tmp / "local"
        self.remote = self.tmp / "remote"
        self.lA = self.local / "projA"
        self.rA = self.remote / "projA"
        self.lA.mkdir(parents=True)
        self.rA.mkdir(parents=True)

    def tearDown(self):
        self._td.cleanup()

    def _w(self, path, objs):
        fx.write_jsonl(objs, str(path))

    def _result(self, report, sid):
        return next(o for o in report.outcomes if o.session_id == sid)

    def _pull(self, **kw):
        kw.setdefault("identity_fn", _name_match)
        return transfer.plan_transfer("pull", self.local, self.remote, remote_name="office", **kw)

    def _push(self, **kw):
        kw.setdefault("identity_fn", _name_match)
        return transfer.plan_transfer("push", self.local, self.remote, remote_name="office", **kw)

    def _apply(self, plan):
        return transfer.apply_transfer(plan, local_root=self.local, remote_root=self.remote)

    # ── pull（remote → local）────────────────────────────────────────────────

    def test_pull_copy(self):
        self._w(self.rA / "s1.jsonl", fx.linear())   # remote 有、local 無
        report = self._apply(self._pull())
        self.assertEqual(self._result(report, "s1").result, "copied-to-local")
        self.assertEqual((self.lA / "s1.jsonl").read_bytes(), (self.rA / "s1.jsonl").read_bytes())

    def test_push_dup_missing_targets_casefold_folded(self):
        # codex mcwd-g2 #4：兩個 --map 指向**皆不存在**的 Hub/hub → exact 不同但 case-insensitive remote 上
        # mkdir 是同一實體夾 → `_rkey` 葉名摺疊後視為同 target 全數 skip（比照 bootstrap key_dups 摺疊）。
        lB = self.local / "projB"
        lB.mkdir()
        self._w(self.lA / "s1.jsonl", fx.linear())
        self._w(lB / "s2.jsonl", fx.linear())
        plan = self._push(mappings={"projA": "Hub", "projB": "hub"})
        self.assertEqual({p.identity for p in plan.projects if p.local_dir}, {"skipped-dup-target"})

    @_caps.needs_case_sensitive_fs
    @_caps.needs_symlink
    def test_push_casefold_symlink_alias_not_clobbered(self):
        # e2e gate9 finding1（transfer 對稱主 sync）：push 目標 remote 有 **casefold-alias** symlink `ABC.jsonl`
        # （vs local 真檔 `abc.jsonl`）→ `_session_files` 略過 → remote 看似 absent → transfer-copy 會把不可信 alias
        # symlink 當 absent 而覆蓋。guard（`scan._name_key` 比對）須擋：skipped-changed、symlink 不被覆蓋、不寫實檔。
        self._w(self.lA / "abc.jsonl", fx.linear())                  # local 真檔
        outside = self.tmp / "outside.jsonl"
        self._w(outside, fx.linear())
        (self.rA / "ABC.jsonl").symlink_to(outside)                  # remote casefold-alias symlink
        report = self._apply(self._push())
        o = self._result(report, "abc")
        self.assertEqual(o.result, "skipped-changed")
        self.assertTrue((self.rA / "ABC.jsonl").is_symlink())        # alias symlink 原封不動
        self.assertFalse((self.rA / "abc.jsonl").exists())           # 未寫入 abc.jsonl 實檔（guard 擋下）

    @_caps.needs_unreadable_dir
    def test_push_unreadable_remote_dir_no_write(self):
        # e2e gate10（transfer 對稱主 sync）：push 目標 remote 夾 write+execute 但不可讀（0o333）→ `_symlink_name_keys`
        # 回空 → alias 偵測失效。scannability guard 須擋：skipped-changed、不寫 remote。POSIX-only。
        self._w(self.lA / "s1.jsonl", fx.linear())          # local-only → push copy
        os.chmod(self.rA, 0o333)
        try:
            report = self._apply(self._push())
        finally:
            os.chmod(self.rA, 0o700)
        o = self._result(report, "s1")
        self.assertEqual(o.result, "skipped-changed")
        self.assertFalse((self.rA / "s1.jsonl").exists())   # 未寫入 remote（fail-closed）

    @_caps.needs_symlink
    def test_push_symlink_tombstones_dir_no_resurrect(self):
        # e2e gate12：remote `.tombstones` 是 symlink→外部（含 s1.deleted.json）→ `read_tombstones` 因 `_tombstones_ok`
        # 拒 symlink 回 {}（**拒讀界外、非「真的沒有」**）→ 若當「無 tombstone」→ push transfer-copy **復活已刪 s1**（A3）。
        # transfer 不 gate on coverage，須以 `tombstones_enumerable` 自檢：skipped-changed、不寫 remote s1。本機可實跑。
        self._w(self.lA / "s1.jsonl", fx.linear())           # local 有 s1（push 來源）
        external = self.tmp / "ext_tombs"
        external.mkdir()
        (external / "s1.deleted.json").write_text(
            '{"kind":"session","target":"s1","base_hash":null,"machine":"m","time":"t"}', encoding="utf-8")
        (self.rA / ".tombstones").symlink_to(external, target_is_directory=True)   # remote .tombstones=symlink→界外
        report = self._apply(self._push())
        o = self._result(report, "s1")
        self.assertEqual(o.result, "skipped-changed")
        self.assertFalse((self.rA / "s1.jsonl").exists())    # 未復活寫入 remote（A3）

    def test_pull_ff_keeps_both_never_overwrites_local(self):
        self._w(self.lA / "s1.jsonl", fx.linear())                   # local 較舊
        self._w(self.rA / "s1.jsonl", fx.fast_forward_of_linear())   # remote 較新
        before = (self.lA / "s1.jsonl").read_bytes()
        report = self._apply(self._pull())
        self.assertEqual(self._result(report, "s1").result, "kept-both-local")
        self.assertEqual((self.lA / "s1.jsonl").read_bytes(), before)   # C3：不覆蓋
        kept = [p for p in self.lA.glob("*.jsonl") if p.name != "s1.jsonl"]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), (self.rA / "s1.jsonl").read_bytes())

    def test_pull_local_newer_is_noop(self):
        self._w(self.lA / "s1.jsonl", fx.fast_forward_of_linear())   # local 較新
        self._w(self.rA / "s1.jsonl", fx.linear())
        report = self._apply(self._pull())
        o = self._result(report, "s1")
        self.assertEqual(o.action, "dest-newer")
        self.assertEqual(o.result, "reported")

    def test_pull_identical_noop(self):
        self._w(self.lA / "s1.jsonl", fx.linear())
        self._w(self.rA / "s1.jsonl", fx.linear())
        self.assertEqual(self._result(self._apply(self._pull()), "s1").action, "identical")

    def test_pull_fork_needs_decision(self):
        self._w(self.lA / "s1.jsonl", fx.fork_of_linear())
        self._w(self.rA / "s1.jsonl", fx.linear())
        o = self._result(self._apply(self._pull()), "s1")
        self.assertEqual(o.action, "needs-decision")
        self.assertEqual(o.result, "reported")

    def test_pull_respects_remote_tombstone(self):
        self._w(self.rA / "s1.jsonl", fx.linear())
        base = tombstone.raw_file_digest(self.rA / "s1.jsonl")
        tombstone.write_session_tombstone(self.rA, "s1", base_hash=base)
        report = self._apply(self._pull())
        self.assertEqual(self._result(report, "s1").action, "suppressed-deleted")
        self.assertFalse((self.lA / "s1.jsonl").exists())   # 不跨群復活已刪

    def test_pull_damaged_source_blocked(self):
        (self.rA / "bad.jsonl").write_bytes(b"")   # 0-byte
        report = self._apply(self._pull())
        self.assertEqual(self._result(report, "bad").action, "blocked-damaged-source")
        self.assertFalse((self.lA / "bad.jsonl").exists())

    def test_pull_remote_only_project_needs_map(self):
        rB = self.remote / "projB"
        rB.mkdir()
        self._w(rB / "s1.jsonl", fx.linear())
        plan = self._pull()
        pp = next(p for p in plan.projects if p.remote_dir and p.remote_dir.endswith("projB"))
        self.assertEqual(pp.identity, "remote-only")
        self.assertEqual(pp.items, [])

    # ── push（local → remote）───────────────────────────────────────────────

    def test_push_copy(self):
        self._w(self.lA / "s1.jsonl", fx.linear())   # local 有、remote 無
        report = self._apply(self._push())
        self.assertEqual(self._result(report, "s1").result, "copied-to-remote")
        self.assertEqual((self.rA / "s1.jsonl").read_bytes(), (self.lA / "s1.jsonl").read_bytes())

    def test_push_ff_overwrites_remote(self):
        self._w(self.lA / "s1.jsonl", fx.fast_forward_of_linear())   # local 較新
        self._w(self.rA / "s1.jsonl", fx.linear())
        report = self._apply(self._push())
        self.assertEqual(self._result(report, "s1").result, "applied-ff-remote")
        self.assertEqual((self.rA / "s1.jsonl").read_bytes(), (self.lA / "s1.jsonl").read_bytes())

    def test_push_remote_newer_is_noop(self):
        self._w(self.lA / "s1.jsonl", fx.linear())
        self._w(self.rA / "s1.jsonl", fx.fast_forward_of_linear())   # remote 較新
        self.assertEqual(self._result(self._apply(self._push()), "s1").action, "dest-newer")

    def test_push_creates_mapped_remote_dir(self):
        lX = self.local / "projX"
        lX.mkdir()
        self._w(lX / "s1.jsonl", fx.linear())
        plan = transfer.plan_transfer("push", self.local, self.remote, remote_name="office",
                                      mappings={"projX": "encX"})   # 預設 git 解析 + 明示 map
        report = transfer.apply_transfer(plan, local_root=self.local, remote_root=self.remote)
        self.assertEqual(self._result(report, "s1").result, "copied-to-remote")
        self.assertTrue((self.remote / "encX" / "s1.jsonl").exists())   # --map 目標夾被建出

    def test_push_respects_remote_tombstone_suppress(self):
        self._w(self.lA / "s1.jsonl", fx.linear())
        base = tombstone.raw_file_digest(self.lA / "s1.jsonl")   # remote 刪的就是這版
        tombstone.write_session_tombstone(self.rA, "s1", base_hash=base)
        report = self._apply(self._push())
        self.assertEqual(self._result(report, "s1").action, "suppressed-deleted")
        self.assertFalse((self.rA / "s1.jsonl").exists())   # 不復活到 remote

    def test_push_tombstone_conflict_when_modified(self):
        self._w(self.lA / "s1.jsonl", fx.linear())
        tombstone.write_session_tombstone(self.rA, "s1", base_hash="0" * 64)   # base ≠ local
        report = self._apply(self._push())
        self.assertEqual(self._result(report, "s1").action, "conflict-delete-vs-update")
        self.assertFalse((self.rA / "s1.jsonl").exists())

    def test_push_bad_map_rejected(self):
        lX = self.local / "projX"
        lX.mkdir()
        self._w(lX / "s1.jsonl", fx.linear())
        plan = transfer.plan_transfer("push", self.local, self.remote, mappings={"projX": "../escape"})
        pp = next(p for p in plan.projects if p.local_dir and p.local_dir.endswith("projX"))
        self.assertEqual(pp.identity, "skipped-bad-map")

    # ── 選擇 / 同一性 / 安全 ─────────────────────────────────────────────────

    def test_session_filter(self):
        self._w(self.rA / "s1.jsonl", fx.linear())
        self._w(self.rA / "s2.jsonl", fx.linear())
        plan = self._pull(session="s1")
        sids = [it.session_id for pp in plan.projects for it in pp.items]
        self.assertEqual(sids, ["s1"])

    def test_unmatched_needs_map(self):
        # 預設 git 解析、local projA 空無 cwd → needs-map（不憑空配對）。
        self._w(self.rA / "s1.jsonl", fx.linear())
        plan = transfer.plan_transfer("pull", self.local, self.remote)   # 無 identity_fn、無 map
        pp = next(p for p in plan.projects if p.local_dir and p.local_dir.endswith("projA"))
        self.assertEqual(pp.identity, "needs-map")
        self.assertFalse((self.lA / "s1.jsonl").exists())

    def test_locked_remote_skipped(self):
        self._w(self.rA / "s1.jsonl", fx.linear())
        plan = self._pull()
        held = atomicio.FileLock(self.rA / "s1.jsonl").acquire()
        try:
            report = transfer.apply_transfer(plan, local_root=self.local, remote_root=self.remote,
                                             lock_timeout_s=0.2)
        finally:
            held.release()
        self.assertEqual(self._result(report, "s1").result, "skipped-locked")
        self.assertFalse((self.lA / "s1.jsonl").exists())

    def test_halt_on_missing_remote_mount(self):
        plan = transfer.plan_transfer("pull", self.local, self.remote / "nope", identity_fn=_name_match)
        self.assertTrue(plan.halt)

    def test_apply_halt_on_missing_local_mount(self):
        self._w(self.rA / "s1.jsonl", fx.linear())
        plan = self._pull()
        report = transfer.apply_transfer(plan, local_root=self.tmp / "nolocal", remote_root=self.remote)
        self.assertTrue(report.halted)

    def test_stable_read(self):
        p = self.lA / "x.jsonl"
        p.write_bytes(b"v1")
        self.assertEqual(transfer._stable_read(p), b"v1")
        self.assertIsNone(transfer._stable_read(self.lA / "nope.jsonl"))
        with mock.patch.object(transfer.Path, "read_bytes", side_effect=[b"v1", b"v2"]):
            self.assertIsNone(transfer._stable_read(p))   # 兩讀不一致（active）→ None

    def _plan_action(self, plan, sid):
        for pp in plan.projects:
            for it in pp.items:
                if it.session_id == sid:
                    return it.action
        return None

    def test_push_source_damaged_at_apply_skipped(self):
        # codex r-transfer-1：寫出的 bytes 綁定分類——plan 時來源好、apply 前變 0-byte → bytes 重分類抓到
        # damaged → skipped、不寫未經分類的內容。
        self._w(self.lA / "s1.jsonl", fx.linear())
        plan = self._push()
        self.assertEqual(self._plan_action(plan, "s1"), "transfer-copy")
        (self.lA / "s1.jsonl").write_bytes(b"")   # 來源在 apply 前壞掉
        o = self._result(self._apply(plan), "s1")
        self.assertEqual(o.result, "skipped-changed")
        self.assertFalse((self.rA / "s1.jsonl").exists())

    def test_remote_sidecar_change_skips(self):
        # codex r-transfer-2：plan 後 remote 專案 _project.json 變（夾被抽換成別專案）→ 鎖內重驗 → skipped。
        self._w(self.rA / "s1.jsonl", fx.linear())
        (self.rA / "_project.json").write_text('{"git_remote": "a"}', encoding="utf-8")
        plan = self._pull()
        (self.rA / "_project.json").write_text('{"git_remote": "b"}', encoding="utf-8")   # 抽換
        o = self._result(self._apply(plan), "s1")
        self.assertEqual(o.result, "skipped-changed")
        self.assertFalse((self.lA / "s1.jsonl").exists())

    @_caps.needs_symlink
    def test_symlink_remote_dir_rejected(self):
        # codex r-transfer-3：--map 目標是逃出 root 的 symlink → skipped-unsafe，不沿 symlink 寫到外面。
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.remote / "evil").symlink_to(outside, target_is_directory=True)
        lX = self.local / "projX"
        lX.mkdir()
        self._w(lX / "s1.jsonl", fx.linear())
        plan = transfer.plan_transfer("push", self.local, self.remote, mappings={"projX": "evil"})
        pp = next(p for p in plan.projects if p.local_dir and p.local_dir.endswith("projX"))
        self.assertEqual(pp.identity, "skipped-unsafe")
        transfer.apply_transfer(plan, local_root=self.local, remote_root=self.remote)
        self.assertFalse((outside / "s1.jsonl").exists())   # 未沿 symlink 寫到 root 外

    def test_push_multi_session_new_mapped_dir_no_halt(self):
        # codex r-transfer-4：push --map 建新夾後，同專案後續 session 不因 fingerprint 變而誤 halt。
        lX = self.local / "projX"
        lX.mkdir()
        self._w(lX / "s1.jsonl", fx.linear())
        self._w(lX / "s2.jsonl", fx.linear())
        plan = transfer.plan_transfer("push", self.local, self.remote, mappings={"projX": "encX"})
        report = transfer.apply_transfer(plan, local_root=self.local, remote_root=self.remote)
        self.assertFalse(report.halted)
        self.assertTrue((self.remote / "encX" / "s1.jsonl").exists())
        self.assertTrue((self.remote / "encX" / "s2.jsonl").exists())

    def test_dup_remote_target_skipped(self):
        # codex r-transfer-3：兩個 local 夾對到同一 remote 夾 → 全數跳過（不合併不同專案）。
        for n in ("projX", "projY"):
            d = self.local / n
            d.mkdir()
            self._w(d / f"{n}.jsonl", fx.linear())
        plan = transfer.plan_transfer("push", self.local, self.remote,
                                      mappings={"projX": "shared", "projY": "shared"})
        ids = {Path(p.local_dir).name: p.identity for p in plan.projects if p.local_dir}
        self.assertEqual(ids["projX"], "skipped-dup-target")
        self.assertEqual(ids["projY"], "skipped-dup-target")
        report = transfer.apply_transfer(plan, local_root=self.local, remote_root=self.remote)
        self.assertFalse((self.remote / "shared").exists())   # 未合併寫入

    @_caps.needs_symlink
    def test_symlink_session_file_excluded_from_plan(self):
        # 來源 session 檔是 symlink（可能指 root 外）→ **plan 就排除**（`scan._session_files` 略過 symlink，e2e gate2
        # #2），不沿 symlink 讀/寫、不列入計畫（比原「apply 鎖內拒 skipped-changed」更早更嚴；鎖內拒仍在守 TOCTOU）。
        outside = self.tmp / "outside"
        outside.mkdir()
        fx.write_jsonl(fx.linear(), str(outside / "evil.jsonl"))
        (self.rA / "s1.jsonl").symlink_to(outside / "evil.jsonl")
        report = self._apply(self._pull())
        self.assertFalse(any(o.session_id == "s1" for o in report.outcomes))   # 未列入（plan 排除 symlink）
        self.assertFalse((self.lA / "s1.jsonl").exists())                      # 未複製（無洩漏）

    @_caps.needs_symlink
    def test_symlink_local_dir_rejected(self):
        # e2e xgrp #3：local 專案夾是逃出 local_root 的 symlink → skipped-unsafe；push 不讀 root 外真檔洩漏進 remote。
        outside = self.tmp / "outside"
        outside.mkdir()
        self._w(outside / "s1.jsonl", fx.linear())          # root 外的「私密」session
        (self.local / "evil").symlink_to(outside, target_is_directory=True)
        plan = transfer.plan_transfer("push", self.local, self.remote, mappings={"evil": "projA"})
        pp = next(p for p in plan.projects if p.local_dir and p.local_dir.endswith("evil"))
        self.assertEqual(pp.identity, "skipped-unsafe")
        transfer.apply_transfer(plan, local_root=self.local, remote_root=self.remote)
        self.assertFalse((self.rA / "s1.jsonl").exists())    # 未把 root 外 session 洩漏進 remote

    @_caps.needs_junction
    def test_dup_remote_target_via_junction_alias_skipped(self):
        # e2e xgrp #5：remote alias 為指向 real 的 junction → 兩 --map（real/alias）實體同夾 → skipped-dup-target
        #（dup 偵測比對 resolve 後路徑、非字串，否則 junction 別名漏偵測、兩專案合進同一實體夾）。
        real = self.remote / "real"
        real.mkdir()
        _caps.make_junction(self.remote / "alias", real)
        for n in ("projX", "projY"):
            d = self.local / n
            d.mkdir()
            self._w(d / f"{n}.jsonl", fx.linear())
        plan = transfer.plan_transfer("push", self.local, self.remote,
                                      mappings={"projX": "real", "projY": "alias"})
        ids = {Path(p.local_dir).name: p.identity for p in plan.projects if p.local_dir}
        self.assertEqual(ids["projX"], "skipped-dup-target")
        self.assertEqual(ids["projY"], "skipped-dup-target")
        transfer.apply_transfer(plan, local_root=self.local, remote_root=self.remote)
        self.assertFalse((real / "projX.jsonl").exists())    # 未合併不同專案進同一實體夾
        self.assertFalse((real / "projY.jsonl").exists())

    def test_write_error_reported_not_raised(self):
        self._w(self.rA / "s1.jsonl", fx.linear())
        plan = self._pull()
        with mock.patch.object(transfer.atomicio, "atomic_create_bytes", side_effect=OSError("disk full")):
            report = self._apply(plan)
        self.assertEqual(self._result(report, "s1").result, "error")


if __name__ == "__main__":
    unittest.main()
