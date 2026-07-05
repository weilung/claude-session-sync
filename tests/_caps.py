r"""跨 OS 能力探測（capability probes）+ 對應的 skip 裝飾器。

設計原則：用「實際試一次」決定某測試能否在**當前環境**執行，而非寫死 `if Windows`。
好處——Windows 一旦開了開發者模式（symlink）、或換成大小寫敏感的卷、或開了長路徑支援，
對應測試會**自動亮起來跑、零程式碼改動**（見 HANDOFF「Windows 可攜性」決策：
capability-probe skip + Linux CI 兜底）。

被 skip 的多半是 **POSIX-only 的測試 setup**（建 symlink 要權限、chmod 讓夾不可讀、
大小寫碰撞檔名、含反斜線/控制字元的檔名、刪除開啟中的檔）——production 程式碼本身是對的，
這些「保護 Windows 使用者」的行為改由 Linux CI 跑足覆蓋。
（`memory-merge` 長/非 ASCII 檔名暫存曾因撞 MAX_PATH=260 而 skip、留 P2；現已由 `atomicio.os_path`
的 `\\?\` 擴充長度前綴修好 → 對應測試在**所有平台**實跑，`_probe_long_path`/`needs_long_path` 已移除。）
（`doctor --break-lock` 曾因 `_pid_alive` 在 Windows 是 no-op 而 skip；現已改用 ctypes 探測，
Windows 亦能判定 dead-PID → `CAN_DETECT_DEAD_PID` 自動亮起、對應測試在 Windows 實跑。）

探測在 import 時各跑一次（模組被 Python 快取，故只跑一次）。任一探測失敗一律當「不支援」→ skip（fail-safe）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from claude_session_sync import atomicio


def _probe_symlink() -> bool:
    """能否建立 symlink（Windows 需開發者模式/管理員，否則 WinError 1314）。"""
    try:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "tgt"
            target.write_text("x", encoding="utf-8")
            link = Path(d) / "lnk"
            os.symlink(str(target), str(link))
            return link.is_symlink()
    except (OSError, NotImplementedError, AttributeError):
        return False


def _probe_case_sensitive() -> bool:
    """FS 是否大小寫敏感（能同時存在 CaseProbe 與 caseprobe）。Windows/macOS 預設否。"""
    try:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "CaseProbe").write_text("x", encoding="utf-8")
            return not (Path(d) / "caseprobe").exists()
    except OSError:
        return False


def _probe_unreadable_dir() -> bool:
    """chmod(0) 能否讓目錄列舉失敗（POSIX 非 root 可；Windows chmod 對目錄無此效果；root 亦不可）。"""
    try:
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d) / "noread"
            sub.mkdir()
            (sub / "f").write_text("x", encoding="utf-8")
            try:
                os.chmod(str(sub), 0)
                try:
                    os.listdir(str(sub))
                    return False
                except PermissionError:
                    return True
            finally:
                os.chmod(str(sub), 0o700)  # 還原權限以便 TemporaryDirectory 清理
    except OSError:
        return False


def _probe_unlink_open() -> bool:
    """能否刪除「自己正開著」的檔（POSIX 可；Windows WinError 32 不允許）。"""
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f"
            fd = os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.unlink(str(p))
                return True
            except OSError:
                return False
            finally:
                os.close(fd)
    except OSError:
        return False


def _probe_filename(name: str) -> bool:
    """能否在 FS 上建立此檔名（反斜線/控制字元在 Windows 非法）。"""
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / name
            p.write_text("x", encoding="utf-8")
            return p.is_file()
    except OSError:
        return False


_pinned_dead_procs: list[subprocess.Popen] = []  # 保留 Popen 參照 → 不被 GC


def dead_pid() -> int:
    """回一個「剛結束的子進程」pid，且**保留其 Popen 參照**（handle 不關）。

    Windows 下未關 handle → OS 不回收該 PID（不會被別的進程重用）、且 GetExitCodeProcess 回真實
    exit code（非 STILL_ACTIVE）→ `_pid_alive` 可**決定性**判為已死，消除 PID-重用 flake。
    Linux 下 wait() 已收屍、短期不重用；保留參照無害。呼叫端據此測 dead-PID 路徑（stale 鎖等）。"""
    p = subprocess.Popen([sys.executable, "-c", ""])
    p.wait()
    _pinned_dead_procs.append(p)  # 釘住 handle：避免 Windows 在檢查前重用該 PID
    return p.pid


def _probe_dead_pid_detectable() -> bool:
    """能否可靠判定某 PID 已死（Windows 改用 ctypes 探測後亦可；見 `atomicio._pid_alive_windows`）。"""
    try:
        return atomicio._pid_alive(dead_pid()) is False
    except Exception:  # noqa: BLE001 — 探測失敗一律當「不可偵測」（fail-safe → skip）
        return False


def make_junction(link: Path, target: Path) -> None:
    """建立 directory junction（Windows `mklink /J`；免權限、限同機）。POSIX 無此概念 → 呼叫端 @needs_junction 擋。"""
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


def _probe_junction() -> bool:
    """能否建立 directory junction（Windows `mklink /J`，免權限、限同機；POSIX 無此概念）。junction＝reparse point，
    但 `is_symlink()`/`S_ISLNK` 對它回 False（不同於 symlink）→ 工具刻意「透明跟隨」的同機共用機制（方式2/方式1）。"""
    if os.name != "nt":
        return False
    try:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "tgt"
            target.mkdir()
            (target / "f").write_text("x", encoding="utf-8")
            link = Path(d) / "lnk"
            make_junction(link, target)
            return link.is_dir() and not link.is_symlink() and (link / "f").is_file()
    except (OSError, subprocess.SubprocessError):
        return False


CAN_SYMLINK = _probe_symlink()
CASE_SENSITIVE_FS = _probe_case_sensitive()
CAN_UNREADABLE_DIR = _probe_unreadable_dir()
CAN_UNLINK_OPEN = _probe_unlink_open()
CAN_BACKSLASH_NAME = _probe_filename("a\\b.md")
CAN_CONTROL_CHAR_NAME = _probe_filename("a\tb.md")
CAN_DETECT_DEAD_PID = _probe_dead_pid_detectable()
CAN_JUNCTION = _probe_junction()


# ── 預建 skip 裝飾器（@_caps.needs_xxx）────────────────────────────────────
needs_symlink = unittest.skipUnless(
    CAN_SYMLINK, "需要建立 symlink 的權限（Windows：開發者模式/管理員）")
needs_case_sensitive_fs = unittest.skipUnless(
    CASE_SENSITIVE_FS, "需要大小寫敏感的檔案系統（Windows/macOS 預設不支援）")
needs_unreadable_dir = unittest.skipUnless(
    CAN_UNREADABLE_DIR, "需要 chmod(0) 讓目錄不可讀（Windows 不支援；root 亦無效）")
needs_unlink_open = unittest.skipUnless(
    CAN_UNLINK_OPEN, "需要刪除開啟中的檔（Windows WinError 32 不允許）")
needs_backslash_name = unittest.skipUnless(
    CAN_BACKSLASH_NAME, "需要含反斜線的檔名（Windows 視為路徑分隔、非法）")
needs_control_char_name = unittest.skipUnless(
    CAN_CONTROL_CHAR_NAME, "需要含控制字元的檔名（Windows 非法）")
needs_dead_pid_detection = unittest.skipUnless(
    CAN_DETECT_DEAD_PID,
    "需要能判定 PID 已死（POSIX 用 os.kill、Windows 用 ctypes OpenProcess；環境不可判時才 skip）")
needs_junction = unittest.skipUnless(
    CAN_JUNCTION, "需要建立 directory junction（Windows mklink /J；POSIX 無此概念）")
