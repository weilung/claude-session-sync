import json
import os
import tempfile
import unittest
from pathlib import Path

from claude_session_sync import tombstone as tb
from tests import _caps


class TestTombstone(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.proj = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_empty_not_initialized(self):
        self.assertFalse(tb.is_initialized(self.proj))
        self.assertIsNone(tb.read_coverage(self.proj))
        self.assertEqual(tb.read_tombstones(self.proj), {})

    @_caps.needs_unreadable_dir
    def test_unenumerable_tombstones_dir_fails_closed(self):
        # e2e gate11 finding1：.tombstones/ 存在、`_coverage.json` 可經確切路徑讀（execute 位），但 .tombstones/ 本身
        # **不可列舉**（read-denied）→ read_tombstones/corrupt_… 的 glob **fail-open** 漏刪除標記 → 復活已刪（A3）。
        # read_coverage 須連可列舉性一併驗 → 回 None（→ is_initialized False → 專案 blocked）。POSIX-only。
        tb.write_coverage(self.proj, epoch=1)
        self.assertTrue(tb.is_initialized(self.proj))            # 前提：正常可讀 → 已初始化
        td = tb.tombstones_dir(self.proj)
        os.chmod(td, 0o311)                                      # execute（可 open _coverage.json）但不可列舉（無 read）
        try:
            self.assertIsNone(tb.read_coverage(self.proj))       # fail-closed（不可列舉 → 視為未初始化）
            self.assertFalse(tb.is_initialized(self.proj))       # → 專案 blocked，不信「無 tombstone」
        finally:
            os.chmod(td, 0o700)

    @_caps.needs_symlink
    def test_symlink_tombstones_dir_blocks_read_and_refuses_write(self):
        # e2e gate3 #3：<proj>/.tombstones 是 symlink（可指界外的假 coverage/tombstone）→ read_coverage 不跟隨、
        # 回 None（→ is_initialized False → 專案 blocked、不信外部）、read_tombstones 回 {}、寫入 refuse（不寫穿界外）。
        elsewhere = self.proj / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "_coverage.json").write_text(
            json.dumps({"schema_version": 1, "initialized": True, "epoch": 1,
                        "bootstrap_time": "t", "machine": "m"}), encoding="utf-8")
        (self.proj / ".tombstones").symlink_to(elsewhere, target_is_directory=True)
        self.assertIsNone(tb.read_coverage(self.proj))         # 不跟隨 symlink 讀界外 coverage
        self.assertFalse(tb.is_initialized(self.proj))          # → 未初始化 → 上層 blocked
        self.assertEqual(tb.read_tombstones(self.proj), {})     # 不讀界外 tombstone
        with self.assertRaises(tb.UnsafeTombstonesDir):
            tb.write_coverage(self.proj)                         # 拒寫（symlink .tombstones）

    @_caps.needs_symlink
    def test_symlink_tombstone_leaf_files_not_followed(self):
        # e2e gate4 #1：.tombstones 是**正常夾**，但其中 leaf 檔是指向界外的 symlink → read_coverage 不信（None）、
        # symlink 刪除標記落 corrupt（fail-closed blocked）、digest 不 hash 界外。
        td = self.proj / ".tombstones"
        td.mkdir()
        outside = self.proj / "outside"
        outside.mkdir()
        (outside / "cov.json").write_text(json.dumps(
            {"schema_version": 1, "initialized": True, "epoch": 1, "bootstrap_time": "t", "machine": "m"}),
            encoding="utf-8")
        (td / "_coverage.json").symlink_to(outside / "cov.json")
        self.assertIsNone(tb.read_coverage(self.proj))          # 不跟隨 symlink _coverage.json 讀界外
        self.assertFalse(tb.is_initialized(self.proj))
        (outside / "t.json").write_text(json.dumps({"kind": "session", "target": "s1"}), encoding="utf-8")
        (td / "s1.deleted.json").symlink_to(outside / "t.json")
        self.assertEqual(tb.read_tombstones(self.proj), {})     # symlink 刪除標記非有效（不跟隨）
        self.assertIn(("session", "s1"), tb.corrupt_tombstone_targets(self.proj))   # → corrupt/blocked（fail-closed）

    def test_coverage_init(self):
        tb.write_coverage(self.proj, epoch=1)
        self.assertTrue(tb.is_initialized(self.proj))
        cov = tb.read_coverage(self.proj)
        self.assertEqual(cov.epoch, 1)
        self.assertIsNotNone(cov.bootstrap_time)

    def test_session_and_memory_tombstones(self):
        tb.write_session_tombstone(self.proj, "sid-1", base_hash="h1")
        tb.write_memory_tombstone(self.proj, "secret.md", base_hash="h2")
        ts = tb.read_tombstones(self.proj)
        self.assertEqual(len(ts), 2)
        self.assertEqual(tb.find_session_tombstone(self.proj, "sid-1").base_hash, "h1")
        self.assertEqual(tb.find_memory_tombstone(self.proj, "secret.md").base_hash, "h2")
        self.assertIsNone(tb.find_session_tombstone(self.proj, "nope"))

    def test_raw_file_digest(self):
        p = self.proj / "f.jsonl"
        p.write_bytes(b"hello")
        import hashlib
        self.assertEqual(tb.raw_file_digest(p), hashlib.sha256(b"hello").hexdigest())
        self.assertEqual(tb.raw_file_digest(p), tb.raw_file_digest(p))            # 確定性
        self.assertIsNone(tb.raw_file_digest(self.proj / "missing.jsonl"))        # 讀不到→None

    def test_digest_deterministic_and_changes(self):
        d0 = tb.tombstone_dir_digest(self.proj)
        tb.write_coverage(self.proj, epoch=1)
        d1 = tb.tombstone_dir_digest(self.proj)
        self.assertNotEqual(d0, d1)
        self.assertEqual(d1, tb.tombstone_dir_digest(self.proj))  # 確定性
        tb.write_session_tombstone(self.proj, "sid-1", base_hash="h1")
        d2 = tb.tombstone_dir_digest(self.proj)
        self.assertNotEqual(d1, d2)

    def test_digest_includes_coverage_epoch_change(self):
        tb.write_coverage(self.proj, epoch=1)
        d1 = tb.tombstone_dir_digest(self.proj)
        tb.write_coverage(self.proj, epoch=2)  # 只改 epoch
        self.assertNotEqual(d1, tb.tombstone_dir_digest(self.proj))

    def test_session_tombstone_wrong_kind_is_corrupt(self):
        # codex r13-1：檔名 secret 但內容 kind=memory → 不可當有效 memory tombstone 而漏掉 secret。
        d = tb.tombstones_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        (d / "secret.deleted.json").write_text('{"kind":"memory","target":"x"}', encoding="utf-8")
        self.assertNotIn(("memory", "x"), tb.read_tombstones(self.proj))
        self.assertIn(("session", "secret"), tb.corrupt_tombstone_targets(self.proj))

    def test_coverage_non_bool_initialized_fails_closed(self):
        # codex r13-2：initialized 非 bool（如字串 "false"）→ 不可被當已初始化。
        d = tb.tombstones_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        (d / tb.COVERAGE_FILE).write_text('{"initialized":"false","epoch":0}', encoding="utf-8")
        self.assertIsNone(tb.read_coverage(self.proj))
        self.assertFalse(tb.is_initialized(self.proj))

    def test_coverage_bad_epoch_fails_closed(self):
        d = tb.tombstones_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        (d / tb.COVERAGE_FILE).write_text('{"initialized":true,"epoch":"1"}', encoding="utf-8")
        self.assertFalse(tb.is_initialized(self.proj))

    def test_digest_excludes_tmp(self):
        tb.write_coverage(self.proj, epoch=1)
        base = tb.tombstone_dir_digest(self.proj)
        (tb.tombstones_dir(self.proj) / "junk.tmp").write_text("x", encoding="utf-8")
        self.assertEqual(base, tb.tombstone_dir_digest(self.proj))

    def test_memory_tombstone_target_mismatch_is_corrupt(self):
        # codex P1d-r1：檔名 memory-secret.md 但內容 target=other.md → 不可當 other.md 的有效 tombstone，
        # 且須把 secret.md（檔名身分）標 corrupt（否則 secret.md 既無有效 tombstone 也不被阻擋 → 復活）。
        d = tb.tombstones_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        (d / "memory-secret.md.deleted.json").write_text(
            '{"kind":"memory","target":"other.md"}', encoding="utf-8")
        self.assertNotIn(("memory", "other.md"), tb.read_tombstones(self.proj))
        self.assertIn(("memory", "secret.md"), tb.corrupt_tombstone_targets(self.proj))

    def test_memory_tombstone_roundtrip_valid(self):
        # 正常寫入（target round-trip 回檔名）→ 有效、不進 corrupt（fail-closed 不誤殺正常 tombstone）。
        tb.write_memory_tombstone(self.proj, "secret.md", base_hash="h")
        self.assertEqual(tb.find_memory_tombstone(self.proj, "secret.md").base_hash, "h")
        self.assertEqual(tb.corrupt_tombstone_targets(self.proj), set())

    def test_memory_tombstone_sanitize_collision_is_corrupt(self):
        # codex P1d-r2：檔名 memory-a_b.md 但內容 target=a/b.md（sanitize 後撞同檔名）→ 不可記成 ("memory","a/b.md")
        # 而讓真扁平檔 a_b.md 無 tombstone。須當 corrupt 並以檔名身分 ("memory","a_b.md") 阻擋（target==ftarget）。
        d = tb.tombstones_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        (d / "memory-a_b.md.deleted.json").write_text(
            '{"kind":"memory","target":"a/b.md"}', encoding="utf-8")
        self.assertNotIn(("memory", "a/b.md"), tb.read_tombstones(self.proj))
        self.assertIn(("memory", "a_b.md"), tb.corrupt_tombstone_targets(self.proj))

    # ── identity（Block 2b 跨檔身分，A14/§7.2.3）─────────────────────────────

    def test_memory_tombstone_identity_roundtrip(self):
        # write→read 帶 identity（刪除檔的 frontmatter name）→ 供換檔名復活偵測。
        tb.write_memory_tombstone(self.proj, "slug-a.md", base_hash="h", identity="my-fact")
        t = tb.find_memory_tombstone(self.proj, "slug-a.md")
        self.assertEqual(t.identity, "my-fact")
        self.assertEqual(tb.corrupt_tombstone_targets(self.proj), set())

    def test_memory_tombstone_default_identity_none(self):
        # 不帶 identity（legacy/Block 1 寫法）→ identity=None（仍是合法檔名鍵 tombstone，只是不參與 identity 配對）。
        tb.write_memory_tombstone(self.proj, "slug-a.md", base_hash="h")
        self.assertIsNone(tb.find_memory_tombstone(self.proj, "slug-a.md").identity)

    def test_identity_non_string_parsed_as_none(self):
        # 缺欄位 / 非字串 / 空白 identity → None（與 MemoryDoc.name 對稱；不讓型別錯的值意外 == 某 doc.name）。
        d = tb.tombstones_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        for body in ('{"kind":"memory","target":"x.md","identity":123}',
                     '{"kind":"memory","target":"x.md","identity":"  "}',
                     '{"kind":"memory","target":"x.md"}'):
            (d / "memory-x.md.deleted.json").write_text(body, encoding="utf-8")
            self.assertIsNone(tb.find_memory_tombstone(self.proj, "x.md").identity, body)

    def test_session_tombstone_identity_none(self):
        # session tombstone 無 frontmatter 身分 → identity 恆 None。
        tb.write_session_tombstone(self.proj, "sid-1", base_hash="h")
        self.assertIsNone(tb.find_session_tombstone(self.proj, "sid-1").identity)


if __name__ == "__main__":
    unittest.main()
