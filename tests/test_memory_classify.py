"""P1d Block 2：memory 檔級 diff + classify（唯讀）。對稱 test_scan.TestPlanProjectPair。

涵蓋閘序逐條對稱 session（tombstone 先於配對、known/local_known 對稱刪除、各 baseline/collision/corrupt/
damaged 閘）+ memory 專屬語意（無 ff：identical/conflict-content；suppress 用正規化 content_hash → 容忍
cosmetic 重排；volatile 鍵不算差異；out-of-subset frontmatter 仍可複製）。
"""
import tempfile
import unicodedata
import unittest
from pathlib import Path

from claude_session_sync import memory, tombstone
from tests import _caps


def _text(slug="fact", body="hello", desc="d", origin=None, extra=None):
    """合法 frontmatter + 正文。origin=originSessionId（易變鍵）；extra=額外 frontmatter 行（可塞 out-of-subset）。"""
    lines = ["---", f"name: {slug}", f"description: {desc}", "metadata:", "  type: project"]
    if origin is not None:
        lines.append(f"originSessionId: {origin}")
    if extra is not None:
        lines.append(extra)
    lines += ["---", body, ""]
    return "\n".join(lines)


class TestPlanMemoryPair(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.ld = self.tmp / "local"   # **專案夾**（memory 在其下 memory/ 子夾）
        self.hd = self.tmp / "hub"
        self.ld.mkdir()
        self.hd.mkdir()

    def tearDown(self):
        self._td.cleanup()

    def _write(self, proj_dir, fname, text):
        mdir = proj_dir / "memory"
        mdir.mkdir(exist_ok=True)
        p = mdir / fname
        p.write_text(text, encoding="utf-8")
        return p

    def _actions(self, plans):
        return {p.name: p.action for p in plans}

    def _tomb(self, name, base_hash):
        return {("memory", name): tombstone.Tombstone(
            kind="memory", target=name, base_hash=base_hash, machine="m", time="t")}

    def _chash(self, path):
        return memory.content_hash(memory.load_memory(path))

    # ── 兩側皆在：identical / conflict-content（memory 無 ff）─────────────────

    def test_both_identical(self):
        self._write(self.ld, "a.md", _text(body="same"))
        self._write(self.hd, "a.md", _text(body="same"))
        a = self._actions(memory.plan_memory_pair(self.ld, self.hd, coverage_initialized=True))
        self.assertEqual(a["a.md"], "identical")

    def test_both_identical_cosmetic_reorder(self):
        # 鍵序/引號/尾 newline 差異 → 正規化後同 content_hash → identical（不誤報衝突）。
        self._write(self.ld, "a.md", "---\nname: x\ndescription: d\n---\nbody")
        self._write(self.hd, "a.md", '---\ndescription: "d"\nname: x\n---\nbody\n')
        a = self._actions(memory.plan_memory_pair(self.ld, self.hd, coverage_initialized=True))
        self.assertEqual(a["a.md"], "identical")

    def test_both_volatile_only_differ_identical(self):
        # 只差 originSessionId（易變 provenance）→ 剔除後同 → identical（避免兩台各記同事實被誤判衝突）。
        self._write(self.ld, "a.md", _text(body="b", origin="AAA"))
        self._write(self.hd, "a.md", _text(body="b", origin="BBB"))
        a = self._actions(memory.plan_memory_pair(self.ld, self.hd, coverage_initialized=True))
        self.assertEqual(a["a.md"], "identical")

    def test_both_body_differ_is_conflict(self):
        self._write(self.ld, "a.md", _text(body="local version"))
        self._write(self.hd, "a.md", _text(body="hub version"))
        a = self._actions(memory.plan_memory_pair(self.ld, self.hd, coverage_initialized=True))
        self.assertEqual(a["a.md"], "conflict-content")

    def test_both_frontmatter_differ_is_conflict(self):
        self._write(self.ld, "a.md", _text(desc="local desc"))
        self._write(self.hd, "a.md", _text(desc="hub desc"))
        a = self._actions(memory.plan_memory_pair(self.ld, self.hd, coverage_initialized=True))
        self.assertEqual(a["a.md"], "conflict-content")

    def test_both_one_damaged_blocked(self):
        self._write(self.ld, "a.md", _text(body="ok"))
        self._write(self.hd, "a.md", "")  # 0-byte → damaged
        a = self._actions(memory.plan_memory_pair(self.ld, self.hd, coverage_initialized=True))
        self.assertEqual(a["a.md"], "blocked-damaged-source")

    # ── 單邊存在：copy / blocked ────────────────────────────────────────────

    def test_single_side_copy_when_baselined(self):
        # 真新檔（不在對應 baseline）+ 有基線 → 雙向 copy。**各自不同 slug**——否則 Block 2b duty (a)
        # 會把「兩不同檔名同一 name」判成跨檔同名 conflict（見 test_cross_file_same_name_*）。
        self._write(self.ld, "new_local.md", _text(slug="local-fact"))
        self._write(self.hd, "new_hub.md", _text(slug="hub-fact"))
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True,
            has_baseline=True, has_local_baseline=True, known=set(), local_known=set()))
        self.assertEqual(a["new_local.md"], "copy-to-hub")
        self.assertEqual(a["new_hub.md"], "copy-to-local")

    def test_single_side_blocked_uninitialized(self):
        self._write(self.hd, "x.md", _text())
        a = self._actions(memory.plan_memory_pair(self.ld, self.hd, coverage_initialized=False))
        self.assertEqual(a["x.md"], "blocked-uninitialized")

    def test_single_side_blocked_unmapped(self):
        # 無對側綁定（local_dir=None）→ both=False → 不知落地 → blocked-unmapped。
        self._write(self.hd, "x.md", _text())
        a = self._actions(memory.plan_memory_pair(None, self.hd, coverage_initialized=True))
        self.assertEqual(a["x.md"], "blocked-unmapped")

    def test_single_side_blocked_no_baseline(self):
        self._write(self.hd, "x.md", _text())
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=False))
        self.assertEqual(a["x.md"], "blocked-no-baseline")

    def test_hub_single_side_blocked_no_local_baseline(self):
        # has_local_baseline=False（migration：有 known_memory、無 local_memory[pk]）→ present=hub fail-closed。
        self._write(self.hd, "x.md", _text())
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            local_known=None, has_local_baseline=False))
        self.assertEqual(a["x.md"], "blocked-no-local-baseline")

    def test_out_of_subset_frontmatter_still_copies(self):
        # frontmatter 超出子集（list 值）但檔身可解碼 → content_hash 走 raw fallback（非 None）→ 仍可複製。
        self._write(self.ld, "weird.md", _text(extra="tags: [a, b]"))
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True,
            has_baseline=True, known=set()))
        self.assertEqual(a["weird.md"], "copy-to-hub")

    def test_damaged_single_side_blocked(self):
        self._write(self.ld, "empty.md", "")  # 0-byte
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True, known=set()))
        self.assertEqual(a["empty.md"], "blocked-damaged-source")

    # ── 對稱刪除偵測（local / hub 兩向）─────────────────────────────────────

    def test_local_only_in_known_is_blocked_known_deleted(self):
        # local 有、hub 無、name∈known（hub baseline）→ hub 不該掉檔 → 不信任 → 交人。
        self._write(self.ld, "a.md", _text())
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True, known={"a.md"}))
        self.assertEqual(a["a.md"], "blocked-known-deleted")

    def test_hub_only_in_local_known_is_local_deleted(self):
        # hub 有、local 無、name∈local_known → 本機刪除 → local-deleted（apply 寫 hub tombstone）。
        self._write(self.hd, "a.md", _text())
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True, local_known={"a.md"}))
        self.assertEqual(a["a.md"], "local-deleted")

    def test_hub_only_not_in_local_known_copies(self):
        self._write(self.hd, "a.md", _text())
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, local_known={"other.md"}))
        self.assertEqual(a["a.md"], "copy-to-local")

    def test_migration_none_local_known_copies_not_deletes(self):
        # local_known=None（防禦）→ 不誤判 local-deleted → copy-to-local。
        self._write(self.hd, "a.md", _text())
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, local_known=None))
        self.assertEqual(a["a.md"], "copy-to-local")

    def test_bulk_local_disappearance_blocks(self):
        # local_known 5 個、local memory 全空（100% 消失）→ bulk guard → 全 blocked-bulk-local-deletion。
        names = {f"m{i}.md" for i in range(5)}
        for n in names:
            self._write(self.hd, n, _text(slug=n))
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True, local_known=names))
        self.assertTrue(all(v == "blocked-bulk-local-deletion" for v in a.values()), a)

    def test_single_local_deletion_below_bulk_threshold(self):
        # 5 known、4 仍在 local（只刪 1，20% < 60%）→ 非 bulk → 該檔 local-deleted、其餘 identical。
        names = {f"m{i}.md" for i in range(5)}
        for n in names:
            self._write(self.hd, n, _text(slug=n, body="same"))
        for n in ("m0.md", "m1.md", "m2.md", "m3.md"):  # m4.md 被刪
            self._write(self.ld, n, _text(slug=n, body="same"))
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True, local_known=names))
        self.assertEqual(a["m4.md"], "local-deleted")
        self.assertEqual(a["m0.md"], "identical")

    def test_local_deleted_requires_baseline(self):
        self._write(self.hd, "a.md", _text())
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=False, local_known={"a.md"}))
        self.assertEqual(a["a.md"], "blocked-no-baseline")

    # ── tombstone 閘（A17.1，正規化 content_hash base）─────────────────────

    def test_suppressed_when_unchanged(self):
        p = self._write(self.hd, "del.md", _text())
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, tombs=self._tomb("del.md", self._chash(p))))
        self.assertEqual(a["del.md"], "suppressed-deleted")

    def test_suppress_tolerates_cosmetic_reformat(self):
        # memory 專屬：tombstone base=正規化 content_hash → 現存側只是 cosmetic 重排仍 ==base → suppress
        #（session 用 raw bytes 會在此轉 conflict；memory 全程正規化故一致）。
        base = memory.content_hash(memory.load_memory_bytes(
            b"---\nname: x\ndescription: d\n---\nbody\n"))
        self._write(self.hd, "del.md", '---\ndescription: "d"\nname: x\n---\nbody')  # 重排+去尾 newline
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, tombs=self._tomb("del.md", base)))
        self.assertEqual(a["del.md"], "suppressed-deleted")

    def test_conflict_when_modified_after_delete(self):
        self._write(self.hd, "del.md", _text(body="changed"))
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, tombs=self._tomb("del.md", "0" * 64)))
        self.assertEqual(a["del.md"], "conflict-delete-vs-update")

    def test_tombstone_base_none_is_conflict(self):
        self._write(self.hd, "del.md", _text())
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, tombs=self._tomb("del.md", None)))
        self.assertEqual(a["del.md"], "conflict-delete-vs-update")

    def test_both_present_both_equal_base_suppressed(self):
        t = _text(body="same")
        lp = self._write(self.ld, "s.md", t)
        self._write(self.hd, "s.md", t)
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, tombs=self._tomb("s.md", self._chash(lp))))
        self.assertEqual(a["s.md"], "suppressed-deleted")

    def test_both_present_divergent_with_tombstone_is_conflict(self):
        lp = self._write(self.ld, "s.md", _text(body="local"))
        self._write(self.hd, "s.md", _text(body="hub"))
        # base = local 的 hash；hub != base → 並非兩側都 ==base → conflict（不復活、不丟更新）。
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, tombs=self._tomb("s.md", self._chash(lp))))
        self.assertEqual(a["s.md"], "conflict-delete-vs-update")

    def test_tombstone_gate_precedes_pairing(self):
        # 兩側都還在、無 tombstone 時會是 identical；有 tombstone（base==內容）則先被抑制（閘先於配對）。
        t = _text(body="same")
        lp = self._write(self.ld, "s.md", t)
        self._write(self.hd, "s.md", t)
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, tombs=self._tomb("s.md", self._chash(lp))))
        self.assertEqual(a["s.md"], "suppressed-deleted")

    def test_corrupt_tombstone_blocks(self):
        self._write(self.hd, "x.md", _text())
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, corrupt={("memory", "x.md")}))
        self.assertEqual(a["x.md"], "blocked-tombstone-corrupt")

    def test_corrupt_memory_tombstone_blocks_not_resurrects(self):
        # codex P1d-r1 整合：竄改的 memory tombstone（檔名 secret.md、內容 target=other.md）經 read/corrupt
        # 兩函式後，secret.md 不得復活 → blocked-tombstone-corrupt（無修復前會誤判 copy-to-local）。
        self._write(self.hd, "secret.md", _text())  # 單邊 hub 檔
        td = tombstone.tombstones_dir(self.hd)
        td.mkdir(parents=True, exist_ok=True)
        (td / "memory-secret.md.deleted.json").write_text(
            '{"kind":"memory","target":"other.md"}', encoding="utf-8")
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, local_known=set(),
            tombs=tombstone.read_tombstones(self.hd),
            corrupt=tombstone.corrupt_tombstone_targets(self.hd)))
        self.assertEqual(a["secret.md"], "blocked-tombstone-corrupt")

    # ── 跨 OS casefold 撞名 + 索引/dotfile 排除 ─────────────────────────────

    def test_casefold_collision_blocked(self):
        self._write(self.ld, "Foo.md", _text())
        self._write(self.hd, "foo.md", _text())
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set()))
        self.assertEqual(a["Foo.md"], "blocked-casefold-collision")
        self.assertEqual(a["foo.md"], "blocked-casefold-collision")

    def test_nfc_nfd_collision_blocked(self):
        # e2e-r1 Finding 2：同一檔名的 NFC 與 NFD 拼法（跨平台撰寫 memory 常見）須被判撞名——否則各自當獨立檔
        # 雙向 copy（norm-sensitive FS）或 norm-insensitive FS 上 aliased 覆蓋。memory 檔名配對按位元組精確 →
        # 唯 name_key（NFC∘casefold∘NFC）折疊能認出（原 raw casefold 漏，對稱既有 A.md/a.md case 撞名）。
        nfc, nfd = unicodedata.normalize("NFC", "café.md"), unicodedata.normalize("NFD", "café.md")
        self.assertNotEqual(nfc, nfd)
        self._write(self.ld, nfc, _text())
        self._write(self.hd, nfd, _text())
        # FS 須保留 NFC/NFD 區別（Windows NTFS / Linux ext4 保留；正規化 FS〔如 macOS APFS〕折疊 → 跳過整合斷言）。
        if nfc not in set(memory.list_memory_files(self.ld / "memory")) \
                or nfd not in set(memory.list_memory_files(self.hd / "memory")):
            self.skipTest("FS 正規化 unicode 檔名，NFC/NFD 區別未保留")
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set()))
        self.assertEqual(a[nfc], "blocked-casefold-collision")
        self.assertEqual(a[nfd], "blocked-casefold-collision")

    @_caps.needs_backslash_name
    def test_backslash_filename_blocked_unsupported(self):
        # codex P1d gate（High）：POSIX 可有含反斜線的檔名，但 _mem_file sanitize 不可逆 → write/read 不對稱
        # 會讓刪除標記落錯身分、真實檔復活 → 一律 blocked-unsupported-name（不複製、不寫 tombstone）。
        self._write(self.ld, "a\\b.md", _text(body="x"))
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True, known=set()))
        self.assertEqual(a["a\\b.md"], "blocked-unsupported-name")

    @_caps.needs_symlink
    def test_symlinked_memory_root_raises(self):
        # codex P1d gate（Medium）：memory/ 根目錄是 symlink → 不跟隨、raise UnsafeMemoryDir（不可當空夾，否則
        # 指向空/錯夾會看似 memory 全刪 → local-deleted/suppress）。
        real = self.tmp / "elsewhere"
        real.mkdir()
        (real / "planted.md").write_text(_text(), encoding="utf-8")
        (self.ld / "memory").symlink_to(real, target_is_directory=True)
        with self.assertRaises(memory.UnsafeMemoryDir):
            memory.plan_memory_pair(self.ld, self.hd, coverage_initialized=True,
                                    has_baseline=True, local_known={"planted.md"})

    def test_index_and_dotfiles_not_classified(self):
        # MEMORY.md（索引）與 dotfile 不是 memory → 不進 plan。
        self._write(self.hd, "MEMORY.md", "# index\n")
        self._write(self.hd, ".secret.md", _text())
        self._write(self.hd, "real.md", _text())
        names = {p.name for p in memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, local_known=set())}
        self.assertEqual(names, {"real.md"})

    # ── Block 2b duty (a)：跨檔同名 conflict（exact-frontmatter cross-file identity，A14/§7.2.3）──────

    def _tomb_id(self, target, base_hash, identity):
        # 記在 `target` 檔名、但帶 frontmatter `identity`（供換檔名復活偵測）。
        return {("memory", target): tombstone.Tombstone(
            kind="memory", target=target, base_hash=base_hash, machine="m", time="t", identity=identity)}

    def test_cross_file_same_name_both_copy_is_conflict(self):
        # 兩不同檔名、同 frontmatter name、各自單邊新檔 → 不可雙向 copy（會在兩側各製造重複事實）→ 交人。
        self._write(self.ld, "foo.md", _text(slug="dup", body="local"))
        self._write(self.hd, "bar.md", _text(slug="dup", body="hub"))
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set()))
        self.assertEqual(a["foo.md"], "conflict-cross-file-identity")
        self.assertEqual(a["bar.md"], "conflict-cross-file-identity")

    def test_cross_file_same_name_identical_content_still_conflict(self):
        # 內容**相同**但檔名不同 → 仍是「同一事實兩檔名」→ 需人挑檔名（自動雙向 copy 會兩側各留兩檔）。
        self._write(self.ld, "foo.md", _text(slug="dup", body="same"))
        self._write(self.hd, "bar.md", _text(slug="dup", body="same"))
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set()))
        self.assertEqual(a["foo.md"], "conflict-cross-file-identity")
        self.assertEqual(a["bar.md"], "conflict-cross-file-identity")

    def test_rename_split_does_not_auto_tombstone_old(self):
        # codex P1d 塊末 fresh gate high：local 把 old.md 改名成 new.md（同 name fact），hub 仍有 old.md，
        # local_known={old.md}。old.md **不可**走 local-deleted（會自動寫 identity=fact tombstone → new.md 之後被當
        # 該 identity 復活而 suppress → 改名被誤解成「刪除＋抑制」、靜默丟掉改名的事實）。整組 rename 須交人 →
        # 兩檔皆 conflict-cross-file-identity（不自動寫 tombstone）。
        self._write(self.ld, "new.md", _text(slug="fact", body="same"))
        self._write(self.hd, "old.md", _text(slug="fact", body="same"))
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known={"old.md"}, local_known={"old.md"}))
        self.assertEqual(a["old.md"], "conflict-cross-file-identity")
        self.assertEqual(a["new.md"], "conflict-cross-file-identity")

    def test_local_deleted_deferred_when_unreadable_local_file(self):
        # codex P1d gate5 high：old.md（hub、name fact、∈local_known）疑似改名成 new.md（**非 fm_ok → 身分 None**，
        # duty a 因身分 None 無從分組偵測）→ 不可自動把 old.md 當 local-deleted 寫 identity=fact tombstone（會把
        # 改名誤記成刪除 → 日後乾淨 new.md 命中該 identity 被抑制 → 靜默丟改名的事實）→ fail-closed。
        self._write(self.hd, "old.md", _text(slug="fact", body="same"))
        self._write(self.ld, "new.md", "---\nname: fact\ntags: [a, b]\n---\nsame\n")  # 非 fm_ok → 身分 None
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known={"old.md"}))
        self.assertEqual(a["old.md"], "blocked-tombstone-no-identity")

    def test_local_deleted_normal_when_all_local_fm_ok(self):
        # 對照（不過度阻擋）：local 現存檔全 fm_ok（無不可解析身分檔）→ local-deleted 照常寫 tombstone。
        self._write(self.hd, "old.md", _text(slug="fact"))
        self._write(self.ld, "other.md", _text(slug="other"))  # fm_ok、與刪除無關
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known={"old.md"}))
        self.assertEqual(a["old.md"], "local-deleted")

    def test_cross_file_same_name_same_side(self):
        # 同一側兩檔同 name（都在 local）→ 仍是分裂（與側別無關）→ 不可都 copy-to-hub → 交人。
        self._write(self.ld, "foo.md", _text(slug="dup", body="a"))
        self._write(self.ld, "bar.md", _text(slug="dup", body="b"))
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set()))
        self.assertEqual(a["foo.md"], "conflict-cross-file-identity")
        self.assertEqual(a["bar.md"], "conflict-cross-file-identity")

    def test_cross_file_split_upgrades_identical_and_copy(self):
        # 三檔同 name：foo.md 兩側 identical、bar.md 單邊 hub copy → 兩者皆在 split group → 一起升級交人
        #（否則 foo 顯 identical、bar 顯 copy 會讓使用者只見半截分裂）。
        self._write(self.ld, "foo.md", _text(slug="dup", body="x"))
        self._write(self.hd, "foo.md", _text(slug="dup", body="x"))   # 兩側相同 → 本會 identical
        self._write(self.hd, "bar.md", _text(slug="dup", body="y"))   # 單邊 hub → 本會 copy-to-local
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set()))
        self.assertEqual(a["foo.md"], "conflict-cross-file-identity")
        self.assertEqual(a["bar.md"], "conflict-cross-file-identity")

    def test_cross_file_does_not_override_blocks(self):
        # split group 內若某動作是 fail-closed（此處 no-baseline）→ 更具體、不被 duty (a) 蓋成 conflict。
        self._write(self.ld, "foo.md", _text(slug="dup"))
        self._write(self.hd, "bar.md", _text(slug="dup"))
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=False))
        self.assertEqual(a["foo.md"], "blocked-no-baseline")
        self.assertEqual(a["bar.md"], "blocked-no-baseline")

    def test_same_name_same_filename_not_split(self):
        # 同 name **同檔名**（正常兩側配對）→ 只有一個檔名 → 不是分裂 → 照常 identical（不誤升 conflict）。
        self._write(self.ld, "a.md", _text(slug="dup", body="same"))
        self._write(self.hd, "a.md", _text(slug="dup", body="same"))
        a = self._actions(memory.plan_memory_pair(self.ld, self.hd, coverage_initialized=True))
        self.assertEqual(a["a.md"], "identical")

    def test_out_of_subset_same_name_not_grouped_p2(self):
        # **範圍邊界（A14/A17.5）**：非子集 frontmatter（含 list 值）身分不可判（None）→ duty (a) 不分組 → 各按
        # 檔名 copy。跨檔同名偵測只對 fm_ok（可精確、完整解析、保證頂層 name 唯一）檔；非子集 frontmatter 的跨檔
        # 身分留 P2。**殘留＝可能重複、非資料 loss**；復活則仍由 tombstone 在場時的 blocked-tombstone-no-identity 守住
        #（見 test_out_of_subset_resurrection_blocked）。lenient 抽取無法保證頂層 name 唯一故不可信（codex gate4）。
        self._write(self.ld, "f1.md", _text(slug="dup", body="x", extra="tags: [a, b]"))
        self._write(self.hd, "f2.md", _text(slug="dup", body="y", extra="tags: [c, d]"))
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set()))
        self.assertEqual(a["f1.md"], "copy-to-hub")
        self.assertEqual(a["f2.md"], "copy-to-local")

    # ── Block 2b duty (b)：換檔名復活防護（identity 鍵 tombstone，A14/§7.2.3）─────────────────────

    def test_rename_resurrection_suppressed_when_identical(self):
        # 已刪事實（tombstone 記在 old.md、identity=dup、base=該內容 hash）以**新檔名** new.md 帶回、內容==base
        # → suppressed-deleted（尊重刪除、不復活），即使 new.md 自己無 tombstone。
        p = self._write(self.ld, "new.md", _text(slug="dup", body="b"))
        tombs = self._tomb_id("old.md", self._chash(p), "dup")
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set(), tombs=tombs))
        self.assertEqual(a["new.md"], "suppressed-deleted")

    def test_rename_resurrection_conflict_when_edited(self):
        # 換檔名**且**改內容（≠base）→ conflict-delete-vs-update（不復活也不丟更新，交人）。
        self._write(self.ld, "new.md", _text(slug="dup", body="edited-after-delete"))
        tombs = self._tomb_id("old.md", "0" * 64, "dup")
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set(), tombs=tombs))
        self.assertEqual(a["new.md"], "conflict-delete-vs-update")

    def test_two_sided_rename_resurrection_suppressed_when_base(self):
        # codex P1d-r1 critical：兩側都有 new.md（name=已刪 dup、內容==base）→ 換檔名復活 must 在**配對前**被攔，
        # 不可當 identical（否則已刪事實在兩側存活、工具還報 in-sync）。內容==base → suppressed-deleted。
        t = _text(slug="dup", body="same")
        lp = self._write(self.ld, "new.md", t)
        self._write(self.hd, "new.md", t)
        tombs = self._tomb_id("old.md", self._chash(lp), "dup")
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set(), tombs=tombs))
        self.assertEqual(a["new.md"], "suppressed-deleted")

    def test_two_sided_rename_resurrection_conflict_when_changed(self):
        # 兩側 new.md（name dup）但內容≠base（刪後又編輯）→ conflict-delete-vs-update（不復活也不丟更新）。
        t = _text(slug="dup", body="edited")
        self._write(self.ld, "new.md", t)
        self._write(self.hd, "new.md", t)
        tombs = self._tomb_id("old.md", "0" * 64, "dup")
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set(), tombs=tombs))
        self.assertEqual(a["new.md"], "conflict-delete-vs-update")

    def test_multiple_same_identity_tombstones_fail_closed_conflict(self):
        # codex P1d-r1 high：同一 identity 撞多個別檔名 tombstone（base 不一、多次換檔名刪除）→ 不臆測哪次 →
        # fail-closed conflict（即使現存內容剛好 ==其中一個 base 也不靜默 suppress）。
        p = self._write(self.ld, "new.md", _text(slug="dup", body="b"))
        tombs = self._tomb_id("old-a.md", self._chash(p), "dup")   # 其一 base==現存內容
        tombs.update(self._tomb_id("old-b.md", "0" * 64, "dup"))   # 另一 base 不同
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set(), tombs=tombs))
        self.assertEqual(a["new.md"], "conflict-delete-vs-update")

    def test_rename_resurrection_duty_b_precedes_duty_a(self):
        # 復活閘比跨檔同名 conflict 更具體：兩單邊新檔同 name、且 name 命中別檔名 tombstone identity
        # → 各走 duty (b)（suppress/conflict-delete），**不**被 duty (a) 蓋成 conflict-cross-file-identity。
        lp = self._write(self.ld, "new1.md", _text(slug="dup", body="b"))     # ==base → suppress
        self._write(self.hd, "new2.md", _text(slug="dup", body="changed"))    # ≠base → conflict-delete
        tombs = self._tomb_id("old.md", self._chash(lp), "dup")
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set(), tombs=tombs))
        self.assertEqual(a["new1.md"], "suppressed-deleted")
        self.assertEqual(a["new2.md"], "conflict-delete-vs-update")

    def test_filename_tombstone_gate_precedes_identity_gate(self):
        # 同檔名既有 tombstone（檔名鍵）→ 走檔名鍵閘（identity 鍵閘要求 mt.target != name，不重複處理）。
        p = self._write(self.hd, "same.md", _text(slug="dup", body="b"))
        tombs = self._tomb_id("same.md", self._chash(p), "dup")  # target==檔名
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set(), tombs=tombs))
        self.assertEqual(a["same.md"], "suppressed-deleted")

    def test_undecidable_identity_tombstone_blocks_copy(self):
        # codex P1d-r1 high：identity=None 的合法 memory tombstone → 無法判斷已刪的是哪個事實 → 無法排除任一
        # would-copy 檔是其換檔名復活 → 該專案 would-copy 一律 fail-closed blocked-tombstone-no-identity
        #（不再如舊版誤判 copy → 復活；Block 3 寫 memory tombstone 必帶 identity 才解此閘）。
        self._write(self.ld, "new.md", _text(slug="dup", body="b"))
        tombs = self._tomb_id("old.md", "0" * 64, None)  # 合法但 identity=None
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set(), tombs=tombs))
        self.assertEqual(a["new.md"], "blocked-tombstone-no-identity")

    def test_source_identity_undecidable_blocks_copy(self):
        # codex P1d-r3 high：復活檔自己 **name 行**讀不出（這裡 list 值 → identity None）+ 專案有帶 identity 的
        # memory tombstone → 無法比對它是否某已刪 identity 復活 → fail-closed（不能因讀不出名字就放行 copy → 復活）。
        self._write(self.ld, "new.md", "---\nname: [a, b]\ndescription: d\n---\nbody\n")  # name 行壞 → identity None
        tombs = self._tomb_id("old.md", "0" * 64, "dup")  # decidable identity tombstone
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set(), tombs=tombs))
        self.assertEqual(a["new.md"], "blocked-tombstone-no-identity")

    def test_out_of_subset_resurrection_blocked(self):
        # 復活檔其它欄出子集 → 整段非 fm_ok → 身分 None；專案有 decidable memory tombstone → 無法精確比對是否其
        # 復活 → fail-closed blocked-tombstone-no-identity（**資料安全：復活仍被擋**，只是不精確 suppress；非子集
        # frontmatter 的精確身分比對留 P2，codex gate4）。
        self._write(self.ld, "new.md", "---\nname: dup\ntags: [a, b]\n---\nbody\n")
        tombs = self._tomb_id("old.md", "0" * 64, "dup")
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set(), tombs=tombs))
        self.assertEqual(a["new.md"], "blocked-tombstone-no-identity")

    def test_malformed_tombstone_identity_is_undecidable(self):
        # codex P1d gate2 high：tombstone identity 非 slug 形（"fact " 帶尾空白）→ 不可信為 decidable（乾淨 name 的
        # 復活檔不會匹配它 → 漏擋）→ 當 undecidable → would-copy fail-closed。
        self._write(self.ld, "new.md", _text(slug="fact", body="b"))
        tombs = self._tomb_id("old.md", "0" * 64, "fact ")  # 尾空白 → 非 slug
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set(), tombs=tombs))
        self.assertEqual(a["new.md"], "blocked-tombstone-no-identity")

    def test_tombstone_identity_trailing_newline_is_undecidable(self):
        # codex P1d gate3 high：identity 帶尾隨 `\n`（`re.$` 會誤放行成 slug）→ 須用 fullmatch 判非 slug → undecidable
        # → would-copy fail-closed（否則乾淨 name 的復活檔 != "fact\n" → 漏配對 → 復活）。
        self._write(self.ld, "new.md", _text(slug="fact", body="b"))
        tombs = self._tomb_id("old.md", "0" * 64, "fact\n")
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set(), tombs=tombs))
        self.assertEqual(a["new.md"], "blocked-tombstone-no-identity")

    def test_source_identity_undecidable_blocks_two_sided(self):
        # 同 blocks_copy 但兩側都有 → 配對前 gate 也須擋（不可當 identical/conflict-content）。name 行壞 → identity None。
        t = "---\nname: [a, b]\ndescription: d\n---\nbody\n"
        self._write(self.ld, "new.md", t)
        self._write(self.hd, "new.md", t)
        tombs = self._tomb_id("old.md", "0" * 64, "dup")
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set(), tombs=tombs))
        self.assertEqual(a["new.md"], "blocked-tombstone-no-identity")

    def test_undecidable_identity_blocks_two_sided_pair(self):
        # codex P1d-r2 high：兩側都有 new.md + 專案有 identity=None memory tombstone → 不可當 identical 報 in-sync
        #（可能正是其換檔名復活）→ fail-closed blocked-tombstone-no-identity（對稱單邊 would-copy）。
        t = _text(slug="dup", body="same")
        self._write(self.ld, "new.md", t)
        self._write(self.hd, "new.md", t)
        tombs = self._tomb_id("old.md", "0" * 64, None)  # 合法但 identity=None
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set(), tombs=tombs))
        self.assertEqual(a["new.md"], "blocked-tombstone-no-identity")

    def test_undecidable_identity_two_sided_damaged_still_damaged(self):
        # damaged 比 undecidable 更具體：兩側配對但一側壞 → 仍 blocked-damaged-source（不被 undecidable 蓋掉）。
        self._write(self.ld, "new.md", _text(slug="dup"))
        self._write(self.hd, "new.md", "")  # 0-byte → damaged
        tombs = self._tomb_id("old.md", "0" * 64, None)
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set(), tombs=tombs))
        self.assertEqual(a["new.md"], "blocked-damaged-source")

    def test_undecidable_does_not_preempt_local_deleted(self):
        # undecidable 閘**不搶**刪除/更具體路徑：hub 有、local 無、name∈local_known（本機刪除）→ 仍 local-deleted，
        # 不被 blocked-tombstone-no-identity 蓋掉（不相關的 undecidable tombstone 不該擋掉正當刪除傳播）。
        self._write(self.hd, "gone.md", _text(slug="gone"))
        tombs = self._tomb_id("old.md", "0" * 64, None)  # 不相關的 undecidable tombstone
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known={"gone.md"}, tombs=tombs))
        self.assertEqual(a["gone.md"], "local-deleted")

    def test_corrupt_memory_tombstone_blocks_unrelated_copy(self):
        # codex P1d-r1 medium：corrupt memory tombstone（identity 讀不出）→ 無法排除別的 would-copy 檔是其換檔名
        # 復活 → 該專案 would-copy 一律 blocked-tombstone-no-identity（不只擋 corrupt 自己的檔名）。
        self._write(self.hd, "fresh.md", _text(slug="brand-new"))  # 與 corrupt 無關的新檔
        td = tombstone.tombstones_dir(self.hd)
        td.mkdir(parents=True, exist_ok=True)
        (td / "memory-old.md.deleted.json").write_text(
            '{"kind":"memory","target":"WRONG.md"}', encoding="utf-8")  # target≠檔名身分 → corrupt
        a = self._actions(memory.plan_memory_pair(
            self.ld, self.hd, coverage_initialized=True, has_baseline=True,
            has_local_baseline=True, known=set(), local_known=set(),
            tombs=tombstone.read_tombstones(self.hd),
            corrupt=tombstone.corrupt_tombstone_targets(self.hd)))
        self.assertEqual(a["fresh.md"], "blocked-tombstone-no-identity")


if __name__ == "__main__":
    unittest.main()
