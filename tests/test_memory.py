"""Block 1（memory 唯讀核心）測試：frontmatter 解析、正規化內容 hash、identity、列舉、三態。"""
import os
import shutil
import tempfile
import unicodedata
import unittest
from pathlib import Path

from claude_session_sync import atomicio, memory
from claude_session_sync.canonical import FileState

FM = """---
name: review-methodology
description: "loop + cross-model"
metadata:
  type: feedback
  node_type: memory
---

正文第一段。

正文第二段。
"""


def _doc(text: str) -> memory.MemoryDoc:
    return memory.load_memory_bytes(text.encode("utf-8"))


class TestFrontmatterParse(unittest.TestCase):
    def test_happy_path(self):
        d = _doc(FM)
        self.assertEqual(d.state, FileState.OK)
        self.assertTrue(d.fm_ok)
        self.assertEqual(d.name, "review-methodology")
        self.assertEqual(d.frontmatter["description"], "loop + cross-model")
        self.assertEqual(d.frontmatter["metadata"], {"type": "feedback", "node_type": "memory"})
        self.assertEqual(d.body, "\n正文第一段。\n\n正文第二段。")  # 前導空行保留（不去除，codex gate）

    def test_no_frontmatter_falls_back(self):
        d = _doc("沒有 frontmatter 的純文字\n第二行\n")
        self.assertEqual(d.state, FileState.OK)
        self.assertFalse(d.fm_ok)
        self.assertIsNone(d.frontmatter)
        self.assertIsNone(d.name)

    def test_unterminated_fence_is_not_frontmatter(self):
        # 開了 --- 但沒收尾 → 不認 frontmatter，fail-closed 退整檔 raw。
        d = _doc("---\nname: x\n沒有收尾圍欄\n")
        self.assertFalse(d.fm_ok)

    def test_list_in_frontmatter_outside_subset(self):
        # 清單 `- x` 超出子集 → 完整解析失敗（fm_ok False、content_hash 退 raw）→ 身分**不可判 None**：
        # identity 只由 fm_ok 完整 parse 取得（唯完整 parse 保證頂層 name 唯一，codex gate4）；非子集 frontmatter
        # 跨檔身分留 P2（A14/A17.5）。
        d = _doc("---\nname: x\ntags:\n  - a\n  - b\n---\nbody\n")
        self.assertFalse(d.fm_ok)
        self.assertIsNone(d.name)

    def test_tab_indent_outside_subset(self):
        d = _doc("---\nmeta:\n\ttype: x\n---\nbody\n")
        self.assertFalse(d.fm_ok)

    def test_orphan_indent_outside_subset(self):
        # 縮排行卻無空值父鍵 → 子集外。
        d = _doc("---\nname: x\n  type: y\n---\nbody\n")
        self.assertFalse(d.fm_ok)

    def test_quotes_stripped(self):
        d = _doc("---\nname: 'quoted'\ndescription: \"dq\"\n---\nb\n")
        self.assertEqual(d.frontmatter["name"], "quoted")
        self.assertEqual(d.frontmatter["description"], "dq")

    def test_colon_no_space_in_value_preserved(self):
        # colon 後**非空白**（如 URL）仍是合法 plain scalar → 保留。
        d = _doc("---\nname: x\nurl: http://example.com\n---\nb\n")
        self.assertTrue(d.fm_ok)
        self.assertEqual(d.frontmatter["url"], "http://example.com")

    def test_colon_space_in_value_fail_closed(self):
        # `foo: bar`（colon+space）非安全 plain scalar（YAML mapping 分隔）→ fail-closed（codex gate）。
        a = _doc("---\nname: x\ndesc: foo: bar\n---\nb\n")
        q = _doc("---\nname: x\ndesc: \"foo: bar\"\n---\nb\n")
        self.assertFalse(a.fm_ok)
        self.assertNotEqual(memory.content_hash(a), memory.content_hash(q))


