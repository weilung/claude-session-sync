import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from claude_session_sync import atomicio as aio
from tests import _caps


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_write_and_readback(self):
        p = self.tmp / "a.jsonl"
        aio.atomic_write_bytes(p, b"hello\nworld\n")
        self.assertEqual(p.read_bytes(), b"hello\nworld\n")

    def test_overwrite_existing(self):
        p = self.tmp / "a.jsonl"
        p.write_bytes(b"old")
        aio.atomic_write_bytes(p, b"new-content")
        self.assertEqual(p.read_bytes(), b"new-content")

    def test_no_temp_left_after_success(self):
        p = self.tmp / "a.jsonl"
        aio.atomic_write_bytes(p, b"x")
        leftovers = [q.name for q in self.tmp.iterdir() if q.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_atomic_create_no_clobber(self):
        p = self.tmp / "new.jsonl"
        aio.atomic_create_bytes(p, b"first")
        self.assertEqual(p.read_bytes(), b"first")
        with self.assertRaises(FileExistsError):
            aio.atomic_create_bytes(p, b"second")  # O_EXCL：不覆蓋
        self.assertEqual(p.read_bytes(), b"first")  # 原檔完好

    def test_atomic_create_cleans_partial_on_failure(self):
        p = self.tmp / "new.jsonl"
        with mock.patch.object(aio, "_write_all", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                aio.atomic_create_bytes(p, b"data")
        self.assertFalse(p.exists())  # 寫入失敗 → 清掉自建的部分新檔

    def test_text_roundtrip_utf8(self):
        p = self.tmp / "u.jsonl"
        aio.atomic_write_text(p, "héllo 世界\n")
        self.assertEqual(p.read_text(encoding="utf-8"), "héllo 世界\n")

    def test_copy(self):
        src = self.tmp / "src.jsonl"
        src.write_bytes(b"payload-bytes")
        dst = self.tmp / "dst.jsonl"
        aio.atomic_copy(src, dst)
        self.assertEqual(dst.read_bytes(), b"payload-bytes")

    def test_verify_error_on_clobber(self):
        # 模擬「寫入後讀回到非己方內容」（並發覆蓋）→ VerifyError，且清掉 temp。
        p = self.tmp / "a.jsonl"
        with mock.patch.object(Path, "read_bytes", return_value=b"someone-else"):
            with self.assertRaises(aio.VerifyError):
                aio.atomic_write_bytes(p, b"mine")
        leftovers = [q.name for q in self.tmp.iterdir() if q.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_temp_cleaned_on_replace_failure(self):
        p = self.tmp / "a.jsonl"
        with mock.patch.object(aio.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                aio.atomic_write_bytes(p, b"data")
        leftovers = [q.name for q in self.tmp.iterdir() if q.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class TestFsAssessment(unittest.TestCase):
    def test_classify_known(self):
        self.assertTrue(aio.classify_fstype("ext4"))
        self.assertTrue(aio.classify_fstype("EXT4"))  # 大小寫無關
        self.assertTrue(aio.classify_fstype("btrfs"))
        self.assertFalse(aio.classify_fstype("vfat"))
        self.assertFalse(aio.classify_fstype("exfat"))
        self.assertFalse(aio.classify_fstype("nfs4"))

    def test_classify_unknown_is_unreliable(self):
        self.assertFalse(aio.classify_fstype(None))
        self.assertFalse(aio.classify_fstype(""))
        self.assertFalse(aio.classify_fstype("some-future-fs"))

    def test_detect_fstype_no_crash(self):
        ft = aio.detect_fstype(self._dir())
        self.assertTrue(ft is None or isinstance(ft, str))

    def test_unescape_mount_octal_only(self):
        # 只還原八進位轉義；多位元組 UTF-8（CJK）原樣保留（codex r7-7）。
        self.assertEqual(aio._unescape_mount(r"/mnt/a\040b"), "/mnt/a b")
        self.assertEqual(aio._unescape_mount(r"/x\011y"), "/x\ty")
        self.assertEqual(aio._unescape_mount("/mnt/共享"), "/mnt/共享")
        self.assertEqual(aio._unescape_mount(r"/mnt/共\040享"), "/mnt/共 享")

    def test_assess_writable_dir(self):
        a = aio.assess_fs(self._dir())
        self.assertTrue(a.can_write)
        self.assertEqual(a.reliable, aio.classify_fstype(a.fstype))
        self.assertTrue(a.reason)

    def _dir(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return td.name


class TestFileLock(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.res = self.tmp / "state.json"

    def tearDown(self):
        self._td.cleanup()

    def test_acquire_blocks_second(self):
        lk = aio.FileLock(self.res).acquire()
        with self.assertRaises(aio.LockHeld):
            aio.FileLock(self.res).acquire()
        lk.release()
        # 釋放後可再取
        aio.FileLock(self.res).acquire().release()

    def test_context_manager(self):
        with aio.FileLock(self.res):
            self.assertTrue((self.tmp / "state.json.lock").exists())
        self.assertFalse((self.tmp / "state.json.lock").exists())

    def test_stale_same_host_dead_pid(self):
        lp = Path(str(self.res) + ".lock")
        lp.write_text(json.dumps({"pid": 4242, "host": aio._local_host(), "time": "t"}), encoding="utf-8")
        with mock.patch.object(aio, "_pid_alive", return_value=False):
            with self.assertRaises(aio.StaleLock):
                aio.FileLock(self.res).acquire()

    def test_other_host_is_held_not_stale(self):
        lp = Path(str(self.res) + ".lock")
        lp.write_text(json.dumps({"pid": 1, "host": "some-other-host", "time": "t"}), encoding="utf-8")
        with mock.patch.object(aio, "_pid_alive", return_value=False):
            with self.assertRaises(aio.LockHeld):  # 跨 host 不可假設已死 → 不奪
                aio.FileLock(self.res).acquire()

    def test_unparseable_lock_is_held_not_stale(self):
        lp = Path(str(self.res) + ".lock")
        lp.write_text("garbage{", encoding="utf-8")
        with self.assertRaises(aio.LockHeld):
            aio.FileLock(self.res).acquire()

    @_caps.needs_unlink_open
    def test_release_does_not_steal_others_lock(self):
        # A 取鎖 → 有人手動清掉 A 的 lockfile → B 取得新鎖 → A.release() 不可刪 B 的鎖（codex r7-3）。
        a = aio.FileLock(self.res).acquire()
        os.unlink(str(a.lock_path))            # 模擬人工/doctor 誤清
        b = aio.FileLock(self.res).acquire()   # B 重新取得（不同 token）
        b_token = b._token
        a.release()                            # A 釋放：應發現已非自己 → 不刪
        self.assertTrue(b.lock_path.exists(), "A.release() 誤刪了 B 的鎖")
        self.assertEqual(aio.FileLock(self.res)._read_info().token, b_token)
        b.release()

    def test_acquire_blocking_times_out(self):
        held = aio.FileLock(self.res).acquire()
        try:
            t0 = time.monotonic()
            with self.assertRaises(aio.LockError):
                aio.FileLock(self.res).acquire_blocking(timeout_s=0.2, poll_s=0.02)
            self.assertGreaterEqual(time.monotonic() - t0, 0.2)
        finally:
            held.release()

    def test_acquire_blocking_succeeds_after_release(self):
        a = aio.FileLock(self.res).acquire()
        a.release()
        b = aio.FileLock(self.res).acquire_blocking(timeout_s=1.0)
        self.assertTrue(b.lock_path.exists())
        b.release()

    def test_acquire_blocking_propagates_stale_immediately(self):
        lp = Path(str(self.res) + ".lock")
        lp.write_text(json.dumps({"pid": 4242, "host": aio._local_host(), "time": "t"}), encoding="utf-8")
        with mock.patch.object(aio, "_pid_alive", return_value=False):
            with self.assertRaises(aio.StaleLock):  # 等待無益 → 立即外拋
                aio.FileLock(self.res).acquire_blocking(timeout_s=5.0)

    def test_pid_alive_windows_never_uses_os_kill(self):
        # Windows os.kill(pid,0) 會以 TerminateProcess 殺進程 → _pid_alive 在 Windows 絕不可呼叫 os.kill
        # （改走 ctypes）。存活的自身 PID 一律回 True；全程不得觸及 os.kill（codex r7-2）。
        # 跨 OS：Linux 強制 name="nt" → ctypes WinDLL 載入失敗 → 保守回 True，同樣不碰 os.kill。
        with mock.patch.object(aio.os, "name", "nt"), \
             mock.patch.object(aio.os, "kill", side_effect=AssertionError("Windows 不可用 os.kill 探測 PID")):
            self.assertTrue(aio._pid_alive(os.getpid()))

    @_caps.needs_dead_pid_detection
    def test_pid_alive_detects_dead_and_live(self):
        # 真實探測（POSIX os.kill／Windows ctypes OpenProcess+GetExitCodeProcess）：
        # 已死 PID → False（可判 stale）、存活 PID（自身）→ True。
        self.assertFalse(aio._pid_alive(_caps.dead_pid()))
        self.assertTrue(aio._pid_alive(os.getpid()))

    def test_bool_pid_never_stale(self):
        # JSON `true` 不可當 PID（bool 是 int 子類，值=1）：_is_stale 必須以 type() is int 擋掉、fail-closed
        # 保留（否則 Windows 會把它當 PID 1 探測 → 誤判 stale 刪掉 malformed 鎖）。跨 OS（bool 在探測前被擋）。
        lp = Path(str(self.res) + ".lock")
        lp.write_text(json.dumps({"pid": True, "host": aio._local_host(), "time": "t"}), encoding="utf-8")
        fl = aio.FileLock(self.res)
        self.assertFalse(fl._is_stale(fl._read_info()))
        with self.assertRaises(aio.LockHeld):   # 非 stale → 當被持有、不奪
            aio.FileLock(self.res).acquire()

    def test_pid_alive_windows_out_of_range_is_conservative(self):
        # 超出 Windows PID（DWORD）範圍的 pid（來自 malformed 鎖/temp 檔名）→ 保守回 True、且**不得拋例外崩潰**。
        # 範圍守衛在 ctypes 之前短路 → 跨 OS 決定性（不觸 kernel32）。
        self.assertTrue(aio._pid_alive_windows(0xFFFFFFFF + 1))
        self.assertTrue(aio._pid_alive_windows(0))
        self.assertTrue(aio._pid_alive_windows(-5))

    def test_huge_pid_fail_closed_cross_os(self):
        # malformed 鎖 pid 超出 C pid_t（POSIX os.kill→OverflowError）/ DWORD（Windows）範圍：_pid_alive 兩平台
        # 都須保守回 True、**不得 crash**；_is_stale fail-closed 保留、acquire 當被持有（codex breaklock-g2 Medium）。
        self.assertTrue(aio._pid_alive(10**30))   # 走實際平台路徑（POSIX os.kill／Windows DWORD 守衛）
        lp = Path(str(self.res) + ".lock")
        lp.write_text(json.dumps({"pid": 10**30, "host": aio._local_host(), "time": "t"}), encoding="utf-8")
        fl = aio.FileLock(self.res)
        self.assertFalse(fl._is_stale(fl._read_info()))
        with self.assertRaises(aio.LockHeld):
            aio.FileLock(self.res).acquire()

    def test_invalid_utf8_lock_is_unparseable_not_crash(self):
        # 非 UTF-8 的 malformed 鎖（如 b"\xff"）：_read_info fail-closed（read_text 拋 UnicodeDecodeError 不得逸出）、
        # _is_stale False、acquire 當被持有——不 crash（codex breaklock-g3 Medium）。
        lp = Path(str(self.res) + ".lock")
        lp.write_bytes(b"\xff\xfe garbage \x00")
        fl = aio.FileLock(self.res)
        info = fl._read_info()
        self.assertIsNone(info.host)
        self.assertIsNone(info.pid)
        self.assertFalse(fl._is_stale(info))
        with self.assertRaises(aio.LockHeld):
            aio.FileLock(self.res).acquire()

    def test_disp_neutralizes_surrogate_and_controls(self):
        self.assertEqual(aio._disp("a\ud800b"), "a?b")   # lone surrogate → ?（可 UTF-8 編碼）
        self.assertEqual(aio._disp("x\n\ty"), "xy")       # 控制字元（含換行/tab）剔除
        self.assertEqual(aio._disp("café中"), "café中")   # 正常字元原樣保留

    def test_lock_message_printable_with_surrogate_metadata(self):
        # JSON-valid 但 host 含 lone surrogate 的 malformed 鎖：_is_stale 不當 stale，LockHeld 訊息須可 UTF-8
        # 編碼（不 crash 印出，codex breaklock-g4）。json.dumps 預設 ensure_ascii → 檔案本身仍是純 ASCII。
        lp = Path(str(self.res) + ".lock")
        lp.write_text(json.dumps({"pid": 1, "host": "\ud800", "time": "t", "token": "x"}), encoding="utf-8")
        with self.assertRaises(aio.LockHeld) as cm:
            aio.FileLock(self.res).acquire()
        str(cm.exception).encode("utf-8")   # 不得拋 UnicodeEncodeError


class TestIsLocalOpen(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    @unittest.skipUnless(sys.platform.startswith("linux"), "需 /proc")
    def test_open_file_detected(self):
        p = self.tmp / "open.jsonl"
        p.write_bytes(b"x")
        with open(p, "rb"):
            self.assertTrue(aio.is_local_open(p))

    @unittest.skipUnless(sys.platform.startswith("linux"), "需 /proc")
    def test_closed_file_not_open(self):
        p = self.tmp / "closed.jsonl"
        p.write_bytes(b"x")
        self.assertFalse(aio.is_local_open(p))

    @unittest.skipIf(sys.platform.startswith("linux"), "非 Linux 才回 None")
    def test_non_linux_returns_none(self):
        self.assertIsNone(aio.is_local_open(self.tmp / "x"))


class TestKeepBothAndCleanup(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_keep_both_path_unique_and_suffix(self):
        target = self.tmp / "sid123.jsonl"
        target.write_bytes(b"local-existing")
        kb = aio.keep_both_path(target)
        self.assertFalse(kb.exists())
        self.assertEqual(kb.suffix, ".jsonl")
        self.assertTrue(kb.stem.startswith("sid123.synced-"))

    def test_keep_both_increments_on_collision(self):
        target = self.tmp / "sid.jsonl"
        kb1 = aio.keep_both_path(target)
        kb1.write_bytes(b"a")  # 佔住第一個候選（同秒）
        kb2 = aio.keep_both_path(target)
        self.assertNotEqual(kb1, kb2)
        self.assertFalse(kb2.exists())

    def test_cleanup_removes_dead_pid_temp(self):
        orphan = self.tmp / f".victim.jsonl.{aio._host_tag()}.4242.{'a'*32}.tmp"
        orphan.write_bytes(b"junk")
        with mock.patch.object(aio, "_pid_alive", return_value=False):
            removed = aio.cleanup_orphan_temps(self.tmp)
        self.assertIn(orphan.name, removed)
        self.assertFalse(orphan.exists())

    def test_cleanup_keeps_live_recent_temp(self):
        live = self.tmp / f".x.jsonl.{aio._host_tag()}.{os.getpid()}.{'b'*32}.tmp"
        live.write_bytes(b"inflight")
        removed = aio.cleanup_orphan_temps(self.tmp)  # 本機 pid 存活 + 剛建
        self.assertNotIn(live.name, removed)
        self.assertTrue(live.exists())

    def test_cleanup_keeps_other_host_recent(self):
        # 他機剛寫的 temp（無法判存活）→ 夠新就保留，不可誤刪別台進行中的寫入。
        other = self.tmp / f".y.jsonl.otherbox.4242.{'c'*32}.tmp"
        other.write_bytes(b"remote-inflight")
        with mock.patch.object(aio, "_pid_alive", return_value=False):  # 本機看他機 pid「已死」也不該刪
            removed = aio.cleanup_orphan_temps(self.tmp)
        self.assertNotIn(other.name, removed)
        self.assertTrue(other.exists())

    def test_cleanup_removes_other_host_old(self):
        other = self.tmp / f".z.jsonl.otherbox.4242.{'d'*32}.tmp"
        other.write_bytes(b"stale")
        old = time.time() - 7200
        os.utime(other, (old, old))
        removed = aio.cleanup_orphan_temps(self.tmp, max_age_s=3600)
        self.assertIn(other.name, removed)

    def test_cleanup_ignores_non_matching(self):
        keep = self.tmp / "real.jsonl"
        keep.write_bytes(b"data")
        aio.cleanup_orphan_temps(self.tmp)
        self.assertTrue(keep.exists())

    def test_real_temp_name_matches_pattern(self):
        # atomic_write_bytes 真正用的 temp 名必須能被孤兒清理辨識（含 host 段）。
        p = self.tmp / "sid.jsonl"
        captured = {}
        real = aio._temp_path

        def spy(t):
            tp = real(t)
            captured["name"] = tp.name
            return tp

        with mock.patch.object(aio, "_temp_path", side_effect=spy):
            aio.atomic_write_bytes(p, b"x")
        self.assertRegex(captured["name"], aio._TEMP_RE)


class TestOsPath(unittest.TestCase):
    r"""os_path / _win_longpath：Windows `\\?\` 擴充長度前綴的字串轉換（繞過 MAX_PATH=260）。"""

    def test_win_longpath_transform(self):
        # 純字串轉換，跨平台可測（不碰 FS）：磁碟／UNC／已前綴／裝置命名空間。
        self.assertEqual(aio._win_longpath("C:\\a\\b"), "\\\\?\\C:\\a\\b")
        self.assertEqual(aio._win_longpath("\\\\srv\\share\\x"), "\\\\?\\UNC\\srv\\share\\x")
        self.assertEqual(aio._win_longpath("\\\\?\\C:\\a"), "\\\\?\\C:\\a")               # 已前綴 → 不重複
        self.assertEqual(aio._win_longpath("\\\\?\\UNC\\srv\\share"), "\\\\?\\UNC\\srv\\share")
        self.assertEqual(aio._win_longpath("\\\\.\\PhysicalDrive0"), "\\\\.\\PhysicalDrive0")  # 裝置命名空間不動

    @unittest.skipIf(os.name == "nt", "POSIX-only：非 Windows 一律原樣回傳（零行為變動）")
    def test_os_path_posix_identity(self):
        self.assertEqual(aio.os_path("/a/b/c"), "/a/b/c")
        self.assertEqual(aio.os_path(Path("/a/b")), "/a/b")
        self.assertEqual(aio.os_path("rel/path"), "rel/path")   # POSIX 不絕對化

    @unittest.skipUnless(os.name == "nt", "Windows-only：驗 os_path 絕對化 + \\?\\ 前綴 + 冪等")
    def test_os_path_windows_prefixes(self):
        p = aio.os_path("C:\\Temp\\x")
        self.assertTrue(p.startswith("\\\\?\\"))
        self.assertTrue(aio.os_path("rel\\file").startswith("\\\\?\\"))    # 相對 → abspath → 前綴
        self.assertEqual(aio.os_path(p), p)                                # 已前綴 → 冪等


class TestLongPath(unittest.TestCase):
    r"""memory-merge staging 的 >260 深路徑原子寫/建/讀（`long_path=True`）——關 MAX_PATH 缺口。

    全平台實跑：Linux 原生支援 >260、Windows 未開 LongPathsEnabled 時靠 os_path 的 `\\?\` 繞過（即回歸守衛）。
    非 staging 寫入（FileLock/keep_both/coverage…）刻意維持 260-bound（`long_path=False`、fail-closed），不在此測。"""

    def setUp(self):
        # 不用 TemporaryDirectory：其 cleanup 走 shutil.rmtree(plain path)，無法移除本測試建立的 >260 深路徑
        # （Windows 未開 LongPathsEnabled 時）→ 改 mkdtemp + tearDown 以 os_path 的 \\?\ 遞迴刪。
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(aio.os_path(self.tmp), ignore_errors=True)   # \\?\ 前綴 → rmtree 可遞迴進深路徑

    def _deep(self, leaf):
        # 兩段近 NAME_MAX(255) 巢狀 → 總長遠超 260（鏡像 memory-merge 暫存最深形狀 <pk>/<key>/<file>）。
        seg = "n" * 130
        return self.tmp / seg / seg / leaf

    def test_atomic_write_read_deep(self):
        p = self._deep("a.jsonl")
        self.assertGreater(len(str(p)), 260)
        aio.atomic_write_bytes(p, b"deep-bytes\n", long_path=True)   # 內建 verify 讀回（>260）
        self.assertEqual(aio.read_bytes(p), b"deep-bytes\n")

    def test_atomic_create_deep(self):
        p = self._deep("c.jsonl")
        aio.atomic_create_bytes(p, b"created", long_path=True)
        self.assertEqual(aio.read_bytes(p), b"created")
        with self.assertRaises(FileExistsError):
            aio.atomic_create_bytes(p, b"again", long_path=True)     # O_EXCL 仍不覆蓋（深路徑）

    def test_write_text_read_deep(self):
        p = self._deep("t.json")
        aio.atomic_write_text(p, '{"k":1}', long_path=True)
        self.assertEqual(json.loads(aio.read_text(p)), {"k": 1})


if __name__ == "__main__":
    unittest.main()
