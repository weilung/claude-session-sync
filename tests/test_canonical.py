import json
import tempfile
import unicodedata
import unittest
from pathlib import Path

from claude_session_sync.canonical import FileState, canon_dumps, canon_hash, load


def _w(tmp: Path, name: str, data, *, binary=False) -> str:
    p = tmp / name
    if binary:
        p.write_bytes(data)
    else:
        p.write_text(data, encoding="utf-8")
    return str(p)


class TestCanonHash(unittest.TestCase):
    def test_key_order_and_whitespace_invariant(self):
        a = canon_hash(json.loads('{"a":1,"b":2}'))
        b = canon_hash(json.loads('{ "b": 2 , "a": 1 }'))
        self.assertEqual(a, b)

    def test_nfc_invariant(self):
        nfc = canon_hash({"c": unicodedata.normalize("NFC", "café")})
        nfd = canon_hash({"c": unicodedata.normalize("NFD", "café")})
        self.assertEqual(nfc, nfd)

    def test_semantic_difference_not_collapsed(self):
        self.assertNotEqual(canon_hash({"a": 1}), canon_hash({"a": 1.0}))
        self.assertNotEqual(canon_hash({"a": 1}), canon_hash({"a": "1"}))
        self.assertNotEqual(canon_hash({"c": "hi"}), canon_hash({"c": "hi "}))

    def test_nfc_distinct_keys_not_collapsed(self):
        # codex r21：NFC 不可正規化 dict 鍵——否則 NFD/NFC 兩個不同 key 互蓋、丟資料。
        nfd_key = unicodedata.normalize("NFD", "é")   # 'e' + 結合重音（2 碼）
        nfc_key = unicodedata.normalize("NFC", "é")   # 'é' 預組（1 碼）
        self.assertNotEqual(nfd_key, nfc_key)              # 確實是兩個不同的 dict key
        c = {"type": "x", nfd_key: 1, nfc_key: 2}
        self.assertEqual(len(c), 3)
        rc = json.loads(canon_dumps(c))
        self.assertEqual(len(rc), 3)                       # 三個 key 全保留（舊碼折成 2、丟一個）
        self.assertEqual(rc[nfd_key], 1)
        self.assertEqual(rc[nfc_key], 2)
        # 不同 key 的兩行 → 不同 hash（不會被誤判雷同而被 union 去重）
        self.assertNotEqual(canon_hash({nfd_key: 1}), canon_hash({nfc_key: 1}))

    def test_nfc_value_still_normalized(self):
        # 值仍要 NFC（跨 OS：內嵌路徑/檔名在 macOS 可能是 NFD）。
        self.assertEqual(
            canon_hash({"c": unicodedata.normalize("NFD", "é")}),
            canon_hash({"c": unicodedata.normalize("NFC", "é")}),
        )


class TestThreeState(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_zero_byte(self):
        r = load(_w(self.tmp, "z.jsonl", b"", binary=True))
        self.assertEqual(r.state, FileState.ZERO_BYTE)
        self.assertTrue(r.state.is_damaged)

    def test_blank_only(self):
        r = load(_w(self.tmp, "b.jsonl", "\n  \n\t\n"))
        self.assertEqual(r.state, FileState.BLANK)
        self.assertTrue(r.state.is_damaged)

    def test_decode_error_not_crash(self):
        # 無 BOM 的非法 UTF-8 → 整檔判 decode_error，不 raise
        r = load(_w(self.tmp, "d.jsonl", b"\xc3\x28 not utf8", binary=True))
        self.assertEqual(r.state, FileState.DECODE_ERROR)
        self.assertIsNotNone(r.decode_error)

    def test_utf16_bom_recovers(self):
        line = json.dumps({"uuid": "u1", "parentUuid": None, "type": "user"})
        p = self.tmp / "u16.jsonl"
        p.write_text(line + "\n", encoding="utf-16")  # 含 BOM
        r = load(str(p))
        self.assertEqual(r.state, FileState.OK)
        self.assertEqual(len(r.ok_lines), 1)
        self.assertEqual(r.ok_lines[0].uuid, "u1")

    def test_ok_with_bad_line(self):
        p = _w(self.tmp, "x.jsonl", '{"uuid":"u1","parentUuid":null,"type":"user"}\nnot json\n')
        r = load(p)
        self.assertEqual(r.state, FileState.OK)
        self.assertTrue(r.has_bad)
        self.assertEqual(len(r.ok_lines), 1)

    def test_crlf_and_bom_line_parse(self):
        body = '{"uuid":"u1","parentUuid":null,"type":"user"}\r\n{"uuid":"u2","parentUuid":"u1","type":"assistant"}\r\n'
        p = self.tmp / "c.jsonl"
        p.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))  # 檔首 BOM + CRLF
        r = load(str(p))
        self.assertEqual(r.state, FileState.OK)
        self.assertEqual([ln.uuid for ln in r.ok_lines], ["u1", "u2"])


if __name__ == "__main__":
    unittest.main()
