"""P1d Block 3b-2：apply memory 寫入路徑。對稱 test_apply（session），驗證 happy paths + 安全閘 + 交易守衛。

關鍵不變量：C3（copy-to-local 絕不覆蓋既有 local memory）、A3（local-deleted 只寫 tombstone、絕不刪 hub）、
不復活（tombstone/undecidable 閘）、conflict-cross-file-identity / blocked-tombstone-no-identity **不自動寫**。
"""
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

from claude_session_sync import (anomaly, apply as apply_mod, atomicio, memory, scan,
                                 state as state_mod, tombstone)
from claude_session_sync.config import Config
from claude_session_sync.state import State
from tests import _caps


def _name_match(local_dir, hub_dirs):
    for hd in hub_dirs:
        if hd.name == local_dir.name:
            return ("match", hd)
    return ("needs-map", None)


def _mem(slug="fact", body="hello", desc="d"):
    return "\n".join(["---", f"name: {slug}", f"description: {desc}",
                      "metadata:", "  type: project", "---", body, ""])


class _MemApplyHarness:
    """共用 harness：setUp + 寫檔/plan/apply 輔助；無 test_ 方法，供兩個 TestCase 共用（避免子類化造成重跑）。"""

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
        # 已 bootstrap：含 projA 的 hub+local memory 基線（has_baseline / has_local_baseline=True）。
        self._save(known_mem=set(), local_mem=set())

    def tearDown(self):
        self._td.cleanup()

    def _save(self, *, known_mem, local_mem, known_sess=None, local_sess=None):
        st = State(known_sessions={"projA": set(known_sess or set())},
                   local_sessions={"projA": set(local_sess or set())},
                   known_memory={"projA": set(known_mem)},
                   local_memory={"projA": set(local_mem)})
        state_mod.save(st, self.state)
        return st

    def _wm(self, proj_dir, name, text):
        mdir = proj_dir / "memory"
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / name).write_text(text, encoding="utf-8")

    def _mfile(self, proj_dir, name):
        return proj_dir / "memory" / name

    def _plan(self, *, coverage=True, state=None):
        if coverage:
            tombstone.write_coverage(self.hA)
        st = state if state is not None else state_mod.load_or_none(self.state)
        return scan.build_plan(self.local, self.hub, st, identity_fn=_name_match)

    def _apply(self, plan, *, state=None, lock_timeout_s=5.0):
        st = state if state is not None else state_mod.load_or_none(self.state)
        return apply_mod.apply_plan(
            plan, local_root=self.local, hub_root=self.hub, config=self.cfg,
            state=st, state_path=str(self.state), lock_timeout_s=lock_timeout_s)

    def _r(self, report, name):
        return next(o for o in report.outcomes if o.kind == "memory" and o.session_id == name)

    def _ldmem(self):
        return state_mod.load_or_none(self.state).local_memory.get("projA", set())

    def _kmem(self):
        return state_mod.load_or_none(self.state).known_memory.get("projA", set())


