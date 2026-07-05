"""P1d Block 3c：MEMORY.md 索引機械重建（`memory.plan_index_rebuild`，純函式）。

極性＝工具自有 auto-block（使用者拍板，A14 字面 USER SECTION 是示意）。鐵則＝**永不靜默丟手寫內容**：
工具只重寫自己 BEGIN/END 標記之間，標記外/無標記/標記異常一律保留原檔。
"""
import tempfile
import unittest
from pathlib import Path

from claude_session_sync import memory
from claude_session_sync.memory import INDEX_BEGIN, INDEX_END
from tests import _caps


def _mem(slug, desc="d", body="b"):
    return "\n".join(["---", f"name: {slug}", f"description: {desc}",
                      "metadata:", "  type: project", "---", body, ""])


class TestPlanIndexRebuild(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.md = Path(self._td.name) / "memory"
        self.md.mkdir()

    def tearDown(self):
        self._td.cleanup()

    def _wm(self, name, slug, desc="d", body="b"):
        (self.md / name).write_text(_mem(slug, desc, body), encoding="utf-8")

    def _raw(self, name, text):
        (self.md / name).write_text(text, encoding="utf-8")

    # ── 建新 ────────────────────────────────────────────────────────────────
    def test_create_when_missing(self):
        self._wm("a.md", "a", "alpha")
        r = memory.plan_index_rebuild(self.md, None)
        self.assertEqual(r.status, "created")
        self.assertIn(INDEX_BEGIN, r.content)
        self.assertIn(INDEX_END, r.content)
        self.assertIn("- [a](a.md) — alpha", r.content)
        self.assertTrue(r.content.startswith("# Memory Index\n"))
        self.assertTrue(r.content.endswith("\n"))  # 檔尾單一 newline

    def test_existing_blank_kept_not_created(self):
        # codex fresh gate r3 Medium：現存但全空白 → markerless（使用者可能刻意清空）→ 保留、不自動建索引；
        # 有檔未列入則漂移警告。只有 current_text is None（真正缺檔）才建新。
        self._wm("a.md", "a")
        for blank in ("", "   ", "\n\n", "\r\n  \r\n"):
            r = memory.plan_index_rebuild(self.md, blank)
            self.assertEqual(r.status, "kept-handwritten", repr(blank))
            self.assertIsNone(r.content)
            self.assertIsNotNone(r.note)            # a.md 未列入 → 漂移
            self.assertIn("a.md", r.note)

    def test_existing_blank_empty_dir_kept_silent(self):
        r = memory.plan_index_rebuild(self.md, "")   # 空白索引 + 無檔 → 保留、無漂移
        self.assertEqual(r.status, "kept-handwritten")
        self.assertIsNone(r.note)

    def test_no_files_no_existing_no_create(self):
        r = memory.plan_index_rebuild(self.md, None)
        self.assertEqual(r.status, "empty")
        self.assertIsNone(r.content)

    def test_deterministic_casefold_sort(self):
        self._wm("Zeta.md", "zeta")
        self._wm("alpha.md", "alpha")
        self._wm("Beta.md", "beta")
        r = memory.plan_index_rebuild(self.md, None)
        order = [ln for ln in r.content.splitlines() if ln.startswith("- [")]
        self.assertEqual(order, ["- [alpha](alpha.md) — d", "- [beta](Beta.md) — d",
                                 "- [zeta](Zeta.md) — d"])

    # ── 已有 auto-block ───────────────────────────────────────────────────────
    def test_unchanged_roundtrip(self):
        self._wm("a.md", "a")
        created = memory.plan_index_rebuild(self.md, None).content
        r = memory.plan_index_rebuild(self.md, created)
        self.assertEqual(r.status, "unchanged")
        self.assertIsNone(r.content)

    def test_rebuild_adds_new_file(self):
        self._wm("a.md", "a")
        created = memory.plan_index_rebuild(self.md, None).content
        self._wm("b.md", "b", "bee")
        r = memory.plan_index_rebuild(self.md, created)
        self.assertEqual(r.status, "rebuilt")
        self.assertIn("- [b](b.md) — bee", r.content)
        self.assertIn("- [a](a.md) — d", r.content)

    def test_rebuild_drops_deleted_file(self):
        self._wm("a.md", "a")
        self._wm("b.md", "b")
        created = memory.plan_index_rebuild(self.md, None).content
        (self.md / "b.md").unlink()
        r = memory.plan_index_rebuild(self.md, created)
        self.assertEqual(r.status, "rebuilt")
        self.assertNotIn("b.md", r.content)
        self.assertIn("a.md", r.content)

    def test_preserves_before_and_after_verbatim(self):
        self._wm("a.md", "a")
        ex = ("PREFACE 手寫\n保留我\n\n" + INDEX_BEGIN + "\n- [old](old.md) — x\n"
              + INDEX_END + "\n\nFOOTER 也保留\n")
        r = memory.plan_index_rebuild(self.md, ex)
        self.assertEqual(r.status, "rebuilt")
        self.assertTrue(r.content.startswith("PREFACE 手寫\n保留我\n\n"))
        self.assertTrue(r.content.endswith("\n\nFOOTER 也保留\n"))
        self.assertIn("- [a](a.md) — d", r.content)
        self.assertNotIn("old.md", r.content)  # 舊條目被換掉

    def test_crlf_block_uses_crlf(self):
        self._wm("a.md", "a")
        crlf = ("HEAD\r\n" + INDEX_BEGIN + "\r\n- [old](old.md)\r\n" + INDEX_END + "\r\nTAIL\r\n")
        r = memory.plan_index_rebuild(self.md, crlf)
        self.assertEqual(r.status, "rebuilt")
        self.assertIn(INDEX_BEGIN + "\r\n- [a](a.md) — d\r\n" + INDEX_END, r.content)
        self.assertTrue(r.content.startswith("HEAD\r\n"))
        self.assertTrue(r.content.endswith("TAIL\r\n"))

    def test_padded_or_indented_markers_not_autoblock(self):
        # codex fresh gate r3 High：縮排/前後空白的標記行**不**算工具標記（精確比對）→ 視為 markerless、不重寫。
        self._wm("a.md", "a")
        for ex in ("  " + INDEX_BEGIN + "  \n- [old](old.md)\n" + INDEX_END + " \n",   # 前後空白
                   "    " + INDEX_BEGIN + "\n    內容\n    " + INDEX_END + "\n"):          # 縮排
            r = memory.plan_index_rebuild(self.md, ex)
            self.assertEqual(r.status, "kept-handwritten", ex)
            self.assertIsNone(r.content)            # 絕不重寫（不吃掉框內手寫）

    def test_documented_markers_in_indented_codeblock_preserved(self):
        # 使用者在手寫索引用縮排 code block 展示標記用法 → 不可被當真標記而重寫（核心安全性質）。
        self._wm("a.md", "a")
        ex = ("# 我的索引\n要自動維護，加入：\n\n    " + INDEX_BEGIN + "\n    " + INDEX_END
              + "\n\n- [A](a.md) — x\n")
        r = memory.plan_index_rebuild(self.md, ex)
        self.assertIsNone(r.content)                # 一字未動
        self.assertEqual(r.status, "kept-handwritten")
        self.assertIsNone(r.note)                   # a.md 已引用 → 無漂移

    def test_fenced_codeblock_markers_not_autoblock(self):
        # codex fresh gate r5 High：``` / ~~~ fence 內的標記（即使第 0 欄精確）不算工具標記 → 保留、不重寫。
        self._wm("a.md", "a")
        cases = [("```", "```"), ("```md", "```"), ("~~~", "~~~"), ("````", "````")]
        for opener, closer in cases:
            ex = ("# 我的索引\n要自動維護，加入：\n\n" + opener + "\n" + INDEX_BEGIN
                  + "\nexample text\n" + INDEX_END + "\n" + closer + "\n\n- [A](a.md) — x\n")
            r = memory.plan_index_rebuild(self.md, ex)
            self.assertEqual(r.status, "kept-handwritten", opener)
            self.assertIsNone(r.content, opener)     # example text 不被吃
            self.assertIsNone(r.note, opener)        # a.md（fence 外）已引用 → 無漂移

    def test_autoblock_after_closed_fence_still_detected(self):
        # fence 追蹤不可破壞正常偵測：關閉的 fence 之後的真標記仍被認、重建。
        self._wm("a.md", "a")
        ex = ("說明：\n```\n範例\n```\n\n" + INDEX_BEGIN + "\n- [old](old.md)\n" + INDEX_END + "\n")
        r = memory.plan_index_rebuild(self.md, ex)
        self.assertEqual(r.status, "rebuilt")
        self.assertIn("- [a](a.md)", r.content)
        self.assertTrue(r.content.startswith("說明：\n```\n範例\n```\n\n"))  # fence 區逐字保留

    def test_empty_dir_with_block_rebuilds_to_empty(self):
        # auto-block 已存在但目錄清空 → 重建成空 block（反映刪除，不留殘條目）。
        ex = INDEX_BEGIN + "\n- [a](a.md) — d\n" + INDEX_END + "\n"
        r = memory.plan_index_rebuild(self.md, ex)
        self.assertEqual(r.status, "rebuilt")
        self.assertEqual(r.content, INDEX_BEGIN + "\n" + INDEX_END + "\n")

    # ── 手寫（無標記）：永不重建 ──────────────────────────────────────────────
    def test_handwritten_all_referenced_silent(self):
        self._wm("a.md", "a")
        self._wm("b.md", "b")
        hw = "# Notes\n- [Alpha](a.md) — 手寫 hook\n- [Bee](b.md) — 手寫 hook\n"
        r = memory.plan_index_rebuild(self.md, hw)
        self.assertEqual(r.status, "kept-handwritten")
        self.assertIsNone(r.content)   # 絕不覆蓋
        self.assertIsNone(r.note)      # 全部已引用 → 不嘮叨

    def test_handwritten_drift_warns_names_missing(self):
        self._wm("a.md", "a")
        self._wm("b.md", "b")
        hw = "# Notes\n- [Alpha](a.md) — 手寫\n"   # b.md 未列入
        r = memory.plan_index_rebuild(self.md, hw)
        self.assertEqual(r.status, "kept-handwritten")
        self.assertIsNone(r.content)
        self.assertIsNotNone(r.note)
        self.assertIn("b.md", r.note)
        self.assertNotIn("a.md", r.note)  # 已引用者不報

    def test_handwritten_angle_bracket_link_referenced(self):
        # `](<a.md>)` 角括號形也算引用，不誤報漂移。
        self._wm("a.md", "a")
        hw = "# Notes\n- [Alpha](<a.md>) — 手寫\n"
        r = memory.plan_index_rebuild(self.md, hw)
        self.assertIsNone(r.note)

    def test_handwritten_curated_not_clobbered_even_if_format_matches(self):
        # curated 標題/hook 與 frontmatter 不同，但無標記 → 一律保留（極性核心：不靠「是否 curated」啟發式）。
        self._wm("reply-in-chinese.md", "reply-in-chinese", "Use zh-TW")
        hw = "- [Reply in Chinese](reply-in-chinese.md) — nicer hand-written hook\n"
        r = memory.plan_index_rebuild(self.md, hw)
        self.assertIsNone(r.content)
        self.assertIsNone(r.note)  # 該檔已引用，靜默保留

    # ── 標記異常：fail-closed 保留 ────────────────────────────────────────────
    def test_malformed_two_begins(self):
        self._wm("a.md", "a")
        bad = f"{INDEX_BEGIN}\nx\n{INDEX_BEGIN}\n{INDEX_END}\n"
        r = memory.plan_index_rebuild(self.md, bad)
        self.assertEqual(r.status, "kept-malformed")
        self.assertIsNone(r.content)
        self.assertIsNotNone(r.note)

    def test_malformed_end_before_begin(self):
        self._wm("a.md", "a")
        bad = f"{INDEX_END}\n{INDEX_BEGIN}\n"
        self.assertEqual(memory.plan_index_rebuild(self.md, bad).status, "kept-malformed")

    def test_malformed_begin_without_end(self):
        self._wm("a.md", "a")
        bad = f"{INDEX_BEGIN}\n- [x](x.md)\n"
        self.assertEqual(memory.plan_index_rebuild(self.md, bad).status, "kept-malformed")

    def test_malformed_end_without_begin(self):
        self._wm("a.md", "a")
        bad = f"prose\n{INDEX_END}\nmore\n"
        self.assertEqual(memory.plan_index_rebuild(self.md, bad).status, "kept-malformed")

    # ── 條目格式 ──────────────────────────────────────────────────────────────
    def test_entry_non_fm_ok_uses_stem_no_desc(self):
        self._raw("weird.md", "no frontmatter, just prose\n")
        r = memory.plan_index_rebuild(self.md, None)
        self.assertIn("- [weird](weird.md)", r.content)
        self.assertNotIn("weird.md) —", r.content)  # 無 description 段

    def test_entry_quoted_description_stripped(self):
        self._raw("a.md", '---\nname: a\ndescription: "quoted hook"\n---\nbody\n')
        r = memory.plan_index_rebuild(self.md, None)
        self.assertIn("- [a](a.md) — quoted hook", r.content)

    def test_entry_missing_description_omitted(self):
        self._raw("a.md", "---\nname: a\n---\nbody\n")
        r = memory.plan_index_rebuild(self.md, None)
        self.assertIn("- [a](a.md)", r.content)
        self.assertNotIn("a.md) —", r.content)

    # ── 損壞檔：fail-closed 中止（codex R1 Medium）──────────────────────────────
    def test_damaged_file_aborts_rebuild(self):
        self._wm("a.md", "a")
        self._raw("broken.md", "")          # 0-byte → damaged
        r = memory.plan_index_rebuild(self.md, None)
        self.assertEqual(r.status, "kept-unreadable")
        self.assertIsNone(r.content)        # 不寫（即使是建新路徑也不寫半套）
        self.assertIn("broken.md", r.note)

    def test_damaged_blank_file_aborts(self):
        self._wm("a.md", "a")
        self._raw("blank.md", "   \n\n")    # 全空白 → damaged
        self.assertEqual(memory.plan_index_rebuild(self.md, None).status, "kept-unreadable")

    def test_non_fm_ok_is_not_damaged_still_indexed(self):
        # 非 fm_ok（可讀、未損、以 raw hash 同步）→ 不算損壞、照列（stem 條目），不中止。
        self._raw("a.md", "no frontmatter, just prose\n")
        self._wm("b.md", "b", "bee")
        r = memory.plan_index_rebuild(self.md, None)
        self.assertEqual(r.status, "created")
        self.assertIn("- [a](a.md)", r.content)
        self.assertIn("- [b](b.md) — bee", r.content)

    # ── 連結目標 percent-encode + 漂移對稱（codex R1 Low）─────────────────────────
    def test_link_target_percent_encoded(self):
        self._raw("foo#bar.md", "no fm\n")          # `#` 不可裸用（會成 fragment）
        self._raw("a b.md", "no fm\n")              # 空白
        r = memory.plan_index_rebuild(self.md, None)
        self.assertIn("(foo%23bar.md)", r.content)
        self.assertIn("(a%20b.md)", r.content)
        self.assertNotIn("(foo#bar.md)", r.content)

    def test_slug_target_not_encoded(self):
        self._wm("reply-in-chinese.md", "reply-in-chinese", "x")
        r = memory.plan_index_rebuild(self.md, None)
        self.assertIn("(reply-in-chinese.md)", r.content)  # unreserved 不編碼

    def test_drift_matches_encoded_and_angle_and_space_forms(self):
        self._wm("a b.md", "ab")                    # 檔名含空白
        for target in ("a%20b.md", "<a b.md>", "<a%20b.md>"):
            hw = f"# Notes\n- [x]({target}) — h\n"
            r = memory.plan_index_rebuild(self.md, hw)
            self.assertIsNone(r.note, target)       # 各種形式都對得上 → 不誤報漂移

    def test_drift_handles_fragment_and_query_links(self):
        # codex R2 Low：手寫連結帶 fragment/query（`](a.md#notes)`/`](a.md?v=1)`）仍視為引用 a.md，不誤報漂移。
        self._wm("a.md", "a")
        for ref in ("a.md#notes", "a.md?v=1", "<a.md#sec>"):
            r = memory.plan_index_rebuild(self.md, f"# Notes\n- [A]({ref}) — h\n")
            self.assertIsNone(r.note, ref)

    def test_drift_non_sibling_link_does_not_suppress(self):
        # codex fresh gate r2 Low：別目錄的 a.md（sub/、../、/abs）不可當本地 sibling 引用 → 仍報本地 a.md 漂移。
        self._wm("a.md", "a")
        for ref in ("sub/a.md", "../archive/a.md", "/tmp/a.md"):
            r = memory.plan_index_rebuild(self.md, f"# Notes\n- [x]({ref}) — y\n")
            self.assertIsNotNone(r.note, ref)
            self.assertIn("a.md", r.note)

    def test_drift_warns_on_stale_referenced_but_missing(self):
        # 雙向：手寫索引列出 b.md 但檔已不在 → 報「索引列出但已不存在」。
        self._wm("a.md", "a")
        hw = "# Notes\n- [A](a.md) — x\n- [B](b.md) — 已刪但還列著\n"
        r = memory.plan_index_rebuild(self.md, hw)
        self.assertIsNotNone(r.note)
        self.assertIn("已不存在", r.note)
        self.assertIn("b.md", r.note)

    def test_drift_self_link_to_index_not_flagged_stale(self):
        # 索引自指 `](MEMORY.md)` 不算殘留死連結（INDEX_FILE 排除）。
        self._wm("a.md", "a")
        hw = "# Notes\n[本檔](MEMORY.md)\n- [A](a.md) — x\n"
        r = memory.plan_index_rebuild(self.md, hw)
        self.assertIsNone(r.note)

    def test_drift_ignores_links_inside_code_examples(self):
        # codex fresh gate r6 Medium：只在 fenced/縮排 code 範例裡出現的連結不算真引用 → present 檔仍報漂移。
        self._wm("new.md", "new")
        r1 = memory.plan_index_rebuild(self.md, "# 索引\n範例：\n```\n- [New](new.md)\n```\n")
        self.assertIsNotNone(r1.note); self.assertIn("new.md", r1.note)        # fenced 內不算
        r2 = memory.plan_index_rebuild(self.md, "# 索引\n範例：\n\n    - [New](new.md)\n")
        self.assertIsNotNone(r2.note); self.assertIn("new.md", r2.note)        # 縮排 code 內不算
        r3 = memory.plan_index_rebuild(self.md, "# 索引\n- [New](new.md) — 真的列了\n")
        self.assertIsNone(r3.note)                                             # prose 真引用 → 不報

    def test_drift_ignores_external_url_links(self):
        # codex fresh gate Low：手寫索引引用外部 URL `https://example.com/a.md` 不算本地 a.md 引用 → 仍報漂移。
        self._wm("a.md", "a")
        for url in ("https://example.com/a.md", "mailto:x@y/a.md", "//host/share/a.md"):
            r = memory.plan_index_rebuild(self.md, f"# Notes\n- [ext]({url}) — x\n")
            self.assertIsNotNone(r.note, url)
            self.assertIn("a.md", r.note)

    def test_undecodable_filename_renders_without_crash(self):
        # codex fresh gate Medium：POSIX 檔名含非 UTF-8 bytes（surrogateescape）→ quote_from_bytes / title 中和
        # surrogate → content.encode("utf-8") 不得 raise（否則 apply 在 memory 已寫後崩潰）。
        import os
        mdb = os.fsencode(str(self.md))
        try:
            with open(os.path.join(mdb, b"bad\xff.md"), "wb") as f:
                f.write(b"---\nname: okk\ndescription: d\n---\nbody\n")     # fm_ok → 連結目標含 surrogate
            with open(os.path.join(mdb, b"raw\xfe.md"), "wb") as f:
                f.write(b"no frontmatter prose\n")                          # 非 fm_ok → 標題 stem 含 surrogate
        except (OSError, ValueError):
            self.skipTest("FS 不支援非 UTF-8 檔名")
        r = memory.plan_index_rebuild(self.md, None)
        self.assertEqual(r.status, "created")
        r.content.encode("utf-8")                  # 關鍵：不得 raise UnicodeEncodeError
        self.assertIn("bad%FF.md", r.content)      # 連結目標 percent-encode bytes
        self.assertIn("raw%FE.md", r.content)

    @_caps.needs_control_char_name
    def test_title_strips_control_chars(self):
        # 非 fm_ok 檔名含控制字元 → 標題剔除、連結 percent-encode → 條目不破行。
        self._raw("a\tb.md", "no fm\n")
        r = memory.plan_index_rebuild(self.md, None)
        entry_lines = [ln for ln in r.content.splitlines() if ln.startswith("- [")]
        self.assertEqual(len(entry_lines), 1)       # 單行、未被 \t 破壞
        self.assertIn("(a%09b.md)", r.content)

    # ── symlink 根 ────────────────────────────────────────────────────────────
    def test_symlink_root_raises(self):
        real = Path(self._td.name) / "real"
        real.mkdir()
        link = Path(self._td.name) / "linkmem"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink 不支援")
        with self.assertRaises(memory.UnsafeMemoryDir):
            memory.plan_index_rebuild(link, None)


if __name__ == "__main__":
    unittest.main()
