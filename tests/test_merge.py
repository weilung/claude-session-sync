"""P1d Block 3 收尾：memory-merge——衝突保留兩版（approach A）+ 提示詞產生器（明文外洩警告）。

關鍵不變量：暫存在 memory/ **之外**（不外洩/不被當新 memory 同步）、**只讀正式 memory 絕不寫回**（A3）、
claim-the-dir 幂等不覆蓋使用者編輯、提示詞帶明文外洩警告且不自動餵 Claude。
"""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_session_sync import atomicio, cli, memory, merge, scan, state as state_mod, tombstone
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


class _Harness:
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.local = self.tmp / "local"
        self.hub = self.tmp / "hub"
        self.cache = self.tmp / "cache"
        self.lA = self.local / "projA"
        self.hA = self.hub / "projA"
        self.lA.mkdir(parents=True)
        self.hA.mkdir(parents=True)
        self.state = self.tmp / "state.json"
        self._save(known_mem=set(), local_mem=set())
        tombstone.write_coverage(self.hA)

    def tearDown(self):
        try:
            self._td.cleanup()
        except OSError:
            # 長路徑測試（>260 staging）令 TemporaryDirectory 的 shutil.rmtree(plain) 失敗（Windows 未開
            # LongPathsEnabled）→ 改以 os_path 的 \\?\ 前綴遞迴刪。
            shutil.rmtree(atomicio.os_path(self.tmp), ignore_errors=True)

    def _save(self, *, known_mem, local_mem):
        st = State(known_memory={"projA": set(known_mem)},
                   local_memory={"projA": set(local_mem)})
        state_mod.save(st, self.state)
        return st

    def _wm(self, proj_dir, name, text):
        mdir = proj_dir / "memory"
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / name).write_text(text, encoding="utf-8")

    def _conflicts(self):
        st = state_mod.load_or_none(self.state)
        return merge.find_conflicts(self.local, self.hub, st, identity_fn=_name_match)

    def _base_hash(self, text):
        return memory.content_hash(memory.load_memory_bytes(text.encode("utf-8")))


class TestFindConflicts(_Harness, unittest.TestCase):
    def test_conflict_content_detected(self):
        self._wm(self.lA, "a.md", _mem("a", body="LOCAL"))
        self._wm(self.hA, "a.md", _mem("a", body="HUB"))
        cs = self._conflicts()
        self.assertEqual(len(cs), 1)
        c = cs[0]
        self.assertEqual(c.kind, "conflict-content")
        self.assertEqual(c.key, "a.md")
        labels = sorted(v.label for v in c.versions)
        self.assertEqual(labels, ["hub", "local"])
        # 內容已讀入、可供 prompt
        self.assertTrue(any("LOCAL" in (v.text or "") for v in c.versions))
        self.assertTrue(any("HUB" in (v.text or "") for v in c.versions))

    def test_identical_no_conflict(self):
        self._wm(self.lA, "a.md", _mem("a"))
        self._wm(self.hA, "a.md", _mem("a"))
        self.assertEqual(self._conflicts(), [])

    def test_cosmetic_reorder_no_conflict(self):
        self._wm(self.lA, "a.md", "---\nname: x\ndescription: d\n---\nbody")
        self._wm(self.hA, "a.md", '---\ndescription: "d"\nname: x\n---\nbody\n')
        self.assertEqual(self._conflicts(), [])

    def test_cross_file_identity_grouped(self):
        # 同 name=fact 落兩檔名（local old.md / hub new.md）→ 歸成一個衝突（key=identity）。
        self._wm(self.lA, "old.md", _mem("fact", body="OLD"))
        self._wm(self.hA, "new.md", _mem("fact", body="NEW"))
        cs = self._conflicts()
        self.assertEqual(len(cs), 1)
        c = cs[0]
        self.assertEqual(c.kind, "conflict-cross-file-identity")
        self.assertEqual(c.key, "fact")
        fnames = sorted(v.filename for v in c.versions)
        self.assertEqual(fnames, ["new.md", "old.md"])

    def test_delete_vs_update_includes_tombstone(self):
        # hub tombstone base=版本A；local 現存版本B(≠A) → conflict-delete-vs-update + tombstone 版本。
        self._wm(self.lA, "f.md", _mem("f", body="UPDATED"))
        tombstone.write_memory_tombstone(self.hA, "f.md",
                                         base_hash=self._base_hash(_mem("f", body="ORIG")),
                                         identity="f", machine="other")
        cs = self._conflicts()
        self.assertEqual(len(cs), 1)
        c = cs[0]
        self.assertEqual(c.kind, "conflict-delete-vs-update")
        self.assertTrue(any(v.is_tombstone for v in c.versions))
        self.assertTrue(any(v.label == "local" and "UPDATED" in (v.text or "") for v in c.versions))
        tv = next(v for v in c.versions if v.is_tombstone)
        self.assertEqual(tv.identity, "f")
        self.assertEqual(tv.machine, "other")

    def test_project_filter(self):
        self._wm(self.lA, "a.md", _mem("a", body="L"))
        self._wm(self.hA, "a.md", _mem("a", body="H"))
        st = state_mod.load_or_none(self.state)
        self.assertEqual(merge.find_conflicts(self.local, self.hub, st, project="nope",
                                              identity_fn=_name_match), [])
        self.assertEqual(len(merge.find_conflicts(self.local, self.hub, st, project="projA",
                                                  identity_fn=_name_match)), 1)


