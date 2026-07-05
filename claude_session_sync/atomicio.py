"""atomicio：原子寫 + fsync + rename + 讀回驗；FS 能力評估；O_EXCL lock；local-open 偵測。

依據 DESIGN 附錄（H6 crash-safe matrix、OQ8 不可靠 FS）+ PLAN v0.8 §2.8 / 決定 #8：
  - 寫入 = 同目錄 temp → 寫 bytes → fsync(檔) → os.replace → fsync(目錄) → **讀回比對**。
    讀回不符（自身寫壞 **或** 並發被別人蓋）一律 raise，**永不靜默**（決定 #8）。
  - `assess_fs`：保守白名單——只有已知日誌式本地 FS 視為可靠；USB/FAT/網路碟 → best-effort+警告。
    rvw+lock **無論可靠與否都照做**；assess 只決定「是否額外警告 / 不宣稱 crash-safe」。
  - `FileLock`：O_EXCL lockfile（非 fcntl，跨 OS）。取不到 → raise（不靜默 proceed）。
    stale **只偵測不自動奪取**（hub 是跨機共用，跨 host 不可假設對方已死）；交 doctor/人工。
  - `is_local_open`：Linux /proc/fd 掃描，僅作**額外保險**，非「ff 進 local」依據（C3）。
  - 不覆蓋 local 既有 JSONL（C3）：上層用 `keep_both_path` 改寫檔名落地，不重寫內文。
純標準庫。
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# 本模組產生的 temp 檔名格式（供孤兒清理辨識）：.<原名>.<host>.<pid>.<hex>.tmp
# 含 host（已 sanitize 成 [A-Za-z0-9_-]）才能在**共用 hub** 上分辨「他機進行中的 temp」，
# 避免本機把別台正在寫的 temp 當孤兒刪掉（codex r7-4）。
_TEMP_RE = re.compile(r"^\.(?P<base>.+)\.(?P<host>[A-Za-z0-9_-]+)\.(?P<pid>\d+)\.[0-9a-f]{32}\.tmp$")


class AtomicWriteError(Exception):
    """原子寫失敗（含讀回驗不符）。"""


class VerifyError(AtomicWriteError):
    """讀回驗不符：自身寫壞或寫入後被並發覆蓋（決定 #8：偵測到即中止，不靜默）。"""


class LockError(Exception):
    """無法取得鎖。"""


class LockHeld(LockError):
    """鎖被（看似存活的）他者持有 → 不 proceed。"""


