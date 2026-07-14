import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from claude_session_sync import cli
from tests import _caps, fixtures as fx


class TestCli(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.local = self.tmp / "local"
        self.hub = self.tmp / "hub"
        self.local.mkdir()
        self.hub.mkdir()
        self.state = self.tmp / "state.json"  # 不存在 → first run

    def tearDown(self):
        self._td.cleanup()

    def _run(self, argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_status_first_run(self):
        code, out, _ = self._run(
            ["status", "--hub", str(self.hub), "--local-root", str(self.local), "--state", str(self.state)]
        )
        self.assertEqual(code, 0)
        self.assertIn("首次同步", out)

    def test_non_utf8_stdout_does_not_crash(self):
        """Big5（cp950）主控台＋輸出導向管線：print `⚠` 曾直接 UnicodeEncodeError 炸掉指令。"""
        buf = io.BytesIO()
        wrapper = io.TextIOWrapper(buf, encoding="cp950")
        with contextlib.redirect_stdout(wrapper):
            code = cli.main(
                ["status", "--hub", str(self.hub), "--local-root", str(self.local), "--state", str(self.state)]
            )
            wrapper.flush()
        self.assertEqual(code, 0)
        self.assertIn("⚠", buf.getvalue().decode("utf-8"))

    def test_missing_hub_errors(self):
        # 沒 --hub 也沒 config → 期望非零。patch default_config_path 指向不存在的檔，不再依賴「真機剛好沒 config」
        #（Windows 的 config 走 %APPDATA%、不讀 XDG → 真機一設過 own_hub 原寫法只能永遠 skip）。
        from unittest import mock
        from claude_session_sync import config as cfg
        with mock.patch.object(cfg, "default_config_path", return_value=self.tmp / "no-such-config.toml"):
            code, _, err = self._run(["status", "--local-root", str(self.local), "--state", str(self.state)])
        self.assertEqual(code, 1)
        self.assertIn("own_hub", err)

    def test_halt_on_missing_mount(self):
        code, out, _ = self._run(
            ["status", "--hub", str(self.tmp / "nohub"), "--local-root", str(self.local), "--state", str(self.state)]
        )
        self.assertEqual(code, 2)
        self.assertIn("mount-missing", out)

    def test_sync_apply_first_run_refused(self):
        (self.local / "projA").mkdir()
        (self.hub / "projA").mkdir()
        fx.write_jsonl(fx.linear(), str(self.local / "projA" / "s1.jsonl"))
        fx.write_jsonl(fx.fast_forward_of_linear(), str(self.hub / "projA" / "s1.jsonl"))
        # 首次同步（無 state）：--apply 應 halt 要求先 bootstrap（codex r15-1），不得寫入。
        code, out, _ = self._run(
            ["sync", "--apply", "--hub", str(self.hub), "--local-root", str(self.local), "--state", str(self.state)]
        )
        self.assertEqual(code, 2)
        self.assertIn("bootstrap", out)

    def test_bootstrap_preview_and_apply(self):
        # bootstrap 預覽不寫；--yes + --map 落地 coverage。
        (self.local / "projZ").mkdir()
        fx.write_jsonl(fx.linear(), str(self.local / "projZ" / "s1.jsonl"))
        common = ["--hub", str(self.hub), "--local-root", str(self.local), "--state", str(self.state)]
        code, out, _ = self._run(["bootstrap", *common, "--map", "projZ=encZ"])
        self.assertEqual(code, 0)
        self.assertIn("baseline", out)
        from claude_session_sync import tombstone
        self.assertFalse(tombstone.is_initialized(self.hub / "encZ"))  # 預覽未寫
        code, _, _ = self._run(["bootstrap", *common, "--map", "projZ=encZ", "--yes"])
        self.assertEqual(code, 0)
        self.assertTrue(tombstone.is_initialized(self.hub / "encZ"))

    def test_end_to_end_bootstrap_then_sync_no_git(self):
        # 無 git：bootstrap --map 建綁定 → sync 經綁定解析 → 單邊新檔 copy-to-hub。
        pz = self.local / "projZ"
        pz.mkdir()
        fx.write_jsonl(
            [fx.umsg("u1", None, "user", 1, cwd="/work/projZ"), fx.umsg("u2", "u1", "assistant", 2)],
            str(pz / "s1.jsonl"),
        )
        common = ["--hub", str(self.hub), "--local-root", str(self.local), "--state", str(self.state)]
        code, _, _ = self._run(["bootstrap", *common, "--map", "projZ=encZ", "--yes"])
        self.assertEqual(code, 0)
        code, out, _ = self._run(["sync", "--apply", *common])
        self.assertEqual(code, 0)
        self.assertTrue((self.hub / "encZ" / "s1.jsonl").exists())  # 綁定持久 → 已複製到 hub

    def test_sync_interactive_union(self):
        # 端到端：bootstrap 建綁定 → 製造 fork → sync --apply --interactive 餵 'u' → union keep-both 新檔。
        import builtins
        from unittest import mock
        pz = self.local / "projZ"
        pz.mkdir()
        fx.write_jsonl([fx.umsg("u1", None, "user", 1, cwd="/work/projZ"),
                        fx.umsg("u2", "u1", "assistant", 2),
                        fx.umsg("u4", "u2", "user", 4), fx.lastprompt("u4")],
                       str(pz / "s1.jsonl"))
        common = ["--hub", str(self.hub), "--local-root", str(self.local), "--state", str(self.state)]
        self._run(["bootstrap", *common, "--map", "projZ=encZ", "--yes"])
        # bootstrap 後寫 hub 分枝（u3）→ 與 local（u4）形成 fork。
        # 共同祖先 u1/u2 須與 local 逐位元組相同（含 cwd），否則同 uuid 異 hash → damaged。
        fx.write_jsonl([fx.umsg("u1", None, "user", 1, cwd="/work/projZ"),
                        fx.umsg("u2", "u1", "assistant", 2),
                        fx.umsg("u3", "u2", "user", 3), fx.lastprompt("u3")],
                       str(self.hub / "encZ" / "s1.jsonl"))
        with mock.patch.object(builtins, "input", lambda *a, **k: "u"):
            code, out, _ = self._run(["sync", "--apply", "--interactive", *common])
        self.assertEqual(code, 0)
        self.assertIn("union-merged", out)
        self.assertEqual(len(list(pz.glob("*.jsonl"))), 2)  # 原 s1 + union keep-both

    def test_interactive_write_error_nonzero_exit(self):
        # 互動寫入錯誤（disk full）→ resolve 回 error → CLI 非零退出（codex r23）。
        import builtins
        from unittest import mock
        from claude_session_sync import atomicio
        pz = self.local / "projZ"
        pz.mkdir()
        u12 = [fx.umsg("u1", None, "user", 1, cwd="/work/projZ"), fx.umsg("u2", "u1", "assistant", 2)]
        fx.write_jsonl(u12 + [fx.umsg("u4", "u2", "user", 4), fx.lastprompt("u4")], str(pz / "s1.jsonl"))
        common = ["--hub", str(self.hub), "--local-root", str(self.local), "--state", str(self.state)]
        self._run(["bootstrap", *common, "--map", "projZ=encZ", "--yes"])
        fx.write_jsonl(u12 + [fx.umsg("u3", "u2", "user", 3), fx.lastprompt("u3")],
                       str(self.hub / "encZ" / "s1.jsonl"))
        with mock.patch.object(builtins, "input", lambda *a, **k: "u"), \
             mock.patch.object(atomicio, "write_keep_both",
                               side_effect=atomicio.AtomicWriteError("disk full")):
            code, out, _ = self._run(["sync", "--apply", "--interactive", *common])
        self.assertEqual(code, 1)
        self.assertIn("error", out)

    def test_interactive_without_apply_is_noop_note(self):
        common = ["--hub", str(self.hub), "--local-root", str(self.local), "--state", str(self.state)]
        code, out, _ = self._run(["sync", "--interactive", *common])
        self.assertEqual(code, 0)
        self.assertIn("需搭配 --apply", out)

    def test_corrupt_config_aborts(self):
        # 壞 config（無 --hub）→ 保守中止訊息，非 stack trace（codex r6）
        # patch default_config_path 而非設 XDG_CONFIG_HOME：Windows 的 config 走 %APPDATA%、XDG 隔離是 no-op，
        # 真機一旦有合法 config（own_hub 在）就讀到真的 → status 正常跑完 exit 0，測試誤紅（2026-07-14 實機中招）。
        from unittest import mock
        from claude_session_sync import config as config_mod
        cfgdir = self.tmp / "cfg" / "claude-session-sync"
        cfgdir.mkdir(parents=True)
        cfg_path = cfgdir / "config.toml"
        cfg_path.write_text('force_unsafe_lock = "false"\n', encoding="utf-8")
        with mock.patch.object(config_mod, "default_config_path", return_value=cfg_path):
            code, _, err = self._run(["status", "--local-root", str(self.local), "--state", str(self.state)])
        self.assertEqual(code, 1)
        self.assertIn("config", err.lower())

    def test_interactive_stdin_eof_skips_gracefully(self):
        # stdin 斷線（被背景化/管線 EOF）→ 安全跳過、不 traceback（2026-07-14 實機中招：`!` 指令被手動背景化，
        # auto 項全部落地後才在 input() 崩、exit 1 且吞掉整份報告）。
        import builtins
        from unittest import mock
        from claude_session_sync import resolve as resolve_mod
        from claude_session_sync.session_merge import MergeOutcome
        ctx = resolve_mod.ResolveContext("s1", "fork", MergeOutcome.MERGED, "", [])
        with mock.patch.object(builtins, "input", side_effect=EOFError):
            d = cli._stdin_decider(ctx)
        self.assertEqual(d.choice, resolve_mod.Choice.SKIP)

    def test_interactive_tip_eof_skips_gracefully(self):
        # 同上，但斷在第二題（tip 編號）——選了 u 之後 stdin 才斷也不得 traceback。
        import builtins
        from types import SimpleNamespace
        from unittest import mock
        from claude_session_sync import resolve as resolve_mod
        from claude_session_sync.session_merge import MergeOutcome
        leaves = [SimpleNamespace(uuid="aaaaaaaa-1", ts="2026-07-14T01:00:00Z")]
        ctx = resolve_mod.ResolveContext("s1", "fork", MergeOutcome.NEEDS_DECISION, "多 leaf", leaves)
        with mock.patch.object(builtins, "input", side_effect=["u", EOFError]):
            d = cli._stdin_decider(ctx)
        self.assertEqual(d.choice, resolve_mod.Choice.SKIP)

    def test_contradictory_map_rejected(self):
        # codex mcwd-g4 #2：--map 同一 local 夾兩個矛盾目標 → 不可靜默 last-wins（--map 是斷言邊界）→ 非零。
        code, _, err = self._run(["bootstrap", "--hub", str(self.hub), "--local-root", str(self.local),
                                  "--state", str(self.state), "--map", "p=A", "--map", "p=B"])
        self.assertEqual(code, 1)
        self.assertIn("矛盾", err)

    def test_corrupt_state_aborts(self):
        self.state.write_text("{ not json", encoding="utf-8")
        code, _, err = self._run(
            ["status", "--hub", str(self.hub), "--local-root", str(self.local), "--state", str(self.state)]
        )
        self.assertEqual(code, 1)
        self.assertIn("state.json", err)

    def test_doctor_diagnose(self):
        (self.hub / "projA").mkdir()
        fx.write_jsonl(fx.linear(), str(self.hub / "projA" / "s1.jsonl"))
        code, out, _ = self._run(
            ["doctor", "--hub", str(self.hub), "--local-root", str(self.local), "--state", str(self.state)]
        )
        self.assertIn("hub 專案", out)
        self.assertIn("projA", out)

    def test_doctor_rebuild_state_preview_then_apply(self):
        from claude_session_sync import state as st, tombstone
        (self.hub / "projA").mkdir()
        fx.write_jsonl(fx.linear(), str(self.hub / "projA" / "s1.jsonl"))
        tombstone.write_coverage(self.hub / "projA")
        common = ["--hub", str(self.hub), "--local-root", str(self.local), "--state", str(self.state)]
        code, out, _ = self._run(["doctor", "--rebuild-state", *common])   # 預覽
        self.assertEqual(code, 0)
        self.assertFalse(self.state.exists())                              # 預覽不寫
        code, _, _ = self._run(["doctor", "--rebuild-state", "--yes", *common])
        self.assertEqual(code, 0)
        self.assertEqual(st.load_or_none(self.state).known_sessions["projA"], {"s1"})

    def test_doctor_rebuild_write_error_nonzero(self):
        # codex r-doctor-5：state.save 的 readback 驗證失敗（AtomicWriteError/VerifyError，非 OSError）
        # 須被捕捉→非零退出，不外拋 traceback。
        from unittest import mock
        from claude_session_sync import atomicio, tombstone
        (self.hub / "projA").mkdir()
        fx.write_jsonl(fx.linear(), str(self.hub / "projA" / "s1.jsonl"))
        tombstone.write_coverage(self.hub / "projA")
        common = ["--hub", str(self.hub), "--local-root", str(self.local), "--state", str(self.state)]
        with mock.patch.object(atomicio, "atomic_write_text",
                               side_effect=atomicio.VerifyError("readback mismatch")):
            code, _, err = self._run(["doctor", "--rebuild-state", "--yes", *common])
        self.assertEqual(code, 1)
        self.assertIn("寫入失敗", err)

    @_caps.needs_dead_pid_detection
    def test_doctor_break_lock_preview_then_apply(self):
        import json
        import subprocess
        import sys
        from claude_session_sync import atomicio
        (self.hub / "projA").mkdir()
        lp = self.hub / "projA" / "s1.jsonl.lock"
        proc = subprocess.Popen([sys.executable, "-c", ""])
        proc.wait()                                                        # pid 已死
        lp.write_text(json.dumps({"pid": proc.pid, "host": atomicio._local_host(),
                                  "time": "t", "token": "x"}), encoding="utf-8")
        common = ["--hub", str(self.hub), "--local-root", str(self.local), "--state", str(self.state)]
        self._run(["doctor", "--break-lock", *common])                    # 預覽
        self.assertTrue(lp.exists())
        self._run(["doctor", "--break-lock", "--yes", *common])
        self.assertFalse(lp.exists())


class TestCliTransfer(unittest.TestCase):
    """跨群 pull/push + remote add/list（patch default_config_path 隔離，以免動到真實 config）。

    原本設 XDG_CONFIG_HOME 隔離：Windows 的 config 走 %APPDATA%、不讀 XDG → 隔離無效，`remote add` 會把
    temp 路徑**寫進使用者真實 config**（2026-07-14 實機中招：%APPDATA% config 出現殘留的 Temp remote）。"""

    def setUp(self):
        from unittest import mock
        from claude_session_sync import config as config_mod
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.local = self.tmp / "local"
        self.remote = self.tmp / "remote"
        (self.local / "projA").mkdir(parents=True)
        (self.remote / "projA").mkdir(parents=True)
        self._cfg_patch = mock.patch.object(
            config_mod, "default_config_path",
            return_value=self.tmp / "cfg" / "claude-session-sync" / "config.toml")
        self._cfg_patch.start()
        self.addCleanup(self._cfg_patch.stop)

    def tearDown(self):
        self._td.cleanup()

    def _run(self, argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_remote_add_list(self):
        code, _, _ = self._run(["remote", "add", "office", str(self.remote)])
        self.assertEqual(code, 0)
        code, out, _ = self._run(["remote", "list"])
        self.assertEqual(code, 0)
        self.assertIn("office", out)
        self.assertIn(str(self.remote), out)

    def test_pull_unknown_remote_errors(self):
        code, _, err = self._run(["pull", "--from", "nope", "--local-root", str(self.local)])
        self.assertEqual(code, 1)
        self.assertIn("未知 remote", err)

    def test_pull_missing_from_errors(self):
        code, _, err = self._run(["pull", "--local-root", str(self.local)])
        self.assertEqual(code, 1)
        self.assertIn("--from", err)

    def test_pull_dry_run_then_apply(self):
        self._run(["remote", "add", "office", str(self.remote)])
        fx.write_jsonl(fx.linear(), str(self.remote / "projA" / "s1.jsonl"))
        common = ["--from", "office", "--map", "projA=projA", "--local-root", str(self.local)]
        code, out, _ = self._run(["pull", *common])               # dry-run
        self.assertEqual(code, 0)
        self.assertFalse((self.local / "projA" / "s1.jsonl").exists())   # 預覽未寫
        code, _, _ = self._run(["pull", *common, "--apply"])
        self.assertEqual(code, 0)
        self.assertTrue((self.local / "projA" / "s1.jsonl").exists())

    def test_push_apply(self):
        self._run(["remote", "add", "office", str(self.remote)])
        fx.write_jsonl(fx.linear(), str(self.local / "projA" / "s1.jsonl"))
        code, _, _ = self._run(["push", "--to", "office", "--map", "projA=projA",
                                "--local-root", str(self.local), "--apply"])
        self.assertEqual(code, 0)
        self.assertTrue((self.remote / "projA" / "s1.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