class TestStaging(_Harness, unittest.TestCase):
    def _one(self):
        self._wm(self.lA, "a.md", _mem("a", body="LOCALBODY"))
        self._wm(self.hA, "a.md", _mem("a", body="HUBBODY"))
        return self._conflicts()[0]

    def test_stage_writes_both_versions_outside_memory(self):
        c = self._one()
        res = merge.stage_conflict(c, root=self.cache, apply=True)
        self.assertEqual(res.status, "staged")
        # 暫存夾在 memory/ 與 hub 之外（不外洩/不被當新 memory）
        self.assertNotIn(self.local, res.dest.parents)
        self.assertNotIn(self.hub, res.dest.parents)
        self.assertIn(self.cache, res.dest.parents)
        # 兩版 + 中繼 + 提示詞都落地
        staged = {p.name for p in res.dest.iterdir()}
        self.assertIn("local__a.md", staged)
        self.assertIn("hub__a.md", staged)
        self.assertIn(merge.META_FILE, staged)
        self.assertIn(merge.PROMPT_FILE, staged)
        self.assertIn("LOCALBODY", (res.dest / "local__a.md").read_text(encoding="utf-8"))

    def test_sources_untouched(self):
        c = self._one()
        before_l = (self.lA / "memory" / "a.md").read_bytes()
        before_h = (self.hA / "memory" / "a.md").read_bytes()
        merge.stage_conflict(c, root=self.cache, apply=True)
        self.assertEqual((self.lA / "memory" / "a.md").read_bytes(), before_l)
        self.assertEqual((self.hA / "memory" / "a.md").read_bytes(), before_h)

    def test_dry_run_writes_nothing(self):
        c = self._one()
        res = merge.stage_conflict(c, root=self.cache, apply=False)
        self.assertEqual(res.status, "would-stage")
        self.assertFalse(res.dest.exists())
        self.assertFalse(self.cache.exists())

    def test_idempotent_already_staged_no_clobber(self):
        c = self._one()
        res1 = merge.stage_conflict(c, root=self.cache, apply=True)
        self.assertEqual(res1.status, "staged")
        # 使用者刪減暫存檔（先刪敏感段）
        (res1.dest / "local__a.md").write_text("REDACTED", encoding="utf-8")
        res2 = merge.stage_conflict(c, root=self.cache, apply=True)
        self.assertEqual(res2.status, "already-staged")
        self.assertEqual((res1.dest / "local__a.md").read_text(encoding="utf-8"), "REDACTED")

    def test_meta_json_provenance(self):
        c = self._one()
        res = merge.stage_conflict(c, root=self.cache, apply=True)
        meta = json.loads((res.dest / merge.META_FILE).read_text(encoding="utf-8"))
        self.assertEqual(meta["kind"], "conflict-content")
        self.assertEqual(meta["project_key"], "projA")
        self.assertEqual(len(meta["versions"]), 2)
        self.assertTrue(all(v["staged_file"] for v in meta["versions"]))

    @_caps.needs_symlink
    def test_symlink_source_skipped(self):
        # source 被換成 symlink → no-follow 讀回 None → 不複製夾外內容。
        secret = self.tmp / "secret.md"
        secret.write_text("SECRET-OUTSIDE", encoding="utf-8")
        mdir = self.lA / "memory"
        mdir.mkdir(parents=True, exist_ok=True)
        os.symlink(secret, mdir / "link.md")
        self.assertIsNone(merge._read_nofollow(mdir, "link.md"))

    @_caps.needs_symlink
    def test_parent_memory_symlink_skipped(self):
        # memory/ 根被換成 symlink（指向夾外）→ 父 symlink 守衛 → 讀回 None（不跟隨父、不複製夾外檔）。
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "x.md").write_text("OUTSIDE", encoding="utf-8")
        mdir = self.hA / "memory"   # hub projA 尚無 memory/ 夾
        os.symlink(outside, mdir)
        self.assertIsNone(merge._read_nofollow(mdir, "x.md"))

    def test_empty_when_all_unreadable(self):
        # 構造一個 versions 全無 data 的衝突 → empty、不建夾。
        c = merge.MemoryConflict("projA", "conflict-content", "x.md",
                                 (merge.ConflictVersion("local", "x.md", None, text=None, data=None),), "r")
        res = merge.stage_conflict(c, root=self.cache, apply=True)
        self.assertEqual(res.status, "empty")
        self.assertFalse(res.dest.exists())

    def test_long_nonascii_filename_stages(self):
        # 長/非 ASCII 檔名 → staging 路徑 >260（<pk>/<key> 巢狀、percent-encode 膨脹）。Windows 走
        # atomicio.os_path 的 \\?\ 繞過 MAX_PATH（原 @_caps.needs_long_path skip、現全平台實跑）。
        # codex R2 Medium：暫存檔名仍 bounded（不 file-name-too-long）。
        name = "é" * 80 + ".md"
        L, H = _mem("longn", body="L"), _mem("longn", body="H")
        self._wm(self.lA, name, L)
        self._wm(self.hA, name, H)
        c = next(c for c in self._conflicts() if c.key == name)
        res = merge.stage_conflict(c, root=self.cache, apply=True)
        self.assertEqual(res.status, "staged")
        self.assertTrue(all(len(f.encode("utf-8")) <= 200 for f in res.files))
        self.assertEqual(len(res.files), 2)
        # 讀回深路徑（>260）驗兩版 bytes 真的落地、與來源逐位元組相同（dogfood atomicio.read_bytes 長路徑讀）。
        # 注意比對**磁碟來源 bytes**而非原字串：_wm 以 Path.write_text 寫入，Windows text-mode 會把 \n 轉 \r\n。
        src = {(self.lA / "memory" / name).read_bytes(), (self.hA / "memory" / name).read_bytes()}
        got = {atomicio.read_bytes(res.dest / f) for f in res.files}
        self.assertEqual(got, src)
        # 再跑一次 → already-staged（走 _completed_match 的深路徑 .done/CONFLICT.json 讀，同樣長路徑安全）。
        self.assertEqual(merge.stage_conflict(c, root=self.cache, apply=True).status, "already-staged")

    def test_cross_file_and_content_keys_no_collision(self):
        # 自審補洞：identity "notes.md"（cross-file）不可與檔名 "notes.md"（content）撞同一暫存夾。
        self._wm(self.lA, "notes.md", _mem("other", body="A"))   # content conflict（name=other）
        self._wm(self.hA, "notes.md", _mem("other", body="B"))
        self._wm(self.lA, "x.md", _mem("notes.md", body="X"))    # cross-file，identity 字面="notes.md"
        self._wm(self.hA, "y.md", _mem("notes.md", body="Y"))
        cs = self._conflicts()
        dests = {merge.staging_dir(self.cache, c) for c in cs}
        self.assertEqual(len(dests), len(cs))                    # 每個衝突落不同夾（無 collision）
        for c in cs:
            self.assertEqual(merge.stage_conflict(c, root=self.cache, apply=True).status, "staged")


