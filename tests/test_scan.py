import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_session_sync import scan, tombstone
from tests import _caps, fixtures as fx


def _name_match(local_dir, hub_dirs):
    for hd in hub_dirs:
        if hd.name == local_dir.name:
            return ("match", hd)
    return ("needs-map", None)


def _mem(slug="fact", body="hello"):
    return "\n".join(["---", f"name: {slug}", "description: d",
                      "metadata:", "  type: project", "---", body, ""])


def _write_mem(proj_dir, fname, text):
    mdir = proj_dir / "memory"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / fname).write_text(text, encoding="utf-8")


class TestDefaultLocalRoot(unittest.TestCase):
    """`scan.default_local_root`：CLAUDE_CONFIG_DIR 決定設定根（多帳號/非預設位置，與 Claude Code 一致）；
    未設或空 → 預設 `~/.claude`。皆接 `/projects`。junction 由 OS 透明跟隨、不在此函式偵測（信任使用者指定位置）。"""

    def test_uses_claude_config_dir_when_set(self):
        cfg = str(Path("X") / ".claude-acct2")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}):
            self.assertEqual(scan.default_local_root(), Path(cfg) / "projects")

    def test_defaults_to_home_claude_when_unset(self):
        with mock.patch.dict(os.environ):           # patch.dict 進出自動還原
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertEqual(scan.default_local_root(), Path.home() / ".claude" / "projects")

    def test_empty_env_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""}):
            self.assertEqual(scan.default_local_root(), Path.home() / ".claude" / "projects")


