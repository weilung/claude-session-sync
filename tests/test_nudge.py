"""P2：`nudge` hook 助手（DESIGN §7.5）＋ `build_plan(memory_only=True)` 快路徑。

nudge = 給 SessionEnd/SessionStart hook 的極簡建議指令：唯讀、fail-silent，掛載點在才比對 memory，有分歧
印一行 `systemMessage`。不寫、不鎖、不讀 stdin、任何錯誤一律靜默 exit 0（絕不干擾 session 結束）。
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_session_sync import cli, memory, scan, state as state_mod, tombstone
from claude_session_sync.config import Config
from claude_session_sync.state import State


def _mp(name, action):
    return memory.MemoryPlan(name, action, None, "r")


def _plan(mems):
    return scan.SyncPlan(first_run=False, anomalies=[], projects=[
        scan.ProjectPlan(local_dir="l", hub_dir="h", identity="match",
                         coverage_initialized=True, memories=list(mems))])


def _mem_text(slug="fact", body="hello"):
    return "\n".join(["---", f"name: {slug}", "description: d",
                      "metadata:", "  type: project", "---", body, ""])


def _name_match(local_dir, hub_dirs):
    for hd in hub_dirs:
        if hd.name == local_dir.name:
            return ("match", hd)
    return ("needs-map", None)


class TestNudgeSummary(unittest.TestCase):
    """純函式：把計畫濃縮成一行（或 None）。"""

    def test_no_divergence_returns_none(self):
        self.assertIsNone(_nudge := cli._nudge_summary(_plan([])))
        self.assertIsNone(cli._nudge_summary(_plan([_mp("a.md", "identical")])))

    def test_suppressed_and_blocked_do_not_nudge(self):
        # 已定案刪除（suppressed-deleted）與工具無法自動解的 blocked-*（其靜音出口是 A15 ack）不 nudge。
        p = _plan([_mp("a.md", "suppressed-deleted"), _mp("b.md", "blocked-damaged-source"),
                   _mp("c.md", "blocked-no-baseline"), _mp("d.md", "identical")])
        self.assertIsNone(cli._nudge_summary(p))

    def test_updates_only(self):
        p = _plan([_mp("a.md", "copy-to-hub"), _mp("b.md", "local-deleted"),
                   _mp("c.md", "copy-to-local")])
        msg = cli._nudge_summary(p)
        self.assertIn("3 個記憶更新", msg)
        self.assertIn("sync --apply", msg)
        self.assertNotIn("衝突", msg)

    def test_conflicts_only(self):
        p = _plan([_mp("a.md", "conflict-content"),
                   _mp("b.md", "conflict-cross-file-identity")])
        msg = cli._nudge_summary(p)
        self.assertIn("2 個記憶衝突", msg)
        self.assertIn("memory-merge", msg)

    def test_both_buckets(self):
        p = _plan([_mp("a.md", "copy-to-hub"), _mp("b.md", "conflict-content"),
                   _mp("c.md", "identical")])
        msg = cli._nudge_summary(p)
        self.assertIn("1 更新", msg)
        self.assertIn("1 衝突", msg)
        self.assertIn("sync --apply", msg)
        self.assertIn("memory-merge", msg)

    def test_counts_across_projects(self):
        p = scan.SyncPlan(first_run=False, anomalies=[], projects=[
            scan.ProjectPlan("l1", "h1", "match", True, memories=[_mp("a.md", "copy-to-hub")]),
            scan.ProjectPlan("l2", "h2", "match", True, memories=[_mp("b.md", "conflict-content")]),
        ])
        msg = cli._nudge_summary(p)
        self.assertIn("1 更新", msg)
        self.assertIn("1 衝突", msg)


class TestNudgeActionabilityGate(unittest.TestCase):
    """g4 Medium：nudge 只算 hub+local 兩側皆綁的專案（memory-merge/sync memory-apply 唯一會實際處理的型態）。
    未配對（hub-only/local-only）的任何 memory 動作都不吵——實測未配對專案的動作皆 blocked-unmapped，此閘為
    防未來 classify 洩漏 auto/conflict 到未配對專案時去吵不可動作項的防線。"""

    def test_hub_only_conflict_not_counted(self):
        p = scan.SyncPlan(first_run=False, anomalies=[], projects=[
            scan.ProjectPlan(local_dir=None, hub_dir="h", identity="hub-only",
                             coverage_initialized=True,
                             memories=[_mp("a.md", "conflict-delete-vs-update")])])
        self.assertIsNone(cli._nudge_summary(p))   # memory-merge 不碰 hub-only → 不可動作 → 不吵

    def test_local_only_conflict_not_counted(self):
        p = scan.SyncPlan(first_run=False, anomalies=[], projects=[
            scan.ProjectPlan(local_dir="l", hub_dir=None, identity="local-only",
                             coverage_initialized=False,
                             memories=[_mp("a.md", "conflict-content")])])
        self.assertIsNone(cli._nudge_summary(p))

    def test_hub_only_update_not_counted(self):
        # 未配對專案即使（假設性地）帶 auto 動作也不吵：sync memory-apply 需 mapping、hub-only 實測皆 blocked-unmapped
        # （見 test_build_plan_hub_only_*），此閘＝防未來洩漏的防線，寧可不吵未配對項也不反覆吵不可動作項。
        p = scan.SyncPlan(first_run=False, anomalies=[], projects=[
            scan.ProjectPlan(local_dir=None, hub_dir="h", identity="hub-only",
                             coverage_initialized=True,
                             memories=[_mp("a.md", "local-deleted")])])
        self.assertIsNone(cli._nudge_summary(p))

    def test_paired_update_and_conflict_counted(self):
        # 對照：兩側皆綁的專案，更新與衝突都照計。
        p = scan.SyncPlan(first_run=False, anomalies=[], projects=[
            scan.ProjectPlan(local_dir="l", hub_dir="h", identity="match", coverage_initialized=True,
                             memories=[_mp("a.md", "copy-to-hub"), _mp("b.md", "conflict-content")])])
        msg = cli._nudge_summary(p)
        self.assertIn("1 更新", msg)
        self.assertIn("1 衝突", msg)

    def test_build_plan_hub_only_conflict_produces_but_nudge_silent(self):
        # 整合（codex g4 的實際觸發）：hub 有 a.md + base_hash 不同的 tombstone → 真的產 conflict-delete-vs-update；
        # 但無 local projB → memory-merge 不碰 → nudge 不吵（否則每次 session 反覆吵不可動作項）。
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            local, hub = root / "local", root / "hub"
            local.mkdir()
            (hub / "projB" / "memory").mkdir(parents=True)
            (hub / "projB" / "memory" / "a.md").write_text(_mem_text(body="hubcontent"), encoding="utf-8")
            tombstone.write_coverage(hub / "projB")
            old = root / "old.md"
            old.write_text(_mem_text(body="oldcontent"), encoding="utf-8")   # 不同內容 → 不同 base_hash
            base = memory.content_hash(memory.load_memory(old))
            tombstone.write_memory_tombstone(hub / "projB", "a.md", base_hash=base)
            st = State(known_sessions={}, local_sessions={}, known_memory={}, local_memory={})
            plan = scan.build_plan(local, hub, st, identity_fn=_name_match, memory_only=True)
            pb = next(p for p in plan.projects if p.hub_dir and Path(p.hub_dir).name == "projB")
            self.assertIsNone(pb.local_dir)                                   # hub-only
            self.assertIn("conflict-delete-vs-update", {m.action for m in pb.memories})  # build_plan 真的產它
            self.assertIsNone(cli._nudge_summary(plan))                       # nudge 不吵


class TestBuildPlanMemoryOnly(unittest.TestCase):
    """`memory_only=True` 跳過 session 分類、memory 計畫不受影響。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.local = self.tmp / "local"
        self.hub = self.tmp / "hub"
        self.lA = self.local / "projA"
        self.hA = self.hub / "projA"
        (self.lA / "memory").mkdir(parents=True)
        (self.hA / "memory").mkdir(parents=True)
        # 一個 session（讓非 memory_only 有 session 計畫可比對）。
        (self.lA / "s1.jsonl").write_text(
            json.dumps({"type": "user", "uuid": "u1", "parentUuid": None,
                        "cwd": "/x", "message": {"role": "user", "content": "hi"}}) + "\n",
            encoding="utf-8")
        # 兩側 a.md 內容不同 → conflict-content。
        (self.lA / "memory" / "a.md").write_text(_mem_text(body="local"), encoding="utf-8")
        (self.hA / "memory" / "a.md").write_text(_mem_text(body="hub"), encoding="utf-8")
        tombstone.write_coverage(self.hA)
        self.state = State(known_sessions={"projA": set()}, local_sessions={"projA": set()},
                           known_memory={"projA": set()}, local_memory={"projA": set()})

    def tearDown(self):
        self._td.cleanup()

    def test_memory_only_skips_sessions_keeps_memory(self):
        full = scan.build_plan(self.local, self.hub, self.state, identity_fn=_name_match)
        mo = scan.build_plan(self.local, self.hub, self.state, identity_fn=_name_match, memory_only=True)
        pa_full = next(p for p in full.projects if p.hub_dir and Path(p.hub_dir).name == "projA")
        pa_mo = next(p for p in mo.projects if p.hub_dir and Path(p.hub_dir).name == "projA")
        self.assertTrue(pa_full.sessions)              # 完整版有 session 計畫
        self.assertEqual(pa_mo.sessions, [])           # memory_only 跳過 session
        self.assertEqual({m.name: m.action for m in pa_mo.memories},
                         {m.name: m.action for m in pa_full.memories})  # memory 計畫不變
        self.assertEqual(pa_mo.memories[0].action, "conflict-content")