class TestGateFixes(_Harness, unittest.TestCase):
    def _one(self):
        self._wm(self.lA, "a.md", _mem("a", body="L"))
        self._wm(self.hA, "a.md", _mem("a", body="H"))
        return self._conflicts()[0]

    @_caps.needs_symlink
    def test_symlinked_subpath_refused_no_write_into_target(self):
        # codex gate F1 High：暫存子層既有 symlink → hub/memory → 拒絕、不跟隨寫入（防外洩）。
        c = self._one()
        evil = self.tmp / "evil"
        evil.mkdir()
        croot = self.cache
        (croot).mkdir(parents=True)
        os.symlink(evil, croot / "projA")          # <root>/projA → 夾外
        res = merge.stage_conflict(c, root=croot, apply=True)
        self.assertEqual(res.status, "error")
        self.assertFalse((evil / "a.md").exists())  # 沒寫穿 symlink
        self.assertFalse(any(evil.iterdir()))       # evil 全空

    @_caps.needs_junction
    def test_junction_subpath_refused_no_write_into_target(self):
        # e2e Pass1 High：暫存子層是指向夾外（模擬正式 memory/hub）的 **junction**。Windows junction 非 symlink，
        # 舊 `os.path.islink` 放行 → 兩版/PROMPT.md 寫穿外洩。改用 `reparse_kind` 全拒 → error、不寫入 target。
        c = self._one()
        evil = self.tmp / "evil"
        evil.mkdir()
        croot = self.cache
        croot.mkdir(parents=True)
        _caps.make_junction(croot / "projA", evil)   # <root>/projA junction → 夾外
        res = merge.stage_conflict(c, root=croot, apply=True)
        self.assertEqual(res.status, "error")
        self.assertFalse(any(evil.iterdir()))        # 沒寫穿 junction（evil 全空）

    @_caps.needs_symlink
    def test_conflicts_skip_escaping_project_dir(self):
        # e2e gate2 #3：plan 時有 memory 衝突，conflicts_from_plan 前 local projA 換成逃逸 symlink → 重驗跳過，
        # 不從界外讀 memory 進暫存/prompt（`_read_nofollow` 只守 memory/ 夾與最終檔、不守其上的專案夾 junction）。
        self._wm(self.lA, "a.md", _mem("a", body="LOCAL"))
        self._wm(self.hA, "a.md", _mem("a", body="HUB"))
        st = state_mod.load_or_none(self.state)
        plan = scan.build_plan(self.local, self.hub, st, identity_fn=_name_match)
        pp = next(p for p in plan.projects if p.identity == "match")
        self.assertTrue(any(m.action == "conflict-content" for m in pp.memories))   # plan 確有衝突（否則測試無意義）
        import shutil
        shutil.rmtree(self.lA)
        outside = self.tmp / "outside"
        (outside / "memory").mkdir(parents=True)
        (outside / "memory" / "a.md").write_text(_mem("a", body="SECRET-OUTSIDE"), encoding="utf-8")
        self.lA.symlink_to(outside, target_is_directory=True)
        self.assertEqual(merge.conflicts_from_plan(plan), [])   # 逃逸專案夾 → 不抽取衝突（不讀界外 memory）

    def test_stale_staging_detected_and_recovers(self):
        # codex gate5 F1：同檔名鍵衝突換內容 → 既有 .done 暫存判 stale（不用舊證據遮蓋新衝突）；刪夾後可復原。
        import shutil
        self._wm(self.lA, "a.md", _mem("a", body="L1"))
        self._wm(self.hA, "a.md", _mem("a", body="H1"))
        c1 = next(c for c in self._conflicts() if c.key == "a.md")
        self.assertEqual(merge.stage_conflict(c1, root=self.cache, apply=True).status, "staged")
        self.assertEqual(merge.stage_conflict(c1, root=self.cache, apply=True).status, "already-staged")
        self._wm(self.lA, "a.md", _mem("a", body="L2"))      # 衝突換內容
        c2 = next(c for c in self._conflicts() if c.key == "a.md")
        res = merge.stage_conflict(c2, root=self.cache, apply=True)
        self.assertEqual(res.status, "stale")                # 不被舊 .done 遮蓋
        shutil.rmtree(res.dest)                              # 使用者刪夾
        self.assertEqual(merge.stage_conflict(c2, root=self.cache, apply=True).status, "staged")

    def test_fingerprint_includes_project_key(self):
        # e2e-g2：兩衝突除 project_key 外全同（同 kind/key/版本內容）→ 指紋須不同——否則兩個大小寫/正規化折疊相同的
        # 相異 pk 在不敏感快取上撞成同一暫存夾時，第二個被 _completed_match 誤判 already-staged 而靜默略過（不同專案
        # memory 混淆）。同 pk 同內容 → 指紋不變（same/stale 判定不受影響）。
        v = merge.ConflictVersion("local", "x.md", "h1", text="A", data=b"A")
        fp_P = merge._conflict_fingerprint(merge.MemoryConflict("P", "conflict-content", "x.md", (v,), "r"))
        fp_p = merge._conflict_fingerprint(merge.MemoryConflict("p", "conflict-content", "x.md", (v,), "r"))
        self.assertNotEqual(fp_P, fp_p)                                     # pk 不同 → 指紋不同
        self.assertEqual(fp_P, merge._conflict_fingerprint(
            merge.MemoryConflict("P", "conflict-content", "x.md", (v,), "r")))   # 同 pk 同內容 → 指紋相同（決定性）

    def test_stale_detects_changed_damaged_version(self):
        # codex gate6 F1：damaged delete-vs-update（content_hash=None）換 raw bytes → fingerprint 用 raw sha 區分 → stale。
        mdir = self.lA / "memory"
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / "f.md").write_bytes(b"\x80\x81\x82\x83v1")     # 非 UTF-8 → DECODE_ERROR、content_hash=None
        tombstone.write_memory_tombstone(self.hA, "f.md", base_hash="b1", identity="f", machine="m")
        c1 = next(c for c in self._conflicts() if c.key == "f.md")
        self.assertEqual(c1.kind, "conflict-delete-vs-update")
        self.assertTrue(any(v.content_hash is None and v.data is not None for v in c1.versions))  # damaged version
        self.assertEqual(merge.stage_conflict(c1, root=self.cache, apply=True).status, "staged")
        (mdir / "f.md").write_bytes(b"\x80\x81\x82\x83v2")     # 換另一段 damaged bytes（仍 content_hash=None）
        c2 = next(c for c in self._conflicts() if c.key == "f.md")
        self.assertEqual(merge.stage_conflict(c2, root=self.cache, apply=True).status, "stale")

    def test_done_marker_written_and_incomplete_detected(self):
        # codex gate F3 Medium：完成標記；缺它＝殘缺暫存，不可當 already-staged。
        c = self._one()
        res = merge.stage_conflict(c, root=self.cache, apply=True)
        self.assertEqual(res.status, "staged")
        self.assertTrue((res.dest / merge.DONE_FILE).exists())
        (res.dest / merge.DONE_FILE).unlink()        # 模擬上次中途失敗
        res2 = merge.stage_conflict(c, root=self.cache, apply=True)
        self.assertEqual(res2.status, "incomplete")

    def test_unreadable_side_after_plan_noted_not_dropped(self):
        # codex gate F2 Medium：plan 後某側讀不到 → 帶退化 note、只 1 版、不靜默丟。
        self._wm(self.lA, "a.md", _mem("a", body="L"))
        self._wm(self.hA, "a.md", _mem("a", body="H"))
        real = merge._read_nofollow
        def fake(mdir, fn):
            return None if "hub" in str(mdir) else real(mdir, fn)   # hub 側模擬讀不到
        with mock.patch("claude_session_sync.merge._read_nofollow", fake):
            cs = self._conflicts()
        c = next(c for c in cs if c.key == "a.md")
        self.assertTrue(c.notes)                                     # 帶退化警告
        self.assertEqual(len([v for v in c.versions if not v.is_tombstone]), 1)

    def test_cli_nonzero_on_degraded_conflict(self):
        self._wm(self.lA, "a.md", _mem("a", body="L"))
        self._wm(self.hA, "a.md", _mem("a", body="H"))
        real = merge._read_nofollow
        def fake(mdir, fn):
            return None if "hub" in str(mdir) else real(mdir, fn)
        env = {"XDG_CACHE_HOME": str(self.cache), "XDG_CONFIG_HOME": str(self.tmp / "cfg")}
        with mock.patch.dict(os.environ, env), \
                mock.patch("claude_session_sync.scan._git_identity", _name_match), \
                mock.patch("claude_session_sync.merge._read_nofollow", fake), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["memory-merge", "--hub", str(self.hub),
                           "--local-root", str(self.local), "--state", str(self.state)])
        self.assertEqual(rc, 1)

    def test_apply_degraded_no_done_and_prompt_warns(self):
        # codex gate2 F1：退化衝突 --apply → 無 .done、status degraded、PROMPT/CONFLICT.json 帶不完整警告。
        self._wm(self.lA, "a.md", _mem("a", body="L"))
        self._wm(self.hA, "a.md", _mem("a", body="H"))
        real = merge._read_nofollow
        def fake(mdir, fn):
            return None if "hub" in str(mdir) else real(mdir, fn)
        with mock.patch("claude_session_sync.merge._read_nofollow", fake):
            c = next(c for c in self._conflicts() if c.key == "a.md")
        res = merge.stage_conflict(c, root=self.cache, apply=True)
        self.assertEqual(res.status, "degraded")
        self.assertFalse((res.dest / merge.DONE_FILE).exists())          # 退化 → 無完成標記
        self.assertIn("不完整", (res.dest / merge.PROMPT_FILE).read_text(encoding="utf-8"))
        meta = json.loads((res.dest / merge.META_FILE).read_text(encoding="utf-8"))
        self.assertFalse(meta["complete"])
        self.assertTrue(meta["notes"])

    @_caps.needs_symlink
    def test_final_symlink_refused_without_o_nofollow(self):
        # codex gate2 F2：模擬 Windows（O_NOFOLLOW=0）→ 最終檔為 symlink 仍由 is_symlink 擋下、不跟隨。
        secret = self.tmp / "secret.md"
        secret.write_text("SECRET", encoding="utf-8")
        mdir = self.lA / "memory"
        mdir.mkdir(parents=True, exist_ok=True)
        os.symlink(secret, mdir / "link.md")
        with mock.patch.object(os, "O_NOFOLLOW", 0, create=True):
            self.assertIsNone(merge._read_nofollow(mdir, "link.md"))

    def test_cross_file_unreadable_member_degrades_survivor(self):
        # codex gate3 F1：cross-file 成員規劃後讀不到 → 倖存成員那組也須退化（不可寫 .done 而卡住缺版本）。
        self._wm(self.lA, "old.md", _mem("fact", body="OLD"))
        self._wm(self.hA, "new.md", _mem("fact", body="NEW"))
        real = merge._read_nofollow
        def fake(mdir, fn):
            return None if fn == "new.md" else real(mdir, fn)
        with mock.patch("claude_session_sync.merge._read_nofollow", fake):
            cs = [c for c in self._conflicts() if c.kind == "conflict-cross-file-identity"]
        survivor = next(c for c in cs if any(v.filename == "old.md" for v in c.versions))
        self.assertTrue(survivor.notes)                         # 倖存組帶退化警告
        res = merge.stage_conflict(survivor, root=self.cache, apply=True)
        self.assertEqual(res.status, "degraded")
        self.assertFalse((res.dest / merge.DONE_FILE).exists())  # 不寫完成標記

    def test_cross_file_member_renamed_degrades(self):
        # codex gate4 F1：cross-file 成員規劃後**改名（仍可讀）**→ 分組裂成 singleton → 全退化（不只「讀不到」）。
        self._wm(self.lA, "a.md", _mem("fact", body="A"))
        self._wm(self.hA, "b.md", _mem("fact", body="B"))
        real = merge._read_nofollow
        changed = _mem("renamed", body="A").encode("utf-8")
        def fake(mdir, fn):
            return changed if fn == "a.md" else real(mdir, fn)
        with mock.patch("claude_session_sync.merge._read_nofollow", fake):
            cs = [c for c in self._conflicts() if c.kind == "conflict-cross-file-identity"]
        self.assertTrue(cs)
        self.assertTrue(all(c.notes for c in cs))            # 全組退化（無一被當完整）

    def test_delete_vs_update_tombstone_lost_degrades(self):
        # codex gate4 F2：delete-vs-update 規劃後現存檔改名 → tombstone re-discover 不到 → 退化、刪除側不靜默漏掉。
        self._wm(self.lA, "f.md", _mem("fact", body="UPD"))
        tombstone.write_memory_tombstone(self.hA, "old.md", base_hash="b1", identity="fact", machine="m")
        real = merge._read_nofollow
        changed = _mem("other", body="UPD").encode("utf-8")
        def fake(mdir, fn):
            return changed if fn == "f.md" else real(mdir, fn)
        with mock.patch("claude_session_sync.merge._read_nofollow", fake):
            c = next(c for c in self._conflicts() if c.kind == "conflict-delete-vs-update")
        self.assertTrue(c.notes)
        self.assertFalse(any(v.is_tombstone for v in c.versions))

    def test_unscannable_trusts_plan_flag(self):
        # codex gate4 F3：plan 記錄 memory 未掃（memory_scan_failed）→ 即使現在 FS recheck 成功仍須列出（不可抹掉）。
        (self.hA / "memory").mkdir(parents=True, exist_ok=True)
        (self.lA / "memory").mkdir(parents=True, exist_ok=True)
        pp = scan.ProjectPlan(local_dir=str(self.lA), hub_dir=str(self.hA), identity="match",
                              coverage_initialized=True, memory_scan_failed=True)
        plan = scan.SyncPlan(first_run=False, anomalies=[], projects=[pp])
        us = merge.unscannable_memory_projects(plan)
        self.assertTrue(any("projA" in u and "未掃描" in u for u in us))

    def test_nonstring_base_hash_tombstone_blocked(self):
        # codex gate7 F2：tombstone base_hash 非字串 → corrupt（blocked）、不放行給 merge 的 _short 切片整數而崩。
        tdir = tombstone.tombstones_dir(self.hA)
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "memory-f.md.deleted.json").write_text(
            json.dumps({"schema_version": 1, "kind": "memory", "target": "f.md",
                        "base_hash": 123, "identity": "f"}), encoding="utf-8")
        self.assertIsNone(tombstone.read_tombstones(self.hA).get(("memory", "f.md")))   # 非有效
        self.assertIn(("memory", "f.md"), tombstone.corrupt_tombstone_targets(self.hA))  # 落 corrupt

    @_caps.needs_unreadable_dir
    def test_unreadable_memory_dir_no_crash(self):
        # codex gate3 F2：不可讀 memory/ 夾 → build_plan 不崩、記 note、unscannable 回報。
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root 略過目錄權限，無法模擬 EACCES")
        self._wm(self.lA, "a.md", _mem("a"))
        (self.hA / "memory").mkdir(parents=True, exist_ok=True)
        mdir = self.lA / "memory"
        os.chmod(mdir, 0)
        try:
            plan = scan.build_plan(self.local, self.hub, state_mod.load_or_none(self.state),
                                   identity_fn=_name_match)   # 不得 raise
            pp = next(p for p in plan.projects if p.hub_dir and Path(p.hub_dir).name == "projA")
            self.assertTrue(any("無法讀取" in n for n in pp.notes))
            self.assertTrue(merge.unscannable_memory_projects(plan))
        finally:
            os.chmod(mdir, 0o755)

    @_caps.needs_symlink
    def test_unscannable_memory_surfaced_and_nonzero(self):
        # codex gate2 F3：memory/ 根 symlink → unscannable 列出 + CLI 非零（不誤報「無衝突」）。
        outside = self.tmp / "out"
        outside.mkdir()
        (self.hA / "memory").mkdir(parents=True, exist_ok=True)
        os.symlink(outside, self.lA / "memory")
        plan = scan.build_plan(self.local, self.hub, state_mod.load_or_none(self.state),
                               identity_fn=_name_match)
        self.assertTrue(any("projA" in u for u in merge.unscannable_memory_projects(plan)))
        env = {"XDG_CACHE_HOME": str(self.cache), "XDG_CONFIG_HOME": str(self.tmp / "cfg")}
        with mock.patch.dict(os.environ, env), \
                mock.patch("claude_session_sync.scan._git_identity", _name_match), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["memory-merge", "--hub", str(self.hub),
                           "--local-root", str(self.local), "--state", str(self.state)])
        self.assertEqual(rc, 1)


