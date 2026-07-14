import os
import tempfile
import unittest
from pathlib import Path

from claude_session_sync import bootstrap, memory, state as state_mod, tombstone
from claude_session_sync.state import State
from tests import _caps, fixtures as fx


def _mem(slug="fact", body="hello", desc="d"):
    return "\n".join(["---", f"name: {slug}", f"description: {desc}",
                      "metadata:", "  type: project", "---", body, ""])


def _name_match(local_dir, hub_dirs):
    for hd in hub_dirs:
        if hd.name == local_dir.name:
            return ("match", hd)
    return ("needs-map", None)


def _linear_cwd(cwd):
    return [fx.umsg("u1", None, "user", 1, cwd=cwd), fx.umsg("u2", "u1", "assistant", 2)]


class TestBootstrap(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.local = self.tmp / "local"
        self.hub = self.tmp / "hub"
        self.local.mkdir()
        self.hub.mkdir()
        self.state_path = self.tmp / "state.json"

    def tearDown(self):
        self._td.cleanup()

    def _mkproj(self, root, name, sids, *, cwd=None):
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        for sid in sids:
            objs = _linear_cwd(cwd) if cwd else fx.linear()
            fx.write_jsonl(objs, str(d / f"{sid}.jsonl"))
        return d

    def _plan(self, **kw):
        kw.setdefault("identity_fn", _name_match)
        return bootstrap.scan_baseline(self.local, self.hub, None, **kw)

    def test_diff_computed(self):
        self._mkproj(self.local, "projA", ["s1", "s2", "s3"])
        self._mkproj(self.hub, "projA", ["s2", "s3", "s4"])
        p = self._plan().projects[0]
        self.assertEqual(p.status, "mapped")
        self.assertEqual(p.both, ["s2", "s3"])
        self.assertEqual(p.local_only, ["s1"])
        self.assertEqual(p.hub_only, ["s4"])

    def test_apply_writes_coverage_and_state(self):
        self._mkproj(self.local, "projA", ["s1"], cwd="/home/me/projA")
        self._mkproj(self.hub, "projA", ["s2"], cwd="/home/me/projA")
        plan = self._plan()
        bootstrap.apply_baseline(plan, self.hub, self.state_path)
        self.assertTrue(tombstone.is_initialized(self.hub / "projA"))
        s = state_mod.load_or_none(self.state_path)
        self.assertEqual(s.known_sessions["projA"], {"s2"})   # known = hub 現況（local-only s1 待 sync 才記）
        self.assertEqual(s.bindings, {"/home/me/projA": "projA"})
        self.assertTrue(s.hub_fingerprint)

    @_caps.needs_symlink
    def test_symlink_tombstones_dir_refuses_apply(self):
        # e2e gate3 #3：hub 專案的 .tombstones 是 symlink → apply_baseline 落地前拒（BootstrapChanged），不寫
        # coverage/tombstone 到界外。
        self._mkproj(self.local, "projA", ["s1"], cwd="/home/me/projA")
        hd = self._mkproj(self.hub, "projA", ["s1"], cwd="/home/me/projA")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (hd / ".tombstones").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(bootstrap.BootstrapChanged):
            bootstrap.apply_baseline(self._plan(), self.hub, self.state_path)
        self.assertFalse((elsewhere / "_coverage.json").exists())   # 未寫界外

    def test_records_local_sessions_baseline(self):
        # local baseline = both ∪ local_only（對稱於 known = both ∪ hub_only），供 P1c 刪除偵測起點。
        self._mkproj(self.local, "projA", ["s1", "s2"])   # local: s1, s2
        self._mkproj(self.hub, "projA", ["s2", "s3"])     # hub:   s2, s3
        bootstrap.apply_baseline(self._plan(), self.hub, self.state_path)
        s = state_mod.load_or_none(self.state_path)
        self.assertEqual(s.local_sessions["projA"], {"s1", "s2"})
        self.assertEqual(s.known_sessions["projA"], {"s2", "s3"})

    def test_writes_local_dir_binding(self):
        # codex r25：夾名綁定供「session 全刪、空夾無 cwd」時仍能配對偵測刪除。
        self._mkproj(self.local, "projA", ["s1"], cwd="/home/me/projA")
        self._mkproj(self.hub, "projA", ["s1"], cwd="/home/me/projA")
        bootstrap.apply_baseline(self._plan(), self.hub, self.state_path)
        s = state_mod.load_or_none(self.state_path)
        self.assertEqual(s.local_dir_bindings.get("projA"), "projA")

    def test_local_baseline_excludes_ignored(self):
        self._mkproj(self.local, "projA", ["s1"])          # local-only s1
        self._mkproj(self.hub, "projA", ["s4"])
        bootstrap.apply_baseline(self._plan(ignore={"s1"}), self.hub, self.state_path)
        s = state_mod.load_or_none(self.state_path)
        self.assertNotIn("s1", s.local_sessions.get("projA", set()))  # 忽略者不入 local baseline

    def test_importable_excludes_ignored(self):
        self._mkproj(self.local, "projA", ["s1"])
        self._mkproj(self.hub, "projA", ["s4"])
        p = self._plan(ignore={"s4"}).projects[0]
        self.assertEqual(p.ignored, ["s4"])
        self.assertEqual(p.importable, ["s1"])  # s4 被排除

    def test_ignore_writes_suppress_tombstone(self):
        self._mkproj(self.local, "projA", ["s1"])
        self._mkproj(self.hub, "projA", ["s4"])
        plan = self._plan(ignore={"s4"})
        bootstrap.apply_baseline(plan, self.hub, self.state_path)
        tomb = tombstone.find_session_tombstone(self.hub / "projA", "s4")
        self.assertIsNotNone(tomb)
        self.assertIsNotNone(tomb.base_hash)        # 記下當時內容 hash
        s = state_mod.load_or_none(self.state_path)
        self.assertNotIn("s4", s.known_sessions["projA"])  # 被忽略者不入 baseline

    def test_explicit_mapping_for_empty_hub(self):
        # 空 hub 首推：hub 尚無對應夾，靠 --map 明示夾名。
        self._mkproj(self.local, "projX", ["a", "b"], cwd="/home/me/x")
        plan = bootstrap.scan_baseline(self.local, self.hub, None, mappings={"projX": "encodedX"})
        p = plan.projects[0]
        self.assertEqual(p.status, "mapped")
        self.assertEqual(p.project_key, "encodedX")
        self.assertEqual(p.hub_only, [])
        self.assertEqual(p.local_only, ["a", "b"])
        bootstrap.apply_baseline(plan, self.hub, self.state_path)
        self.assertTrue((self.hub / "encodedX").is_dir())   # 建了 hub 夾放 coverage
        self.assertTrue(tombstone.is_initialized(self.hub / "encodedX"))
        s = state_mod.load_or_none(self.state_path)
        self.assertEqual(s.known_sessions["encodedX"], set())  # hub 還空

    def test_unmatched_skipped(self):
        self._mkproj(self.local, "orphanLocal", ["s1"])
        p = self._plan().projects[0]
        self.assertEqual(p.status, "skipped-needs-map")
        self.assertIsNone(p.project_key)

    def test_multi_cwd_skipped(self):
        d = self.local / "projM"
        d.mkdir()
        fx.write_jsonl([fx.umsg("a1", None, "user", 1, cwd="/home/a")], str(d / "s1.jsonl"))
        fx.write_jsonl([fx.umsg("b1", None, "user", 1, cwd="/home/b")], str(d / "s2.jsonl"))
        self._mkproj(self.hub, "projM", ["s1"])
        p = self._plan().projects[0]
        self.assertEqual(p.status, "skipped-multi-cwd")

    @_caps.needs_unreadable_dir
    def test_unreadable_local_project_dir_skipped(self):
        # e2e gate11 finding2：local 專案夾存在但不可讀 → `_stems`(glob) **fail-open** 回空 → baseline 漏現存 session
        # → 日後真正刪除認不出、hub 檔復活。scan_baseline 須標 skipped-unreadable、不建基線（fail-closed）。POSIX-only。
        self._mkproj(self.local, "projA", ["s1", "s2"])
        self._mkproj(self.hub, "projA", ["s1"])
        os.chmod(self.local / "projA", 0)
        try:
            p = self._plan().projects[0]
        finally:
            os.chmod(self.local / "projA", 0o700)
        self.assertEqual(p.status, "skipped-unreadable")
        self.assertEqual(p.both, [])                             # 未從 fail-open 空視圖建基線

    def test_reapply_bumps_epoch(self):
        self._mkproj(self.local, "projA", ["s1"])
        self._mkproj(self.hub, "projA", ["s1"])
        bootstrap.apply_baseline(self._plan(), self.hub, self.state_path)
        self.assertEqual(tombstone.read_coverage(self.hub / "projA").epoch, 1)
        bootstrap.apply_baseline(self._plan(), self.hub, self.state_path)
        self.assertEqual(tombstone.read_coverage(self.hub / "projA").epoch, 2)

    def test_state_baseline_preserves_other_projects(self):
        # 已有別專案的 state，不可被 bootstrap 覆蓋掉。
        state_mod.commit_session("preexisting", "old1", self.state_path)
        self._mkproj(self.local, "projA", ["s1"])
        self._mkproj(self.hub, "projA", ["s1"])
        bootstrap.apply_baseline(self._plan(), self.hub, self.state_path)
        s = state_mod.load_or_none(self.state_path)
        self.assertIn("preexisting", s.known_sessions)
        self.assertIn("projA", s.known_sessions)

    def test_apply_aborts_on_drift(self):
        # codex r9-1：確認後、落地前 hub 冒出新檔 → 拒絕落地（否則被悄悄 bless 成可匯入）。
        self._mkproj(self.local, "projA", ["s1"])
        self._mkproj(self.hub, "projA", ["s1"])
        plan = self._plan()
        (self.hub / "projA" / "secret.jsonl").write_text("{}\n", encoding="utf-8")  # 確認後冒出
        with self.assertRaises(bootstrap.BootstrapChanged):
            bootstrap.apply_baseline(plan, self.hub, self.state_path)

    def test_coverage_not_written_if_state_commit_fails(self):
        # codex r9-2：state 提交失敗時不可留下「已 initialized 但無 baseline」的危險半成品。
        from claude_session_sync import atomicio as aio
        self._mkproj(self.local, "projA", ["s1"])
        self._mkproj(self.hub, "projA", ["s1"])
        plan = self._plan()
        held = aio.FileLock(self.state_path).acquire()  # 卡住 state 鎖 → update_under_lock 逾時
        try:
            with self.assertRaises(aio.LockError):
                bootstrap.apply_baseline(plan, self.hub, self.state_path, lock_timeout_s=0.2)
        finally:
            held.release()
        self.assertFalse(tombstone.is_initialized(self.hub / "projA"))  # coverage 未寫 → 仍 uninitialized

    def test_bad_map_rejected(self):
        self._mkproj(self.local, "projX", ["a"])
        for bad in ("../outside", "/abs/path", "a/b"):
            p = bootstrap.scan_baseline(self.local, self.hub, None, mappings={"projX": bad}).projects[0]
            self.assertEqual(p.status, "skipped-bad-map", bad)
            self.assertIsNone(p.project_key)

    def test_dup_map_key_skipped(self):
        # 兩個 local 撞同一 hub 夾名 → 全數 skip（不挑）。
        self._mkproj(self.local, "p1", ["a"])
        self._mkproj(self.local, "p2", ["b"])
        plan = bootstrap.scan_baseline(self.local, self.hub, None,
                                       mappings={"p1": "shared", "p2": "shared"})
        self.assertTrue(all(p.status == "skipped-dup-key" for p in plan.projects))
        self.assertEqual(plan.mapped, [])

    def test_dup_cwd_skipped(self):
        self._mkproj(self.local, "p1", ["a"], cwd="/same/cwd")
        self._mkproj(self.local, "p2", ["b"], cwd="/same/cwd")
        plan = bootstrap.scan_baseline(self.local, self.hub, None,
                                       mappings={"p1": "h1", "p2": "h2"})
        self.assertTrue(all(p.status == "skipped-dup-cwd" for p in plan.projects))

    def test_ignore_damaged_file_gets_raw_base_hash(self):
        # codex r9-4：被忽略的「壞」檔（語意 content_hash=None）仍要有原始 bytes base_hash。
        self._mkproj(self.local, "projA", ["s1"])
        d = self.hub / "projA"
        d.mkdir(parents=True, exist_ok=True)
        (d / "broken.jsonl").write_bytes(b"")  # 0-byte：語意上 damaged
        plan = self._plan(ignore={"broken"})
        bootstrap.apply_baseline(plan, self.hub, self.state_path)
        tomb = tombstone.find_session_tombstone(d, "broken")
        self.assertIsNotNone(tomb)
        self.assertIsNotNone(tomb.base_hash)  # 原始 bytes hash，非 None

    def test_ignore_tombstone_respects_session_lock(self):
        # codex r10-4：ignore 的 tombstone 寫入須持有同一把 per-session 鎖，與 apply gate 互斥。
        from claude_session_sync import atomicio as aio
        self._mkproj(self.local, "projA", ["s1"])
        d = self.hub / "projA"
        d.mkdir(parents=True, exist_ok=True)
        (d / "x.jsonl").write_bytes(b"{}\n")
        plan = self._plan(ignore={"x"})
        held = aio.FileLock(d / "x.jsonl").acquire()  # 卡住該 session 鎖
        try:
            with self.assertRaises(aio.LockError):
                bootstrap.apply_baseline(plan, self.hub, self.state_path, lock_timeout_s=0.2)
        finally:
            held.release()

    def test_apply_baseline_rechecks_mount(self):
        # codex r11-5：掃描後、落地前 hub 掛載消失 → 不可在錯的 FS 建空夾並 bless。
        import shutil
        self._mkproj(self.local, "projA", ["s1"])
        self._mkproj(self.hub, "projA", ["s1"])
        plan = self._plan()
        shutil.rmtree(self.hub)  # 掛載消失
        with self.assertRaises(RuntimeError):
            bootstrap.apply_baseline(plan, self.hub, self.state_path)

    def test_halt_blocks_apply(self):
        self._mkproj(self.hub, "projA", ["s1"])
        st = State(hub_fingerprint="stale-fp")  # 與現況不符 → fingerprint halt
        plan = bootstrap.scan_baseline(self.local, self.hub, st, identity_fn=_name_match)
        self.assertTrue(plan.halt)
        with self.assertRaises(RuntimeError):
            bootstrap.apply_baseline(plan, self.hub, self.state_path)


class TestBootstrapMemory(unittest.TestCase):
    """P1d Block 3a：bootstrap 同時建 memory 基線（known_memory/local_memory）+ ignored memory tombstone。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.local = self.tmp / "local"
        self.hub = self.tmp / "hub"
        self.local.mkdir()
        self.hub.mkdir()
        self.state_path = self.tmp / "state.json"

    def tearDown(self):
        self._td.cleanup()

    def _mkproj(self, root, name, sids):
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        for sid in sids:
            fx.write_jsonl(fx.linear(), str(d / f"{sid}.jsonl"))
        return d

    def _mkmem(self, proj_dir, files):
        mdir = proj_dir / "memory"
        mdir.mkdir(parents=True, exist_ok=True)
        for fname, text in files.items():
            (mdir / fname).write_text(text, encoding="utf-8")

    def _plan(self, **kw):
        kw.setdefault("identity_fn", _name_match)
        return bootstrap.scan_baseline(self.local, self.hub, None, **kw)

    def test_memory_diff_computed(self):
        ld = self._mkproj(self.local, "projA", ["s1"])
        hd = self._mkproj(self.hub, "projA", ["s1"])
        self._mkmem(ld, {"a.md": _mem("a"), "b.md": _mem("b")})
        self._mkmem(hd, {"a.md": _mem("a"), "c.md": _mem("c")})
        p = self._plan().projects[0]
        self.assertEqual(p.mem_both, ["a.md"])
        self.assertEqual(p.mem_local_only, ["b.md"])
        self.assertEqual(p.mem_hub_only, ["c.md"])
        self.assertFalse(p.mem_unsafe)

    def test_apply_writes_memory_baseline(self):
        # known_memory = both ∪ hub_only；local_memory = both ∪ local_only（對稱 session）。
        ld = self._mkproj(self.local, "projA", ["s1"])
        hd = self._mkproj(self.hub, "projA", ["s1"])
        self._mkmem(ld, {"a.md": _mem("a"), "b.md": _mem("b")})   # local: a, b
        self._mkmem(hd, {"a.md": _mem("a"), "c.md": _mem("c")})   # hub:   a, c
        bootstrap.apply_baseline(self._plan(), self.hub, self.state_path)
        s = state_mod.load_or_none(self.state_path)
        self.assertEqual(s.known_memory["projA"], {"a.md", "c.md"})
        self.assertEqual(s.local_memory["projA"], {"a.md", "b.md"})

    def test_empty_memory_writes_empty_baseline_not_missing(self):
        # 無 memory 檔 → 寫**空集**基線（has_baseline=True，下次 hub 新 memory 可 copy-to-local）；
        # 空集 ≠ 缺欄位（後者才是 migration → blocked-no-local-baseline）。
        self._mkproj(self.local, "projA", ["s1"])
        self._mkproj(self.hub, "projA", ["s1"])
        bootstrap.apply_baseline(self._plan(), self.hub, self.state_path)
        s = state_mod.load_or_none(self.state_path)
        self.assertEqual(s.known_memory.get("projA"), set())
        self.assertEqual(s.local_memory.get("projA"), set())
        self.assertIn("projA", s.known_memory)   # 欄位存在（非 migration）

    def test_memory_ignore_writes_suppress_tombstone(self):
        # --ignore 涵蓋 memory 檔名 → 寫 memory suppress tombstone（base=content_hash、identity=name）+ 不入基線。
        ld = self._mkproj(self.local, "projA", ["s1"])
        hd = self._mkproj(self.hub, "projA", ["s1"])
        self._mkmem(hd, {"secret.md": _mem("secret-fact")})  # hub-only
        plan = self._plan(ignore={"secret.md"})
        p = plan.projects[0]
        self.assertEqual(p.mem_ignored, ["secret.md"])
        self.assertEqual(p.mem_importable, [])
        bootstrap.apply_baseline(plan, self.hub, self.state_path)
        tomb = tombstone.find_memory_tombstone(self.hub / "projA", "secret.md")
        self.assertIsNotNone(tomb)
        self.assertEqual(tomb.identity, "secret-fact")
        expect = memory.content_hash(memory.load_memory(hd / "memory" / "secret.md"))
        self.assertEqual(tomb.base_hash, expect)
        s = state_mod.load_or_none(self.state_path)
        self.assertNotIn("secret.md", s.known_memory["projA"])  # 被忽略者不入基線

    def test_ignored_memory_suppressed_on_next_sync(self):
        # 端到端：ignored memory 的 tombstone 讓下次 sync classify 為 suppressed-deleted（不傳播）。
        ld = self._mkproj(self.local, "projA", ["s1"])
        hd = self._mkproj(self.hub, "projA", ["s1"])
        self._mkmem(hd, {"secret.md": _mem("secret-fact")})
        bootstrap.apply_baseline(self._plan(ignore={"secret.md"}), self.hub, self.state_path)
        hub_proj, local_proj = self.hub / "projA", self.local / "projA"
        plans = memory.plan_memory_pair(
            local_proj, hub_proj, coverage_initialized=True,
            tombs=tombstone.read_tombstones(hub_proj),
            corrupt=tombstone.corrupt_tombstone_targets(hub_proj),
            has_baseline=True, has_local_baseline=True,
            known={"secret.md"}, local_known=set())
        self.assertEqual({p.name: p.action for p in plans}["secret.md"], "suppressed-deleted")

    @_caps.needs_symlink
    def test_unsafe_memory_root_skips_baseline(self):
        # memory/ 根是 symlink → mem_unsafe；不建記憶基線（known_memory 無此 pk）→ 下次 sync fail-closed。
        ld = self._mkproj(self.local, "projA", ["s1"])
        self._mkproj(self.hub, "projA", ["s1"])
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "planted.md").write_text(_mem("planted"), encoding="utf-8")
        (ld / "memory").symlink_to(elsewhere, target_is_directory=True)
        plan = self._plan()
        self.assertTrue(plan.projects[0].mem_unsafe)
        bootstrap.apply_baseline(plan, self.hub, self.state_path)
        s = state_mod.load_or_none(self.state_path)
        self.assertNotIn("projA", s.known_memory)   # 未建記憶基線
        self.assertNotIn("projA", s.local_memory)
        self.assertIn("projA", s.known_sessions)     # session 基線照常

    @_caps.needs_symlink
    def test_rebootstrap_unsafe_clears_stale_memory_baseline(self):
        # codex 3a-R1 #1：曾有 memory 基線的專案，memory/ 改成 symlink 後 re-bootstrap → 必須清掉 stale 基線
        # （否則殘留基線會讓下次 sync 把 hub memory 當本機已刪、寫抑制 tombstone 蓋掉真實 memory）。
        ld = self._mkproj(self.local, "projA", ["s1"])
        hd = self._mkproj(self.hub, "projA", ["s1"])
        self._mkmem(ld, {"a.md": _mem("a")})
        self._mkmem(hd, {"a.md": _mem("a")})
        bootstrap.apply_baseline(self._plan(), self.hub, self.state_path)
        self.assertIn("projA", state_mod.load_or_none(self.state_path).local_memory)  # 第一次有基線
        # 把 local memory/ 換成 symlink → mem_unsafe；re-bootstrap
        import shutil
        shutil.rmtree(ld / "memory")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (ld / "memory").symlink_to(elsewhere, target_is_directory=True)
        bootstrap.apply_baseline(self._plan(), self.hub, self.state_path)
        s = state_mod.load_or_none(self.state_path)
        self.assertNotIn("projA", s.known_memory)   # stale 基線已清
        self.assertNotIn("projA", s.local_memory)
        self.assertIn("projA", s.known_sessions)     # session 基線照常存在

    @_caps.needs_symlink
    def test_symlink_local_project_dir_skips_baseline(self):
        # e2e xgrp #4：local 專案夾是逃出 local_root 的 symlink → skipped-unsafe，不從 root 外夾建基線/bless。
        outside = self.tmp / "outside"
        outside.mkdir()
        fx.write_jsonl(fx.linear(), str(outside / "s1.jsonl"))
        (self.local / "evil").symlink_to(outside, target_is_directory=True)
        self._mkproj(self.hub, "evil", ["s1"])
        plan = self._plan(mappings={"evil": "evil"})
        p = next(pp for pp in plan.projects if pp.local_dir and pp.local_dir.endswith("evil"))
        self.assertEqual(p.status, "skipped-unsafe")
        bootstrap.apply_baseline(plan, self.hub, self.state_path)
        self.assertFalse(tombstone.is_initialized(self.hub / "evil"))   # 未 bless

    @_caps.needs_symlink
    def test_symlink_hub_project_dir_skips_baseline(self):
        # e2e xgrp #4：--map 目標 hub 夾是逃出 hub_root 的既存 symlink → skipped-unsafe，不寫 tombstone/coverage 到 root 外。
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.hub / "evilhub").symlink_to(outside, target_is_directory=True)
        self._mkproj(self.local, "projA", ["s1"])   # 顯式 --map，不需 cwd 解析
        plan = self._plan(mappings={"projA": "evilhub"})
        p = next(pp for pp in plan.projects if pp.local_dir and pp.local_dir.endswith("projA"))
        self.assertEqual(p.status, "skipped-unsafe")
        bootstrap.apply_baseline(plan, self.hub, self.state_path)
        self.assertFalse((outside / tombstone.TOMB_DIR).exists())   # 未把中繼寫到 hub_root 外

    def test_revalidate_aborts_on_memory_drift(self):
        # 確認後、落地前 hub 冒出新 memory → 拒絕落地（否則被悄悄 bless 成可匯入）。
        ld = self._mkproj(self.local, "projA", ["s1"])
        hd = self._mkproj(self.hub, "projA", ["s1"])
        self._mkmem(ld, {"a.md": _mem("a")})
        self._mkmem(hd, {"a.md": _mem("a")})
        plan = self._plan()
        (hd / "memory" / "sneaky.md").write_text(_mem("sneaky"), encoding="utf-8")  # 確認後冒出
        with self.assertRaises(bootstrap.BootstrapChanged):
            bootstrap.apply_baseline(plan, self.hub, self.state_path)

    def test_memory_tombstone_respects_project_memory_lock(self):
        # ignored memory tombstone 寫入須持 per-project memory 鎖（與未來 memory apply gate 互斥）。
        from claude_session_sync import atomicio as aio
        ld = self._mkproj(self.local, "projA", ["s1"])
        hd = self._mkproj(self.hub, "projA", ["s1"])
        self._mkmem(hd, {"secret.md": _mem("secret")})
        plan = self._plan(ignore={"secret.md"})
        # 先建 .tombstones 夾，卡住 per-project memory 鎖
        tombstone.tombstones_dir(self.hub / "projA").mkdir(parents=True, exist_ok=True)
        held = aio.FileLock(tombstone.tombstones_dir(self.hub / "projA") / "memory").acquire()
        try:
            with self.assertRaises(aio.LockError):
                bootstrap.apply_baseline(plan, self.hub, self.state_path, lock_timeout_s=0.2)
        finally:
            held.release()


class TestBootstrapAsserted(unittest.TestCase):
    """--map 斷言整夾 + 還原模式（決定 2026-07-14）：multi-cwd 夾憑使用者明示 --map 放行（寫 asserted_dirs）；
    --map 指到不存在的 local 夾（全新機器/災難還原）→ 建空夾＋空 local 基線，內容由下次 sync 拉下。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.local = self.tmp / "local"
        self.hub = self.tmp / "hub"
        self.local.mkdir()
        self.hub.mkdir()
        self.state_path = self.tmp / "state.json"

    def tearDown(self):
        self._td.cleanup()

    def _mk_multi_cwd(self, name="projM"):
        d = self.local / name
        d.mkdir()
        fx.write_jsonl([fx.umsg("a1", None, "user", 1, cwd="/home/a")], str(d / "s1.jsonl"))
        fx.write_jsonl([fx.umsg("b1", None, "user", 1, cwd="/home/b")], str(d / "s2.jsonl"))
        return d

    def test_map_asserts_multi_cwd_folder(self):
        self._mk_multi_cwd()
        (self.hub / "encM").mkdir()
        plan = bootstrap.scan_baseline(self.local, self.hub, None, mappings={"projM": "encM"})
        p = plan.projects[0]
        self.assertEqual(p.status, "mapped")
        self.assertTrue(p.asserted)
        self.assertIsNone(p.cwd)              # 不挑單一 cwd（C-r6-1 語意保留：斷言綁的是夾名，不是猜 cwd）
        self.assertEqual(len(p.cwds), 2)      # 預覽攤開全部 cwd（知情確認）
        bootstrap.apply_baseline(plan, self.hub, self.state_path)
        s = state_mod.load_or_none(self.state_path)
        self.assertEqual(s.local_dir_bindings["projM"], "encM")
        self.assertIn("projM", s.asserted_dirs)
        self.assertEqual(s.bindings, {})      # multi-cwd：不寫任何 cwd 綁定
        self.assertEqual(s.local_sessions["encM"], {"s1", "s2"})

    def test_multi_cwd_without_map_still_skipped_and_unasserted(self):
        # 迴歸：無 --map 的 multi-cwd 夾照舊 skip，且不得寫入斷言。
        self._mk_multi_cwd()
        (self.hub / "projM").mkdir()
        plan = bootstrap.scan_baseline(self.local, self.hub, None, identity_fn=_name_match)
        self.assertEqual(plan.projects[0].status, "skipped-multi-cwd")
        self.assertFalse(plan.projects[0].asserted)

    def test_asserted_folder_syncs_after_bless(self):
        # 端到端（實際情境）：斷言 bless 後，sync 能配對、local-only session 可匯入 hub。
        from claude_session_sync import scan
        self._mk_multi_cwd()
        (self.hub / "encM").mkdir()
        plan = bootstrap.scan_baseline(self.local, self.hub, None, mappings={"projM": "encM"})
        bootstrap.apply_baseline(plan, self.hub, self.state_path)
        s = state_mod.load_or_none(self.state_path)
        sp = scan.build_plan(self.local, self.hub, s)
        pp = next(x for x in sp.projects if x.local_dir and x.local_dir.endswith("projM"))
        self.assertEqual(pp.identity, "match")
        self.assertEqual({x.action for x in pp.sessions}, {"copy-to-hub"})

    def test_auto_rebless_downgrades_assertion(self):
        # 非斷言 re-bless（自動配對）→ 撤舊斷言：斷言不可替**自動**配對背書（r26-1 分界的另一半）。
        d = self.local / "projA"
        d.mkdir()
        fx.write_jsonl(_linear_cwd("/home/me/projA"), str(d / "s1.jsonl"))
        (self.hub / "projA").mkdir()
        plan = bootstrap.scan_baseline(self.local, self.hub, None, mappings={"projA": "projA"})
        bootstrap.apply_baseline(plan, self.hub, self.state_path)
        self.assertIn("projA", state_mod.load_or_none(self.state_path).asserted_dirs)
        plan2 = bootstrap.scan_baseline(self.local, self.hub, state_mod.load_or_none(self.state_path),
                                        identity_fn=_name_match)
        bootstrap.apply_baseline(plan2, self.hub, self.state_path)
        self.assertNotIn("projA", state_mod.load_or_none(self.state_path).asserted_dirs)

    def test_map_zero_cwd_folder_asserted_syncs(self):
        # 舊死路：--map「有檔但全讀不到 cwd」的夾 → bless 成功但 sync 永遠 needs-map（r26-1 只該擋未斷言弱猜）。
        from claude_session_sync import scan
        d = self.local / "projZ"
        d.mkdir()
        fx.write_jsonl([fx.umsg("u1", None, "user", 1)], str(d / "s1.jsonl"))   # 無 cwd 欄位
        (self.hub / "encZ").mkdir()
        plan = bootstrap.scan_baseline(self.local, self.hub, None, mappings={"projZ": "encZ"})
        self.assertEqual(plan.projects[0].status, "mapped")
        bootstrap.apply_baseline(plan, self.hub, self.state_path)
        s = state_mod.load_or_none(self.state_path)
        sp = scan.build_plan(self.local, self.hub, s)
        pp = next(x for x in sp.projects if x.local_dir and x.local_dir.endswith("projZ"))
        self.assertEqual(pp.identity, "match")

    # ── 還原模式（--map 指到不存在的 local 夾）───────────────────────────────

    def test_restore_creates_missing_local_folder(self):
        from claude_session_sync import scan
        hd = self.hub / "encR"
        hd.mkdir()
        fx.write_jsonl(fx.linear(), str(hd / "s1.jsonl"))
        fx.write_jsonl(fx.linear(), str(hd / "s2.jsonl"))
        (hd / "memory").mkdir()
        (hd / "memory" / "fact.md").write_text(_mem(), encoding="utf-8")
        plan = bootstrap.scan_baseline(self.local, self.hub, None, mappings={"projR": "encR"})
        p = next(x for x in plan.projects if x.status == "mapped")
        self.assertTrue(p.create_local)
        self.assertTrue(p.asserted)
        self.assertEqual(p.hub_only, ["s1", "s2"])
        self.assertEqual(p.mem_hub_only, ["fact.md"])
        self.assertFalse((self.local / "projR").exists())          # 預覽不建夾（唯讀）
        bootstrap.apply_baseline(plan, self.hub, self.state_path)
        self.assertTrue((self.local / "projR").is_dir())
        s = state_mod.load_or_none(self.state_path)
        self.assertEqual(s.known_sessions["encR"], {"s1", "s2"})
        self.assertEqual(s.local_sessions["encR"], set())          # 空 local 基線（pk 在、集合空 ≠ 缺欄位）
        self.assertEqual(s.local_dir_bindings["projR"], "encR")
        self.assertIn("projR", s.asserted_dirs)
        sp = scan.build_plan(self.local, self.hub, s)              # 還原的實際搬運交下一次 sync
        pp = next(x for x in sp.projects if x.local_dir and x.local_dir.endswith("projR"))
        self.assertEqual(pp.identity, "match")
        self.assertEqual({x.action for x in pp.sessions}, {"copy-to-local"})

    def test_restore_creates_missing_local_root(self):
        # 全新機器：連 local_root 都不存在 → mkdir(parents=True) 一路建齊。
        root = self.tmp / "fresh" / "projects"
        hd = self.hub / "encR"
        hd.mkdir()
        fx.write_jsonl(fx.linear(), str(hd / "s1.jsonl"))
        plan = bootstrap.scan_baseline(root, self.hub, None, mappings={"projR": "encR"})
        self.assertEqual([p.status for p in plan.projects], ["mapped"])
        bootstrap.apply_baseline(plan, self.hub, self.state_path)
        self.assertTrue((root / "projR").is_dir())

    def test_restore_requires_existing_hub(self):
        # 兩側皆無（多半拼錯）→ 拒建雙空死夾。
        plan = bootstrap.scan_baseline(self.local, self.hub, None, mappings={"projR": "encR"})
        self.assertEqual(plan.projects[0].status, "skipped-map-no-hub")
        self.assertEqual(plan.mapped, [])
        self.assertFalse((self.local / "projR").exists())

    def test_restore_rejects_unsafe_local_name(self):
        (self.hub / "encR").mkdir()
        plan = bootstrap.scan_baseline(self.local, self.hub, None, mappings={"../evil": "encR"})
        self.assertEqual(plan.projects[0].status, "skipped-bad-map")
        self.assertEqual(plan.mapped, [])

    def test_restore_rejects_casefold_collision(self):
        # 現存夾 ProjR 與待建 projr casefold 撞名 → Windows mkdir 會「開到」既存夾、基線名與磁碟名脫鉤 → 拒絕。
        d = self.local / "ProjR"
        d.mkdir()
        fx.write_jsonl(fx.linear(), str(d / "s1.jsonl"))
        (self.hub / "encR").mkdir()
        plan = bootstrap.scan_baseline(self.local, self.hub, None, mappings={"projr": "encR"})
        st_by_dir = {p.local_dir: p.status for p in plan.projects}
        self.assertEqual(st_by_dir[str(self.local / "projr")], "skipped-map-collision")

    def test_restore_and_existing_dup_key_all_skipped(self):
        # 還原合成項也受 dup-key guard（兩個 --map 撞同一 hub 夾 → 全數 skip，比照 codex r9-3）。
        self._mk_multi_cwd("projM")
        hd = self.hub / "encM"
        hd.mkdir()
        fx.write_jsonl(fx.linear(), str(hd / "s9.jsonl"))
        plan = bootstrap.scan_baseline(self.local, self.hub, None,
                                       mappings={"projM": "encM", "projR": "encM"})
        self.assertEqual({p.status for p in plan.projects}, {"skipped-dup-key"})

    def test_restore_aborts_if_folder_appears_before_apply(self):
        # codex mcwd-r1 F2：scan 後、apply 前冒出同名夾（空夾 → _revalidate 比 stems 抓不到）→ 中止，
        # 不 bless 未經預覽確認的夾。
        hd = self.hub / "encR"
        hd.mkdir()
        fx.write_jsonl(fx.linear(), str(hd / "s1.jsonl"))
        plan = bootstrap.scan_baseline(self.local, self.hub, None, mappings={"projR": "encR"})
        (self.local / "projR").mkdir()
        with self.assertRaises(bootstrap.BootstrapChanged):
            bootstrap.apply_baseline(plan, self.hub, self.state_path)

    def test_restore_aborts_on_casefold_alias_appearing(self):
        # codex mcwd-r1 F2：期間冒出 casefold 別名夾（Windows mkdir exist_ok 會「開到」它＝名實脫鉤）→ 中止。
        hd = self.hub / "encR"
        hd.mkdir()
        fx.write_jsonl(fx.linear(), str(hd / "s1.jsonl"))
        plan = bootstrap.scan_baseline(self.local, self.hub, None, mappings={"projr": "encR"})
        (self.local / "ProjR").mkdir()
        with self.assertRaises(bootstrap.BootstrapChanged):
            bootstrap.apply_baseline(plan, self.hub, self.state_path)

    def test_restore_rejects_windows_reserved_name(self):
        # codex mcwd-r1 F3：保留裝置名/尾隨點的待建名 → skipped-bad-map（不等到 apply 才 OSError/名實脫鉤）。
        (self.hub / "encR").mkdir()
        for bad in ("CON", "proj.", "proj "):
            plan = bootstrap.scan_baseline(self.local, self.hub, None, mappings={bad: "encR"})
            self.assertEqual(plan.projects[0].status, "skipped-bad-map", bad)
            self.assertEqual(plan.mapped, [])

    def test_restore_aborts_if_hub_vanishes_before_apply(self):
        # codex mcwd-g2 #2：預覽時就空的 hub 專案在 apply 前消失 → `_revalidate` 比不出（missing→空集）、
        # step-1 mkdir 又會憑空重建 → 不可 bless 兩側皆空的死基線 → 中止。
        hd = self.hub / "encR"
        hd.mkdir()
        plan = bootstrap.scan_baseline(self.local, self.hub, None, mappings={"projR": "encR"})
        self.assertEqual(plan.projects[0].status, "mapped")
        hd.rmdir()
        with self.assertRaises(bootstrap.BootstrapChanged):
            bootstrap.apply_baseline(plan, self.hub, self.state_path)
        self.assertFalse((self.local / "projR").exists())
        self.assertFalse(hd.exists())                 # 未被 mkdir 憑空重建

    def test_restore_rejects_pairwise_synth_collision(self):
        # codex mcwd-g1 #1：兩個 --map 待建夾**彼此** casefold 撞名（各自對現存夾都不撞）→ 全數 skip，
        # 不可放行到 apply 才 mkdir 撞（中途中止留半成品）。
        (self.hub / "H1").mkdir()
        (self.hub / "H2").mkdir()
        plan = bootstrap.scan_baseline(self.local, self.hub, None,
                                       mappings={"Proj": "H1", "proj": "H2"})
        self.assertEqual({p.status for p in plan.projects}, {"skipped-map-collision"})
        self.assertEqual(plan.mapped, [])

    def test_map_hub_target_canonicalized_to_disk_name(self):
        # codex mcwd-g1 #2：--map 目標大小寫與磁碟不同（encr vs EncR）→ canonical 到磁碟實際夾名，
        # 否則 state key 掛在使用者字串上，`_bindings_first` exact 比對永遠失敗＝bless 完即 needs-map 死路。
        from claude_session_sync import scan
        d = self.local / "projR"
        d.mkdir()
        fx.write_jsonl(_linear_cwd("/home/me/projR"), str(d / "s1.jsonl"))
        (self.hub / "EncR").mkdir()
        plan = bootstrap.scan_baseline(self.local, self.hub, None, mappings={"projR": "encr"})
        p = plan.projects[0]
        self.assertEqual(p.status, "mapped")
        self.assertEqual(p.project_key, "EncR")            # canonical 到磁碟名
        bootstrap.apply_baseline(plan, self.hub, self.state_path)
        s = state_mod.load_or_none(self.state_path)
        self.assertIn("EncR", s.known_sessions)
        self.assertEqual(s.local_dir_bindings["projR"], "EncR")
        sp = scan.build_plan(self.local, self.hub, s)      # bless 後可正常配對（無死 key）
        pp = next(x for x in sp.projects if x.local_dir and x.local_dir.endswith("projR"))
        self.assertEqual(pp.identity, "match")

    @_caps.needs_junction
    def test_map_junction_alias_dup_key_skipped(self):
        # codex mcwd-g4 #1：--map locA=Hub --map locB=Alias（Alias=root 內 junction→Hub）→ 實體同夾 →
        # 以實體 canonical 鍵摺疊後 dup-key 全數 skip（exact project_key 比對看不出）。
        la = self.local / "locA"
        lb = self.local / "locB"
        la.mkdir()
        lb.mkdir()
        fx.write_jsonl(_linear_cwd("/home/me/a"), str(la / "s1.jsonl"))
        fx.write_jsonl(_linear_cwd("/home/me/b"), str(lb / "s2.jsonl"))
        hub = self.hub / "Hub"
        hub.mkdir()
        _caps.make_junction(self.hub / "Alias", hub)
        plan = bootstrap.scan_baseline(self.local, self.hub, None,
                                       mappings={"locA": "Hub", "locB": "Alias"})
        self.assertEqual({p.status for p in plan.projects}, {"skipped-dup-key"})

    def test_rebless_to_same_hub_revokes_old_folder_claims(self):
        # codex mcwd-g3 #1：--map new=Hub 落地時撤掉 old→Hub 的舊 claims（夾名/斷言/cwd）——否則空的 old 夾
        # （這輪 bootstrap 因無 cwd 解析不出 → skip、不進 dup-key guard）下次 sync 仍與 new 同配一 hub →
        # 空夾把 hub 檔判 local-deleted 寫 false tombstone。
        (self.local / "oldF").mkdir()                  # 空舊夾（session 已刪光）
        new = self.local / "newF"
        new.mkdir()
        fx.write_jsonl(_linear_cwd("/home/me/new"), str(new / "s1.jsonl"))
        (self.hub / "Hub").mkdir()
        st0 = State(local_dir_bindings={"oldF": "Hub"}, asserted_dirs={"oldF"},
                    bindings={"/home/me/old": "Hub"})
        state_mod.save(st0, self.state_path)
        plan = bootstrap.scan_baseline(self.local, self.hub, state_mod.load_or_none(self.state_path),
                                       mappings={"newF": "Hub"})
        bootstrap.apply_baseline(plan, self.hub, self.state_path)
        s = state_mod.load_or_none(self.state_path)
        self.assertEqual(s.local_dir_bindings, {"newF": "Hub"})       # 舊夾名 claim 撤除
        self.assertEqual(s.asserted_dirs, {"newF"})
        self.assertEqual(s.bindings, {"/home/me/new": "Hub"})         # 舊 cwd claim 撤除

    def _mk_projR_and_escape(self):
        d = self.local / "projR"
        d.mkdir()
        fx.write_jsonl(_linear_cwd("/home/me/projR"), str(d / "s1.jsonl"))
        outside = self.tmp / "outside"
        outside.mkdir()
        os.symlink(outside, self.hub / "HubX", target_is_directory=True)   # unsafe（逃逸 symlink）hub 夾

    @_caps.needs_symlink
    def test_map_target_unsafe_exact_and_alias_fail_closed(self):
        # codex mcwd-g3 #2：--map 目標 exact 命中 unsafe 夾、或 casefold 撞鍵 unsafe 夾 → skipped-unsafe
        # （alias 型在 Windows 上 mkdir 會 case-insensitive 開到 unsafe reparse＝寫穿界外；不可視為待建新夾）。
        self._mk_projR_and_escape()
        for target in ("HubX", "hubx"):
            plan = bootstrap.scan_baseline(self.local, self.hub, None, mappings={"projR": target})
            p = next(x for x in plan.projects if x.local_dir and x.local_dir.endswith("projR"))
            self.assertEqual(p.status, "skipped-unsafe", target)

    @_caps.needs_symlink
    @_caps.needs_case_sensitive_fs
    def test_map_target_exact_unsafe_not_canonicalized_to_twin(self):
        # codex mcwd-g3 #2（孿生變體，僅 case-sensitive FS 可存在）：unsafe `HubX` 旁有 safe 孿生 `hubx`，
        # --map 指 exact unsafe → 不可「好心」canonical 到孿生夾（使用者指的就是 unsafe 那個）→ skipped-unsafe。
        self._mk_projR_and_escape()
        (self.hub / "hubx").mkdir()                                        # safe 孿生（case-sensitive 才建得出）
        plan = bootstrap.scan_baseline(self.local, self.hub, None, mappings={"projR": "HubX"})
        p = next(x for x in plan.projects if x.local_dir and x.local_dir.endswith("projR"))
        self.assertEqual(p.status, "skipped-unsafe")

    def test_map_dup_hub_targets_casefold_folded(self):
        # codex mcwd-g1 #1：--map A=Hub --map B=hub（皆待建新 hub 夾）exact 不同、casefold 同 →
        # case-insensitive hub FS 上是同一實體夾 → 摺疊後視為 dup-key 全數 skip。
        da = self.local / "projA"
        db = self.local / "projB"
        da.mkdir()
        db.mkdir()
        fx.write_jsonl(_linear_cwd("/home/me/a"), str(da / "s1.jsonl"))
        fx.write_jsonl(_linear_cwd("/home/me/b"), str(db / "s2.jsonl"))
        plan = bootstrap.scan_baseline(self.local, self.hub, None,
                                       mappings={"projA": "Hub", "projB": "hub"})
        self.assertEqual({p.status for p in plan.projects}, {"skipped-dup-key"})


class TestSafeLeafName(unittest.TestCase):
    """pathsafe.safe_leaf_name（codex mcwd-r1 F3）：--map 待建名的跨 OS 安全尺（bootstrap/doctor/transfer 共用）。"""

    def test_rejects_windows_hostile_names(self):
        from claude_session_sync import pathsafe
        for bad in ("proj.", "proj ", "CON", "con", "NUL", "COM1", "LPT9", "CON.txt", "com1.tar.gz",
                    "a:b", "a*b", "a?b", "a<b", "a>b", "a|b", 'a"b', "a\x1fb", "", ".", "..", "a/b", "a\\b"):
            self.assertFalse(pathsafe.safe_leaf_name(bad), repr(bad))

    def test_accepts_normal_names(self):
        from claude_session_sync import pathsafe
        for ok in ("projA", "D--Workshops-Will-claude-session-sync-dev", "a.b", "console", "COM10",
                   "CONx", "encodedX", "名字", "café"):
            self.assertTrue(pathsafe.safe_leaf_name(ok), repr(ok))


if __name__ == "__main__":
    unittest.main()