class TestContentHash(unittest.TestCase):
    def test_key_order_invariant(self):
        a = _doc("---\nname: x\nmetadata:\n  a: p\n  b: q\n---\nbody\n")
        b = _doc("---\nname: x\nmetadata:\n  b: q\n  a: p\n---\nbody\n")
        self.assertTrue(a.fm_ok and b.fm_ok)
        self.assertEqual(memory.content_hash(a), memory.content_hash(b))

    def test_volatile_origin_session_excluded(self):
        # 只差 originSessionId（per-session provenance）→ 不應判成衝突。
        a = _doc("---\nname: x\nmetadata:\n  type: t\n  originSessionId: AAA\n---\nbody\n")
        b = _doc("---\nname: x\nmetadata:\n  type: t\n  originSessionId: BBB\n---\nbody\n")
        self.assertEqual(memory.content_hash(a), memory.content_hash(b))

    def test_top_level_volatile_excluded(self):
        a = _doc("---\nname: x\noriginSessionId: AAA\n---\nbody\n")
        b = _doc("---\nname: x\noriginSessionId: BBB\n---\nbody\n")
        self.assertEqual(memory.content_hash(a), memory.content_hash(b))

    def test_body_single_final_newline_invariant(self):
        # 只吸收**單一** POSIX 檔尾 newline（body\n == body）。
        a = _doc("---\nname: x\n---\nbody line\n")
        c = _doc("---\nname: x\n---\nbody line")
        self.assertEqual(memory.content_hash(a), memory.content_hash(c))

    def test_eof_blank_lines_preserved(self):
        # 多個檔尾空行不壓（unclosed code fence 內的尾端空白行是內容，codex gate2）。
        a = _doc("---\nname: x\n---\nbody line\n")
        b = _doc("---\nname: x\n---\nbody line\n\n\n")
        self.assertNotEqual(memory.content_hash(a), memory.content_hash(b))

    def test_hard_break_trailing_spaces_preserved(self):
        # Markdown hard break（行尾兩空格）有語意 → 不得被吃掉 → 不同 hash（codex gate critical）。
        hard = _doc("---\nname: x\n---\nline  \nnext\n")
        soft = _doc("---\nname: x\n---\nline\nnext\n")
        self.assertNotEqual(memory.content_hash(hard), memory.content_hash(soft))

    def test_leading_blank_line_in_body_preserved(self):
        # 前導空行不去除（可能有語意）→ 安全方向（假衝突勝過靜默丟）。
        a = _doc("---\nname: x\n---\n\nbody\n")
        b = _doc("---\nname: x\n---\nbody\n")
        self.assertNotEqual(memory.content_hash(a), memory.content_hash(b))

    def test_crlf_invariant(self):
        a = memory.load_memory_bytes("---\nname: x\n---\nbody\n".encode("utf-8"))
        b = memory.load_memory_bytes("---\r\nname: x\r\n---\r\nbody\r\n".encode("utf-8"))
        self.assertEqual(memory.content_hash(a), memory.content_hash(b))

    def test_quote_cosmetic_invariant(self):
        a = _doc("---\nname: x\ndescription: hi\n---\nb\n")
        b = _doc("---\nname: x\ndescription: \"hi\"\n---\nb\n")
        self.assertEqual(memory.content_hash(a), memory.content_hash(b))

    def test_body_difference_detected(self):
        a = _doc("---\nname: x\n---\nbody A\n")
        b = _doc("---\nname: x\n---\nbody B\n")
        self.assertNotEqual(memory.content_hash(a), memory.content_hash(b))

    def test_frontmatter_value_difference_detected(self):
        a = _doc("---\nname: x\ndescription: A\n---\nb\n")
        b = _doc("---\nname: x\ndescription: B\n---\nb\n")
        self.assertNotEqual(memory.content_hash(a), memory.content_hash(b))

    def test_nfc_invariant(self):
        nfd = unicodedata.normalize("NFD", "café")
        nfc = unicodedata.normalize("NFC", "café")
        self.assertNotEqual(nfd, nfc)
        a = _doc(f"---\nname: x\n---\n{nfd}\n")
        b = _doc(f"---\nname: x\n---\n{nfc}\n")
        self.assertEqual(memory.content_hash(a), memory.content_hash(b))

    def test_raw_fallback_distinguishes(self):
        # fail-closed raw 路徑仍須區分內容、又吸收 CRLF 與單一檔尾 newline（多個尾端空行不吸收，gate2）。
        a = _doc("沒 frontmatter A\n")
        b = _doc("沒 frontmatter B\n")
        c = _doc("沒 frontmatter A\r\n")  # 與 a 僅 CRLF 差異
        self.assertNotEqual(memory.content_hash(a), memory.content_hash(b))
        self.assertEqual(memory.content_hash(a), memory.content_hash(c))