class TestPrompt(_Harness, unittest.TestCase):
    def test_prompt_has_warning_and_both_versions(self):
        self._wm(self.lA, "a.md", _mem("a", body="LBODY"))
        self._wm(self.hA, "a.md", _mem("a", body="HBODY"))
        p = merge.build_prompt(self._conflicts()[0])
        self.assertIn("明文外洩警告", p)
        self.assertIn("JSONL", p)
        self.assertIn("LBODY", p)
        self.assertIn("HBODY", p)
        self.assertIn("合併", p)

    def test_prompt_mentions_tombstone(self):
        self._wm(self.lA, "f.md", _mem("f", body="UPD"))
        tombstone.write_memory_tombstone(self.hA, "f.md",
                                         base_hash=self._base_hash(_mem("f", body="ORIG")),
                                         identity="f", machine="m2")
        p = merge.build_prompt(self._conflicts()[0])
        self.assertIn("刪除", p)
        self.assertIn("f.md", p)        # tombstone target 列出
        self.assertIn("UPD", p)


class TestHardening(_Harness, unittest.TestCase):
    def test_backtick_content_uses_longer_fence(self):
        # 正文含 ``` code block → fence 須更長，不被提前關閉。
        self._wm(self.lA, "a.md", _mem("a", body="```py\nx=1\n```"))
        self._wm(self.hA, "a.md", _mem("a", body="OTHER"))
        c = next(c for c in self._conflicts() if c.key == "a.md")
        p = merge.build_prompt(c)
        self.assertIn("````md", p)        # ≥4 backticks
        self.assertIn("```py", p)         # 內容原樣保留

    def test_control_char_filename_does_not_crash(self):
        # 控制字元/surrogate 檔名：build_prompt / CONFLICT.json / PROMPT.md 寫入不得 UnicodeEncodeError 崩潰。
        v_local = merge.ConflictVersion("local", "wei\nrd\udcff.md", "h1", text="A", data=b"A")
        v_hub = merge.ConflictVersion("hub", "wei\nrd\udcff.md", "h2", text="B", data=b"B")
        c = merge.MemoryConflict("proj\udcff", "conflict-content", "wei\nrd\udcff.md",
                                 (v_local, v_hub), "r")
        prompt = merge.build_prompt(c)            # 不崩
        prompt.encode("utf-8")                    # 可編碼（無 lone surrogate / 控制字元破行）
        res = merge.stage_conflict(c, root=self.cache, apply=True)
        self.assertEqual(res.status, "staged")
        json.loads((res.dest / merge.META_FILE).read_text(encoding="utf-8"))  # 合法 JSON
        # 原始 bytes 仍原樣保留（淨化只用於顯示）
        staged = list(res.dest.glob("*__*"))
        self.assertEqual({p.read_bytes() for p in staged}, {b"A", b"B"})