class TestPlanProjectPair(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.ld = self.tmp / "local"
        self.hd = self.tmp / "hub"
        self.ld.mkdir()
        self.hd.mkdir()

    def tearDown(self):
        self._td.cleanup()

    def _actions(self, plans):
        return {p.session_id: p.action for p in plans}

    def test_paired_identical(self):
        fx.write_jsonl(fx.linear(), str(self.ld / "s1.jsonl"))
        fx.write_jsonl(fx.linear(), str(self.hd / "s1.jsonl"))
        a = self._actions(scan.plan_project_pair(self.ld, self.hd, coverage_initialized=True))
        self.assertEqual(a["s1"], "identical")

    def test_paired_fast_forward(self):
        fx.write_jsonl(fx.linear(), str(self.ld / "s1.jsonl"))
        fx.write_jsonl(fx.fast_forward_of_linear(), str(self.hd / "s1.jsonl"))
        a = self._actions(scan.plan_project_pair(self.ld, self.hd, coverage_initialized=True))
        self.assertEqual(a["s1"], "fast-forward")

    def test_single_side_copy_when_initialized(self):
        fx.write_jsonl(fx.linear(), str(self.ld / "only_local.jsonl"))
        fx.write_jsonl(fx.linear(), str(self.hd / "only_hub.jsonl"))
        a = self._actions(scan.plan_project_pair(self.ld, self.hd, coverage_initialized=True))
        self.assertEqual(a["only_local"], "copy-to-hub")
        self.assertEqual(a["only_hub"], "copy-to-local")

    def test_single_side_blocked_when_uninitialized(self):
        fx.write_jsonl(fx.linear(), str(self.hd / "only_hub.jsonl"))
        a = self._actions(scan.plan_project_pair(self.ld, self.hd, coverage_initialized=False))
        self.assertEqual(a["only_hub"], "blocked-uninitialized")

    def _tomb(self, target, base_hash):
        return {("session", target): tombstone.Tombstone(
            kind="session", target=target, base_hash=base_hash, machine="m", time="t")}

    def test_single_side_suppressed_when_unchanged(self):
        # 條件式 suppress：現存內容 raw == base → 抑制復活（A3）。
        p = self.hd / "deleted.jsonl"
        fx.write_jsonl(fx.linear(), str(p))
        a = self._actions(scan.plan_project_pair(
            self.ld, self.hd, coverage_initialized=True,
            tombs=self._tomb("deleted", tombstone.raw_file_digest(p))))
        self.assertEqual(a["deleted"], "suppressed-deleted")

    def test_single_side_conflict_when_modified_after_delete(self):
        # 刪除後內容又被改（raw != base）→ delete-vs-update → conflict（不復活也不丟更新）。
        fx.write_jsonl(fx.linear(), str(self.hd / "deleted.jsonl"))
        a = self._actions(scan.plan_project_pair(
            self.ld, self.hd, coverage_initialized=True,
            tombs=self._tomb("deleted", "0" * 64)))
        self.assertEqual(a["deleted"], "conflict-delete-vs-update")

    def test_tombstone_base_none_is_conflict(self):
        # base 不明 → 無法確認 == base → 保守轉 conflict（不靜默 suppress 可能的新工作）。
        fx.write_jsonl(fx.linear(), str(self.hd / "deleted.jsonl"))
        a = self._actions(scan.plan_project_pair(
            self.ld, self.hd, coverage_initialized=True, tombs=self._tomb("deleted", None)))
        self.assertEqual(a["deleted"], "conflict-delete-vs-update")

    def test_both_present_identical_to_base_suppressed(self):
        # 兩側都在且都 byte-identical 於 base → suppress。
        fx.write_jsonl(fx.linear(), str(self.ld / "s1.jsonl"))
        fx.write_jsonl(fx.linear(), str(self.hd / "s1.jsonl"))
        a = self._actions(scan.plan_project_pair(
            self.ld, self.hd, coverage_initialized=True,
            tombs=self._tomb("s1", tombstone.raw_file_digest(self.hd / "s1.jsonl"))))
        self.assertEqual(a["s1"], "suppressed-deleted")

    def test_both_present_divergent_is_conflict(self):
        # 兩側都在但不一致（ff）→ 不可能都 ==base → conflict（r14-1 不復活；且揭露刪除衝突）。
        fx.write_jsonl(fx.fast_forward_of_linear(), str(self.ld / "s1.jsonl"))
        fx.write_jsonl(fx.linear(), str(self.hd / "s1.jsonl"))
        a = self._actions(scan.plan_project_pair(
            self.ld, self.hd, coverage_initialized=True,
            tombs=self._tomb("s1", tombstone.raw_file_digest(self.hd / "s1.jsonl"))))
        self.assertEqual(a["s1"], "conflict-delete-vs-update")

    # ── local-presence 對稱刪除偵測（P1c）────────────────────────────────────

    def test_hub_only_in_local_known_is_local_deleted(self):
        # hub 有、local 無、且 sid 曾在本機 local（∈local_known）→ 本機刪除 → local-deleted。
        fx.write_jsonl(fx.linear(), str(self.hd / "s1.jsonl"))  # local (self.ld) 刻意為空
        a = self._actions(scan.plan_project_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True, local_known={"s1"}))
        self.assertEqual(a["s1"], "local-deleted")

    def test_hub_only_not_in_local_known_copies_to_local(self):
        # hub 有、local 無、sid 不在 local_known → 真新 hub 檔 → copy-to-local（非 local-deleted）。
        fx.write_jsonl(fx.linear(), str(self.hd / "newhub.jsonl"))
        a = self._actions(scan.plan_project_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            local_known={"other"}))
        self.assertEqual(a["newhub"], "copy-to-local")

    def test_migration_none_local_known_copies_not_deletes(self):
        # 舊 state（local_known=None）→ 不可誤判 local-deleted（會抑制真檔）→ 退回 copy-to-local。
        fx.write_jsonl(fx.linear(), str(self.hd / "s1.jsonl"))
        a = self._actions(scan.plan_project_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True, local_known=None))
        self.assertEqual(a["s1"], "copy-to-local")

    def test_bulk_local_disappearance_blocks_not_tombstones(self):
        # local_known 5 個、local 全空（100% 消失）→ bulk guard → 全 blocked-bulk-local-deletion。
        sids = {f"s{i}" for i in range(5)}
        for s in sids:
            fx.write_jsonl(fx.linear(), str(self.hd / f"{s}.jsonl"))
        a = self._actions(scan.plan_project_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True, local_known=sids))
        self.assertTrue(all(v == "blocked-bulk-local-deletion" for v in a.values()), a)

    def test_single_local_deletion_below_bulk_threshold(self):
        # 5 個 local_known、只 1 個消失（20% < 60%）→ 非 bulk → 該檔 local-deleted、其餘 identical。
        sids = {f"s{i}" for i in range(5)}
        for s in sids:
            fx.write_jsonl(fx.linear(), str(self.hd / f"{s}.jsonl"))
        for s in ("s0", "s1", "s2", "s3"):  # local 仍有 4 個，s4 被刪
            fx.write_jsonl(fx.linear(), str(self.ld / f"{s}.jsonl"))
        a = self._actions(scan.plan_project_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True, local_known=sids))
        self.assertEqual(a["s4"], "local-deleted")
        self.assertEqual(a["s0"], "identical")

    def test_local_deleted_requires_baseline(self):
        # 無本機基線（has_baseline=False）→ 連 local-deleted 都不判，回 blocked-no-baseline（避免無基準誤判）。
        fx.write_jsonl(fx.linear(), str(self.hd / "s1.jsonl"))
        a = self._actions(scan.plan_project_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=False, local_known={"s1"}))
        self.assertEqual(a["s1"], "blocked-no-baseline")

    def test_is_bulk_local_deletion_thresholds(self):
        f = scan.is_bulk_local_deletion
        self.assertFalse(f(None, set()))                                  # None
        self.assertFalse(f(set(), set()))                                 # 空
        # (a) 掛載無法確認：≥2 known 全不在現況（含現況為別碟檔的零交集）→ 即便 <project_min 也擋
        self.assertTrue(f({"a", "b", "c"}, set()))                        # 3 個全消失
        self.assertTrue(f({"a", "b"}, {"x", "y"}))                        # 現況是別碟檔（零交集）
        self.assertFalse(f({"a"}, set()))                                 # 單一 session 刪到空 → 信任（floor=2）
        self.assertFalse(f({"a", "b", "c"}, {"a"}))                       # 掛載已確認(a 仍在)+樣本<4 → 信任部分刪除
        # (b) 大量比例消失（樣本夠大、掛載已確認）
        self.assertTrue(f({"a", "b", "c", "d", "e"}, {"a", "b"}))         # 3/5=0.6（邊界含）
        self.assertFalse(f({"a", "b", "c", "d", "e"}, {"a", "b", "c"}))   # 2/5=0.4

    def test_no_local_baseline_blocks_hub_single_side(self):
        # has_local_baseline=False（migration：有 known、無 local_sessions[pk]）→ present=hub fail-closed。
        fx.write_jsonl(fx.linear(), str(self.hd / "s1.jsonl"))
        a = self._actions(scan.plan_project_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            local_known=None, has_local_baseline=False))
        self.assertEqual(a["s1"], "blocked-no-local-baseline")