class TestApplyMemory(_MemApplyHarness, unittest.TestCase):
    # ── auto happy paths ────────────────────────────────────────────────────

    def test_copy_to_hub(self):
        self._wm(self.lA, "new.md", _mem("new"))
        report = self._apply(self._plan())
        self.assertEqual(self._r(report, "new.md").result, "copied-to-hub")
        self.assertTrue(self._mfile(self.hA, "new.md").exists())
        self.assertIn("new.md", self._kmem())  # 記 known

    def test_copy_to_local(self):
        self._wm(self.hA, "h.md", _mem("h"))
        report = self._apply(self._plan())
        self.assertEqual(self._r(report, "h.md").result, "copied-to-local")
        self.assertTrue(self._mfile(self.lA, "h.md").exists())
        self.assertIn("h.md", self._ldmem())   # reconcile 納入 local_memory
        self.assertIn("h.md", self._kmem())

    def test_identical_commits_known_no_write(self):
        self._wm(self.lA, "a.md", _mem("a"))
        self._wm(self.hA, "a.md", _mem("a"))
        report = self._apply(self._plan())
        self.assertEqual(self._r(report, "a.md").result, "identical")
        self.assertIn("a.md", self._kmem())

    def test_identical_tolerates_cosmetic_reorder(self):
        # 正規化 content_hash → 鍵序/尾 newline 差異仍 identical（不誤 conflict）。
        self._wm(self.lA, "a.md", "---\nname: x\ndescription: d\n---\nbody")
        self._wm(self.hA, "a.md", '---\ndescription: "d"\nname: x\n---\nbody\n')
        report = self._apply(self._plan())
        self.assertEqual(self._r(report, "a.md").result, "identical")

    def test_local_deleted_writes_tombstone_keeps_hub(self):
        self._wm(self.hA, "a.md", _mem("a"))   # hub 有；local 無（已刪）
        st = self._save(known_mem={"a.md"}, local_mem={"a.md"})
        report = self._apply(self._plan(state=st), state=st)
        o = self._r(report, "a.md")
        self.assertEqual(o.action, "local-deleted")
        self.assertEqual(o.result, "tombstoned-local-deletion")
        tomb = tombstone.find_memory_tombstone(self.hA, "a.md")
        self.assertIsNotNone(tomb)
        self.assertEqual(tomb.base_hash, memory.content_hash(memory.load_memory(self._mfile(self.hA, "a.md"))))
        self.assertEqual(tomb.identity, "a")                       # frontmatter name slug
        self.assertTrue(self._mfile(self.hA, "a.md").exists())      # A3：hub 保留
        self.assertNotIn("a.md", self._ldmem())                     # reconcile 移出 local_memory

    @_caps.needs_symlink
    def test_local_deleted_symlink_leaf_not_tombstoned(self):
        # e2e gate5 High（對稱 session apply 的 local-leaf 防線）：local memory 檔被換成**指向界外**的 symlink →
        # list_memory_files 略過 → 該 name 在 local 側「看似 absent」→ 誤分類 local-deleted。防線須擋：不可信的
        # symlink leaf **絕不可**當「使用者確認刪除」而寫抑制 tombstone（fail-closed／A3）。驗：不寫 tombstone、
        # hub 保留、且該 name **留在 local_memory 基線**（reconcile pending，不收斂）→ 下次 sync 續 blocked。
        self._wm(self.hA, "a.md", _mem("a"))                        # hub 有正常檔
        outside = self.tmp / "outside.md"
        outside.write_text(_mem("evil", body="界外機密"), encoding="utf-8")
        link = self._mfile(self.lA, "a.md")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)                                    # local a.md → 界外 symlink
        self.assertTrue(link.is_symlink())
        st = self._save(known_mem={"a.md"}, local_mem={"a.md"})
        report = self._apply(self._plan(state=st), state=st)
        o = self._r(report, "a.md")
        self.assertEqual(o.action, "local-deleted")                 # symlink 被略過 → 分類仍看似 local-deleted
        self.assertEqual(o.result, "skipped-changed")               # 但 apply 擋下：不寫 tombstone
        self.assertIsNone(tombstone.find_memory_tombstone(self.hA, "a.md"))  # 無抑制 tombstone
        self.assertTrue(self._mfile(self.hA, "a.md").exists())              # A3：hub 保留
        self.assertIn("a.md", self._ldmem())                        # 未 reconcile 掉 → 留基線（下次續 blocked，fail-closed）

    @_caps.needs_symlink
    def test_copy_to_hub_hub_symlink_dest_not_clobbered(self):
        # e2e gate6#1（對稱 session apply loop 的 hf.is_symlink()）：hub memory leaf 為 symlink → list_memory_files
        # 略過 → hub 看似 absent → copy-to-hub。dest 端須擋：不可把不可信的 hub symlink 當 absent 而覆蓋（即便
        # os.replace 不跟隨、無界外寫，仍不該悄悄替換使用者的 symlink 設定）。
        self._wm(self.lA, "new.md", _mem("new"))                    # local 真檔（copy 來源）
        outside = self.tmp / "outside.md"
        outside.write_text(_mem("evil", body="界外機密"), encoding="utf-8")
        hub_link = self._mfile(self.hA, "new.md")
        hub_link.parent.mkdir(parents=True, exist_ok=True)
        hub_link.symlink_to(outside)                                # hub new.md → 界外 symlink
        report = self._apply(self._plan())
        o = self._r(report, "new.md")
        self.assertEqual(o.action, "copy-to-hub")
        self.assertEqual(o.result, "skipped-changed")               # dest 為 symlink → 不覆蓋
        self.assertTrue(hub_link.is_symlink())                      # hub symlink 原封不動（未被 local 內容覆蓋）
        self.assertNotIn("new.md", self._kmem())                    # 未落地 → 不記 known

    @_caps.needs_symlink
    def test_index_rebuild_skipped_when_symlink_leaf(self):
        # e2e gate6#2：local memory leaf 為 symlink → 索引重建須 fail-closed（否則 list_memory_files 略過該 leaf →
        # 把 name 從 auto-block 移除＝被略過 leaf 驅動 auto write，與 delete 路徑 fail-closed 立場不一致）。驗：保留
        # 現有索引位元不變 + 警告（plan_index_rebuild 回 kept-symlink-leaf）。
        self._wm(self.hA, "a.md", _mem("a"))
        outside = self.tmp / "outside.md"
        outside.write_text(_mem("evil"), encoding="utf-8")
        link = self._mfile(self.lA, "a.md")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)                                    # local a.md → 界外 symlink
        idx = self._mfile(self.lA, memory.INDEX_FILE)
        idx.write_text("\n".join(["# Memory Index", "", memory.INDEX_BEGIN,
                                  "- [a](a.md) — d", memory.INDEX_END, ""]), encoding="utf-8")
        before = idx.read_text(encoding="utf-8")
        st = self._save(known_mem={"a.md"}, local_mem={"a.md"})
        report = self._apply(self._plan(state=st), state=st)
        self.assertEqual(idx.read_text(encoding="utf-8"), before)   # 索引未被重寫（a.md 條目保留、不 auto write）
        self.assertTrue(any("symlink" in w for w in report.warnings))

    @_caps.needs_case_sensitive_fs
    @_caps.needs_symlink
    def test_local_deleted_casefold_symlink_alias_not_tombstoned(self):
        # e2e gate7 finding1（memory）：case-sensitive FS 上 tracked `a.md` 的 local 檔被換成 casefold-alias symlink
        # `A.md` → list_memory_files 略過、casefold 碰撞偵測只看**列出**名字亦漏 → 舊 exact-name _leaf_symlink 放行寫
        # tombstone。改 casefold 後須擋：skipped-changed、不寫 tombstone、hub 保留、a.md 留 local 基線（fail-closed）。
        self._wm(self.hA, "a.md", _mem("a"))                        # hub 真檔
        outside = self.tmp / "outside.md"
        outside.write_text(_mem("evil"), encoding="utf-8")
        (self.lA / "memory").mkdir(parents=True, exist_ok=True)
        (self.lA / "memory" / "A.md").symlink_to(outside)           # local casefold-alias symlink（大寫）
        st = self._save(known_mem={"a.md"}, local_mem={"a.md"})
        report = self._apply(self._plan(state=st), state=st)
        o = self._r(report, "a.md")
        self.assertEqual(o.action, "local-deleted")
        self.assertEqual(o.result, "skipped-changed")
        self.assertIsNone(tombstone.find_memory_tombstone(self.hA, "a.md"))  # 無抑制 tombstone
        self.assertTrue(self._mfile(self.hA, "a.md").exists())              # A3：hub 保留
        self.assertIn("a.md", self._ldmem())                        # 未 reconcile 掉 → 留基線

    @_caps.needs_symlink
    def test_local_deleted_nfd_symlink_alias_not_tombstoned(self):
        # e2e gate8：Unicode 正規化 alias——tracked NFC `café.md`、local 檔換成 **NFD** symlink `café.md`（不同 byte）
        # → list_memory_files 略過、casefold-only 不匹配 NFC/NFD → 仍寫 tombstone。normalized key（NFC+casefold）須擋。
        # 在保留 NFD 的 FS（NTFS/ext4）驗 normalized-set 分支；FS 若正規化檔名（macOS）則 symlink 落 NFC、exact 亦擋。
        nfc = unicodedata.normalize("NFC", "café.md")
        nfd = unicodedata.normalize("NFD", "café.md")
        self._wm(self.hA, nfc, _mem("cafe"))                        # hub 真檔（NFC）
        outside = self.tmp / "outside.md"
        outside.write_text(_mem("evil"), encoding="utf-8")
        (self.lA / "memory").mkdir(parents=True, exist_ok=True)
        (self.lA / "memory" / nfd).symlink_to(outside)             # local NFD symlink alias（不同 byte 於 NFC）
        st = self._save(known_mem={nfc}, local_mem={nfc})
        report = self._apply(self._plan(state=st), state=st)
        o = self._r(report, nfc)
        self.assertEqual(o.action, "local-deleted")
        self.assertEqual(o.result, "skipped-changed")               # normalized-alias guard 擋下
        self.assertIsNone(tombstone.find_memory_tombstone(self.hA, nfc))    # 無抑制 tombstone
        self.assertTrue(self._mfile(self.hA, nfc).exists())         # A3：hub 保留
        self.assertIn(nfc, self._ldmem())                           # 未 reconcile 掉 → 留基線

    def test_deletion_then_resync_suppressed_not_resurrected(self):
        self._wm(self.hA, "a.md", _mem("a"))
        st = self._save(known_mem={"a.md"}, local_mem={"a.md"})
        self._apply(self._plan(state=st), state=st)                 # run1：寫 tombstone
        st2 = state_mod.load_or_none(self.state)
        report2 = self._apply(scan.build_plan(self.local, self.hub, st2, identity_fn=_name_match), state=st2)
        self.assertEqual(self._r(report2, "a.md").action, "suppressed-deleted")
        self.assertFalse(self._mfile(self.lA, "a.md").exists())     # 未復活到 local

    # ── 安全閘：不自動寫 ─────────────────────────────────────────────────────

    def test_conflict_content_reported_not_written(self):
        self._wm(self.lA, "a.md", _mem("a", body="local"))
        self._wm(self.hA, "a.md", _mem("a", body="hub"))
        before = self._mfile(self.hA, "a.md").read_bytes()
        report = self._apply(self._plan())
        o = self._r(report, "a.md")
        self.assertEqual(o.action, "conflict-content")
        self.assertEqual(o.result, "reported")
        self.assertEqual(self._mfile(self.hA, "a.md").read_bytes(), before)

    def test_cross_file_identity_reported_not_written(self):
        # **核心閘**：同 frontmatter name、不同檔名 → conflict-cross-file-identity → 不自動雙向 copy（A14）。
        self._wm(self.lA, "foo.md", _mem("dup", body="l"))
        self._wm(self.hA, "bar.md", _mem("dup", body="h"))
        report = self._apply(self._plan())
        self.assertEqual(self._r(report, "foo.md").action, "conflict-cross-file-identity")
        self.assertEqual(self._r(report, "foo.md").result, "reported")
        self.assertEqual(self._r(report, "bar.md").result, "reported")
        self.assertFalse(self._mfile(self.hA, "foo.md").exists())   # foo 未 copy 到 hub
        self.assertFalse(self._mfile(self.lA, "bar.md").exists())   # bar 未 copy 到 local

    def test_undecidable_tombstone_blocks_copy_not_written(self):
        # identity=None 的 memory tombstone → would-copy 一律 blocked-tombstone-no-identity（不復活）→ reported。
        self._wm(self.lA, "new.md", _mem("dup"))
        tombstone.write_coverage(self.hA)
        tombstone.write_memory_tombstone(self.hA, "old.md", base_hash="0" * 64, identity=None)
        report = self._apply(scan.build_plan(self.local, self.hub,
                                             state_mod.load_or_none(self.state), identity_fn=_name_match))
        o = self._r(report, "new.md")
        self.assertEqual(o.action, "blocked-tombstone-no-identity")
        self.assertEqual(o.result, "reported")
        self.assertFalse(self._mfile(self.hA, "new.md").exists())

    def test_suppressed_not_resurrected(self):
        self._wm(self.hA, "a.md", _mem("a"))
        base = memory.content_hash(memory.load_memory(self._mfile(self.hA, "a.md")))
        tombstone.write_coverage(self.hA)
        tombstone.write_memory_tombstone(self.hA, "a.md", base_hash=base, identity="a")
        report = self._apply(scan.build_plan(self.local, self.hub,
                                             state_mod.load_or_none(self.state), identity_fn=_name_match))
        o = self._r(report, "a.md")
        self.assertEqual(o.action, "suppressed-deleted")
        self.assertEqual(o.result, "reported")
        self.assertFalse(self._mfile(self.lA, "a.md").exists())     # 未復活

    def test_damaged_source_not_copied(self):
        self._wm(self.lA, "zero.md", "")   # 0-byte → damaged
        report = self._apply(self._plan())
        o = self._r(report, "zero.md")
        self.assertEqual(o.action, "blocked-damaged-source")
        self.assertEqual(o.result, "reported")
        self.assertFalse(self._mfile(self.hA, "zero.md").exists())

    def test_blocked_no_baseline_reported(self):
        # 無 memory 基線（state 無 known_memory[projA]）→ blocked-no-baseline。
        self._wm(self.lA, "x.md", _mem("x"))
        tombstone.write_coverage(self.hA)
        st = State(known_sessions={"projA": set()}, local_sessions={"projA": set()})  # 無 memory 基線
        state_mod.save(st, self.state)
        report = self._apply(scan.build_plan(self.local, self.hub, st, identity_fn=_name_match), state=st)
        self.assertEqual(self._r(report, "x.md").action, "blocked-no-baseline")
        self.assertFalse(self._mfile(self.hA, "x.md").exists())

    def test_uninitialized_at_apply_blocks_memory(self):
        # plan 算 copy-to-hub，但 apply 時專案變 uninitialized（coverage 消失）→ F1 擋下（信任邊界）。
        self._wm(self.lA, "new.md", _mem("new"))
        plan = self._plan(coverage=True)
        (tombstone.tombstones_dir(self.hA) / tombstone.COVERAGE_FILE).unlink()
        report = self._apply(plan)
        self.assertEqual(self._r(report, "new.md").result, "blocked-uninitialized")
        self.assertFalse(self._mfile(self.hA, "new.md").exists())

    def test_bulk_local_deletion_blocked_no_tombstone(self):
        names = [f"m{i}.md" for i in range(5)]
        for n in names:
            self._wm(self.hA, n, _mem(n[:-3]))     # hub 5 個；local memory 全空
        st = self._save(known_mem=set(names), local_mem=set(names))
        report = self._apply(self._plan(state=st), state=st)
        for n in names:
            self.assertEqual(self._r(report, n).action, "blocked-bulk-local-deletion")
        self.assertEqual(list(tombstone.tombstones_dir(self.hA).glob("memory-*.deleted.json")), [])
        self.assertEqual(self._ldmem(), set(names))  # local_memory 不被未受信任現況覆蓋

    # ── C3 / 交易守衛 ────────────────────────────────────────────────────────

    def test_copy_to_local_keep_both_on_race(self):
        # C3：copy-to-local 用 O_EXCL；若 local 期間冒出同名 → keep-both（不覆蓋）。以 mock 觸發 FileExistsError 分支。
        self._wm(self.hA, "h.md", _mem("h"))
        plan = self._plan()
        real = atomicio.atomic_create_bytes
        calls = {"n": 0}

        def boom(path, data, **kw):
            # 只對第一次（copy-to-local 的 dest）模擬 race FileExistsError；keep-both 的 sibling 走真實實作。
            if calls["n"] == 0:
                calls["n"] += 1
                raise FileExistsError(path)
            return real(path, data, **kw)

        with mock.patch.object(apply_mod.atomicio, "atomic_create_bytes", boom):
            report = self._apply(plan)
        o = self._r(report, "h.md")
        self.assertEqual(o.result, "kept-both-local")
        self.assertTrue(calls["n"] >= 1)
        kept = list((self.lA / "memory").glob("h.synced-*.md"))
        self.assertEqual(len(kept), 1)                                  # 另存不碰撞 sibling
        self.assertEqual(kept[0].read_bytes(), self._mfile(self.hA, "h.md").read_bytes())

    def test_stale_copy_reclassified_skipped(self):
        # plan 算 copy-to-hub，但 apply 時 hub 也有了該檔（內容不同→conflict）→ 鎖內重分類 ≠ copy → skipped，不覆蓋 hub。
        self._wm(self.lA, "new.md", _mem("new", body="local"))
        plan = self._plan()
        self._wm(self.hA, "new.md", _mem("new", body="hub"))   # hub 期間冒出（衝突）
        before = self._mfile(self.hA, "new.md").read_bytes()
        report = self._apply(plan)
        o = self._r(report, "new.md")
        self.assertEqual(o.result, "skipped-changed")
        self.assertEqual(self._mfile(self.hA, "new.md").read_bytes(), before)  # hub 未被過期 copy 覆蓋

    def test_memory_lock_held_skips(self):
        self._wm(self.lA, "new.md", _mem("new"))
        plan = self._plan()
        tombstone.tombstones_dir(self.hA).mkdir(parents=True, exist_ok=True)
        held = atomicio.FileLock(tombstone.tombstones_dir(self.hA) / "memory").acquire()
        try:
            report = self._apply(plan, lock_timeout_s=0.2)
        finally:
            held.release()
        self.assertEqual(self._r(report, "new.md").result, "skipped-locked")
        self.assertFalse(self._mfile(self.hA, "new.md").exists())

    def test_commit_failure_is_uncommitted(self):
        # 寫檔成功但 state 未提交（state 鎖卡住）→ committed=False、had_uncommitted、CLI 非零。
        self._wm(self.lA, "new.md", _mem("new"))
        plan = self._plan()
        held = atomicio.FileLock(self.state).acquire()
        try:
            report = self._apply(plan, lock_timeout_s=0.2)
        finally:
            held.release()
        o = self._r(report, "new.md")
        self.assertEqual(o.result, "copied-to-hub")
        self.assertFalse(o.committed)
        self.assertTrue(report.had_uncommitted)
        self.assertTrue(self._mfile(self.hA, "new.md").exists())

    def test_failed_tombstone_keeps_pending_no_resurrect(self):
        self._wm(self.hA, "a.md", _mem("a"))
        st = self._save(known_mem={"a.md"}, local_mem={"a.md"})
        plan = self._plan(state=st)
        with mock.patch.object(apply_mod.tombstone, "write_memory_tombstone",
                               side_effect=OSError("disk full")):
            report = self._apply(plan, state=st)
        self.assertEqual(self._r(report, "a.md").result, "error")
        self.assertIsNone(tombstone.find_memory_tombstone(self.hA, "a.md"))
        self.assertIn("a.md", self._ldmem())   # pending 保留（不靜默遺忘→復活）

    # ── 直接打 _apply_project_memory（精準注入 halt）─────────────────────────

    def _call_mem(self, memories, *, base_fp=None, lock_timeout_s=0.2):
        report = apply_mod.ApplyReport(outcomes=[])
        pp = scan.ProjectPlan(local_dir=str(self.lA), hub_dir=str(self.hA), identity="match",
                              coverage_initialized=True, memories=memories)
        halted = apply_mod._apply_project_memory(
            pp, report=report, hub_dir=self.hA, local_dir=self.lA, project_key="projA",
            state_path=str(self.state), hub_root=self.hub,
            base_fp=base_fp if base_fp is not None else anomaly.hub_fingerprint(self.hub),
            machine="testhost", lock_timeout_s=lock_timeout_s)
        return report, halted

    # ── codex 3b2-R1 修正驗證 ────────────────────────────────────────────────

    def test_mem_copy_bytes_binds_to_src_hash(self):
        # #1：寫出 bytes 綁定到分類的 src_hash（讀一次、hash 須相符）。
        self._wm(self.lA, "x.md", _mem("x"))
        p = self._mfile(self.lA, "x.md")
        good = memory.content_hash(memory.load_memory(p))
        self.assertEqual(apply_mod._mem_copy_bytes(p, good), p.read_bytes())
        self.assertIsNone(apply_mod._mem_copy_bytes(p, "deadbeef"))            # hash 不符 → None
        self.assertIsNone(apply_mod._mem_copy_bytes(self._mfile(self.lA, "nope.md"), good))  # 讀不到

    def test_copy_plan_carries_src_hash(self):
        # copy 計畫須帶 src_hash（apply 綁定用）。
        self._wm(self.lA, "new.md", _mem("new"))
        plan = self._plan()
        mp = next(m for m in plan.projects[0].memories if m.name == "new.md")
        self.assertEqual(mp.action, "copy-to-hub")
        self.assertEqual(mp.src_hash, memory.content_hash(memory.load_memory(self._mfile(self.lA, "new.md"))))

    def test_local_deleted_undecidable_poisons_sibling_copy(self):
        # #2（核心）：local-deleted 的 hub doc 無 frontmatter name → 寫 identity=None tombstone → 毒化全專案 copy。
        # 同批 sibling copy（z.md）須在 tombstone 寫入後**重分類**被擋（不用過期 auth 復活）。
        self._wm(self.hA, "a.md", "---\ndescription: d\nmetadata:\n  type: x\n---\nbody\n")  # fm_ok 但無 name
        self._wm(self.lA, "z.md", _mem("z"))                  # 新 local memory（plan：copy-to-hub）
        st = self._save(known_mem={"a.md"}, local_mem={"a.md"})
        report = self._apply(self._plan(state=st), state=st)
        self.assertEqual(self._r(report, "a.md").result, "tombstoned-local-deletion")
        self.assertIsNone(tombstone.find_memory_tombstone(self.hA, "a.md").identity)   # identity None（毒化）
        self.assertEqual(self._r(report, "z.md").result, "skipped-changed")            # 被毒化、未復活
        self.assertFalse(self._mfile(self.hA, "z.md").exists())                        # z.md 未寫 hub

    def test_reconcile_failure_flags_reconcile_failed(self):
        # #3：reconcile 失敗 → report.reconcile_failed=True（CLI 非零）。檔已寫但基線沒落地，須促使用者重跑。
        self._wm(self.hA, "h.md", _mem("h"))
        plan = self._plan()
        with mock.patch.object(apply_mod.state_mod, "reconcile_local_memory_presence",
                               side_effect=OSError("boom")):
            report = self._apply(plan)
        self.assertEqual(self._r(report, "h.md").result, "copied-to-local")  # 檔確實寫了
        self.assertTrue(report.reconcile_failed)                             # 但標記非零

    def test_apply_memory_scan_oserror_degrades_not_crash(self):
        # e2e Pass1 Medium：apply 鎖內 memory 掃描（_plan_memories）拋一般 OSError（memory/ 變不可讀，非 symlink）→
        # degrade（skipped-changed），不逸出成 traceback（對稱 plan-time build_plan 已 catch OSError；先前只 catch
        # UnsafeMemoryDir，PermissionError 會直接崩 apply）。
        self._wm(self.hA, "h.md", _mem("h"))   # hub-only → copy-to-local（auto，觸發鎖內 _plan_memories auth）
        plan = self._plan()
        with mock.patch.object(apply_mod.scan, "_plan_memories", side_effect=PermissionError("boom")):
            report = self._apply(plan)          # 不應 raise
        self.assertEqual(self._r(report, "h.md").result, "skipped-changed")
        self.assertFalse(self._mfile(self.lA, "h.md").exists())   # 未複製（degrade）

    # ── codex 3b2 fresh-gate 修正驗證 ────────────────────────────────────────

    def test_reconcile_runs_even_with_only_conflict(self):
        # gate #1：專案只有 conflict-content（無 auto）仍須 reconcile → 把 present 的 a.md 記進 local_memory，
        # 否則前次 reconcile 失敗留下的 stale 基線永不收斂 → 使用者刪檔後下次復活。
        self._wm(self.lA, "a.md", _mem("a", body="local"))
        self._wm(self.hA, "a.md", _mem("a", body="hub"))     # conflict-content（非 auto）
        st = self._save(known_mem={"a.md"}, local_mem=set())  # 模擬前次失敗：基線缺 a.md（但 a.md 在 local 磁碟）
        report = self._apply(self._plan(state=st), state=st)
        self.assertEqual(self._r(report, "a.md").action, "conflict-content")  # 仍是衝突、不自動寫
        self.assertIn("a.md", self._ldmem())                  # reconcile 有跑 → 收斂納入 a.md

    def test_stale_baseline_converges_via_disk_tombstone(self):
        # gate #2：別台/前次留下的 memory tombstone + stale local_memory（含已刪 x.md）→ reconcile 讀磁碟**全部**
        # tombstone → x.md 從 local_memory 移除（收斂），不再永久殘留污染 bulk 簿記。x.md 兩側皆無檔、僅有 tombstone。
        tombstone.write_coverage(self.hA)
        tombstone.write_memory_tombstone(self.hA, "x.md", base_hash="0" * 64, identity="x")
        st = self._save(known_mem=set(), local_mem={"x.md"})  # stale：基線仍含 x.md
        report = self._apply(scan.build_plan(self.local, self.hub, st, identity_fn=_name_match), state=st)
        self.assertNotIn("x.md", self._ldmem())               # 收斂移除（讀磁碟 tombstone）

    def test_partial_tombstone_write_still_blocks_sibling_copy(self):
        # gate #3：local-deleted 的 tombstone「replace 後 verify 失敗」→ 已落地但回報 error；re-auth 須由
        # `if deletes`（非 tombstoned_names）觸發 → 看見落地的 identity=None tombstone → 毒化 sibling copy（不復活）。
        self._wm(self.hA, "a.md", "---\ndescription: d\nmetadata:\n  type: x\n---\nbody\n")  # 無 name → identity None
        self._wm(self.lA, "z.md", _mem("z"))                  # sibling copy-to-hub
        st = self._save(known_mem={"a.md"}, local_mem={"a.md"})
        real = tombstone.write_memory_tombstone

        def write_then_raise(project_dir, name, **kw):
            real(project_dir, name, **kw)                     # tombstone 真的落地
            raise OSError("verify failed after replace")

        with mock.patch.object(apply_mod.tombstone, "write_memory_tombstone", write_then_raise):
            report = self._apply(self._plan(state=st), state=st)
        self.assertEqual(self._r(report, "a.md").result, "error")          # 寫入回報失敗
        self.assertEqual(self._r(report, "z.md").result, "skipped-changed")  # 被落地 tombstone 毒化、未復活
        self.assertFalse(self._mfile(self.hA, "z.md").exists())            # z.md 未寫到 hub

    def test_fingerprint_change_mid_memory_apply_halts(self):
        self._wm(self.lA, "new.md", _mem("new"))
        tombstone.write_coverage(self.hA)
        report, halted = self._call_mem(
            [memory.MemoryPlan("new.md", "copy-to-hub", "local->other", "t")],
            base_fp="a-different-fingerprint")
        self.assertTrue(halted)
        self.assertFalse(self._mfile(self.hA, "new.md").exists())   # halt 前未寫