class TestSafeComponent(unittest.TestCase):
    def test_traversal_neutralized(self):
        self.assertNotIn("/", merge._safe_component("../../etc/passwd"))
        self.assertNotEqual(merge._safe_component(".."), "..")
        self.assertNotEqual(merge._safe_component("."), ".")
        self.assertNotEqual(merge._safe_component(""), "")
        self.assertEqual(merge._safe_component("fact.md"), "fact.md")   # slug 原樣可讀

    def test_injective_no_collision(self):
        # codex R1 High：`a:b.md` 與 `a?b.md` 不可撞同名（會丟版本/誤判 already-staged）。
        cases = ["a:b.md", "a?b.md", "a b.md", "local+hub__a.md", "..", ".", "fact.md", "a/b.md"]
        outs = [merge._safe_component(s) for s in cases]
        self.assertEqual(len(set(outs)), len(cases))           # 全互異（injective）
        self.assertTrue(all("/" not in o for o in outs))       # 無路徑分隔
        self.assertNotEqual(merge._safe_component("a:b.md"), merge._safe_component("a?b.md"))

    def test_long_component_bounded_and_injective(self):
        # codex R2 Medium：非 ASCII 長檔名 percent-encode 膨脹 → 須 bounded < NAME_MAX，且仍 injective。
        s1 = "local__" + "é" * 80 + ".md"
        s2 = "local__" + "é" * 80 + "X.md"
        o1, o2 = merge._safe_component(s1), merge._safe_component(s2)
        self.assertLessEqual(len(o1.encode("utf-8")), 200)
        self.assertLessEqual(len(o2.encode("utf-8")), 200)
        self.assertNotEqual(o1, o2)                            # 不同原名 → 不同（digest 區分）