class TestBuildPlan(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.local_root = self.tmp / "local"
        self.hub_root = self.tmp / "hub"
        self.local_root.mkdir()
        self.hub_root.mkdir()

    def tearDown(self):
        self._td.cleanup()

    def test_matched_project_classifies(self):
        (self.local_root / "projA").mkdir()
        (self.hub_root / "projA").mkdir()
        fx.write_jsonl(fx.linear(), str(self.local_root / "projA" / "s1.jsonl"))
        fx.write_jsonl(fx.linear(), str(self.hub_root / "projA" / "s1.jsonl"))
        plan = scan.build_plan(self.local_root, self.hub_root, state=None, identity_fn=_name_match)
        self.assertTrue(plan.first_run)
        self.assertFalse(plan.halt)
        self.assertEqual(len(plan.projects), 1)
        pp = plan.projects[0]
        self.assertEqual(pp.identity, "match")
        self.assertEqual(pp.sessions[0].action, "identical")

    def test_hub_only_initialized_is_blocked_not_copy(self):
        # 即使 coverage initialized，hub-only（無 local 綁定）也不可 copy-to-local（C-r6-2）
        (self.hub_root / "orphan").mkdir()
        fx.write_jsonl(fx.linear(), str(self.hub_root / "orphan" / "s1.jsonl"))
        tombstone.write_coverage(self.hub_root / "orphan", epoch=1)
        plan = scan.build_plan(self.local_root, self.hub_root, state=None, identity_fn=_name_match)
        pp = next(p for p in plan.projects if p.identity == "hub-only")
        self.assertEqual(pp.sessions[0].action, "blocked-unmapped")

    def test_multi_cwd_local_blocked(self):
        # 同一 local 夾混入兩個 cwd → 不可挑第一個配對 → blocked-multi-cwd（C-r6-1）
        px = self.local_root / "projX"
        px.mkdir()
        fx.write_jsonl([fx.umsg("a1", None, "user", 1, cwd="/home/a")], str(px / "s1.jsonl"))
        fx.write_jsonl([fx.umsg("b1", None, "user", 1, cwd="/home/b")], str(px / "s2.jsonl"))
        plan = scan.build_plan(self.local_root, self.hub_root, state=None)  # 預設 git identity
        pp = next(p for p in plan.projects if p.local_dir and p.local_dir.endswith("projX"))
        self.assertEqual(pp.identity, "blocked-multi-cwd")
        self.assertTrue(all(s.action == "blocked-unmapped" for s in pp.sessions))

    @_caps.needs_case_sensitive_fs
    def test_casefold_duplicate_sid_blocked(self):
        ld = self.tmp / "cf_local"
        hd = self.tmp / "cf_hub"
        ld.mkdir()
        hd.mkdir()
        fx.write_jsonl(fx.linear(), str(ld / "ABC.jsonl"))
        fx.write_jsonl(fx.linear(), str(ld / "abc.jsonl"))
        plans = scan.plan_project_pair(ld, hd, coverage_initialized=True)
        self.assertTrue(all(p.action == "blocked-casefold-collision" for p in plans))

    def test_binding_resolves_identity(self):
        # state 綁定（A17.4，由 bootstrap/--map 寫）優先於 git 指紋。
        from claude_session_sync.state import State
        (self.local_root / "projL").mkdir()
        (self.hub_root / "encH").mkdir()
        fx.write_jsonl([fx.umsg("u1", None, "user", 1, cwd="/work/L")],
                       str(self.local_root / "projL" / "s1.jsonl"))
        st = State(bindings={"/work/L": "encH"})
        plan = scan.build_plan(self.local_root, self.hub_root, st)
        pp = next(p for p in plan.projects if p.local_dir and p.local_dir.endswith("projL"))
        self.assertEqual(pp.identity, "match")
        self.assertTrue(pp.hub_dir.endswith("encH"))

    def test_binding_to_missing_hub_is_needs_map(self):
        from claude_session_sync.state import State
        (self.local_root / "projL").mkdir()
        fx.write_jsonl([fx.umsg("u1", None, "user", 1, cwd="/work/L")],
                       str(self.local_root / "projL" / "s1.jsonl"))
        st = State(bindings={"/work/L": "gone"})  # 綁定的 hub 夾不在
        plan = scan.build_plan(self.local_root, self.hub_root, st)
        pp = next(p for p in plan.projects if p.local_dir and p.local_dir.endswith("projL"))
        self.assertEqual(pp.identity, "needs-map")  # 不憑空配對

    def test_empty_local_dir_matched_via_dir_binding_detects_deletion(self):
        # codex r25：session 全刪 → 空 local 夾無 cwd → 靠 local_dir_bindings 配對 → 仍偵測對稱刪除。
        from claude_session_sync.state import State
        (self.local_root / "projL").mkdir()                       # 空夾（唯一 session 已刪）
        (self.hub_root / "encH").mkdir()
        fx.write_jsonl(fx.linear(), str(self.hub_root / "encH" / "s1.jsonl"))
        tombstone.write_coverage(self.hub_root / "encH")
        st = State(known_sessions={"encH": {"s1"}}, local_sessions={"encH": {"s1"}},
                   local_dir_bindings={"projL": "encH"})
        plan = scan.build_plan(self.local_root, self.hub_root, st)
        pp = next(p for p in plan.projects if p.local_dir and p.local_dir.endswith("projL"))
        self.assertEqual(pp.identity, "match")
        self.assertEqual(pp.sessions[0].action, "local-deleted")  # 單一 session 刪到空 → 信任本機刪除

    def test_dir_binding_only_for_truly_empty_dir(self):
        # codex r26-1：有檔但讀不到 cwd（session 無 cwd 欄位）→ **不可**用夾名綁定誤配 → needs-map。
        from claude_session_sync.state import State
        px = self.local_root / "projL"
        px.mkdir()
        fx.write_jsonl([fx.umsg("u1", None, "user", 1)], str(px / "s1.jsonl"))  # 有檔、無 cwd
        (self.hub_root / "encH").mkdir()
        st = State(local_dir_bindings={"projL": "encH"})
        plan = scan.build_plan(self.local_root, self.hub_root, st)
        pp = next(p for p in plan.projects if p.local_dir and p.local_dir.endswith("projL"))
        self.assertEqual(pp.identity, "needs-map")   # 有檔但無 cwd → 不夾名誤配

    def test_empty_local_dir_multi_session_blocked_bulk(self):
        # 空夾且曾有 ≥2 session → 掛載無法確認（present 空）→ blocked-bulk（不自動寫 tombstone）。
        from claude_session_sync.state import State
        (self.local_root / "projL").mkdir()
        (self.hub_root / "encH").mkdir()
        for s in ("s1", "s2"):
            fx.write_jsonl(fx.linear(), str(self.hub_root / "encH" / f"{s}.jsonl"))
        tombstone.write_coverage(self.hub_root / "encH")
        st = State(known_sessions={"encH": {"s1", "s2"}}, local_sessions={"encH": {"s1", "s2"}},
                   local_dir_bindings={"projL": "encH"})
        plan = scan.build_plan(self.local_root, self.hub_root, st)
        pp = next(p for p in plan.projects if p.local_dir and p.local_dir.endswith("projL"))
        self.assertTrue(all(s.action == "blocked-bulk-local-deletion" for s in pp.sessions), pp.sessions)

    def test_halt_on_missing_mount(self):
        plan = scan.build_plan(self.local_root, self.tmp / "nohub", state=None)
        self.assertTrue(plan.halt)
        self.assertEqual(plan.projects, [])

    def test_format_plan_smoke(self):
        (self.local_root / "projA").mkdir()
        (self.hub_root / "projA").mkdir()
        fx.write_jsonl(fx.linear(), str(self.local_root / "projA" / "s1.jsonl"))
        fx.write_jsonl(fx.fast_forward_of_linear(), str(self.hub_root / "projA" / "s1.jsonl"))
        plan = scan.build_plan(self.local_root, self.hub_root, state=None, identity_fn=_name_match)
        text = scan.format_plan(plan)
        self.assertIn("fast-forward", text)

    # ── P1d Block 3b-1：memory 進 build_plan / format_plan（read-only）─────────

    def test_build_plan_includes_memory_identical(self):
        (self.local_root / "projA").mkdir()
        (self.hub_root / "projA").mkdir()
        _write_mem(self.local_root / "projA", "a.md", _mem("a"))
        _write_mem(self.hub_root / "projA", "a.md", _mem("a"))
        plan = scan.build_plan(self.local_root, self.hub_root, state=None, identity_fn=_name_match)
        pp = plan.projects[0]
        self.assertEqual({m.name: m.action for m in pp.memories}, {"a.md": "identical"})

    def test_build_plan_memory_copy_with_state_baseline(self):
        from claude_session_sync.state import State
        (self.local_root / "projA").mkdir()
        (self.hub_root / "projA").mkdir()
        tombstone.write_coverage(self.hub_root / "projA")
        _write_mem(self.local_root / "projA", "new.md", _mem("new"))  # local-only 新 memory
        st = State(known_sessions={"projA": set()}, local_sessions={"projA": set()},
                   known_memory={"projA": set()}, local_memory={"projA": set()})
        plan = scan.build_plan(self.local_root, self.hub_root, st, identity_fn=_name_match)
        pp = plan.projects[0]
        self.assertEqual({m.name: m.action for m in pp.memories}, {"new.md": "copy-to-hub"})

    def test_build_plan_memory_blocked_no_baseline_without_state(self):
        # 無 memory 基線（state 無 known_memory[pk]）→ 單邊 memory blocked-no-baseline（fail-closed）。
        from claude_session_sync.state import State
        (self.local_root / "projA").mkdir()
        (self.hub_root / "projA").mkdir()
        tombstone.write_coverage(self.hub_root / "projA")
        _write_mem(self.hub_root / "projA", "h.md", _mem("h"))
        st = State(known_sessions={"projA": set()}, local_sessions={"projA": set()})  # 無 memory 基線
        plan = scan.build_plan(self.local_root, self.hub_root, st, identity_fn=_name_match)
        pp = plan.projects[0]
        self.assertEqual({m.name: m.action for m in pp.memories}, {"h.md": "blocked-no-baseline"})

    @_caps.needs_symlink
    def test_build_plan_unsafe_memory_dir_noted_not_crash(self):
        # memory/ 根是 symlink → 不崩、記憶計畫為空、加 note（session 計畫照常）。
        (self.local_root / "projA").mkdir()
        (self.hub_root / "projA").mkdir()
        fx.write_jsonl(fx.linear(), str(self.local_root / "projA" / "s1.jsonl"))
        fx.write_jsonl(fx.linear(), str(self.hub_root / "projA" / "s1.jsonl"))
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.local_root / "projA" / "memory").symlink_to(elsewhere, target_is_directory=True)
        plan = scan.build_plan(self.local_root, self.hub_root, state=None, identity_fn=_name_match)
        pp = plan.projects[0]
        self.assertEqual(pp.memories, [])
        self.assertTrue(any("symlink" in n for n in pp.notes))
        self.assertEqual(pp.sessions[0].action, "identical")  # session 照常

    @_caps.needs_symlink
    def test_build_plan_escaping_local_dir_skipped_unsafe(self):
        # e2e gate G-High：local 專案夾是逃出 local_root 的 symlink → skipped-unsafe（可見）、不讀其內容、不同步
        #（否則主 sync 會把界外 session copy 進 hub＝洩漏）。
        outside = self.tmp / "outside"
        outside.mkdir()
        fx.write_jsonl(fx.linear(), str(outside / "s1.jsonl"))     # root 外「私密」session
        (self.local_root / "evil").symlink_to(outside, target_is_directory=True)
        plan = scan.build_plan(self.local_root, self.hub_root, state=None, identity_fn=_name_match)
        pp = next(p for p in plan.projects if p.local_dir and p.local_dir.endswith("evil"))
        self.assertEqual(pp.identity, "skipped-unsafe")
        self.assertEqual(pp.sessions, [])           # 未讀逃逸夾內容（不列 s1）
        self.assertEqual(pp.memories, [])

    @_caps.needs_junction
    def test_build_plan_in_root_junction_allowed(self):
        # ccdir 多帳號不被誤擋：local_root **內**的 junction（resolve 仍在 root 內）→ 照常同步、非 skipped-unsafe。
        real = self.local_root / "real"
        real.mkdir()
        fx.write_jsonl(fx.linear(), str(real / "s1.jsonl"))
        _caps.make_junction(self.local_root / "projA", real)       # local_root/projA junction → local_root/real（root 內）
        (self.hub_root / "projA").mkdir()
        fx.write_jsonl(fx.linear(), str(self.hub_root / "projA" / "s1.jsonl"))
        plan = scan.build_plan(self.local_root, self.hub_root, state=None, identity_fn=_name_match)
        pp = next(p for p in plan.projects if p.local_dir and p.local_dir.endswith("projA"))
        self.assertNotEqual(pp.identity, "skipped-unsafe")         # in-root junction 允許（透明跟隨）
        self.assertEqual(pp.sessions[0].action, "identical")       # 正常同步

    @_caps.needs_symlink
    def test_session_leaf_symlink_excluded_no_leak(self):
        # e2e gate2 #2：安全專案夾內的 `secret.jsonl` 是指向夾外檔的 symlink → `_session_files` 略過 → 不列為 session、
        # 不分類、不 copy 進 hub（洩漏防線）。真實 session 不受影響。
        (self.local_root / "projA").mkdir()
        (self.hub_root / "projA").mkdir()
        tombstone.write_coverage(self.hub_root / "projA")
        secret = self.tmp / "secret.txt"
        secret.write_text('{"type":"user","uuid":"x"}', encoding="utf-8")   # JSONL 形狀的夾外機密
        (self.local_root / "projA" / "secret.jsonl").symlink_to(secret)
        fx.write_jsonl(fx.linear(), str(self.local_root / "projA" / "real.jsonl"))   # 真實 session
        from claude_session_sync.state import State
        st = State(known_sessions={"projA": set()}, local_sessions={"projA": set()})
        plan = scan.build_plan(self.local_root, self.hub_root, st, identity_fn=_name_match)
        pp = next(p for p in plan.projects if p.identity == "match")
        sids = {s.session_id for s in pp.sessions}
        self.assertIn("real", sids)          # 真實 session 照常
        self.assertNotIn("secret", sids)     # symlink 被排除（不列為 session、不 copy 進 hub）

    def test_build_plan_hub_only_memory_blocked_unmapped(self):
        (self.hub_root / "orphan").mkdir()
        _write_mem(self.hub_root / "orphan", "h.md", _mem("h"))
        tombstone.write_coverage(self.hub_root / "orphan")
        plan = scan.build_plan(self.local_root, self.hub_root, state=None, identity_fn=_name_match)
        pp = next(p for p in plan.projects if p.identity == "hub-only")
        self.assertEqual({m.name: m.action for m in pp.memories}, {"h.md": "blocked-unmapped"})

    def test_format_plan_shows_memory(self):
        (self.local_root / "projA").mkdir()
        (self.hub_root / "projA").mkdir()
        _write_mem(self.local_root / "projA", "a.md", _mem("a"))
        _write_mem(self.hub_root / "projA", "a.md", _mem("a"))
        plan = scan.build_plan(self.local_root, self.hub_root, state=None, identity_fn=_name_match)
        text = scan.format_plan(plan)
        self.assertIn("memory a.md", text)


if __name__ == "__main__":
    unittest.main()
