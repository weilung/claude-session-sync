"""P2：`memory-merge --from <remote>`（跨群 memory 衝突偵測）。

跨群＝偵測本機 memory ↔ remote hub memory 的衝突（conflict-content / remote-tombstone delete-vs-update），
保留兩版到**本機**快取（memory/ 與兩側 hub 之外，不外洩）。stateless（無 per-remote 基線，對稱 transfer）。
關鍵不變量：**保留兩版絕不落進 local/remote 任一受同步樹**（DoD 不外洩）；只讀正式 memory、絕不寫回。
"""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_session_sync import cli, memory, merge as merge_mod, state as state_mod, tombstone
from claude_session_sync.config import Config
from claude_session_sync.state import State


def _mem(body, slug="fact"):
    return "\n".join(["---", f"name: {slug}", "description: d",
                      "metadata:", "  type: project", "---", body, ""])


class TestMemoryMergeFrom(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.local = self.tmp / "local"
        self.remote = self.tmp / "remoteHub"
        self.cache = self.tmp / "cache"
        self.own_hub = self.tmp / "ownhub"
        self.lA = self.local / "projA" / "memory"
        self.rA = self.remote / "projA" / "memory"
        self.lA.mkdir(parents=True)
        self.rA.mkdir(parents=True)
        tombstone.write_coverage(self.remote / "projA")
        self.cfg = Config(own_hub=str(self.own_hub), remotes={"office": str(self.remote)})

    def tearDown(self):
        self._td.cleanup()

    def _run(self, argv, *, cache=None):
        out, err = io.StringIO(), io.StringIO()
        env = {"XDG_CACHE_HOME": str(cache or self.cache)}
        with mock.patch.object(cli.config_mod, "load", return_value=self.cfg), \
                mock.patch.dict(os.environ, env), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["memory-merge", "--local-root", str(self.local), *argv])
        return code, out.getvalue(), err.getvalue()

    def _conflict(self):
        (self.lA / "a.md").write_text(_mem(body="local ver"), encoding="utf-8")
        (self.rA / "a.md").write_text(_mem(body="remote ver"), encoding="utf-8")

    # ── 偵測 ──────────────────────────────────────────────────────────────────

    def test_conflict_content_dry_run(self):
        self._conflict()
        code, out, _ = self._run(["--from", "office", "--map", "projA=projA"])
        self.assertEqual(code, 0)
        self.assertIn("projA", out)
        self.assertIn("a.md", out)
        self.assertIn("明文外洩警告", out)   # LEAK_WARNING 有印

    def test_delete_vs_update_dry_run(self):
        # remote 有 a.md 的 tombstone（base≠local 現值）+ local 改過 → conflict-delete-vs-update（A3 跨群不復活）。
        (self.lA / "a.md").write_text(_mem(body="local updated"), encoding="utf-8")
        old = self.tmp / "old.md"
        old.write_text(_mem(body="old base"), encoding="utf-8")
        base = memory.content_hash(memory.load_memory(old))
        tombstone.write_memory_tombstone(self.remote / "projA", "a.md", base_hash=base)
        code, out, _ = self._run(["--from", "office", "--map", "projA=projA"])
        self.assertEqual(code, 0)
        self.assertIn("a.md", out)

    def test_no_conflict_when_identical(self):
        (self.lA / "a.md").write_text(_mem(body="same"), encoding="utf-8")
        (self.rA / "a.md").write_text(_mem(body="same"), encoding="utf-8")
        code, out, _ = self._run(["--from", "office", "--map", "projA=projA"])
        self.assertEqual(code, 0)
        self.assertIn("未偵測到 memory 衝突", out)

    # ── 保留兩版（leak-safety）─────────────────────────────────────────────────

    def test_apply_stages_to_local_cache_not_synced_trees(self):
        self._conflict()
        code, out, _ = self._run(["--from", "office", "--map", "projA=projA", "--apply"])
        self.assertEqual(code, 0)
        staged = self.cache / "claude-session-sync" / "merge" / "projA" / "a.md"
        got = {p.name for p in staged.iterdir()} if staged.is_dir() else set()
        self.assertTrue({"local__a.md", "hub__a.md", "PROMPT.md", "CONFLICT.json", ".done"} <= got, got)
        # **不外洩**：保留兩版絕不落進 local/remote 任一受同步 memory 樹。
        self.assertEqual({p.name for p in self.rA.iterdir()}, {"a.md"})
        self.assertEqual({p.name for p in self.lA.iterdir()}, {"a.md"})

    def test_apply_refuses_when_cache_inside_hub_override(self):
        # codex R1 High：staging_forbidden 必含**本次生效的 own hub**——`--hub` override 優先於 config；否則暫存
        # 落進使用者明指的 own hub 被自群同步＝外洩。
        self._conflict()
        hub_override = self.tmp / "explicit_hub"
        hub_override.mkdir()
        cache_in_hub = hub_override / "cache"
        code, out, err = self._run(
            ["--from", "office", "--map", "projA=projA", "--hub", str(hub_override), "--apply"],
            cache=cache_in_hub)
        self.assertEqual(code, 1)
        self.assertIn("暫存根不安全", err)
        self.assertFalse((cache_in_hub / "claude-session-sync").exists())

    def test_apply_refuses_when_cache_inside_remote(self):
        # 暫存根落在 remote hub **內** → unsafe_staging_root fail-closed 拒絕（否則兩版落進 remote 被對方群同步）。
        self._conflict()
        bad_cache = self.remote / "sneaky-cache"
        code, out, err = self._run(["--from", "office", "--map", "projA=projA", "--apply"], cache=bad_cache)
        self.assertEqual(code, 1)
        self.assertIn("暫存根不安全", err)
        # 什麼都沒 stage 進 remote。
        self.assertEqual({p.name for p in self.rA.iterdir()}, {"a.md"})
        self.assertFalse((bad_cache / "claude-session-sync").exists())

    def test_apply_forbids_configured_own_hub_even_with_hub_override(self):
        # g2 High：--hub override 與 cfg.own_hub **兩者**都是受同步樹 → 都須 forbidden（非 `or` 互斥）。cache 落在
        # config own_hub（非 override）內時仍須拒。
        self._conflict()
        self.own_hub.mkdir()
        override = self.tmp / "override_hub"
        override.mkdir()
        cache_in_config_own = self.own_hub / "cache"
        code, out, err = self._run(
            ["--from", "office", "--map", "projA=projA", "--hub", str(override), "--apply"],
            cache=cache_in_config_own)
        self.assertEqual(code, 1)
        self.assertIn("暫存根不安全", err)
        self.assertFalse((cache_in_config_own / "claude-session-sync").exists())

    def test_dup_target_scoped_by_project_no_false_positive(self):
        # g2 Medium：--project 限定正常 1:1 專案時，另一無關 dup-target 不該誤觸警告/非零。
        (self.local / "good" / "memory").mkdir(parents=True)
        (self.local / "good" / "memory" / "g.md").write_text(_mem(body="g local"), encoding="utf-8")
        (self.remote / "good" / "memory").mkdir(parents=True)
        (self.remote / "good" / "memory" / "g.md").write_text(_mem(body="g remote"), encoding="utf-8")
        tombstone.write_coverage(self.remote / "good")
        for d in ("x", "y"):                       # x,y 兩 local → 同 remote 夾 projA（dup，但不在 --project good 範圍）
            (self.local / d / "memory").mkdir(parents=True)
            (self.local / d / "memory" / "a.md").write_text(_mem(body=d), encoding="utf-8")
        (self.rA / "a.md").write_text(_mem(body="shared remote"), encoding="utf-8")
        code, out, _ = self._run(["--from", "office", "--project", "good",
                                  "--map", "good=good", "--map", "x=projA", "--map", "y=projA"])
        self.assertEqual(code, 0)                  # good 1:1 正常，無關 dup 未誤觸
        self.assertNotIn("映到同一", out)           # dup 警告未誤印
        self.assertIn("g.md", out)                 # good 的衝突有列

    def test_apply_refuses_when_cache_inside_other_remote(self):
        # g1 強化：暫存根落在**另一個** remote（非本次 --from 的）內也須拒（否則被那一群同步＝外洩）。
        self._conflict()
        other = self.tmp / "homeHub"
        other.mkdir()
        self.cfg = Config(own_hub=str(self.own_hub),
                          remotes={"office": str(self.remote), "home": str(other)})
        bad_cache = other / "cache"
        code, out, err = self._run(["--from", "office", "--map", "projA=projA", "--apply"], cache=bad_cache)
        self.assertEqual(code, 1)
        self.assertIn("暫存根不安全", err)
        self.assertFalse((bad_cache / "claude-session-sync").exists())

    # ── 配對 / 錯誤路徑 ────────────────────────────────────────────────────────

    def test_no_map_shows_needs_map_hint(self):
        # 無 --map、remote 無 `_project.json` sidecar → git 指紋判不出 → needs-map；提示用 --map。
        self._conflict()
        code, out, _ = self._run(["--from", "office"])
        self.assertEqual(code, 0)
        self.assertIn("--map", out)

    def test_unpaired_hint_shown_even_with_conflict(self):
        # codex R1 Medium：有衝突時也要印未配對專案（否則 partial scan 被包成完整掃描）。
        self._conflict()                       # projA 兩側 → conflict
        (self.local / "projB" / "memory").mkdir(parents=True)
        (self.local / "projB" / "memory" / "x.md").write_text(_mem(body="only local"), encoding="utf-8")
        code, out, _ = self._run(["--from", "office", "--map", "projA=projA"])
        self.assertEqual(code, 0)
        self.assertIn("a.md", out)             # projA 衝突有列
        self.assertIn("未對應到 remote", out)   # projB 未配對也有提示（非只在無衝突時）

    def test_scope_caveat_always_printed(self):
        # codex R1 Medium：誠實範圍聲明——「未偵測到衝突」不代表跨檔改名也沒有。
        (self.lA / "a.md").write_text(_mem(body="same"), encoding="utf-8")
        (self.rA / "a.md").write_text(_mem(body="same"), encoding="utf-8")
        code, out, _ = self._run(["--from", "office", "--map", "projA=projA"])
        self.assertEqual(code, 0)
        self.assertIn("跨檔改名", out)          # 範圍聲明有印（即使無衝突）

    def test_unknown_remote_errors(self):
        code, out, err = self._run(["--from", "nope", "--map", "projA=projA"])
        self.assertEqual(code, 1)
        self.assertIn("未知 remote", err)

    def test_missing_remote_dir_halts(self):
        # remote hub 路徑不存在（未掛載）→ build_plan halt → exit 2。
        self.cfg = Config(own_hub=str(self.own_hub), remotes={"office": str(self.tmp / "nohub")})
        self._conflict()
        code, out, _ = self._run(["--from", "office", "--map", "projA=projA"])
        self.assertEqual(code, 2)

    def test_bad_map_format_errors(self):
        code, out, err = self._run(["--from", "office", "--map", "noequals"])
        self.assertEqual(code, 1)
        self.assertIn("--map", err)

    def test_own_hub_path_unaffected_by_from_absence(self):
        # 不給 --from → 走 own-hub 路徑（此處 own_hub 不存在 → mount halt exit 2），證明分派正確、未誤入跨群。
        self._conflict()
        code, out, _ = self._run([])
        self.assertEqual(code, 2)   # own_hub 未掛載 → halt（走 own-hub 路徑，非跨群）

    def test_own_hub_apply_refuses_cache_inside_configured_remote(self):
        # g1 High：own-hub 路徑（無 --from）的 staging_forbidden 也須含**所有 config remote**——否則 XDG_CACHE 落在
        # 某 remote hub 內時 own-hub `--apply` 會把兩版 stage 進該 remote 被對方群同步＝外洩。
        self.own_hub.mkdir()
        (self.own_hub / "projA" / "memory").mkdir(parents=True)
        (self.own_hub / "projA" / "memory" / "a.md").write_text(_mem(body="hub ver"), encoding="utf-8")
        (self.lA / "a.md").write_text(_mem(body="local ver"), encoding="utf-8")
        tombstone.write_coverage(self.own_hub / "projA")
        state_file = self.tmp / "state.json"
        state_mod.save(State(known_sessions={"projA": set()}, local_sessions={"projA": set()},
                             known_memory={"projA": set()}, local_memory={"projA": set()},
                             local_dir_bindings={"projA": "projA"}), state_file)
        cache_in_remote = self.remote / "cache"      # self.remote = config remote "office"
        code, out, err = self._run(["--apply", "--state", str(state_file)], cache=cache_in_remote)
        self.assertEqual(code, 1)
        self.assertIn("暫存根不安全", err)
        self.assertFalse((cache_in_remote / "claude-session-sync").exists())

    def test_dup_target_mapping_skipped(self):
        # g1 Medium：兩個 local 專案 --map 到同一 remote 夾 → 撞夾 → 跳過該夾所有衝突 + 警告 + 非零（不靜默丟）。
        (self.lA / "a.md").write_text(_mem(body="A local"), encoding="utf-8")
        (self.local / "projB" / "memory").mkdir(parents=True)
        (self.local / "projB" / "memory" / "a.md").write_text(_mem(body="B local"), encoding="utf-8")
        (self.rA / "a.md").write_text(_mem(body="remote"), encoding="utf-8")   # remote 夾名 = projA
        code, out, _ = self._run(["--from", "office", "--map", "projA=projA", "--map", "projB=projA"])
        self.assertEqual(code, 1)                     # 撞夾 → 非零
        self.assertIn("映到同一", out)                 # 警告有印
        # 撞夾的衝突被跳過（不獨立保留、不覆蓋）。
        self.assertNotIn("=== 保留兩版", out)

    def test_unsafe_staging_root_fail_closed_on_forbidden_resolve_error(self):
        # g3 High：某 forbidden 受同步樹 resolve 拋 OSError → 不可 continue 略過（無法證明暫存不重疊）→ fail-closed。
        root = self.tmp / "cache"
        ok = self.tmp / "localtree"
        bad = self.tmp / "offline_remote"
        orig = Path.resolve

        def fake(self, *a, **k):
            if str(self) == str(bad):
                raise OSError("cannot resolve")
            return orig(self, *a, **k)

        with mock.patch.object(Path, "resolve", fake):
            reason = merge_mod.unsafe_staging_root(root, [ok, bad])
        self.assertIsNotNone(reason)       # 保守拒絕（非 None）
        self.assertIn("無法解析", reason)

    def test_unpaired_hint_scoped_by_project(self):
        # g1 Low：--project 限定時，未配對警告只算該 project、不報無關專案。
        self._conflict()                              # projA 配對成功（有衝突）
        (self.local / "projB" / "memory").mkdir(parents=True)
        (self.local / "projB" / "memory" / "x.md").write_text(_mem(body="only local"), encoding="utf-8")
        code, out, _ = self._run(["--from", "office", "--map", "projA=projA", "--project", "projA"])
        self.assertEqual(code, 0)
        self.assertNotIn("未對應到 remote", out)       # 無關的 projB 未配對不報（scoped）


if __name__ == "__main__":
    unittest.main()