class TestUnsafeRoot(_Harness, unittest.TestCase):
    def test_relative_root_rejected(self):
        self.assertIsNotNone(merge.unsafe_staging_root(Path("rel/cache"), [self.hub, self.local]))

    def test_root_inside_hub_rejected(self):
        bad = self.hub / "cachedir"
        self.assertIsNotNone(merge.unsafe_staging_root(bad, [self.hub, self.local]))

    def test_root_outside_ok(self):
        self.assertIsNone(merge.unsafe_staging_root(self.cache, [self.hub, self.local]))

    def test_root_contains_hub_rejected(self):
        # codex gate7 F1：hub 在 cache root **內**（root ⊃ hub）→ per-conflict dest 可能落進 hub → 雙向重疊須拒。
        hub_inside = self.cache / "projA"
        self.assertIsNotNone(merge.unsafe_staging_root(self.cache, [hub_inside, self.local]))

    def test_root_inside_hub_casefold_rejected(self):
        # mmfrom-g4 High：macOS 預設 APFS 大小寫不敏感 → hub `HubCase` 與 cache 路徑 `hubcase/...` 是同一實體，
        # 但 PosixPath 大小寫敏感 + resolve 保留拼寫 → 舊 is_relative_to 漏判 → 暫存落進 hub 外洩。逐段 casefold
        # 前綴比對在**任何** OS 上都能認出（不依賴 runner FS 是否真的大小寫不敏感；resolve 對不存在路徑保留拼寫）。
        hub_ci = self.tmp / "HubCase"
        root_ci = self.tmp / "hubcase" / "cache"          # 僅大小寫不同 → 同一實體
        self.assertIsNotNone(merge.unsafe_staging_root(root_ci, [hub_ci, self.local]))

    def test_root_inside_hub_nfc_nfd_rejected(self):
        import unicodedata
        # 同一 leak class 的 NFC/NFD 變體（e2e g8 檔名別名的路徑版）：hub NFC「café」、cache NFD「café」在 macOS
        # 為同一實體、位元組不同 → 舊比對漏判。_name_key 的 NFC∘casefold∘NFC 折疊之。NFD 由程式導出（原始碼只留
        # 一個 NFC 字面 + assertNotEqual 護欄，確保真的在測折疊、而非兩字面位元組相同而 trivially pass）。
        base = "caféhub"
        nfc = unicodedata.normalize("NFC", base)
        nfd = unicodedata.normalize("NFD", base)
        self.assertNotEqual(nfc, nfd)
        hub_nfc = self.tmp / nfc
        root_nfd = self.tmp / nfd / "cache"
        self.assertIsNotNone(merge.unsafe_staging_root(root_nfd, [hub_nfc, self.local]))

    def test_casefold_unrelated_dirs_still_safe(self):
        # 不可矯枉過正（守住「只多拒真重疊、非 naive 字串前綴」）：僅**字尾**不同的相異夾 casefold 後仍非
        # 逐段前綴關係 → 安全（naive `str.startswith` 會誤把 `hubbase-cache` 當在 `hubbase` 下 → 誤拒）。
        sib = self.tmp / "hubbase"
        root = self.tmp / "hubbase-cache"                 # 段名 `hubbase-cache` ≠ `hubbase` → 非其下
        self.assertIsNone(merge.unsafe_staging_root(root, [sib, self.local]))

    def test_cli_apply_refuses_unsafe_root(self):
        # XDG_CACHE_HOME 指到 hub 內 → --apply 拒絕保留、非零退出、不寫進同步區。
        self._wm(self.lA, "a.md", _mem("a", body="L"))
        self._wm(self.hA, "a.md", _mem("a", body="H"))
        env = {"XDG_CACHE_HOME": str(self.hub / "cache"), "XDG_CONFIG_HOME": str(self.tmp / "cfg")}
        with mock.patch.dict(os.environ, env), \
                mock.patch("claude_session_sync.scan._git_identity", _name_match), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["memory-merge", "--apply", "--hub", str(self.hub),
                           "--local-root", str(self.local), "--state", str(self.state)])
        self.assertEqual(rc, 1)
        self.assertFalse((self.hub / "cache").exists())   # 沒寫進 hub