class TestNudgeCli(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.local = self.tmp / "local"
        self.hub = self.tmp / "hub"
        self.lA = self.local / "projA"
        self.hA = self.hub / "projA"
        (self.lA / "memory").mkdir(parents=True)
        (self.hA / "memory").mkdir(parents=True)
        self.state = self.tmp / "state.json"
        # 空 local 專案夾（無 jsonl）→ 靠 local_dir_bindings 配對（CLI 無 identity_fn 注入）。
        st = State(known_sessions={"projA": set()}, local_sessions={"projA": set()},
                   known_memory={"projA": set()}, local_memory={"projA": set()},
                   local_dir_bindings={"projA": "projA"})
        state_mod.save(st, self.state)
        tombstone.write_coverage(self.hA)

    def tearDown(self):
        self._td.cleanup()

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        # config.load 用空 Config，避免讀到本機真實 config（測試 hermetic；nudge 用 --hub 覆寫）。
        with mock.patch.object(cli.config_mod, "load", return_value=Config()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["nudge", "--hub", str(self.hub), "--local-root", str(self.local),
                             "--state", str(self.state), *argv])
        return code, out.getvalue(), err.getvalue()

    def _conflict(self):
        (self.lA / "memory" / "a.md").write_text(_mem_text(body="local"), encoding="utf-8")
        (self.hA / "memory" / "a.md").write_text(_mem_text(body="hub"), encoding="utf-8")

    def test_conflict_emits_systemmessage_json(self):
        self._conflict()
        code, out, err = self._run([])
        self.assertEqual(code, 0)
        obj = json.loads(out)                          # 預設輸出合法 JSON
        self.assertIn("衝突", obj["systemMessage"])
        self.assertIn("memory-merge", obj["systemMessage"])
        self.assertEqual(err, "")

    def test_text_mode_plain_line(self):
        self._conflict()
        code, out, _ = self._run(["--text"])
        self.assertEqual(code, 0)
        self.assertIn("衝突", out)
        self.assertNotIn("systemMessage", out)         # --text 不是 JSON
        self.assertNotIn("{", out)

    def test_in_sync_no_output(self):
        # 兩側 a.md 相同 → identical → 無分歧 → 靜默。
        (self.lA / "memory" / "a.md").write_text(_mem_text(body="same"), encoding="utf-8")
        (self.hA / "memory" / "a.md").write_text(_mem_text(body="same"), encoding="utf-8")
        code, out, err = self._run([])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_json_ascii_safe_under_ascii_stdout(self):
        # codex R1 Medium：JSON 用 ensure_ascii=True（純 ASCII \uXXXX）→ 即使 stdout 是 ascii 也印得出、不吞提示。
        self._conflict()
        buf = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict", newline="")
        with mock.patch.object(cli.config_mod, "load", return_value=Config()), \
                contextlib.redirect_stdout(buf):
            code = cli.main(["nudge", "--hub", str(self.hub), "--local-root", str(self.local),
                             "--state", str(self.state)])
        self.assertEqual(code, 0)
        buf.flush()
        obj = json.loads(buf.buffer.getvalue().decode("ascii"))   # \uXXXX 還原成中文
        self.assertIn("衝突", obj["systemMessage"])

    def test_text_encoding_error_is_silent(self):
        # codex R1 Medium：--text 原樣中文在 ascii stdout 會拋 UnicodeEncodeError → 輸出在 try 內 → 靜默 exit 0，
        # 不得以 traceback 非零退出破壞 fail-silent。
        self._conflict()
        buf = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict", newline="")
        with mock.patch.object(cli.config_mod, "load", return_value=Config()), \
                contextlib.redirect_stdout(buf):
            code = cli.main(["nudge", "--text", "--hub", str(self.hub),
                             "--local-root", str(self.local), "--state", str(self.state)])
        self.assertEqual(code, 0)
        buf.flush()
        self.assertEqual(buf.buffer.getvalue(), b"")   # 拋在 write 前段，未落地半截

    def test_missing_hub_silent(self):
        # 掛載點不在（hub 夾不存在）→ 靜默 exit 0，無輸出（G5：載體可有可無）。
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.config_mod, "load", return_value=Config()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["nudge", "--hub", str(self.tmp / "nohub"),
                             "--local-root", str(self.local), "--state", str(self.state)])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")

    def test_corrupt_state_silent(self):
        # 壞 state → fail-silent（不像 sync 會報錯中止）→ exit 0 無輸出。
        self._conflict()
        Path(self.state).write_text("{ not json", encoding="utf-8")
        code, out, _ = self._run([])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_malformed_args_silent_no_stderr(self):
        # g1 Medium：壞 hook 設定（未知旗標）→ nudge 仍 exit 0。g2 Medium：連 argparse 的 usage/error 都不得洩到
        # real stderr（導進記憶體 StringIO）。
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["nudge", "--bogus"])
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")   # argparse usage 未洩到 real stderr
        # 對照：非 nudge 的壞用法仍照常非零退出（SystemExit）＋印 usage——只有 nudge 攔。
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["status", "--bogus"])

    def test_help_flag_silent_no_stdout(self):
        # g3 Medium：hook 誤含 --help/-h → argparse 把 help 寫 stdout 後 SystemExit(0)。nudge 須連 help 都不污染
        # stdout（否則 hook 讀到非 JSON help 文字）→ 導進 StringIO、exit 0、real stdout/stderr 皆空。
        for flag in ("--help", "-h"):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["nudge", flag])
            self.assertEqual(code, 0, flag)
            self.assertEqual(out.getvalue(), "", flag)   # help 未洩到 real stdout
            self.assertEqual(err.getvalue(), "", flag)

    def test_malformed_nonascii_arg_silent_under_ascii_stderr(self):
        # g2 Medium：壞參數含非 ASCII、real stderr 又是 ascii → argparse 寫 error 訊息若直觸 real stderr 會
        # UnicodeEncodeError 在 SystemExit 前逃出。導進 StringIO 後：ascii stderr 未被寫、不炸、exit 0。
        buf = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict", newline="")
        with contextlib.redirect_stderr(buf):
            code = cli.main(["nudge", "--föö"])
        self.assertEqual(code, 0)
        buf.flush()
        self.assertEqual(buf.buffer.getvalue(), b"")

    def test_broken_pipe_is_silent(self):
        # g1 Low：stdout 提早關（BrokenPipeError）→ 導向 devnull、exit 0，不以非零/traceback 收場。
        self._conflict()

        class _BrokenOut:
            encoding = "utf-8"

            def write(self, *a):
                raise BrokenPipeError()

            def flush(self):
                pass

            def fileno(self):
                raise OSError("no fd")   # 仿測試流/無真實 fd → dup2 導不了、須被內層吞

        with mock.patch.object(cli.config_mod, "load", return_value=Config()), \
                mock.patch.object(cli.sys, "stdout", _BrokenOut()):
            code = cli.main(["nudge", "--hub", str(self.hub), "--local-root", str(self.local),
                             "--state", str(self.state)])
        self.assertEqual(code, 0)

    def test_unset_hub_silent(self):
        # 未設定 own_hub 且無 --hub → 不 nudge（工具未設起來）。
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.config_mod, "load", return_value=Config()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["nudge", "--local-root", str(self.local), "--state", str(self.state)])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
