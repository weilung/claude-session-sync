"""P2 Block A：memory fuzzy 近似候選（唯讀 advisory）。

cardinal＝fuzzy 只建議、**絕不自動合併/寫檔**。純模組（tokenize/jaccard/similarity/find_candidates）為主
（風險所在＝計分正確性 + 「真重複被標、雜訊不被標」+ 決定性）；CLI 端到端唯讀煙霧（含「跑完一位元組未改」）。
"""
import argparse
import contextlib
import io
import os
import shutil
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

from claude_session_sync import atomicio, cli, fuzzy, merge, scan, state as state_mod, tombstone
from claude_session_sync.config import Config
from claude_session_sync.state import State
from tests import _caps


def _mem(slug, desc, body="b"):
    return "\n".join(["---", f"name: {slug}", f"description: {desc}",
                      "metadata:", "  type: project", "---", body, ""])


class TestTokens(unittest.TestCase):
    def test_name_tokens_split(self):
        self.assertEqual(fuzzy.name_tokens("codex-run_stall.handling"),
                         frozenset({"codex", "run", "stall", "handling"}))

    def test_name_tokens_empty(self):
        self.assertEqual(fuzzy.name_tokens(None), frozenset())
        self.assertEqual(fuzzy.name_tokens(""), frozenset())

    def test_name_tokens_casefold_and_nfc_nfd(self):
        self.assertEqual(fuzzy.name_tokens("ABC-x"), fuzzy.name_tokens("abc-X"))  # casefold
        nfc = unicodedata.normalize("NFC", "café-x")
        nfd = unicodedata.normalize("NFD", "café-x")
        self.assertNotEqual(nfc, nfd)                                   # bytes 不同
        self.assertEqual(fuzzy.name_tokens(nfc), fuzzy.name_tokens(nfd))  # 正規化後同鍵

    def test_desc_tokens_latin_and_cjk(self):
        t = fuzzy.desc_tokens("hang vs slow 重跑 pkill")
        for w in ("hang", "vs", "slow", "pkill", "重", "跑"):
            self.assertIn(w, t)
        self.assertEqual(fuzzy.desc_tokens(None), frozenset())

    def test_jaccard(self):
        self.assertEqual(fuzzy.jaccard(frozenset("ab"), frozenset("ab")), 1.0)
        self.assertEqual(fuzzy.jaccard(frozenset("a"), frozenset("b")), 0.0)
        self.assertAlmostEqual(fuzzy.jaccard(frozenset("ab"), frozenset("bc")), 1 / 3)
        self.assertEqual(fuzzy.jaccard(frozenset(), frozenset("a")), 0.0)   # 空側→0（安全方向）


