import json
import tempfile
import unittest
from pathlib import Path

from claude_session_sync import state as st


class TestState(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_missing_is_none(self):
        self.assertIsNone(st.load_or_none(self.tmp / "nope.json"))

    def test_roundtrip(self):
        s = st.State(
            hub_fingerprint="hubfp",
            known_sessions={"projA": {"s1", "s2"}},
            known_memory={"projA": {"a.md", "b.md"}},
            bindings={"/home/x/proj": "projA"},
            local_dir_bindings={"-home-x-proj": "projA"},
        )
        p = self.tmp / "state.json"
        st.save(s, p)
        back = st.load_or_none(p)
        self.assertIsNotNone(back)
        self.assertEqual(back.hub_fingerprint, "hubfp")
        self.assertEqual(back.known_sessions, {"projA": {"s1", "s2"}})
        self.assertEqual(back.known_memory, {"projA": {"a.md", "b.md"}})
        self.assertEqual(back.bindings, {"/home/x/proj": "projA"})
        self.assertEqual(back.local_dir_bindings, {"-home-x-proj": "projA"})

    def test_local_sessions_roundtrip(self):
        s = st.State(known_sessions={"projA": {"s1"}}, local_sessions={"projA": {"s1", "s2"}})
        p = self.tmp / "state.json"
        st.save(s, p)
        back = st.load_or_none(p)
        self.assertEqual(back.local_sessions, {"projA": {"s1", "s2"}})

    def test_local_sessions_clean_migration(self):
        # 舊 state（無 local_sessions 欄位）載入 → 空 dict，不報錯（clean migration）。
        p = self.tmp / "state.json"
        payload = {"schema_version": 1, "hub_fingerprint": None,
                   "known_sessions": {"projA": ["s1"]}, "known_memory": {}, "bindings": {}}
        doc = {**payload, "_checksum": st._checksum(payload)}
        p.write_text(json.dumps(doc), encoding="utf-8")
        back = st.load_or_none(p)
        self.assertEqual(back.local_sessions, {})
        self.assertEqual(back.known_sessions, {"projA": {"s1"}})

    def test_asserted_dirs_roundtrip(self):
        s = st.State(local_dir_bindings={"projM": "encM"}, asserted_dirs={"projM"})
        p = self.tmp / "state.json"
        st.save(s, p)
        back = st.load_or_none(p)
        self.assertEqual(back.asserted_dirs, {"projM"})

    def test_asserted_dirs_clean_migration(self):
        # 舊 state（無 asserted_dirs 欄位）載入 → 空集（fail-closed：multi-cwd 夾維持阻擋，重跑 bootstrap --map 才斷言）。
        p = self.tmp / "state.json"
        payload = {"schema_version": 1, "hub_fingerprint": None,
                   "known_sessions": {}, "known_memory": {}, "bindings": {},
                   "local_dir_bindings": {"projM": "encM"}}
        doc = {**payload, "_checksum": st._checksum(payload)}
        p.write_text(json.dumps(doc), encoding="utf-8")
        back = st.load_or_none(p)
        self.assertEqual(back.asserted_dirs, set())
        self.assertEqual(back.local_dir_bindings, {"projM": "encM"})

    def test_reconcile_local_presence_merges_pending(self):
        # 新 baseline = present ∪ pending(舊 baseline 中已不在 present 且未 tombstone 者)；不動 known。
        p = self.tmp / "state.json"
        st.commit_session("projA", "s1", p)                              # known_sessions[projA]={s1}
        st.reconcile_local_presence("projA", {"s1", "s2"}, set(), p)     # prev={} → {s1,s2}
        self.assertEqual(st.load_or_none(p).local_sessions["projA"], {"s1", "s2"})
        st.reconcile_local_presence("projA", {"s1"}, set(), p)           # s2 刪、無 tomb → pending 保留 s2
        self.assertEqual(st.load_or_none(p).local_sessions["projA"], {"s1", "s2"})
        st.reconcile_local_presence("projA", {"s1"}, {"s2"}, p)          # s2 已 tombstone → 移除
        back = st.load_or_none(p)
        self.assertEqual(back.local_sessions["projA"], {"s1"})
        self.assertEqual(back.known_sessions["projA"], {"s1"})           # known 不被動到

    def test_reconcile_pending_from_disk_baseline_not_caller(self):
        # codex r24-4：pending 由**鎖內 disk baseline** 算 → 並發保留的未落地刪除不被本次 stale 視角抹掉。
        p = self.tmp / "state.json"
        st.reconcile_local_presence("projA", {"a", "b"}, set(), p)       # disk baseline = {a,b}
        st.reconcile_local_presence("projA", {"a"}, set(), p)            # b 刪、無 tomb → 由 disk prev 取 → 留 b
        self.assertEqual(st.load_or_none(p).local_sessions["projA"], {"a", "b"})

    def test_reconcile_does_not_clobber_concurrent(self):
        p = self.tmp / "state.json"
        st.commit_session("projB", "x", p)
        st.reconcile_local_presence("projA", {"a"}, set(), p)
        back = st.load_or_none(p)
        self.assertEqual(back.local_sessions["projA"], {"a"})
        self.assertEqual(back.known_sessions["projB"], {"x"})

    def test_reconcile_require_baseline_skips_when_pk_absent(self):
        # e2e Pass2 Medium：require_baseline=True 時，鎖內最新 state 無此 pk 的 local 基線（並發 doctor
        # --rebuild-state 移除 / migration）→ **不重建**（否則 hub-only 下次當 copy-to-local 復活）。
        p = self.tmp / "state.json"
        st.commit_session("projA", "s1", p)   # 只有 known_sessions[projA]，無 local_sessions[projA]
        st.reconcile_local_presence("projA", {"s1", "s2"}, set(), p, require_baseline=True)
        self.assertNotIn("projA", st.load_or_none(p).local_sessions)   # 未重建基線（fail-closed）
        st.reconcile_local_memory_presence("projA", {"m.md"}, set(), p, require_baseline=True)
        self.assertNotIn("projA", st.load_or_none(p).local_memory)     # memory 版對稱
        # 對照：pk 已有基線 → require_baseline 照常更新；預設（False）仍 create（既有契約不變）。
        st.reconcile_local_presence("projB", {"a"}, set(), p)                       # 先建 projB（default create）
        st.reconcile_local_presence("projB", {"a", "b"}, set(), p, require_baseline=True)
        self.assertEqual(st.load_or_none(p).local_sessions["projB"], {"a", "b"})

    def test_corrupt_checksum_raises(self):
        p = self.tmp / "state.json"
        st.save(st.State(hub_fingerprint="x"), p)
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["hub_fingerprint"] = "tampered"  # 動內容但不更新 checksum
        p.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(st.StateCorruptError):
            st.load_or_none(p)

    def test_invalid_json_raises(self):
        p = self.tmp / "state.json"
        p.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(st.StateCorruptError):
            st.load_or_none(p)

    def test_missing_checksum_raises(self):
        p = self.tmp / "state.json"
        p.write_text(json.dumps({"hub_fingerprint": "x"}), encoding="utf-8")
        with self.assertRaises(st.StateCorruptError):
            st.load_or_none(p)

    def test_default_path(self):
        p = st.default_state_path()
        self.assertIn("claude-session-sync", str(p))
        self.assertTrue(str(p).endswith("state.json"))

    # ── per-session CAS（加鎖 RMW）──────────────────────────────────────────

    def test_commit_session_persists(self):
        p = self.tmp / "state.json"
        st.commit_session("projA", "s1", p, cwd="/home/x/projA", hub_fingerprint="fp1")
        back = st.load_or_none(p)
        self.assertEqual(back.known_sessions, {"projA": {"s1"}})
        self.assertEqual(back.bindings, {"/home/x/projA": "projA"})
        self.assertEqual(back.hub_fingerprint, "fp1")

    def test_commit_session_merges_not_clobbers(self):
        # 第二次 commit 必須在持鎖時重讀最新 → 累積，不可丟掉前一次（CAS 核心）。
        p = self.tmp / "state.json"
        st.commit_session("projA", "a", p)
        st.commit_session("projA", "b", p)        # 用過期空 State 為基底也不該丟 a
        st.commit_session("projB", "c", p)
        back = st.load_or_none(p)
        self.assertEqual(back.known_sessions, {"projA": {"a", "b"}, "projB": {"c"}})

    def test_commit_does_not_clobber_concurrent_write(self):
        # 模擬：A 取得 state（空）→ B 在 A 提交前已寫入 x → A 提交 y。RMW 重讀 → x、y 都在。
        p = self.tmp / "state.json"
        st.commit_session("projA", "x", p)                 # B 先寫
        st.update_under_lock(lambda s: s.known_sessions.setdefault("projA", set()).add("y"), p)
        back = st.load_or_none(p)
        self.assertEqual(back.known_sessions["projA"], {"x", "y"})

    def test_commit_blocks_on_held_lock(self):
        from claude_session_sync import atomicio as aio
        p = self.tmp / "state.json"
        held = aio.FileLock(p).acquire()
        try:
            with self.assertRaises(aio.LockError):
                st.commit_session("projA", "s1", p, lock_timeout_s=0.2)
        finally:
            held.release()

    def test_commit_propagates_corrupt_state(self):
        # 壞 state 不可被 commit 靜默覆蓋成「只有這次 delta」→ 必須拋。
        p = self.tmp / "state.json"
        p.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(st.StateCorruptError):
            st.commit_session("projA", "s1", p)


if __name__ == "__main__":
    unittest.main()