class TestThreeState(unittest.TestCase):
    def test_zero_byte_damaged(self):
        d = memory.load_memory_bytes(b"")
        self.assertEqual(d.state, FileState.ZERO_BYTE)
        self.assertTrue(d.state.is_damaged)
        self.assertIsNone(memory.content_hash(d))

    def test_blank_damaged(self):
        d = memory.load_memory_bytes(b"  \n\t\n")
        self.assertEqual(d.state, FileState.BLANK)
        self.assertIsNone(memory.content_hash(d))

    def test_decode_error_not_crash(self):
        d = memory.load_memory_bytes(b"\xc3\x28 not utf8")
        self.assertEqual(d.state, FileState.DECODE_ERROR)
        self.assertIsNotNone(d.decode_error)
        self.assertIsNone(memory.content_hash(d))

    def test_utf16_bom_recovers(self):
        d = memory.load_memory_bytes(FM.encode("utf-16"))  # 含 BOM
        self.assertEqual(d.state, FileState.OK)
        self.assertTrue(d.fm_ok)
        self.assertEqual(d.name, "review-methodology")


class TestListing(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_excludes_index_dotfiles_subdirs(self):
        (self.tmp / "a.md").write_text("---\nname: a\n---\nx\n", encoding="utf-8")
        (self.tmp / "b.md").write_text("---\nname: b\n---\ny\n", encoding="utf-8")
        (self.tmp / "MEMORY.md").write_text("# index\n", encoding="utf-8")
        (self.tmp / ".hidden.md").write_text("hidden\n", encoding="utf-8")
        (self.tmp / ".merge").mkdir()
        (self.tmp / ".merge" / "c.md").write_text("inner\n", encoding="utf-8")
        (self.tmp / "notes.txt").write_text("not md\n", encoding="utf-8")
        files = memory.list_memory_files(self.tmp)
        self.assertEqual(set(files), {"a.md", "b.md"})

    def test_missing_dir(self):
        self.assertEqual(memory.list_memory_files(self.tmp / "nope"), {})

    def test_uppercase_ext_and_index_casefold(self):
        # 跨 OS（codex r1-6）：大寫副檔名應視為 memory；大寫 MEMORY.MD 仍須排除。
        (self.tmp / "up.MD").write_text("---\nname: up\n---\nx\n", encoding="utf-8")
        (self.tmp / "MEMORY.MD").write_text("# idx\n", encoding="utf-8")
        files = memory.list_memory_files(self.tmp)
        self.assertIn("up.MD", files)
        self.assertNotIn("MEMORY.MD", files)


class TestFailClosedHardening(unittest.TestCase):
    """codex R1 修正回歸：語意不同不得壓成同 hash；超出子集一律 fail-closed 退 raw。"""

    def test_bool_value_fail_closed(self):
        # flag: true（YAML bool）不得與 flag: "true"（string）壓成同 hash。
        t = _doc("---\nname: x\nflag: true\n---\nb\n")
        s = _doc("---\nname: x\nflag: \"true\"\n---\nb\n")
        self.assertFalse(t.fm_ok)   # bool token → fail-closed 退 raw
        self.assertTrue(s.fm_ok)
        self.assertNotEqual(memory.content_hash(t), memory.content_hash(s))

    def test_number_value_fail_closed(self):
        n = _doc("---\nname: x\nver: 1\n---\nb\n")
        q = _doc("---\nname: x\nver: \"1\"\n---\nb\n")
        self.assertFalse(n.fm_ok)
        self.assertNotEqual(memory.content_hash(n), memory.content_hash(q))

    def test_flow_list_fail_closed(self):
        lst = _doc("---\nname: x\ntags: [a, b]\n---\nb\n")
        s = _doc("---\nname: x\ntags: \"[a, b]\"\n---\nb\n")
        self.assertFalse(lst.fm_ok)
        self.assertNotEqual(memory.content_hash(lst), memory.content_hash(s))

    def test_quoted_with_backslash_fail_closed(self):
        d = _doc('---\nname: x\ndesc: "a\\b"\n---\nbody\n')
        self.assertFalse(d.fm_ok)

    def test_quoted_with_inner_quote_fail_closed(self):
        d = _doc('---\nname: x\n' + 'desc: "a"b"' + '\n---\nbody\n')
        self.assertFalse(d.fm_ok)

    def test_duplicate_top_key_fail_closed(self):
        dup = _doc("---\nname: x\ndesc: A\ndesc: B\n---\nb\n")
        only = _doc("---\nname: x\ndesc: B\n---\nb\n")
        self.assertFalse(dup.fm_ok)   # 重複鍵 → 退 raw，不靜默丟 A
        self.assertNotEqual(memory.content_hash(dup), memory.content_hash(only))

    def test_duplicate_nested_key_fail_closed(self):
        # 用字串值（非數字），確保 fail-closed 是因「重複巢狀鍵」而非值型別（codex r2-4）。
        dup = _doc("---\nname: x\nmeta:\n  a: one\n  a: two\n---\nb\n")
        self.assertFalse(dup.fm_ok)

    def test_nested_string_non_dup_parses(self):
        # 正向對照：同路徑非重複的巢狀字串應可解析（證上面的 fail 來自重複檢查，非型別）。
        d = _doc("---\nname: x\nmeta:\n  a: one\n  b: two\n---\nbody\n")
        self.assertTrue(d.fm_ok)
        self.assertEqual(d.frontmatter["meta"], {"a": "one", "b": "two"})

    def test_deeper_indent_fail_closed(self):
        d = _doc("---\nname: x\nmeta:\n  sub: v\n    deep: y\n---\nb\n")
        self.assertFalse(d.fm_ok)

    def test_four_space_indent_fail_closed(self):
        d = _doc("---\nname: x\nmeta:\n    sub: v\n---\nb\n")
        self.assertFalse(d.fm_ok)

    def test_one_space_indent_fail_closed(self):
        d = _doc("---\nname: x\nmeta:\n sub: v\n---\nb\n")
        self.assertFalse(d.fm_ok)

    def test_name_slug_identity_exact_or_none(self):
        # identity = name slug，回原值不改（exact，含 `-_.`）；非 slug 形（含空白/引號等）→ None（不可判），
        # **不**靜默 strip 成 "x"（靜默 strip 會把語意不同者壓同身分）。slug 限制是 gate2 防誤抽的安全閥。
        self.assertEqual(_doc("---\nname: x-y_z.1\n---\nb\n").name, "x-y_z.1")  # slug 原樣
        self.assertIsNone(_doc('---\nname: " x "\n---\nb\n').name)             # 含空白 → 非 slug → None

    def test_plain_string_quote_still_collapses(self):
        # 純字串的引號差異仍須吸收（只有「型別會變」者才 fail-closed）。
        a = _doc("---\nname: x\ndesc: hello\n---\nb\n")
        b = _doc("---\nname: x\ndesc: \"hello\"\n---\nb\n")
        self.assertTrue(a.fm_ok and b.fm_ok)
        self.assertEqual(memory.content_hash(a), memory.content_hash(b))


class TestBomBlank(unittest.TestCase):
    """codex r1-5：BOM 須在 blank 判斷前移除，否則 BOM-only/BOM+空白被誤判 OK。"""

    def test_utf16_bom_only_is_blank(self):
        d = memory.load_memory_bytes("".encode("utf-16"))   # 只有 BOM
        self.assertEqual(d.state, FileState.BLANK)
        self.assertIsNone(memory.content_hash(d))

    def test_utf16_bom_plus_whitespace_is_blank(self):
        d = memory.load_memory_bytes("  \n".encode("utf-16"))
        self.assertEqual(d.state, FileState.BLANK)


class TestR2Hardening(unittest.TestCase):
    """codex R2（resume）修正回歸：key 子集 allowlist、完整 YAML 隱式型別、單一 BOM。"""

    def test_list_of_mapping_key_fail_closed(self):
        # `- a: p` 的 key 是 "- a"（list marker），不得被當 mapping key（反序 list 經 canon 排序後同 hash）。
        d = _doc("---\nname: x\n- a: p\n- b: q\n---\nbody\n")
        self.assertFalse(d.fm_ok)

    def test_inline_comment_fail_closed(self):
        a = _doc("---\nname: x\ndesc: foo # c\n---\nb\n")
        q = _doc("---\nname: x\ndesc: \"foo # c\"\n---\nb\n")
        self.assertFalse(a.fm_ok)   # YAML 把 # c 當註解 → 與 quoted 不同義 → fail-closed
        self.assertNotEqual(memory.content_hash(a), memory.content_hash(q))

    def test_timestamp_fail_closed(self):
        d = _doc("---\nname: x\ndate: 2024-06-20\n---\nb\n")
        q = _doc("---\nname: x\ndate: \"2024-06-20\"\n---\nb\n")
        self.assertFalse(d.fm_ok)
        self.assertNotEqual(memory.content_hash(d), memory.content_hash(q))

    def test_sexagesimal_fail_closed(self):
        d = _doc("---\nname: x\ndur: 1:20\n---\nb\n")
        self.assertFalse(d.fm_ok)

    def test_yes_no_y_n_bool_fail_closed(self):
        for tok in ("yes", "no", "on", "off", "y", "n", "Yes", "TRUE"):
            d = _doc(f"---\nname: x\nf: {tok}\n---\nb\n")
            self.assertFalse(d.fm_ok, f"{tok} 應 fail-closed")

    def test_e_notation_number_fail_closed(self):
        d = _doc("---\nname: x\nv: 1e3\n---\nb\n")
        q = _doc("---\nname: x\nv: \"1e3\"\n---\nb\n")
        self.assertFalse(d.fm_ok)
        self.assertNotEqual(memory.content_hash(d), memory.content_hash(q))

    def test_slug_and_uuid_still_parse(self):
        # 真實 memory 值（kebab slug、UUID）不得被新嚴格規則誤殺。
        d = _doc("---\nname: review-methodology\nmetadata:\n"
                 "  originSessionId: 9d2447dc-afdb-466a-bb8f-68f4b9f6f7ce\n---\nbody\n")
        self.assertTrue(d.fm_ok)
        self.assertEqual(d.name, "review-methodology")

    def test_colon_lead_value_fail_closed(self):
        # `desc: : foo` → 值 ": foo"（leading `:` indicator）不得被當純字串、與 quoted 同 hash（codex r3）。
        a = _doc("---\nname: x\ndesc: : foo\n---\nb\n")
        q = _doc("---\nname: x\ndesc: \": foo\"\n---\nb\n")
        self.assertFalse(a.fm_ok)
        self.assertNotEqual(memory.content_hash(a), memory.content_hash(q))

    def test_double_bom_preserves_content(self):
        # 只剝一個 codec BOM；內容自帶的第二個 ﻿ 不得靜默丟（utf-8-sig 已消耗第一個）。
        one = memory.load_memory_bytes("﻿body".encode("utf-8"))
        two = memory.load_memory_bytes("﻿﻿body".encode("utf-8"))
        self.assertNotEqual(memory.content_hash(one), memory.content_hash(two))


class TestGateHardening(unittest.TestCase):
    """codex 塊末 fresh gate 修正回歸：圍欄縮排、空 mapping、型別折疊 key、symlink、非目錄。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_indented_fence_not_frontmatter(self):
        # `  ---`（縮排）不是圍欄 → 不得與真 frontmatter 同 hash。
        a = _doc("  ---\nname: x\n  ---\nbody\n")
        b = _doc("---\nname: x\n---\nbody\n")
        self.assertFalse(a.fm_ok)
        self.assertNotEqual(memory.content_hash(a), memory.content_hash(b))

    def test_empty_mapping_vs_only_volatile_child(self):
        # `meta:` 無 child（null/空 map 歧義）→ fail-closed；尤其不得與「只有 volatile child」剔除後同 hash。
        empty = _doc("---\nname: x\nmeta:\n---\nbody\n")
        only_vol = _doc("---\nname: x\nmeta:\n  originSessionId: A\n---\nbody\n")
        self.assertFalse(empty.fm_ok)
        self.assertTrue(only_vol.fm_ok)
        self.assertNotEqual(memory.content_hash(empty), memory.content_hash(only_vol))

    def test_empty_mapping_mid_block_fail_closed(self):
        # 空 `key:` 在中段（後接另一頂層鍵）也須 fail-closed。
        d = _doc("---\nname: x\nmeta:\nother: y\n---\nbody\n")
        self.assertFalse(d.fm_ok)

    def test_nbsp_after_fence_not_fence(self):
        # `---` 後接 NBSP（U+00A0）不是圍欄（只容忍 ASCII 空格）→ 不得與真 `---` 同 hash（codex gate2）。
        a = _doc("--- \nname: x\n---\nbody\n")
        b = _doc("---\nname: x\n---\nbody\n")
        self.assertFalse(a.fm_ok)
        self.assertNotEqual(memory.content_hash(a), memory.content_hash(b))

    def test_trailing_ascii_space_fence_ok(self):
        # 反向：`---` 後接 ASCII 空格仍是圍欄（cosmetic）→ 與 `---` 同 hash（\x20 強制 ASCII 空格）。
        a = _doc("---\x20\nname: x\n---\x20\nbody\n")
        b = _doc("---\nname: x\n---\nbody\n")
        self.assertTrue(a.fm_ok and b.fm_ok)
        self.assertEqual(memory.content_hash(a), memory.content_hash(b))

    def test_typed_key_fail_closed(self):
        # YAML 把 true/True 折成同一 bool key、1/01 折成 int → 反序經 canon 排序後同 hash → 須 fail-closed。
        d = _doc("---\nname: x\ntrue: A\nTrue: B\n---\nbody\n")
        self.assertFalse(d.fm_ok)
        n = _doc("---\nname: x\n1: A\n---\nbody\n")
        self.assertFalse(n.fm_ok)

    def test_symlink_excluded(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        memdir = self.tmp / "mem"
        memdir.mkdir()
        (memdir / "real.md").write_text("---\nname: r\n---\nx\n", encoding="utf-8")
        try:
            (memdir / "link.md").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink 不可用")
        files = memory.list_memory_files(memdir)
        self.assertIn("real.md", files)
        self.assertNotIn("link.md", files)

    def test_non_dir_returns_empty(self):
        f = self.tmp / "notadir"
        f.write_text("x\n", encoding="utf-8")
        self.assertEqual(memory.list_memory_files(f), {})


class TestIdentityExtraction(unittest.TestCase):
    """identity（`.name` slug）抽取——與完整子集解析解耦（gate2）但結構安全（gate3）。最高不變量：永不把
    多行構造內的 `name:` 誤當頂層身分（conflation/mis-suppress），也不因出子集欄漏掉乾淨命名（漏判重複/漏擋復活）。"""

    def test_fm_ok_name_extracted_any_position(self):
        # fm_ok 用 strict parse（懂結構）→ name 不論位置都取得（即使在巢狀區塊之後）。
        self.assertEqual(_doc("---\nname: my-fact\n---\nb\n").name, "my-fact")
        self.assertEqual(_doc("---\nmetadata:\n  type: project\nname: my-fact\n---\nb\n").name, "my-fact")

    def test_out_of_subset_identity_is_none(self):
        # 非 fm_ok（出子集欄如 list 值）→ 身分一律 None（lenient 部分掃描無法保證頂層 name 唯一 → 不可信，
        # codex gate4）。跨檔身分對非子集 frontmatter 留 P2（A14/A17.5）；content_hash 仍可按檔名+raw 同步。
        self.assertIsNone(_doc("---\nname: my-fact\ntags: [a, b]\n---\nb\n").name)
        self.assertIsNone(_doc("---\ndesc-x: plain\nname: my-fact\ntags: [a, b]\n---\nb\n").name)

    def test_bad_name_line_is_none(self):
        # name 行本身不可判（list 值 / 非 slug / 缺）→ None（不可判，fail-closed）。
        self.assertIsNone(_doc("---\nname: [a, b]\ndesc: d\n---\nb\n").name)
        self.assertIsNone(_doc('---\nname: "a b"\ntags: [x]\n---\nb\n').name)   # 含空白 → 非 slug
        self.assertIsNone(_doc("---\ndesc: d\ntags: [x]\n---\nb\n").name)        # 無 name

    def test_multiline_flow_mapping_name_not_misread(self):
        # gate3/gate4：`metadata: {` 開多行 flow → 整段非 fm_ok → 身分 None（不論 flow 內有無 col-0 `name:`、
        # 也不論真頂層 name 在 flow 前後）→ 絕不誤抽 flow 內的 `name:`，亦不冒險回部分掃描值。
        self.assertIsNone(_doc("---\nmetadata: {\nname: fake\n}\n---\nb\n").name)
        self.assertIsNone(_doc("---\nname: real\nmetadata: {\nname: fake\n}\n---\nb\n").name)

    def test_multiline_quoted_scalar_name_not_misread(self):
        # gate3：多行雙引號 scalar 的續行 col-0 `name: fake` 不可被當頂層 → None（停在開引號的複雜值）。
        self.assertIsNone(_doc('---\ndesc: "line1\nname: fake"\n---\nb\n').name)

    def test_nested_name_not_misread(self):
        # 巢狀 `  name: fake`（在 metadata: 下）不是頂層身分；頂層 name: real 才是。
        self.assertEqual(_doc("---\nname: real\nmetadata:\n  name: fake\n---\nb\n").name, "real")

    def test_name_after_complex_value_fail_closed_when_not_fm_ok(self):
        # 非 fm_ok 檔：name 在複雜值（flow）之後 → 開頭掃描在複雜值處停 → None（fail-closed；保守但安全）。
        self.assertIsNone(_doc("---\ntags: [a, b]\nname: my-fact\n---\nb\n").name)


class TestListingLongPath(unittest.TestCase):
    r"""codex longpath-r2 High 回歸守衛：**非 staging** 的 >260 memory/ 夾必須維持 fail-closed。

    `reparse_kind` 預設 plain（長路徑僅 memory-merge staging opt-in）；否則深 memory/ 夾的 lstat 過關、
    但後續 plain is_dir()/iterdir() 260-bound 失敗 → `list_memory_files` 誤回 {}（看似空）→ 驅動
    local-deleted 抑制真實 memory。此測驗「深夾不會被誤當空夾」。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())   # 非 TemporaryDirectory：其 cleanup 無法刪 >260 深夾

    def tearDown(self):
        shutil.rmtree(atomicio.os_path(self.tmp), ignore_errors=True)   # \\?\ → 遞迴刪深路徑

    def test_deep_memory_dir_not_seen_as_empty(self):
        seg = "n" * 130
        deep_mem = self.tmp / seg / seg / "memory"                 # >260
        os.makedirs(atomicio.os_path(deep_mem), exist_ok=True)     # setup 以 \\?\ 建深夾
        atomicio.atomic_create_bytes(deep_mem / "a.md", b"---\nname: a\n---\nx\n", long_path=True)
        try:
            os.lstat(str(deep_mem))   # plain lstat 能否處理 >260 決定本平台分支
            native_long = True
        except OSError:
            native_long = False
        if native_long:              # Linux / Windows LongPaths-ON：原生 >260 → 正常列舉（**非空**）
            self.assertIn("a.md", memory.list_memory_files(deep_mem))
        else:                        # Windows LongPaths-OFF：plain reparse_kind→os.lstat raise→"other"
            with self.assertRaises(memory.UnsafeMemoryDir):   # fail-closed；**不可**回 {}（否則抑制真實 memory）
                memory.list_memory_files(deep_mem)


if __name__ == "__main__":
    unittest.main()