class TestFindCandidates(unittest.TestCase):
    def _e(self, fn, name, desc):
        return fuzzy.FuzzyEntry(fn, name, desc)

    def test_true_dup_flagged(self):
        es = [self._e("codex-run-stall-handling.md", "codex-run-stall-handling",
                      "codex hang vs slow resume pkill"),
              self._e("codex-stall-triage.md", "codex-stall-triage",
                      "codex hang slow 重跑 pkill")]
        cs = fuzzy.find_candidates("p", es)
        self.assertEqual(len(cs), 1)
        self.assertEqual((cs[0].a, cs[0].b),
                         ("codex-run-stall-handling.md", "codex-stall-triage.md"))
        self.assertEqual(set(cs[0].shared_name_tokens), {"codex", "stall"})

    def test_distinct_not_flagged(self):
        es = [self._e("reply-in-chinese.md", "reply-in-chinese", "traditional chinese chat"),
              self._e("dev-env-windows.md", "dev-env-windows", "windows powershell python")]
        self.assertEqual(fuzzy.find_candidates("p", es), [])

    def test_exact_same_name_excluded(self):
        # 同 name 不同檔名 = exact cross-file-identity（exact 層已處理）→ fuzzy 不重複列。
        es = [self._e("a.md", "same-fact", "x y z"), self._e("b.md", "same-fact", "x y z")]
        self.assertEqual(fuzzy.find_candidates("p", es), [])

    def test_name_key_alias_not_paired(self):
        # 檔名僅大小寫不同 = 同一檔別名（_name_key 相同）→ 去重成一檔 → 不成對（非「兩檔」）。
        es = [self._e("Foo.md", "n1", "alpha beta"), self._e("foo.md", "n2", "alpha beta")]
        self.assertEqual(fuzzy.find_candidates("p", es), [])

    def test_name_key_alias_deduped_no_double_pairing(self):
        # 別名兩檔（Codex-A / codex-a，name_key 同）+ 第三檔：去重保一 → 與第三檔恰配 1 次（非 2 次重複）。
        es = [self._e("Codex-A.md", "codex-alpha", "hang slow"),
              self._e("codex-a.md", "codex-beta", "hang slow"),
              self._e("codex-runner.md", "codex-runner", "hang slow")]
        cs = fuzzy.find_candidates("p", es, threshold=0.0)
        self.assertEqual(len(cs), 1)

    def test_desc_only_when_name_undecidable(self):
        # name=None（非 fm_ok）→ 靠 desc；desc 全重疊 → score 0.3 ≥ 預設 0.25。
        es = [self._e("x.md", None, "alpha beta gamma delta"),
              self._e("y.md", None, "alpha beta gamma delta")]
        cs = fuzzy.find_candidates("p", es)
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0].name_sim, 0.0)
        self.assertEqual(cs[0].desc_sim, 1.0)

    def test_threshold_boundary(self):
        # name {a,b} vs {a,c} → jaccard 1/3；desc 無交集 → score 0.7*0.333=0.233。
        es = [self._e("a-b.md", "a-b", "d1"), self._e("a-c.md", "a-c", "d2")]
        self.assertEqual(fuzzy.find_candidates("p", es, threshold=0.25), [])      # 0.233 < .25
        self.assertEqual(len(fuzzy.find_candidates("p", es, threshold=0.2)), 1)   # ≥ .2

    def test_deterministic_order_and_keys(self):
        es = [self._e("a-z.md", "a-z", "p q"), self._e("a-x.md", "a-x", "p q"),
              self._e("a-y.md", "a-y", "p q")]
        cs = fuzzy.find_candidates("p", es, threshold=0.0)
        self.assertEqual([c.score for c in cs], sorted((c.score for c in cs), reverse=True))
        for c in cs:                       # a 恆小於 b（決定性鍵）
            self.assertLess(c.a, c.b)
        # 重跑同輸入 → 完全相同（跨機/跨次一致）
        self.assertEqual(cs, fuzzy.find_candidates("p", list(reversed(es)), threshold=0.0))


class TestEmitFuzzyDisplay(unittest.TestCase):
    def test_surrogate_filename_no_crash(self):
        # POSIX 非 UTF-8 檔名（surrogateescape）→ 顯示層 _disp 中和 → strict UTF-8 stdout 不崩（比照 merge）。
        c = fuzzy.FuzzyCandidate("pr\udc80oj", "a\udc80.md", "b.md", "n-a", "n-b", 0.5, 0.5, 0.5, ("x",))
        buf = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="")   # strict，如真實 stdout
        with contextlib.redirect_stdout(buf):
            rc = cli._emit_fuzzy([c], [], 0.25)
            buf.flush()
        self.assertEqual(rc, 0)

    def test_unscannable_surrogate_no_crash(self):
        # 警告行內嵌 raw pk（surrogate 專案夾名）→ 亦須過 _disp、不崩（fuzzy-g1 Low）。
        buf = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="")
        with contextlib.redirect_stdout(buf):
            rc = cli._emit_fuzzy([], ["bad\udc80（memory/ 根為 symlink）"], 0.25)
            buf.flush()
        self.assertEqual(rc, 1)   # unscannable → 非零