class TestCrossFileGrouping(_Harness, unittest.TestCase):
    def test_divergent_side_identity_grouped_together(self):
        # codex R1 Medium：a.md 在 local=xname、hub=yname；b.md(hub)=yname → a/b 應同組（不漏 yname 合併群）。
        self._wm(self.lA, "a.md", _mem("xname", body="A-LOCAL"))
        self._wm(self.hA, "a.md", _mem("yname", body="A-HUB"))
        self._wm(self.hA, "b.md", _mem("yname", body="B-HUB"))
        cs = [c for c in self._conflicts() if c.kind == "conflict-cross-file-identity"]
        self.assertEqual(len(cs), 1)                    # 一組，不是兩組
        fnames = {v.filename for v in cs[0].versions}
        self.assertEqual(fnames, {"a.md", "b.md"})


class TestMultiTombstone(_Harness, unittest.TestCase):
    def test_all_matching_tombstones_included(self):
        # codex R1 Medium：同 identity 多個別檔名 tombstone → 全數附上（不只第一個）。
        self._wm(self.lA, "new.md", _mem("fact", body="UPD"))
        tombstone.write_memory_tombstone(self.hA, "old1.md", base_hash="b1", identity="fact", machine="m1")
        tombstone.write_memory_tombstone(self.hA, "old2.md", base_hash="b2", identity="fact", machine="m2")
        c = next(c for c in self._conflicts() if c.kind == "conflict-delete-vs-update")
        tomb_targets = sorted(v.filename for v in c.versions if v.is_tombstone)
        self.assertEqual(tomb_targets, ["old1.md", "old2.md"])
        p = merge.build_prompt(c)                       # 提示詞須**兩筆都**呈現（不只第一筆）
        self.assertIn("old1.md", p)
        self.assertIn("old2.md", p)