class StaleLock(LockError):
    """鎖看似陳舊（同 host 且持有 PID 已死）→ 交人工/二階段，**不自動奪取**。"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ── 原子寫 ────────────────────────────────────────────────────────────────

# Windows：os.open() 預設 **text mode**，os.write 會把 \n 譯成 \r\n、讀回 raw bytes 多出 \r
# → 寫出 bytes ≠ 讀回 bytes：rvw 必 VerifyError，且即便不驗也已靜默竄改檔案內容（每個 \n 加 \r）。
# 故所有「寫/讀檔案內容」的 fd 一律加 O_BINARY（POSIX 無此旗標＝0，無害）。跨 OS bytes 一致是本工具地基。
_O_BINARY = getattr(os, "O_BINARY", 0)


# ── 長路徑（繞過 Windows MAX_PATH=260）— **僅 memory-merge staging opt-in** ─────
#
# Windows 預設把路徑限在 260 字元（MAX_PATH）。memory-merge 暫存路徑
# `<cache>/claude-session-sync/merge/<pk>/<key>/<label>__<file>` 的 <pk>/<key> 各可達 ~200 字元
# （percent-encode 後）、兩段巢狀 → 由設計就常 >600 → os.mkdir/os.open 直接 OSError（memory-merge
# 現況回 status='error'）。解法＝把路徑轉成 `\\?\` 擴充長度形式（繞過 260，不依賴 LongPathsEnabled 登錄檔）。
#
# **範圍刻意限縮在 memory-merge staging**（各 atomic_* 的 `long_path=True`＋merge 直接呼叫 os_path）：其餘
# 寫入（session/tombstone/state/coverage/keep-both/index/lock）一律 `long_path=False` 預設＝**260-bound、
# 等同改動前**。原因（codex longpath-r1 F1/F2）：atomicio 的**讀取／枚舉端**（scan._session_files、
# tombstone.read_coverage/read_tombstones、canonical.load_bytes…）仍是 plain `Path`（260-bound）；若讓非
# staging 寫入也能過 260，會造出「寫得進、下次讀不回」的**衍生路徑**不對稱——keep-both 加 `.synced-…`
# 尾綴後 >260 → 寫成功但下次 scan 枚舉略過 → 每輪重造隱形 keep-both（F1）；`_coverage.json` >260 →
# bootstrap 報成功但 read_coverage 讀不到 → 專案被當未初始化（F2）。故非 staging 一律 260-bound、失敗即
# OSError（與改動前一致、fail-closed、可見），**不**靜默造出讀不回的檔。深 cwd 非 staging 檔的完整 >260
# 支援＝需連讀取/枚舉層一起長路徑化（大範圍動 scan/apply/tombstone），留待需要時（見 HANDOFF 有界殘留）。
#
# `\\?\` 語意雷（故轉換前必先 abspath：絕對化＋normpath）：① 須絕對路徑；② 只認反斜線（停用 `/` 轉譯）；
# ③ 不做 `.`/`..` 正規化（殘留即成字面元件）；④ UNC 須寫成 `\\?\UNC\server\share\…`。只在 os.* 邊界套用；
# 模組內部 Path/字串保持未加前綴（例外訊息/state 不外洩 `\\?\`）。`os_path` 只改**長度**、不改跟隨語意
# （symlink/reparse 跟隨與否仍由 O_NOFOLLOW 與上游 lstat 守衛決定）。

def _win_longpath(abs_win_path: str) -> str:
    r"""已 abspath 的 Windows 路徑 → `\\?\` 擴充長度形式（純字串轉換，可跨平台單元測試）。"""
    s = abs_win_path
    if s.startswith("\\\\?\\") or s.startswith("\\\\.\\"):
        return s                                   # 已是擴充長度／裝置命名空間 → 不重複前綴
    if s.startswith("\\\\"):                         # UNC：\\server\share\… → \\?\UNC\server\share\…
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s                             # 磁碟：C:\… → \\?\C:\…


def os_path(path: str | os.PathLike) -> str:
    r"""把路徑轉成可直接餵給 `os.*` 系統呼叫的字串。Windows 上套 `\\?\` 擴充長度前綴（繞過 260 字元
    MAX_PATH）；POSIX 原樣回傳（`os.fspath`，零行為變動）。只在系統呼叫邊界呼叫、結果不儲存/不顯示。"""
    s = os.fspath(path)
    if os.name != "nt":
        return s
    if s.startswith("\\\\?\\") or s.startswith("\\\\.\\"):
        return s                                   # 已加前綴 → 不重複（abspath 可能毀損既有 \\?\）
    return _win_longpath(os.path.abspath(s))        # abspath＝絕對化＋normpath（收斂 `.`/`..`/混用斜線）


def read_bytes(path: str | os.PathLike) -> bytes:
    """讀整檔 bytes（長路徑安全；跟隨 symlink，同 `Path.read_bytes()`）。"""
    with open(os_path(path), "rb") as f:
        return f.read()


def read_text(path: str | os.PathLike, *, encoding: str = "utf-8") -> str:
    """讀整檔文字（長路徑安全；沿用預設 universal-newline，對 JSON/文字中繼無害，同 `Path.read_text()`）。"""
    with open(os_path(path), "r", encoding=encoding) as f:
        return f.read()


def _mkdirs(d: str | os.PathLike, *, long_path: bool = False) -> None:
    r"""`os.makedirs(exist_ok=True)`（等價原 `Path.mkdir(parents=True, exist_ok=True)`）。
    `long_path=True`（僅 memory-merge staging）→ 走 os_path 的 `\\?\` 繞過 MAX_PATH；否則原樣（260-bound）。"""
    os.makedirs(os_path(d) if long_path else os.fspath(d), exist_ok=True)


def _write_all(fd: int, data: bytes) -> None:
    """os.write 可能短寫；迴圈寫到完。"""
    mv = memoryview(data)
    while mv:
        n = os.write(fd, mv)
        mv = mv[n:]


def _fsync_dir(d: Path) -> None:
    """fsync 目錄項（讓 rename 落地）。某些平台/FS（Windows、部分網路碟）不支援 → 安靜略過。"""
    try:
        dfd = os.open(str(d), os.O_RDONLY)   # 260-bound（best-effort：Windows/網路碟本就失敗→略過；staging 深夾在 Linux 原生可開）
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass  # 不支援 dir fsync（best-effort）
    finally:
        os.close(dfd)


def _temp_path(target: Path) -> Path:
    """同目錄、隱藏、含 host+pid+亂數的唯一 temp 名（同 FS → rename 原子；host 供跨機孤兒辨識）。"""
    return target.with_name(f".{target.name}.{_host_tag()}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def atomic_write_bytes(path: str | os.PathLike, data: bytes, *,
                       do_fsync: bool = True, verify: bool = True, long_path: bool = False) -> None:
    """原子寫 bytes：同目錄 temp → fsync → os.replace → fsync(dir) → 讀回比對。

    讀回不符 → VerifyError（自身寫壞或並發覆蓋；決定 #8 不靜默）。失敗會清掉自己的 temp。
    註：本函式只寫**目的端**（source 不動），故即便崩潰丟失剛寫入的檔，重跑同步即可復原；
    跨斷電的耐久性僅在可靠 FS 保證（見 assess_fs），不可靠 FS 為 best-effort（DESIGN H6）。
    """
    target = Path(path)
    _wp = os_path if long_path else os.fspath   # long_path（僅 memory-merge staging）→ \\?\ 繞過 260；否則 260-bound（改動前行為）
    _mkdirs(target.parent, long_path=long_path)
    tmp = _temp_path(target)
    created = False  # 只有 os.open 成功、且尚未 rename 時，temp 才是「我們建的、待清理」
    try:
        fd = os.open(_wp(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
        created = True
        try:
            _write_all(fd, data)
            if do_fsync:
                os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(_wp(tmp), _wp(target))  # POSIX 同目錄 rename 原子；Windows 可覆蓋
        created = False  # 已 rename，temp 不再存在 → 別在 finally 誤刪到真檔
        if do_fsync:
            _fsync_dir(target.parent)
        if verify:
            got = read_bytes(target) if long_path else target.read_bytes()
            if got != data:
                raise VerifyError(
                    f"讀回驗不符：{target}（自身寫壞或寫入後被並發覆蓋；len got={len(got)} want={len(data)}）"
                )
    finally:
        if created:
            with contextlib.suppress(OSError):
                os.unlink(_wp(tmp))


def atomic_write_text(path: str | os.PathLike, text: str, *,
                      do_fsync: bool = True, verify: bool = True, long_path: bool = False) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), do_fsync=do_fsync, verify=verify, long_path=long_path)


def atomic_copy(src: str | os.PathLike, dst: str | os.PathLike, *,
                do_fsync: bool = True, verify: bool = True) -> None:
    """把 src 的 bytes 原子寫到 dst（讀回驗，**可覆蓋**）。供寫 hub（允許覆蓋）用。"""
    atomic_write_bytes(dst, Path(src).read_bytes(), do_fsync=do_fsync, verify=verify)


def atomic_create_bytes(path: str | os.PathLike, data: bytes, *,
                        do_fsync: bool = True, verify: bool = True, long_path: bool = False) -> None:
    """**只建不覆蓋**：以 O_CREAT|O_EXCL 直接開最終路徑寫入；已存在 → FileExistsError（呼叫端決定 keep-both）。

    供 local 寫入（C3：絕不覆蓋既有 local JSONL）。不走 temp+rename——rename 會覆蓋，破壞 no-clobber；
    O_EXCL 跨所有 FS（含 exFAT/網路碟）都成立。崩潰中途 → 部分**新**檔（無既有資料損失）；失敗清掉自建檔。
    """
    target = Path(path)
    _wp = os_path if long_path else os.fspath   # long_path（僅 memory-merge staging）→ \\?\ 繞過 260；否則 260-bound
    _mkdirs(target.parent, long_path=long_path)
    try:
        fd = os.open(_wp(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
    except FileExistsError:
        raise  # 不覆蓋；不刪別人的檔
    ok = False
    try:
        try:
            _write_all(fd, data)
            if do_fsync:
                os.fsync(fd)
        finally:
            os.close(fd)
        if do_fsync:
            _fsync_dir(target.parent)
        if verify and (read_bytes(target) if long_path else target.read_bytes()) != data:
            raise VerifyError(f"讀回驗不符（新建）：{target}")
        ok = True
    finally:
        if not ok:  # 寫入/驗證階段失敗 → 清掉自己建的部分檔
            with contextlib.suppress(OSError):
                os.unlink(_wp(target))


# ── FS 能力評估（H6 / OQ8）────────────────────────────────────────────────

# 只有「已知日誌式本地 FS」視為可靠（crash-safe + fsync 有意義 + 單機獨占）。
# 其餘（FAT 家族/USB/網路碟/未知）一律保守視為不可靠 → best-effort + 警告。
RELIABLE_FS = frozenset({
    "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "jfs", "reiserfs",
    "f2fs", "apfs", "hfs", "hfsplus", "ufs", "bcachefs",
})
# 明確不可靠（列出僅供訊息更清楚；判定仍以「不在白名單即不可靠」為準）。
UNRELIABLE_FS = frozenset({
    "vfat", "fat", "fat32", "msdos", "exfat", "ntfs", "fuseblk",
    "nfs", "nfs4", "cifs", "smbfs", "smb2", "smb3", "9p", "afpfs",
    "fuse.sshfs", "fuse.gvfsd-fuse", "tmpfs",
})


def classify_fstype(fstype: str | None) -> bool:
    """純函式：fstype → 是否可靠（crash-safe）。未知/不在白名單 → 不可靠（保守）。"""
    if not fstype:
        return False
    return fstype.lower() in RELIABLE_FS


def _unescape_mount(field: str) -> str:
    """/proc/mounts 把 space/tab/nl/backslash 等寫成八進位 \\040 \\011 \\012 \\134。
    **只**還原這些八進位序列；不可用 codecs `unicode_escape`——它會把多位元組 UTF-8（如 CJK
    掛載點 /mnt/共享）當 latin-1 拆解而毀損，導致最長前綴比對失敗、誤判為可靠 FS（codex r7-7）。"""
    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1), 8)), field)


def detect_fstype(path: str | os.PathLike) -> str | None:
    """偵測 path 所在掛載的 FS 類型。Linux 走 /proc/mounts（取最長匹配掛載點）；
    其他平台無可靠跨 OS 法 → None（呼叫端保守視為不可靠）。"""
    if not sys.platform.startswith("linux"):
        return None
    try:
        real = os.path.realpath(str(path))
    except OSError:
        return None
    try:
        raw = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    best_mount = ""
    best_type: str | None = None
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mount = _unescape_mount(parts[1])  # 只還原八進位轉義，不毀損非 ASCII（見 _unescape_mount）
        fstype = parts[2]
        if (real == mount or real.startswith(mount.rstrip("/") + "/")) and len(mount) >= len(best_mount):
            best_mount = mount
            best_type = fstype
    return best_type


@dataclass
class FsAssessment:
    path: str
    fstype: str | None
    reliable: bool
    can_write: bool
    reason: str


def assess_fs(dir_path: str | os.PathLike) -> FsAssessment:
    """評估某目錄：FS 類型、是否可靠（crash-safe）、是否可寫。對 hub/local/state/quarantine 各評一次。"""
    d = Path(dir_path)
    fstype = detect_fstype(d)
    reliable = classify_fstype(fstype)
    can_write = False
    write_note = ""
    if not d.is_dir():
        # **不自動建立**——若這是消失的掛載點，建立它會在裸 mountpoint 上製造假目錄、後續寫入落錯 FS（codex r15-2）。
        write_note = "，目錄不存在（不自動建立）"
    else:
        probe = d / f".csync-probe.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            probe.write_bytes(b"probe")
            can_write = True
        except OSError as e:
            write_note = f"，無法寫入（{e.__class__.__name__}）"
        finally:
            with contextlib.suppress(OSError):
                probe.unlink()
    if fstype is None:
        reason = f"未知 FS 類型（保守視為不可靠：保留 rvw+lock，不宣稱 crash-safe）{write_note}"
    elif reliable:
        reason = f"已知日誌式本地 FS（{fstype}）{write_note}"
    else:
        reason = f"非日誌式/可移除/網路 FS（{fstype}）→ best-effort + 警告{write_note}"
    return FsAssessment(str(d), fstype, reliable, can_write, reason)


# ── O_EXCL lock ───────────────────────────────────────────────────────────

@dataclass
class LockInfo:
    pid: int | None
    host: str | None
    time: str | None
    token: str | None = None
    raw: str | None = None


def _local_host() -> str:
    return socket.gethostname() or "unknown"


def _host_tag() -> str:
    """sanitize 過的本機名（供 temp 命名 / 跨機孤兒比對；只含 [A-Za-z0-9_-]）。"""
    return re.sub(r"[^A-Za-z0-9_-]", "-", _local_host())[:32] or "host"


_DISP_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _disp(v: object) -> str:
    """把**不可信的鎖 metadata**（host/pid/time/path，可能來自 malformed .lock）轉成可安全印出的字串：剔控制
    字元（含換行/CR/tab）+ 中和 lone surrogate（→ `?`）。否則含 surrogate 的鎖內容會令 strict-UTF-8 stdout 在印
    doctor 報告／FileLock 例外訊息時拋 UnicodeEncodeError（破指令）。對稱 `merge._disp`。**只用於顯示**——
    比對用的原始值（如 break_locks 的 token）不經此淨化。"""
    return _DISP_CTRL_RE.sub("", str(v)).encode("utf-8", "replace").decode("utf-8")


def _pid_alive(pid: int) -> bool:
    """同 host 上的 PID 是否存活。無法判定一律當「存活」（保守，不奪鎖）。"""
    if pid <= 0:
        return True
    if os.name == "nt":
        # Windows：os.kill(pid, 0) **不是**存活探測，會以 TerminateProcess **殺掉**目標進程！
        # 改用 ctypes OpenProcess+GetExitCodeProcess（唯讀查詢、不動目標）（codex r7-2 舊註「一律 True」已由此取代）。
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:
        return True  # pid 超出 C pid_t 範圍（malformed 鎖/temp 名）→ 無法判定 → 保守（對稱 _pid_alive_windows 的 DWORD 守衛）
    except PermissionError:
        return True  # 存在但他人擁有
    except OSError:
        return True  # 無法判定 → 保守
    return True


def _pid_alive_windows(pid: int) -> bool:
    """Windows PID 存活探測（ctypes；**絕不用 os.kill**——Windows os.kill(pid,0) 會殺進程）。

    A6 保守鐵則（安全方向＝寧可回「存活」不奪鎖）：只有「明確無此 PID」或「明確已終止」才回 False
    （可判 stale）；任何無法判定（DLL 載入失敗／權限不足 ACCESS_DENIED／查詢失敗／罕見 exit code
    259＝STILL_ACTIVE 與存活無法區分）一律回 True。故 PID 重用只會令已死者被誤判「存活」→ 保守保留
    （不誤奪鎖）；break-lock 另在 unlink 前再驗一次（doctor.break_locks）擋列出→移除間的重取 race。"""
    if not (0 < pid <= 0xFFFFFFFF):
        return True  # 超出 Windows PID（DWORD）合法範圍（來自 malformed 鎖/temp 檔名）→ 無法判定 → 保守存活
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_INVALID_PARAMETER = 87   # OpenProcess 對「無此 PID」回此碼 → 已死
        STILL_ACTIVE = 259             # GetExitCodeProcess：仍在跑
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE   # c_void_p：避免 64-bit handle 被截斷
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)  # 同理，handle 以 pointer-size 傳回
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # 明確「無此 PID」→ 已死；其餘（ACCESS_DENIED=5 等）＝存在但無權查 → 保守當存活。
            return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
        try:
            code = wintypes.DWORD()
            kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # 查詢失敗 → 無法判定 → 保守當存活
            return code.value == STILL_ACTIVE  # 仍在跑＝存活；其餘＝已終止（已死）
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 — 任何 ctypes 載入/呼叫失敗＝無法判定 → 保守當存活（絕不誤奪鎖）
        return True


class FileLock:
    """`<resource>.lock` 的 O_EXCL 鎖。取不到 → raise（LockHeld/StaleLock），不靜默 proceed。

    stale 只**偵測**（同 host 且 PID 已死）並 raise StaleLock，**不自動奪取**——hub 跨機共用，
    跨 host 無法判定對方死活，誤奪會互蓋（決定 #8 / PLAN §2.8）。
    """

    def __init__(self, resource_path: str | os.PathLike):
        self.lock_path = Path(str(resource_path) + ".lock")
        self._fd: int | None = None
        self._token: str | None = None  # 本次持有的唯一憑證（release 時憑此確認仍是自己再刪）

    def _read_info(self) -> LockInfo:
        try:
            raw = self.lock_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # 缺/不可讀/**非 UTF-8**（malformed 鎖，如 b"\xff"）→ 無法解析 → fail-closed（不 crash break-lock/acquire）。
            return LockInfo(None, None, None)
        try:
            d = json.loads(raw)
            return LockInfo(d.get("pid"), d.get("host"), d.get("time"), d.get("token"), raw)
        except Exception:  # noqa: BLE001
            return LockInfo(None, None, None, None, raw)

    def _is_stale(self, info: LockInfo) -> bool:
        # 只有「同 host 且 PID 明確已死」才算 stale；跨 host / 無法解析 → 不算（不奪）。
        if info.host != _local_host():
            return False
        # `type() is int`（非 isinstance）——bool 是 int 子類，JSON `true` 會被 isinstance 當 int（值=1）→
        # 在 Windows 被當 PID 1 探測 → 誤判 stale 刪掉 malformed 鎖。malformed pid 一律 fail-closed 不奪。
        if type(info.pid) is not int:
            return False
        return not _pid_alive(info.pid)

    def acquire(self) -> "FileLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY, 0o600)
        except FileExistsError:
            info = self._read_info()
            if self._is_stale(info):
                raise StaleLock(
                    f"鎖看似陳舊（host={_disp(info.host)} pid={_disp(info.pid)} 已不存在）：{_disp(self.lock_path)}。"
                    f"請確認無其他同步在跑後手動移除（doctor），不自動奪取。"
                )
            raise LockHeld(
                f"鎖被持有中（host={_disp(info.host)} pid={_disp(info.pid)} time={_disp(info.time)}）：{_disp(self.lock_path)}")
        self._fd = fd
        self._token = uuid.uuid4().hex
        payload = json.dumps(
            {"pid": os.getpid(), "host": _local_host(), "time": _utc_now_iso(), "token": self._token},
            ensure_ascii=False,
        ).encode("utf-8")
        with contextlib.suppress(OSError):
            os.write(fd, payload)
            os.fsync(fd)
        return self

    def acquire_blocking(self, *, timeout_s: float = 5.0, poll_s: float = 0.05) -> "FileLock":
        """LockHeld（他者存活持有）時輪詢重試到 timeout；逾時 raise LockError，不靜默 proceed。
        StaleLock 不在此攔截 → 直接外拋（等待不會讓已死的持有者釋放，交 doctor/人工）。"""
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            try:
                return self.acquire()
            except LockHeld:
                if time.monotonic() >= deadline:
                    raise LockError(f"等待鎖逾時（{timeout_s}s）：{self.lock_path}")
                time.sleep(poll_s)

    def release(self) -> None:
        if self._fd is None:
            return
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None
        token, self._token = self._token, None
        # 只在「目前 lockfile 仍是自己」時移除：憑 content token 比對（比 inode 在 exFAT/網路碟更可靠）。
        # 否則（被人手動清掉後、別的 writer 已重取鎖）會誤刪他人的鎖、放第三者進來（codex r7-3）。
        if token is not None and self._read_info().token == token:
            with contextlib.suppress(OSError):
                os.unlink(str(self.lock_path))

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


# ── local-open 偵測（額外保險，非 ff 依據）────────────────────────────────

def is_local_open(path: str | os.PathLike) -> bool | None:
    """該檔是否被某進程開啟。Linux 掃 /proc/*/fd；無法判定（非 Linux/無 /proc/權限不足）→ None。

    僅作**額外保險**：C3 的真正保護是「絕不覆蓋 local 既有 JSONL」，不靠此偵測。
    """
    if not sys.platform.startswith("linux"):
        return None
    proc = Path("/proc")
    if not proc.exists():
        return None
    try:
        target = os.path.realpath(str(path))
    except OSError:
        return None
    saw_denied = False  # 有看不到的進程（hidepid/他人持有）→ 不能斷言「沒開」，回 None
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        fd_dir = pid_dir / "fd"
        try:
            entries = list(fd_dir.iterdir())
        except PermissionError:
            saw_denied = True
            continue
        except OSError:
            continue  # 進程已退
        for fd in entries:
            try:
                if os.path.realpath(str(fd)) == target:
                    return True
            except OSError:
                continue
    return None if saw_denied else False


# ── keep-both / 孤兒 temp 清理 ─────────────────────────────────────────────

_KEEP_BOTH_TRIES = 8  # 取不碰撞 keep-both 檔名的重試次數（O_EXCL race 極罕見）


def write_keep_both(target: str | os.PathLike, data: bytes, *,
                    machine: str | None = None, tries: int = _KEEP_BOTH_TRIES) -> Path:
    """把 data 以 **O_EXCL 只建不覆蓋** 寫到 target 旁的不碰撞 sibling 檔名（C3：絕不覆蓋既有檔）。

    回實際寫入的新路徑。名字被搶（極罕見 race）→ 換一個重試；用罄 → AtomicWriteError。
    供 ff hub->local / copy 期間冒出同名 / 互動 union·keep-both 共用（單一來源）。"""
    for _ in range(tries):
        dest = keep_both_path(target, machine=machine)
        try:
            atomic_create_bytes(dest, data)
            return dest
        except FileExistsError:
            continue
    raise AtomicWriteError(f"無法取得不碰撞的 keep-both 檔名：{target}")


def keep_both_path(target: str | os.PathLike, *, machine: str | None = None) -> Path:
    """為「不可覆蓋 local 既有 JSONL」產生不碰撞的 sibling 檔名（B6：複製+改檔名，不重寫內文）。

    新 stem 即 Claude resume 時的 session 身分（檔名為配對鍵）。回**尚不存在**的路徑。
    """
    t = Path(target)
    host = re.sub(r"[^A-Za-z0-9_-]", "-", (machine or _local_host()))[:24]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"{t.stem}.synced-{host}-{stamp}"
    cand = t.with_name(base + t.suffix)
    n = 1
    while cand.exists():
        cand = t.with_name(f"{base}.{n}{t.suffix}")
        n += 1
    return cand


def cleanup_orphan_temps(dir_path: str | os.PathLike, *, max_age_s: float = 3600.0) -> list[str]:
    """清掉本模組遺留的孤兒 temp（.<名>.<host>.<pid>.<hex>.tmp）。回已刪清單。

    跨機共用 hub 安全規則（codex r7-4）——只在以下情況刪，避免誤刪他機進行中的 temp：
      - **本機** host + PID 已死 → 孤兒，刪。
      - **本機** host + PID 存活 + 新（≤max_age）→ 進行中，留。
      - **本機** host + 很舊（>max_age）→ 視為孤兒（PID 可能已被回收再用），刪。
      - **他機** host → 無法判存活；只在很舊（>max_age）才刪，否則一律留。
    """
    d = Path(dir_path)
    removed: list[str] = []
    if not d.exists():
        return removed
    local = _host_tag()
    now = time.time()
    for p in d.iterdir():
        if not p.is_file():
            continue
        m = _TEMP_RE.match(p.name)
        if not m:
            continue
        host = m.group("host")
        pid = int(m.group("pid"))
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        old = age > max_age_s
        if host == local:
            if _pid_alive(pid) and not old:
                continue  # 本機、存活、新 → 進行中
        elif not old:
            continue       # 他機、無法判存活、且新 → 保守保留
        with contextlib.suppress(OSError):
            p.unlink()
            removed.append(p.name)
    return removed