class TestFuzzyCLI(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.local = self.tmp / "local"
        self.hub = self.tmp / "hub"
        self.lA = self.local / "projA" / "memory"
        self.lA.mkdir(parents=True)
        self.hub.mkdir()
        self.nostate = self.tmp / "nostate.json"   # 不存在 → load_or_none 回 None（不讀真實 state）
        self.cfg = Config(own_hub=str(self.hub), remotes={})

    def tearDown(self):
        self._td.cleanup()

    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.config_mod, "load", return_value=self.cfg), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["memory-merge", "--local-root", str(self.local),
                             "--state", str(self.nostate), "--fuzzy", *argv])
        return code, out.getvalue(), err.getvalue()

    def _w(self, name, text):
        (self.lA / name).write_text(text, encoding="utf-8")

    def test_lists_near_dup(self):
        self._w("codex-run-stall-handling.md", _mem("codex-run-stall-handling", "codex hang slow resume pkill"))
        self._w("codex-stall-triage.md", _mem("codex-stall-triage", "codex hang slow 重跑 pkill"))
        code, out, _ = self._run()
        self.assertEqual(code, 0)
        self.assertIn("模糊近似候選", out)
        self.assertIn("codex-run-stall-handling.md", out)
        self.assertIn("codex-stall-triage.md", out)

    def test_no_candidates(self):
        self._w("reply-in-chinese.md", _mem("reply-in-chinese", "traditional chinese chat"))
        self._w("dev-env-windows.md", _mem("dev-env-windows", "windows powershell python"))
        code, out, _ = self._run()
        self.assertEqual(code, 0)
        self.assertIn("未偵測到", out)

    def test_writes_nothing(self):
        # cardinal：fuzzy 唯讀——跑完 local 檔一位元組未改、無新增；hub 未新增檔。
        self._w("codex-run-stall-handling.md", _mem("codex-run-stall-handling", "codex hang slow"))
        self._w("codex-stall-triage.md", _mem("codex-stall-triage", "codex hang slow"))
        before = {p: p.read_bytes() for p in self.local.rglob("*") if p.is_file()}
        hub_before = sorted(self.hub.rglob("*"))
        self._run()
        after = {p: p.read_bytes() for p in self.local.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(sorted(self.hub.rglob("*")), hub_before)

    def test_threshold_override(self):
        self._w("a-b.md", _mem("a-b", "d1"))
        self._w("a-c.md", _mem("a-c", "d2"))
        _, out, _ = self._run()                          # 預設 0.25 → 0.233 不中
        self.assertIn("未偵測到", out)
        _, out2, _ = self._run("--fuzzy-threshold", "0.2")
        self.assertIn("a-b.md", out2)

    def test_from_not_implemented(self):
        code, _, err = self._run("--from", "office")
        self.assertEqual(code, 1)
        self.assertIn("尚未實作", err)

    def test_cross_side_union_same_name_unpaired(self):
        # local projA + hub projA（同名、未配對）→ 依 pk 聚合 → 跨側近似候選抓得到、標頭只一次（codex r1 M#2）。
        self._w("codex-run-stall-handling.md", _mem("codex-run-stall-handling", "codex hang slow resume"))
        hA = self.hub / "projA" / "memory"
        hA.mkdir(parents=True)
        (hA / "codex-stall-triage.md").write_text(
            _mem("codex-stall-triage", "codex hang slow 重跑"), encoding="utf-8")
        tombstone.write_coverage(self.hub / "projA")
        code, out, _ = self._run()
        self.assertEqual(code, 0)
        self.assertIn("codex-run-stall-handling.md", out)
        self.assertIn("codex-stall-triage.md", out)
        self.assertEqual(out.count("● projA"), 1)   # 標頭只印一次

    def test_threshold_nan_rejected(self):
        # nan 會讓 score < nan 恆 False → 全印；驗證被擋（codex r1 Low）。
        self._w("a.md", _mem("a", "d"))
        self._w("b.md", _mem("b", "d2"))
        code, _, err = self._run("--fuzzy-threshold", "nan")
        self.assertEqual(code, 1)
        self.assertIn("0~1", err)

    @unittest.skipUnless(_caps.CAN_SYMLINK, "需 symlink 能力")
    def test_unscannable_memory_root_nonzero(self):
        # memory/ 根為 symlink → list_memory_files raise UnsafeMemoryDir → unscannable → 非零（不誤當無候選）。
        import os
        (self.local / "projB").mkdir()
        target = self.tmp / "elsewhere"
        target.mkdir()
        os.symlink(target, self.local / "projB" / "memory", target_is_directory=True)
        code, out, _ = self._run()
        self.assertEqual(code, 1)
        self.assertIn("未被掃描", out)


class _StageHarness:
    """Block B：使用者放行 → leak-safe 保留兩版。XDG_CACHE_HOME 設在 local/hub **之外**（暫存根安全）。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.local = self.tmp / "local"
        self.hub = self.tmp / "hub"
        self.cache = self.tmp / "cache"     # 暫存根：在 local/hub 之外 → unsafe_staging_root 放行
        self.lA = self.local / "projA" / "memory"
        self.lA.mkdir(parents=True)
        self.hub.mkdir()
        self.nostate = self.tmp / "nostate.json"
        self.cfg = Config(own_hub=str(self.hub), remotes={})

    def tearDown(self):
        try:
            self._td.cleanup()
        except OSError:                     # >260 staging 令 plain rmtree 失敗（Windows）→ \\?\ 遞迴刪
            shutil.rmtree(atomicio.os_path(self.tmp), ignore_errors=True)

    def _wl(self, name, text):
        (self.lA / name).write_text(text, encoding="utf-8")

    def _two_near_dups(self):
        # score = 0.7*(2/6) + 0.3*1.0 ≈ 0.53 ≥ 0.25（name 共享 codex/stall，desc 高度重疊）。
        self._wl("codex-run-stall-handling.md", _mem("codex-run-stall-handling", "codex hang slow resume"))
        self._wl("codex-stall-triage.md", _mem("codex-stall-triage", "codex hang slow 重跑"))

    @property
    def mroot(self):
        return self.cache / "claude-session-sync" / "merge"

    def _run(self, *argv, inputs=None):
        env = {"XDG_CACHE_HOME": str(self.cache), "XDG_CONFIG_HOME": str(self.tmp / "cfg")}
        out, err = io.StringIO(), io.StringIO()
        cm = mock.patch("builtins.input", side_effect=inputs) if inputs is not None else contextlib.nullcontext()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(cli.config_mod, "load", return_value=self.cfg), \
                cm, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["memory-merge", "--local-root", str(self.local),
                             "--state", str(self.nostate), "--fuzzy", *argv])
        return code, out.getvalue(), err.getvalue()


class TestFuzzyStage(_StageHarness, unittest.TestCase):
    def test_stage_all_preserves_both_and_writes_prompt(self):
        self._two_near_dups()
        code, out, _ = self._run("--stage")
        self.assertEqual(code, 0)
        prompts = list((self.mroot / "projA" / "fuzzy").rglob("PROMPT.md"))
        self.assertEqual(len(prompts), 1)                       # 一對候選 → 一個暫存夾
        staged = list(prompts[0].parent.glob("*__*.md"))
        self.assertEqual(len(staged), 2)                        # 兩檔各一版（皆單側 local）
        self.assertIn("保留兩版", out)

    def test_stage_writes_nothing_to_memory(self):
        # cardinal：放行保留只寫 memory/ 外的暫存，正式 memory 一位元組未改、無新增；hub 未增。
        self._two_near_dups()
        before = {p: p.read_bytes() for p in self.local.rglob("*") if p.is_file()}
        hub_before = sorted(self.hub.rglob("*"))
        self._run("--stage")
        after = {p: p.read_bytes() for p in self.local.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(sorted(self.hub.rglob("*")), hub_before)

    def test_prompt_has_advisory_fuzzy_framing(self):
        # fuzzy 提示詞守 advisory：先確認是否同一事實、允許判「其實不同」→ 不合併；不用確定式抬頭。
        self._two_near_dups()
        _, out, _ = self._run("--stage", "--prompt-stdout")
        self.assertIn("疑似", out)
        self.assertIn("是否真是同一件事", out)
        self.assertIn("不要合併", out)
        self.assertNotIn("下面是同一則記憶的多個版本", out)

    def test_interactive_yes_stages(self):
        self._two_near_dups()
        code, _, _ = self._run("--interactive", inputs=["y"])
        self.assertEqual(code, 0)
        self.assertEqual(len(list(self.mroot.rglob("PROMPT.md"))), 1)

    def test_interactive_no_stages_nothing(self):
        self._two_near_dups()
        code, out, _ = self._run("--interactive", inputs=["n"])
        self.assertEqual(code, 0)
        self.assertFalse(self.mroot.exists())                   # 未放行 → 不寫
        self.assertIn("未放行任何候選", out)

    def test_interactive_default_no_on_empty(self):
        # 空輸入（Enter）→ 預設 N（保守、不寫）。
        self._two_near_dups()
        self._run("--interactive", inputs=[""])
        self.assertFalse(self.mroot.exists())

    def test_stage_injective_dirs_no_clobber(self):
        # 三互相近似檔 → 3 對候選 → 3 個各異暫存夾（兩層 fuzzy/<a>/<b> 對 (a,b) 單射、互不覆蓋）。
        for n in ("codex-a", "codex-b", "codex-c"):
            self._wl(f"{n}.md", _mem(n, "hang slow"))
        code, _, _ = self._run("--stage")
        self.assertEqual(code, 0)
        self.assertEqual(len(list(self.mroot.rglob("PROMPT.md"))), 3)

    def test_stage_refuses_unsafe_root(self):
        # XDG_CACHE_HOME 指到 hub 內 → --stage 拒絕保留、非零、不寫進同步區。
        self._two_near_dups()
        env = {"XDG_CACHE_HOME": str(self.hub / "cache"), "XDG_CONFIG_HOME": str(self.tmp / "cfg")}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(cli.config_mod, "load", return_value=self.cfg), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["memory-merge", "--local-root", str(self.local),
                             "--state", str(self.nostate), "--fuzzy", "--stage"])
        self.assertEqual(code, 1)
        self.assertFalse((self.hub / "cache").exists())          # 沒寫進 hub
        self.assertIn("暫存根不安全", err.getvalue())

    def test_stage_idempotent_rerun(self):
        self._two_near_dups()
        self.assertEqual(self._run("--stage")[0], 0)
        code, out, _ = self._run("--stage")                      # 二次：already-staged、不覆蓋
        self.assertEqual(code, 0)
        self.assertIn("已存在", out)

    def test_stage_no_candidates(self):
        self._wl("reply-in-chinese.md", _mem("reply-in-chinese", "traditional chinese chat"))
        self._wl("dev-env-windows.md", _mem("dev-env-windows", "windows powershell python"))
        code, out, _ = self._run("--stage")
        self.assertEqual(code, 0)
        self.assertIn("未偵測到", out)
        self.assertFalse(self.mroot.exists())

    def test_stage_cross_side_reads_each_from_its_side(self):
        # a 在 local、b 在 hub（不同檔名、跨側）→ sides_by_pk 綁定 → 各自從其側讀、兩檔都保留（e2e 驗 sides_by_pk）。
        self._wl("codex-run-stall-handling.md", _mem("codex-run-stall-handling", "codex hang slow resume"))
        hA = self.hub / "projA" / "memory"; hA.mkdir(parents=True)
        (hA / "codex-stall-triage.md").write_text(
            _mem("codex-stall-triage", "codex hang slow 重跑"), encoding="utf-8")
        tombstone.write_coverage(self.hub / "projA")
        code, _, _ = self._run("--stage")
        self.assertEqual(code, 0)
        self.assertEqual(len(list(self.mroot.rglob("PROMPT.md"))), 1)
        staged = [p.name for p in self.mroot.rglob("*__*.md")]
        self.assertTrue(any("run-stall-handling" in n for n in staged))   # local 側
        self.assertTrue(any("stall-triage" in n for n in staged))         # hub 側

    def test_stage_reads_only_scoring_side_not_other_samename(self):
        # g2 High：a.md 同名出現在 local(X) 與 hub(Y，無關) → 只讀計分來源一側、只保留**一個** a.md 版本，
        # 不把兩個無關同名檔都當 a 讀入（更不會在計分側消失時替換成別側的無關同名檔）。
        self._wl("a.md", _mem("codex-alpha", "hang slow resume", "LOCAL-X"))
        self._wl("b.md", _mem("codex-beta", "hang slow resume", "b"))
        hA = self.hub / "projA" / "memory"; hA.mkdir(parents=True)
        (hA / "a.md").write_text(_mem("codex-alpha", "hang slow resume", "HUB-Y-SECRET"), encoding="utf-8")
        tombstone.write_coverage(self.hub / "projA")
        code, _, _ = self._run("--stage")
        self.assertEqual(code, 0)
        staged_a = list(self.mroot.rglob("*__a.md"))
        self.assertEqual(len(staged_a), 1)                               # 只一個來源側（非兩側同名檔都讀）
        content = staged_a[0].read_bytes()
        self.assertTrue((b"LOCAL-X" in content) ^ (b"HUB-Y-SECRET" in content))   # 恰一側，不混入別側

    @unittest.skipUnless(_caps.CAN_SYMLINK, "需 symlink 能力")
    def test_stage_unscannable_nonzero(self):
        # memory/ 根為 symlink → 未掃描 → 非零（不把「沒掃到」誤當「無候選/已完成」），且不因它中止其它。
        (self.local / "projB").mkdir()
        target = self.tmp / "elsewhere"
        target.mkdir()
        os.symlink(target, self.local / "projB" / "memory", target_is_directory=True)
        self._two_near_dups()
        code, out, _ = self._run("--stage")
        self.assertEqual(code, 1)
        self.assertIn("未被掃描", out)
        self.assertEqual(len(list(self.mroot.rglob("PROMPT.md"))), 1)   # projA 仍照常保留

    def test_stage_dup_target_skipped_not_silently_first_seen(self):
        # e2e-r1 Finding 1：≥2 本機專案綁到同一 hub 夾（此處 projA/projB 皆綁 hub/projA）→ by_pk 聚合會靜默取
        # 首見側、暫存夾名 <projA>/fuzzy/… 也會撞。比照非-fuzzy `_dup_target_pks` fail-closed：跳過該 pk 全部候選
        # + 警告 + 非零，**不**靜默保留首見側的兩版（否則另一專案的版本沉默丟失、exit 0 誤報成功）。
        for proj, tag in (("projA", "AAA"), ("projB", "BBB")):
            m = self.local / proj / "memory"
            m.mkdir(parents=True, exist_ok=True)
            m.joinpath("codex-run-stall-handling.md").write_text(
                _mem("codex-run-stall-handling", f"codex hang slow resume {tag}"), encoding="utf-8")
            m.joinpath("codex-stall-triage.md").write_text(
                _mem("codex-stall-triage", f"codex hang slow 重跑 {tag}"), encoding="utf-8")
        (self.hub / "projA").mkdir()
        tombstone.write_coverage(self.hub / "projA")
        # 兩個 memory-only（無 session 檔）夾靠 local_dir_bindings 綁到同一 hub pk → dup-target。
        state_mod.save(State(known_sessions={}, local_sessions={}, known_memory={}, local_memory={},
                             local_dir_bindings={"projA": "projA", "projB": "projA"}), self.nostate)
        code, out, _ = self._run("--stage")
        self.assertEqual(code, 1)                         # 撞夾 → 非零（fail-closed）
        self.assertIn("會與其他專案的 memory 混", out)                # dup 警告有印
        self.assertNotIn("保留兩版", out)                  # 未保留任何兩版（不取首見側）
        self.assertFalse((self.mroot / "projA" / "fuzzy").exists())   # 無暫存落地

    def test_stage_local_only_pk_collides_with_hub_pk_skipped(self):
        # e2e-g1：local A 綁 hub P（pk="P" via hub 名）+ local-only P（pk="P" via local 名）→ 兩個**不同專案**落入
        # 同一 by_pk["P"] 桶（原 _dup_target_pks 只算兩側皆綁的 pp、漏此）→ 廣義護欄（某 pk 有 >1 相異 local 側）須抓。
        for proj, tag in (("A", "AAA"), ("P", "PPP")):
            m = self.local / proj / "memory"
            m.mkdir(parents=True, exist_ok=True)
            m.joinpath("codex-run-stall-handling.md").write_text(
                _mem("codex-run-stall-handling", f"codex hang slow resume {tag}"), encoding="utf-8")
            m.joinpath("codex-stall-triage.md").write_text(
                _mem("codex-stall-triage", f"codex hang slow 重跑 {tag}"), encoding="utf-8")
        (self.hub / "P").mkdir()
        tombstone.write_coverage(self.hub / "P")
        # 只綁 A→P；local-only P 無綁定 → 未配對（hub_dir=None）→ pk 退回 local 名 "P" → 與 A 的 hub pk 撞。
        state_mod.save(State(known_sessions={}, local_sessions={}, known_memory={}, local_memory={},
                             local_dir_bindings={"A": "P"}), self.nostate)
        code, out, _ = self._run("--stage")
        self.assertEqual(code, 1)                         # 混桶 → 非零（fail-closed）
        self.assertIn("會與其他專案的 memory 混", out)                # 廣義護欄警告
        self.assertNotIn("保留兩版", out)                  # 不同專案未混保留
        self.assertFalse((self.mroot / "P" / "fuzzy").exists())   # 無暫存落地

    def test_stage_case_aliasing_pks_skipped(self):
        # e2e-g2：local "P"（未配對，pk="P"）+ hub "p"（hub-only，pk="p"）＝相異 raw pk、但 name_key 折疊後相同 → 各自
        # 獨立 by_pk 桶，暫存夾 <merge>/P 與 <merge>/p 在大小寫/正規化**不敏感**的快取 FS（Windows NTFS 預設）上撞成同
        # 一實體夾 → 不同專案 memory 混淆、第二個被 already-staged 誤判靜默略過。廣義護欄依 name_key 折疊偵測 → 跳過。
        # （name_key 是純字串運算、與 FS 大小寫敏感度無關 → Linux/Windows 皆 fail-closed，保守但安全。）
        lP = self.local / "P" / "memory"; lP.mkdir(parents=True, exist_ok=True)
        hp = self.hub / "p" / "memory"; hp.mkdir(parents=True, exist_ok=True)
        for m in (lP, hp):
            (m / "codex-run-stall-handling.md").write_text(
                _mem("codex-run-stall-handling", "codex hang slow resume"), encoding="utf-8")
            (m / "codex-stall-triage.md").write_text(
                _mem("codex-stall-triage", "codex hang slow 重跑"), encoding="utf-8")
        tombstone.write_coverage(self.hub / "p")
        code, out, _ = self._run("--stage")     # nostate → local/P 未配對、hub/p 為 hub-only
        self.assertEqual(code, 1)                         # 折疊撞 → 非零（fail-closed）
        self.assertIn("會與其他專案的 memory 混", out)
        self.assertNotIn("保留兩版", out)
        self.assertFalse(list(self.mroot.rglob("*fuzzy*")))   # 無 fuzzy 暫存落地


class TestFuzzyFlagGuards(_StageHarness, unittest.TestCase):
    def test_apply_with_fuzzy_rejected(self):
        code, _, err = self._run("--apply")
        self.assertEqual(code, 1)
        self.assertIn("--stage", err)

    def test_prompt_stdout_needs_selection(self):
        code, _, err = self._run("--prompt-stdout")
        self.assertEqual(code, 1)
        self.assertIn("--stage 或 --interactive", err)

    def test_stage_interactive_mutually_exclusive(self):
        # argparse mutually_exclusive_group → SystemExit(2)（用法錯誤）。
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            cli.main(["memory-merge", "--local-root", str(self.local),
                      "--state", str(self.nostate), "--fuzzy", "--stage", "--interactive"])
        self.assertEqual(cm.exception.code, 2)

    def test_stage_without_fuzzy_rejected(self):
        # --stage 無 --fuzzy → 一般 memory-merge 路徑擋下（非零、指向 --apply）。
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.config_mod, "load", return_value=self.cfg), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["memory-merge", "--local-root", str(self.local),
                             "--state", str(self.nostate), "--stage"])
        self.assertEqual(code, 1)
        self.assertIn("僅用於 --fuzzy", err.getvalue())


class TestFuzzyStageUnit(unittest.TestCase):
    def test_fuzzy_kind_not_in_conflict_actions(self):
        # cardinal（結構）：FUZZY_KIND 絕不列入 CONFLICT_ACTIONS → classify/apply/sync/nudge 永遠碰不到它。
        self.assertNotIn(merge.FUZZY_KIND, merge.CONFLICT_ACTIONS)

    def test_fuzzy_conflict_missing_file_degrades(self):
        # 放行後某檔讀不到 → 退化 note（→ stage 不寫 .done、CLI 非零，比照現存衝突退化路徑）。
        with tempfile.TemporaryDirectory() as td:
            mdir = Path(td) / "memory"
            mdir.mkdir()
            (mdir / "a.md").write_text(_mem("a", "d"), encoding="utf-8")   # b.md 不存在
            c = merge.fuzzy_conflict("p", "a.md", [("local", mdir)], "b.md", [("local", mdir)], reason="r")
            self.assertEqual(c.kind, merge.FUZZY_KIND)
            self.assertTrue(c.notes)
            self.assertIn("b.md", c.notes[0])
            self.assertEqual(len(c.staged_versions()), 1)                  # 只讀到 a.md 一版

    @unittest.skipUnless(_caps.CAN_SYMLINK, "需 symlink 能力")
    def test_stage_revalidates_project_dir_no_leak(self):
        # R1 High：list→stage 間專案夾被換成逃逸 symlink（memory/*.md 現解析到界外）→ fuzzy_conflict 讀前重驗
        # 專案夾（_stage_safe_mdir）→ 該側視為缺、不讀界外 bytes（界外 secret 絕不進任何版本）。
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            mdir = tmp / "local" / "projA" / "memory"
            mdir.mkdir(parents=True)
            (mdir / "a.md").write_text(_mem("a", "d"), encoding="utf-8")
            (mdir / "b.md").write_text(_mem("b", "d2"), encoding="utf-8")
            secret = "SECRET-OUTSIDE-CONTENT"
            (tmp / "outside" / "memory").mkdir(parents=True)
            (tmp / "outside" / "memory" / "a.md").write_text(_mem("a", secret), encoding="utf-8")
            (tmp / "outside" / "memory" / "b.md").write_text(_mem("b", secret), encoding="utf-8")
            shutil.rmtree(tmp / "local" / "projA")                       # 換掉專案夾 → 逃逸 symlink
            os.symlink(tmp / "outside", tmp / "local" / "projA", target_is_directory=True)
            c = merge.fuzzy_conflict("projA", "a.md", [("local", mdir)], "b.md", [("local", mdir)], reason="r")
            self.assertEqual(c.staged_versions(), [])                   # 逃逸側視為缺 → 不讀
            self.assertTrue(c.notes)                                    # degraded
            blob = b"".join(v.data or b"" for v in c.versions)
            self.assertNotIn(secret.encode(), blob)                     # 界外 secret 未進任何版本

    def test_both_missing_stage_nonzero(self):
        # R1 Medium：候選兩檔在 stage 時皆讀不到 → stage_conflict 回 empty + notes → CLI 非零（不誤報成功）。
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache = tmp / "cache"
            mdir = tmp / "local" / "projA" / "memory"
            mdir.mkdir(parents=True)                                    # 空 memory/（a.md/b.md 不存在）
            args = argparse.Namespace(interactive=False, stage=True, prompt_stdout=False)
            cand = fuzzy.FuzzyCandidate("projA", "a.md", "b.md", "na", "nb", 0.5, 0.5, 0.5, ())
            score_src = {"projA": {scan._name_key("a.md"): ("local", mdir),
                                   scan._name_key("b.md"): ("local", mdir)}}   # 綁 local，但檔已不在
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}), \
                    contextlib.redirect_stdout(out):
                rc = cli._run_fuzzy_stage(args, [cand], [], 0.25, score_src, [tmp / "forbidden"])
            self.assertEqual(rc, 1)                                     # 兩檔皆缺 → 非零
            self.assertFalse((cache / "claude-session-sync").exists())  # empty → 未寫任何暫存

    def test_stage_binds_to_source_side_no_substitution(self):
        # R1 Medium：候選 a.md 綁 local 側；hub 有**無關**同名 a.md（不在 a_sides）→ 放行後 local a.md 不見時，
        # 絕不回退去讀 hub 的無關 a.md 當作 a 保留（靜默替換）；而是判 a 缺 → degraded。
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lm = tmp / "local" / "projA" / "memory"; lm.mkdir(parents=True)
            hm = tmp / "hub" / "projA" / "memory"; hm.mkdir(parents=True)
            (hm / "a.md").write_text(_mem("a", "HUB-UNRELATED"), encoding="utf-8")   # 無關同名（別側）
            (lm / "b.md").write_text(_mem("b", "d"), encoding="utf-8")               # local a.md 已不在
            c = merge.fuzzy_conflict("projA", "a.md", [("local", lm)], "b.md", [("local", lm)], reason="r")
            blob = b"".join(v.data or b"" for v in c.versions)
            self.assertNotIn(b"HUB-UNRELATED", blob)                     # 別側無關同名檔未被替換保留
            self.assertTrue(c.notes)                                     # a.md 綁定側讀不到 → degraded
            self.assertIn("a.md", c.notes[0])

    def test_print_stage_surrogate_note_no_crash(self):
        # R1 Low：退化 note 含 surrogate 檔名 → _print_stage 過 _disp → strict UTF-8 stdout 不崩。
        c = merge.MemoryConflict("p", merge.FUZZY_KIND, "a\x00b", (), "r", ("讀不到：a\udc80.md",))
        res = merge.StageResult(c, Path("dest"), "empty", [], list(c.notes))
        buf = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="")
        with contextlib.redirect_stdout(buf):
            cli._print_stage(res)
            buf.flush()                                                 # 不 raise 即通過

    def test_print_stage_surrogate_dest_no_crash(self):
        # g3 Low：res.dest 的根（XDG_CACHE_HOME）在 POSIX 可含 surrogate → _print_stage 過 _disp、暫存後印結果不崩。
        c = merge.MemoryConflict("p", merge.FUZZY_KIND, "a\x00b", (), "r", ())
        res = merge.StageResult(c, Path("/tmp/cache-\udc80/x"), "staged", ["local__a.md"], [])
        buf = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="")
        with contextlib.redirect_stdout(buf):
            cli._print_stage(res)
            buf.flush()                                                 # 不 raise 即通過

    def test_format_conflicts_surrogate_dest_no_crash(self):
        # g4 Low：format_conflicts 的 dest 根（XDG_CACHE_HOME）在 POSIX 可含 surrogate → 過 _disp、strict encode 不崩。
        v = merge.ConflictVersion("local", "a.md", "h", text="x", data=b"x")
        c = merge.MemoryConflict("p", "conflict-content", "a.md", (v,), "r", ())
        s = merge.format_conflicts([c], root=Path("/tmp/cache-\udc80"))
        s.encode("utf-8")                                               # lone surrogate 已中和 → 不 raise

    def test_similar_diff_name_not_a_plan_conflict(self):
        # cardinal（功能）：兩個近似但不同 name 的檔（兩側各有、內容相同）→ plan 分類 identical、
        # conflicts_from_plan 不產任何衝突（fuzzy 永不從 plan/apply 路徑冒出來）。
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            local, hub = tmp / "local", tmp / "hub"
            (local / "projA" / "memory").mkdir(parents=True)
            (hub / "projA" / "memory").mkdir(parents=True)
            for side in (local, hub):
                (side / "projA" / "memory" / "codex-a.md").write_text(_mem("codex-a", "hang slow"), encoding="utf-8")
                (side / "projA" / "memory" / "codex-b.md").write_text(_mem("codex-b", "hang slow"), encoding="utf-8")
            tombstone.write_coverage(hub / "projA")
            st = State(known_memory={"projA": {"codex-a.md", "codex-b.md"}},
                       local_memory={"projA": {"codex-a.md", "codex-b.md"}})

            def _nm(ld, hds):
                return next((("match", hd) for hd in hds if hd.name == ld.name), ("needs-map", None))

            plan = scan.build_plan(local, hub, st, identity_fn=_nm)
            self.assertEqual(merge.conflicts_from_plan(plan), [])


if __name__ == "__main__":
    unittest.main()