class TestApplyMemoryIndex(_MemApplyHarness, unittest.TestCase):
    """Block 3c：apply 鎖內、reconcile 後重建 local MEMORY.md 索引（§7.4 + A14）。hub 端不維護索引。"""

    def _idx(self, proj_dir):
        return proj_dir / "memory" / "MEMORY.md"

    def test_copy_to_local_creates_local_index_not_hub(self):
        self._wm(self.hA, "h.md", _mem("h", desc="hub fact"))
        report = self._apply(self._plan())
        self.assertTrue(self._mfile(self.lA, "h.md").exists())
        txt = self._idx(self.lA).read_text(encoding="utf-8")
        self.assertIn(memory.INDEX_BEGIN, txt)
        self.assertIn("- [h](h.md) — hub fact", txt)
        self.assertFalse(self._idx(self.hA).exists())   # hub 索引不維護（從不被讀、排除於 sync）
        o = self._r(report, "MEMORY.md")
        self.assertEqual(o.result, "index-created")
        self.assertFalse(report.had_error)
        self.assertFalse(report.had_uncommitted)

    def test_identical_only_still_builds_index(self):
        # 無 auto 動作（兩側 identical）也會建索引（index rebuild 無條件跑於 has_local_baseline+local_dir）。
        self._wm(self.lA, "a.md", _mem("a", desc="fact a"))
        self._wm(self.hA, "a.md", _mem("a", desc="fact a"))
        self._apply(self._plan())
        self.assertIn("- [a](a.md) — fact a", self._idx(self.lA).read_text(encoding="utf-8"))

    def test_index_unchanged_second_run_no_outcome(self):
        self._wm(self.lA, "a.md", _mem("a"))
        self._wm(self.hA, "a.md", _mem("a"))
        self._apply(self._plan())                       # 第一次建索引
        report = self._apply(self._plan())              # 第二次：已同步
        self.assertEqual([o for o in report.outcomes if o.session_id == "MEMORY.md"], [])

    def test_curated_markerless_index_preserved_with_drift_warning(self):
        mdir = self.lA / "memory"
        mdir.mkdir(parents=True, exist_ok=True)
        curated = "# 我的索引\n- [美化標題](old.md) — 手寫 hook\n"
        self._idx(self.lA).write_text(curated, encoding="utf-8")
        self._wm(self.hA, "h.md", _mem("h"))            # 新檔 → copy-to-local
        report = self._apply(self._plan())
        self.assertEqual(self._idx(self.lA).read_text(encoding="utf-8"), curated)  # 一字未改
        self.assertTrue(any("h.md" in w and "未列入索引" in w for w in report.warnings))
        self.assertEqual([o for o in report.outcomes if o.session_id == "MEMORY.md"], [])  # 無寫入 outcome

    def test_autoblock_index_rebuilt_preserving_surrounds(self):
        mdir = self.lA / "memory"
        mdir.mkdir(parents=True, exist_ok=True)
        ex = ("# Memory Index\n前言保留\n\n" + memory.INDEX_BEGIN + "\n" + memory.INDEX_END
              + "\n後記保留\n")
        self._idx(self.lA).write_text(ex, encoding="utf-8")
        self._wm(self.hA, "h.md", _mem("h", desc="hub fact"))
        report = self._apply(self._plan())
        txt = self._idx(self.lA).read_text(encoding="utf-8")
        self.assertIn("- [h](h.md) — hub fact", txt)
        self.assertTrue(txt.startswith("# Memory Index\n前言保留\n\n"))
        self.assertTrue(txt.endswith("後記保留\n"))
        self.assertEqual(self._r(report, "MEMORY.md").result, "index-rebuilt")

    def test_local_deletion_drops_entry_from_autoblock(self):
        # local 刪檔 → 寫 tombstone（不刪 hub）；local 索引（auto-block）重建後不再列該檔。
        self._wm(self.hA, "a.md", _mem("a"))            # hub 有；local 無（已刪）
        mdir = self.lA / "memory"; mdir.mkdir(parents=True, exist_ok=True)
        self._idx(self.lA).write_text(
            memory.INDEX_BEGIN + "\n- [a](a.md) — d\n" + memory.INDEX_END + "\n", encoding="utf-8")
        st = self._save(known_mem={"a.md"}, local_mem={"a.md"})
        self._apply(self._plan(state=st), state=st)
        txt = self._idx(self.lA).read_text(encoding="utf-8")
        self.assertNotIn("a.md", txt)                   # local 端已無此檔 → 索引移除
        self.assertTrue(self._mfile(self.hA, "a.md").exists())  # hub 仍在（A3）

    def test_no_index_when_no_local_baseline(self):
        # migration fail-closed（有 known_memory 無 local_memory）→ 不寫索引（與 reconcile 同閘）。
        st = State(known_sessions={"projA": set()}, known_memory={"projA": set()})  # local_memory 缺 projA
        state_mod.save(st, self.state)
        self._wm(self.hA, "h.md", _mem("h"))
        self._apply(self._plan(state=st), state=st)
        self.assertFalse(self._idx(self.lA).exists())

    def test_index_write_failure_is_warning_not_fatal(self):
        # 索引寫入失敗 → 警告、不 set reconcile_failed、不影響 memory 檔本身（便利性非安全性質）。建新路徑走
        # atomic_create_bytes（亦供 copy-to-local 用）→ 只攔 MEMORY.md。
        self._wm(self.hA, "h.md", _mem("h"))
        real = atomicio.atomic_create_bytes

        def fail_index(path, data, **kw):
            if Path(path).name == memory.INDEX_FILE:
                raise atomicio.AtomicWriteError("disk full")
            return real(path, data, **kw)

        with mock.patch.object(apply_mod.atomicio, "atomic_create_bytes", side_effect=fail_index):
            report = self._apply(self._plan())
        self.assertTrue(self._mfile(self.lA, "h.md").exists())    # memory 檔仍寫成
        self.assertFalse(self._idx(self.lA).exists())             # 索引未落地
        self.assertFalse(report.reconcile_failed)                 # 不影響 exit code
        self.assertTrue(any("索引重建失敗" in w for w in report.warnings))

    def test_index_changed_between_read_and_write_skips(self):
        # TOCTOU 守衛：分類用的讀取與寫前重讀不一致（外部改動）→ 略過寫入、警告、不覆蓋。bytes 比對。
        # 索引讀走 _read_index_bytes_nofollow（os.open/read，非 Path.read_bytes）→ 攔該函式。
        self._wm(self.hA, "h.md", _mem("h"))
        n = {"i": 0}

        def fake(p):
            n["i"] += 1
            if n["i"] == 1:
                raise FileNotFoundError                          # cur_bytes = None（看似缺檔）
            return "EXTERNAL 手寫，外部剛建立\n".encode("utf-8")     # 重讀：已被外部寫入 → 與 cur_bytes 不符

        with mock.patch.object(apply_mod, "_read_index_bytes_nofollow", side_effect=fake):
            report = self._apply(self._plan())
        self.assertTrue(self._mfile(self.lA, "h.md").exists())
        self.assertTrue(any("索引重建期間被改動" in w for w in report.warnings))
        self.assertEqual([o for o in report.outcomes if o.session_id == "MEMORY.md"], [])

    def test_apply_preserves_crlf_outside_block(self):
        # codex R1 High：框外 CRLF 必須逐字保留（bytes 讀寫，非 text mode 正規化成 LF）。
        mdir = self.lA / "memory"
        mdir.mkdir(parents=True, exist_ok=True)
        ex = ("# Memory Index\r\n前言\r\n\r\n" + memory.INDEX_BEGIN + "\r\n" + memory.INDEX_END
              + "\r\n後記\r\n")
        self._idx(self.lA).write_bytes(ex.encode("utf-8"))
        self._wm(self.hA, "h.md", _mem("h", desc="hub fact"))
        self._apply(self._plan())
        raw = self._idx(self.lA).read_bytes()
        self.assertTrue(raw.startswith("# Memory Index\r\n前言\r\n\r\n".encode("utf-8")))  # 框前 CRLF 保留
        self.assertTrue(raw.endswith("後記\r\n".encode("utf-8")))                          # 框後 CRLF 保留
        self.assertIn("- [h](h.md) — hub fact\r\n".encode("utf-8"), raw)                  # 框內亦用 CRLF

    def test_index_create_race_uses_o_excl_no_overwrite(self):
        # codex fresh gate r4 High：建新路徑用 atomic_create_bytes（O_EXCL）；窗內冒出檔 → FileExistsError → 略過。
        self._wm(self.hA, "h.md", _mem("h"))
        real = atomicio.atomic_create_bytes

        def race(path, data, **kw):
            if Path(path).name == memory.INDEX_FILE:
                raise FileExistsError("appeared in window")
            return real(path, data, **kw)

        with mock.patch.object(apply_mod.atomicio, "atomic_create_bytes", side_effect=race):
            report = self._apply(self._plan())
        self.assertTrue(self._mfile(self.lA, "h.md").exists())       # copy-to-local 仍成
        self.assertTrue(any("索引建立期間被建立" in w for w in report.warnings))
        self.assertEqual([o for o in report.outcomes if o.session_id == "MEMORY.md"], [])

    def test_read_index_nofollow_rejects_symlink_and_fifo(self):
        # codex fresh gate r5 Medium：no-follow 讀普通檔 OK；symlink→ELOOP；FIFO/特殊檔→S_ISREG 拒（不卡死）。
        import os
        d = self.tmp / "nf"
        d.mkdir()
        reg = d / "reg.md"
        reg.write_text("hello", encoding="utf-8")
        self.assertEqual(apply_mod._read_index_bytes_nofollow(reg), b"hello")
        link = d / "link.md"
        try:
            link.symlink_to(reg)
        except (OSError, NotImplementedError):
            self.skipTest("symlink 不支援")
        with self.assertRaises(OSError):
            apply_mod._read_index_bytes_nofollow(link)
        if hasattr(os, "mkfifo"):
            fifo = d / "fifo.md"
            os.mkfifo(fifo)
            with self.assertRaises(OSError):
                apply_mod._read_index_bytes_nofollow(fifo)

    def test_symlink_memory_root_index_not_followed(self):
        # codex fresh gate r4 Medium：memory/ 根為 symlink → 索引步驟不跟隨去讀/寫外部 MEMORY.md。
        external = self.tmp / "external_mem"
        external.mkdir()
        (external / memory.INDEX_FILE).write_text("EXTERNAL index — 不可動\n", encoding="utf-8")
        try:
            (self.lA / "memory").symlink_to(external, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink 不支援")
        self._wm(self.hA, "h.md", _mem("h"))
        report = self._apply(self._plan())                            # 不得崩
        self.assertEqual((external / memory.INDEX_FILE).read_text(encoding="utf-8"),
                         "EXTERNAL index — 不可動\n")                  # 外部索引未被讀寫覆蓋
        self.assertEqual([o for o in report.outcomes if o.session_id == "MEMORY.md"], [])

    def test_symlink_memory_md_skipped_not_clobbered(self):
        # codex fresh gate Medium：MEMORY.md 為 symlink → 不跟隨、不覆蓋（rename 會把 symlink 換成普通檔）。
        mdir = self.lA / "memory"
        mdir.mkdir(parents=True, exist_ok=True)
        target = self.tmp / "external_index.md"
        target.write_text("EXTERNAL user-managed index\n", encoding="utf-8")
        link = mdir / memory.INDEX_FILE
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink 不支援")
        self._wm(self.hA, "h.md", _mem("h"))
        report = self._apply(self._plan())
        self.assertTrue(link.is_symlink())                                       # 仍是 symlink
        self.assertEqual(target.read_text(encoding="utf-8"), "EXTERNAL user-managed index\n")  # 目標未動
        self.assertTrue(any("symlink" in w for w in report.warnings))
        self.assertEqual([o for o in report.outcomes if o.session_id == "MEMORY.md"], [])

    @_caps.needs_junction
    def test_junction_memory_root_index_followed(self):
        # CLAUDE_CONFIG_DIR 模型：memory/ 根為 **junction**（使用者刻意同機共用）→ 索引步驟**跟隨**、照常維護共用
        # 索引（對比 symlink：拒絕）。junction 由 OS 透明跟隨；`_is_symlink_noraise`/`list_memory_files` 只擋 symlink。
        external = self.tmp / "external_mem"
        external.mkdir()
        _caps.make_junction(self.lA / "memory", external)            # mklink /J，免權限、限同機
        self._wm(self.hA, "h.md", _mem("h"))                         # hub 有、local 無 → copy-to-local（經 junction 寫入）
        report = self._apply(self._plan())                           # 不得崩
        self.assertTrue((external / "h.md").is_file())               # 已 copy 進真實共用夾（跟隨 junction）
        idx = external / memory.INDEX_FILE
        self.assertTrue(idx.is_file())                               # 索引建在共用夾（跟隨、未跳過）
        self.assertIn("h.md", idx.read_text(encoding="utf-8"))       # 列入共用記憶
        self.assertTrue(any(o.session_id == "MEMORY.md" for o in report.outcomes))   # 有索引動作（非跳過）
        self.assertFalse(any("symlink" in w for w in report.warnings))               # 無 symlink 跳過警告

    @_caps.needs_junction
    def test_dangling_junction_memory_root_no_false_deletion(self):
        # fresh gate ccdir-g1 High：memory/ 是 junction 但**目標離線/被刪** → 不可當「使用者刪光 memory」→ 絕不寫抑制
        # tombstone（單一 known memory 時 bulk guard 不觸發）。list_memory_files 對 dangling junction raise
        # UnsafeMemoryDir → build_plan memory_scan_failed、reconcile 略過，皆不誤判刪除。
        import shutil
        external = self.tmp / "external_mem"
        external.mkdir()
        (external / "k.md").write_text(_mem("k"), encoding="utf-8")
        _caps.make_junction(self.lA / "memory", external)
        self._wm(self.hA, "k.md", _mem("k"))                  # hub 也有 k.md
        self._save(known_mem={"k.md"}, local_mem={"k.md"})    # 基線：projA 已知 k.md（兩側都有過）
        shutil.rmtree(external)                               # 目標離線 → junction 變 dangling
        plan = self._plan()
        report = self._apply(plan)
        tombs = {t for (k, t) in tombstone.read_tombstones(self.hA) if k == "memory"}
        self.assertNotIn("k.md", tombs)                       # 不得抑制有效 memory（A3 + 不誤判刪除）
        self.assertTrue(any(p.memory_scan_failed for p in plan.projects))   # dangling → scan_failed（非空夾）

    def test_damaged_memory_aborts_index_keeps_existing(self):
        # codex R1 Medium：indexed 檔損壞（0-byte）→ 中止重建、保留現有索引、警告（不把損壞檔列成有效條目）。
        mdir = self.lA / "memory"
        mdir.mkdir(parents=True, exist_ok=True)
        prior = memory.INDEX_BEGIN + "\n- [a](a.md) — d\n" + memory.INDEX_END + "\n"
        self._idx(self.lA).write_text(prior, encoding="utf-8")
        (self.lA / "memory" / "a.md").write_text(_mem("a"), encoding="utf-8")
        (self.lA / "memory" / "broken.md").write_text("", encoding="utf-8")  # 0-byte → damaged
        st = self._save(known_mem={"a.md"}, local_mem={"a.md"})
        report = self._apply(self._plan(state=st), state=st)
        self.assertEqual(self._idx(self.lA).read_text(encoding="utf-8"), prior)  # 索引未動
        self.assertTrue(any("損壞" in w and "broken.md" in w for w in report.warnings))


if __name__ == "__main__":
    unittest.main()
