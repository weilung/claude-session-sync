import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_session_sync import atomicio, doctor, state as state_mod, tombstone
from tests import _caps, fixtures as fx


def _dead_pid() -> int:
    """剛結束的子進程 pid（保留 handle 防 Windows PID 重用）→ 判已死。見 `_caps.dead_pid`。"""
    return _caps.dead_pid()


class TestDoctor(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.local = self.tmp / "local"
        self.hub = self.tmp / "hub"
        self.lA = self.local / "projA"
        self.hA = self.hub / "projA"
        self.lA.mkdir(parents=True)
        self.hA.mkdir(parents=True)
        self.state_path = self.tmp / "state.json"

    def tearDown(self):
        self._td.cleanup()

    def _w(self, path, objs):
        fx.write_jsonl(objs, str(path))

    def _lock(self, path, *, pid, host):
        path.write_text(json.dumps({"pid": pid, "host": host, "time": "t", "token": "x"}), encoding="utf-8")

    # ── 鎖分類 / break-lock ──────────────────────────────────────────────────

    def test_find_locks_classifies(self):
        # held/foreign/unparseable 在所有 OS 都成立 → 不 skip（dead-PID 的 stale 另測，見下，codex R1 #3）。
        me = atomicio._local_host()
        self._lock(self.hA / "a.jsonl.lock", pid=os.getpid(), host=me)   # 自己存活
        self._lock(self.hA / "c.jsonl.lock", pid=1, host="other-host")                 # 他機
        (self.hA / "d.jsonl.lock").write_text("{garbage", encoding="utf-8")            # 壞
        status = {p.path.name: p.status for p in doctor.find_locks([self.hub])}
        self.assertEqual(status["a.jsonl.lock"], "held")
        self.assertEqual(status["c.jsonl.lock"], "foreign")
        self.assertEqual(status["d.jsonl.lock"], "unparseable")

    def test_invalid_utf8_lock_unparseable_kept(self):
        # 非 UTF-8 的 malformed .lock（read_text 會拋 UnicodeDecodeError）→ unparseable（不 crash find_locks），
        # break-lock 一律保留不刪（fail-closed，codex breaklock-g3 Medium）。
        lp = self.hA / "u.jsonl.lock"
        lp.write_bytes(b"\xff\xfe\x00 not-utf8")
        status = {p.path.name: p.status for p in doctor.find_locks([self.hub])}
        self.assertEqual(status["u.jsonl.lock"], "unparseable")
        rep = doctor.break_locks([self.hub], apply=True)
        self.assertTrue(lp.exists())
        self.assertNotIn(str(lp), rep.removed)

    def test_lock_display_printable_with_surrogate_host(self):
        # JSON-valid 但 host 含 lone surrogate 的 malformed 鎖：find_locks 不 crash、break_locks/diagnose 的輸出行
        # 皆可 UTF-8 編碼（doctor 不因 malformed 鎖內容崩，codex breaklock-g4）。host≠本機 → 非 stale → 保留。
        lp = self.hA / "sg.jsonl.lock"
        lp.write_text(json.dumps({"pid": 1, "host": "\ud800", "time": "t", "token": "x"}), encoding="utf-8")
        rep = doctor.break_locks([self.hub], apply=True)
        "\n".join(rep.lines).encode("utf-8")           # 不得拋 UnicodeEncodeError
        self.assertTrue(lp.exists())                    # host≠本機 → 非 stale → 保留
        self.assertNotIn(str(lp), rep.removed)
        doctor.diagnose(self.local, self.hub, self.state_path).text().encode("utf-8")   # diagnose 也不崩

    @_caps.needs_dead_pid_detection
    def test_find_locks_classifies_stale(self):
        # 同機已死 PID → stale。需 dead-PID 偵測能力（POSIX os.kill／Windows ctypes；環境不可判時才 skip）。
        me = atomicio._local_host()
        self._lock(self.hA / "b.jsonl.lock", pid=_dead_pid(), host=me)
        status = {p.path.name: p.status for p in doctor.find_locks([self.hub])}
        self.assertEqual(status["b.jsonl.lock"], "stale")

    @_caps.needs_dead_pid_detection
    def test_break_lock_preview_then_remove_stale(self):
        me = atomicio._local_host()
        lp = self.hA / "s1.jsonl.lock"
        self._lock(lp, pid=_dead_pid(), host=me)
        doctor.break_locks([self.hub], apply=False)   # preview
        self.assertTrue(lp.exists())                   # 預覽不刪
        rep = doctor.break_locks([self.hub], apply=True)
        self.assertFalse(lp.exists())                  # 同機已死 → 移除
        self.assertIn(str(lp), rep.removed)

    def test_break_lock_keeps_foreign_and_alive(self):
        me = atomicio._local_host()
        foreign = self.hA / "f.jsonl.lock"
        alive = self.hA / "a.jsonl.lock"
        self._lock(foreign, pid=1, host="other-host")
        self._lock(alive, pid=os.getpid(), host=me)
        doctor.break_locks([self.hub], apply=True)
        self.assertTrue(foreign.exists())              # 他機不動（無法判存活）
        self.assertTrue(alive.exists())                # 存活持有者不動

    def test_break_lock_skips_when_token_differs_from_listing(self):
        # check→unlink race（codex breaklock-r1 High）：列出時看到 stale 鎖（token=OLD），unlink 前該路徑已被
        # writer 重取成另一把（磁碟現值 token≠OLD）→ 不可刪（否則誤刪活鎖＝雙 writer）。用「列出 token 與磁碟
        # 現值不符」模擬那道窗；_pid_alive→False 讓現值仍判 stale，以隔離出 token 身分核對這一關。
        me = atomicio._local_host()
        lp = self.hA / "r.jsonl.lock"
        self._lock(lp, pid=4242, host=me)                              # 磁碟：token="x"
        listed = doctor.LockEntry(lp, "stale", me, 4242, token="OLD")  # 列出時看到的是另一把（token 不同）
        with mock.patch.object(atomicio, "_pid_alive", return_value=False), \
             mock.patch.object(doctor, "find_locks", return_value=[listed]):
            rep = doctor.break_locks([self.hub], apply=True)
        self.assertTrue(lp.exists())                                   # token 不符 → 不刪（保住可能的活鎖）
        self.assertNotIn(str(lp), rep.removed)

    @_caps.needs_junction
    def test_find_locks_skips_junction_escape(self):
        # e2e gate G-Medium #2：hub 專案夾是 junction → 界外；rglob 會遞迴進去列到界外鎖，find_locks 用
        # `_within_root` 過濾掉（否則 break-lock 可能 unlink 到信任根外）。
        outside = self.tmp / "outside"
        outside.mkdir()
        self._lock(outside / "x.jsonl.lock", pid=1, host="other-host")   # 界外鎖
        _caps.make_junction(self.hub / "projJ", outside)                  # hub/projJ junction → outside
        paths = {p.path for p in doctor.find_locks([self.hub])}
        self.assertNotIn(self.hub / "projJ" / "x.jsonl.lock", paths)     # 界外鎖未列入（不會被 break-lock 觸及）
        doctor.break_locks([self.hub], apply=True)
        self.assertTrue((outside / "x.jsonl.lock").exists())             # 界外鎖仍在

    # ── rebuild-state ────────────────────────────────────────────────────────

    def test_rebuild_hub_side_only(self):
        self._w(self.hA / "s1.jsonl", fx.linear())
        self._w(self.hA / "s2.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        res = doctor.rebuild_state(self.local, self.hub)
        self.assertEqual(res.state.known_sessions["projA"], {"s1", "s2"})
        self.assertTrue(res.state.hub_fingerprint)
        self.assertEqual(res.state.local_sessions, {})   # 無 --map → 無 local 基線（fail-closed）

    def test_rebuild_excludes_tombstoned(self):
        self._w(self.hA / "s1.jsonl", fx.linear())
        self._w(self.hA / "s2.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        tombstone.write_session_tombstone(self.hA, "s2", base_hash="h")
        res = doctor.rebuild_state(self.local, self.hub)
        self.assertEqual(res.state.known_sessions["projA"], {"s1"})   # s2 已 tombstone → 不入基線

    def test_rebuild_skips_uninitialized(self):
        self._w(self.hA / "s1.jsonl", fx.linear())   # 無 coverage
        res = doctor.rebuild_state(self.local, self.hub)
        self.assertNotIn("projA", res.state.known_sessions)

    def test_rebuild_local_via_map(self):
        self._w(self.hA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        self._w(self.lA / "s1.jsonl", fx.linear())
        res = doctor.rebuild_state(self.local, self.hub, mappings={"projA": "projA"})
        self.assertEqual(res.state.local_sessions["projA"], {"s1"})
        self.assertEqual(res.state.local_dir_bindings["projA"], "projA")

    def test_rebuild_local_excludes_hub_tombstoned(self):
        # codex r-doctor-2：local 基線要扣掉 **hub** 的 tombstone（local 夾本身無 tombstone）。
        self._w(self.hA / "s1.jsonl", fx.linear())
        self._w(self.hA / "s2.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        tombstone.write_session_tombstone(self.hA, "s2", base_hash="h")
        self._w(self.lA / "s1.jsonl", fx.linear())
        self._w(self.lA / "s2.jsonl", fx.linear())   # local 也有 s2，但 hub 已 tombstone
        res = doctor.rebuild_state(self.local, self.hub, mappings={"projA": "projA"})
        self.assertEqual(res.state.local_sessions["projA"], {"s1"})   # s2 由 hub tombstone 扣除

    def test_rebuild_rejects_unsafe_map_name(self):
        # codex r-doctor-3：--map local 名是 ../ 逃逸 → 不建 local 基線（不 baseline root 外的夾）。
        self._w(self.hA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        res = doctor.rebuild_state(self.local, self.hub, mappings={"../escape": "projA"})
        self.assertEqual(res.state.local_sessions, {})

    def test_rebuild_rejects_dup_hub_target(self):
        # 多個 local 對到同一 hub 夾 → 全數略過（避免互覆）。
        self._w(self.hA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        (self.local / "projB").mkdir()
        res = doctor.rebuild_state(self.local, self.hub,
                                   mappings={"projA": "projA", "projB": "projA"})
        self.assertNotIn("projA", res.state.local_sessions)

    def test_rebuild_never_touches_tombstones(self):
        self._w(self.hA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        tombstone.write_session_tombstone(self.hA, "gone", base_hash="h")
        before = sorted(p.name for p in tombstone.tombstones_dir(self.hA).iterdir())
        doctor.rebuild_state(self.local, self.hub, mappings={"projA": "projA"})
        after = sorted(p.name for p in tombstone.tombstones_dir(self.hA).iterdir())
        self.assertEqual(before, after)

    def test_rebuild_fatal_on_missing_or_nondir_hub(self):
        # codex r-doctor-1：hub 不存在/非目錄 → fatal、不寫（否則覆成空 state）。
        res = doctor.rebuild_state(self.local, self.tmp / "nohub")
        self.assertTrue(res.fatal)
        self.assertEqual(res.state.known_sessions, {})
        f = self.tmp / "hubfile"
        f.write_text("x", encoding="utf-8")
        self.assertTrue(doctor.rebuild_state(self.local, f).fatal)   # 非目錄不 crash

    @_caps.needs_symlink
    def test_rebuild_excludes_symlink_hub_dir(self):
        # codex r-doctor-2：symlink 專案夾不從 root 外建基線。
        outside = self.tmp / "outside"
        outside.mkdir()
        self._w(outside / "s1.jsonl", fx.linear())
        tombstone.write_coverage(outside)
        (self.hub / "evilproj").symlink_to(outside, target_is_directory=True)
        res = doctor.rebuild_state(self.local, self.hub)
        self.assertNotIn("evilproj", res.state.known_sessions)

    @_caps.needs_symlink
    def test_rebuild_excludes_symlink_local_dir(self):
        self._w(self.hA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        outside = self.tmp / "loutside"
        outside.mkdir()
        self._w(outside / "s1.jsonl", fx.linear())
        (self.local / "evil").symlink_to(outside, target_is_directory=True)
        res = doctor.rebuild_state(self.local, self.hub, mappings={"evil": "projA"})
        self.assertEqual(res.state.local_sessions, {})   # symlink local 夾 → 不建基線

    @_caps.needs_dead_pid_detection
    def test_break_lock_scopes_to_hub_and_explicit_state_lock(self):
        # codex r-doctor-3：只動 hub（遞迴）+ 明確 state 鎖；state 父夾下無關 *.lock 不碰。
        me = atomicio._local_host()
        unrelated = self.tmp / "unrelated.lock"
        self._lock(unrelated, pid=_dead_pid(), host=me)
        state_lock = Path(str(self.state_path) + ".lock")
        self._lock(state_lock, pid=_dead_pid(), host=me)
        doctor.break_locks([self.hub], [state_lock], apply=True)
        self.assertTrue(unrelated.exists())    # 無關鎖不動
        self.assertFalse(state_lock.exists())  # 明確 state 鎖移除

    @_caps.needs_dead_pid_detection
    def test_break_lock_unlink_error_tracked(self):
        # codex r-doctor-4：移除失敗須記 errors（呼叫端據此非零退出）。
        me = atomicio._local_host()
        lp = self.hA / "s1.jsonl.lock"
        self._lock(lp, pid=_dead_pid(), host=me)
        with mock.patch.object(doctor.os, "unlink", side_effect=OSError("read-only")):
            rep = doctor.break_locks([self.hub], apply=True)
        self.assertIn(str(lp), rep.errors)
        self.assertTrue(lp.exists())

    def test_write_rebuilt_state_recovers_corrupt(self):
        self.state_path.write_text("{ corrupt json", encoding="utf-8")
        with self.assertRaises(state_mod.StateCorruptError):   # 壞到無法載入
            state_mod.load_or_none(self.state_path)
        self._w(self.hA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        res = doctor.rebuild_state(self.local, self.hub)
        doctor.write_rebuilt_state(res, self.state_path)
        st = state_mod.load_or_none(self.state_path)            # 現在載得回
        self.assertEqual(st.known_sessions["projA"], {"s1"})

    # ── 診斷 ─────────────────────────────────────────────────────────────────

    def test_diagnose_smoke(self):
        self._w(self.hA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        text = doctor.diagnose(self.local, self.hub, self.state_path).text()
        self.assertIn("hub 專案", text)
        self.assertIn("projA", text)

    def test_diagnose_flags_corrupt_state(self):
        self.state_path.write_text("{ corrupt", encoding="utf-8")
        rep = doctor.diagnose(self.local, self.hub, self.state_path)
        self.assertIn("損壞", rep.text())
        self.assertGreater(rep.problems, 0)

    def test_diagnose_does_not_probe_fs(self):
        # codex r-doctor-1：diagnose 真正唯讀 → 不得呼叫會寫探測檔的 assess_fs。
        self._w(self.hA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        with mock.patch.object(doctor.atomicio, "assess_fs",
                               side_effect=AssertionError("diagnose 必須唯讀，不可探測 FS")):
            doctor.diagnose(self.local, self.hub, self.state_path)   # 不應拋


if __name__ == "__main__":
    unittest.main()