class TestMergeRoot(unittest.TestCase):
    def test_respects_xdg_cache_home(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "/tmp/xdgcache"}):
            self.assertEqual(merge.merge_root(),
                             Path("/tmp/xdgcache") / "claude-session-sync" / "merge")


class TestCLI(_Harness, unittest.TestCase):
    def _run(self, *args):
        env = {"XDG_CACHE_HOME": str(self.cache), "XDG_CONFIG_HOME": str(self.tmp / "cfg")}
        # CLI 走預設 git 同一性（temp 夾無 git → 不配對）→ patch 成夾名配對，測端到端 staging。
        with mock.patch.dict(os.environ, env), \
                mock.patch("claude_session_sync.scan._git_identity", _name_match), \
                contextlib.redirect_stdout(io.StringIO()):
            return cli.main(["memory-merge", "--hub", str(self.hub),
                             "--local-root", str(self.local), "--state", str(self.state), *args])

    def test_dry_run_reports_no_writes(self):
        self._wm(self.lA, "a.md", _mem("a", body="L"))
        self._wm(self.hA, "a.md", _mem("a", body="H"))
        rc = self._run()
        self.assertEqual(rc, 0)
        # dry-run 不寫暫存
        self.assertFalse((self.cache / "claude-session-sync").exists())

    def test_apply_stages(self):
        self._wm(self.lA, "a.md", _mem("a", body="L"))
        self._wm(self.hA, "a.md", _mem("a", body="H"))
        rc = self._run("--apply")
        self.assertEqual(rc, 0)
        dest = self.cache / "claude-session-sync" / "merge" / "projA" / "a.md"
        self.assertTrue((dest / "PROMPT.md").exists())

    def test_no_conflicts_returns_zero(self):
        self._wm(self.lA, "a.md", _mem("a"))
        self._wm(self.hA, "a.md", _mem("a"))
        self.assertEqual(self._run(), 0)


if __name__ == "__main__":
    unittest.main()
