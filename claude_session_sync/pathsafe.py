"""pathsafe：信任根逃逸防線（reparse/symlink）——供所有「iterdir 專案夾候選」與「plan-dir 消費者」共用。

單一真相源，杜絕各自實作漂移（端到端整合審連續三輪抓到 build_plan/transfer/bootstrap/doctor/merge/resolve/
anomaly 各處漏檢的成因）。**leaf 模組、零專案相依**——故 anomaly 這種被 scan 依賴的 leaf 也能 import（避免循環）。

政策：專案夾（`<root>/<name>`）須非 symlink 且 resolve 後仍在 root 內（resolve-then-contain）。root 內的 junction
（ccdir 多帳號在同機刻意共用）透明允許（resolve 後仍在 root 內）；symlink 或逃出 root 的 reparse 一律拒。與
`memory.reparse_kind`（memory/ 夾**跟隨** junction 的另一情境）不同層、各自合理。
"""
from __future__ import annotations

import os
import stat
import unicodedata
from pathlib import Path


def name_key(name: str) -> str:
    """檔名/識別名的 **caseless + Unicode 正規化** 比對鍵（e2e gate7 casefold + gate8 NFC/NFD）：先 NFC（統一分解形）、
    `casefold()`（統一大小寫）、再 NFC（casefold 可能反正規化）。使「僅大小寫、僅正規化形、或兩者皆異」的名字映到
    同鍵。ASCII 名字僅小寫化。**全 codebase 單一正規化真相源**——放在 leaf 的 pathsafe，故 `scan`（re-export 為
    `scan._name_key`，既有呼叫端不變）、被 scan 依賴的 `anomaly`、以及 `memory` 皆可 import（免循環、免各自實作漂移）。"""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


_WIN_RESERVED_STEMS = {"CON", "PRN", "AUX", "NUL",
                       *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_leaf_name(name: str) -> bool:
    """跨 OS 安全**單一夾名**——`--map` 目標這類「將被 mkdir／當 state key」的名字用（bootstrap/doctor/transfer
    共用單一真相源，codex mcwd-r1 F3）。除既有的「非空、單一 component、非 . / ..、非絕對路徑」外，再擋：

    - Windows 不合法字元（`<>:\"/\\|?*` 與控制字元）：`a:b` 在 POSIX 合法但 hub 常在 exFAT/NTFS 外接碟 → 跨 OS 炸；
    - **尾隨 `.` / 空白**：Win32 路徑正規化會**悄悄剝掉**（`proj.` 實際開/建 `proj`）→ state key 與磁碟名脫鉤、
      且 `name_key` 撞名檢查看不出（`proj.` ≠ `proj`）＝別名繞過；
    - 保留裝置名（CON/PRN/AUX/NUL/COM1-9/LPT1-9，含任何副檔名形式如 `CON.txt`）：mkdir 到 apply 才 OSError。

    POSIX 上這些名字雖合法，跨 OS 同步工具的**待建名**採交集最嚴（fail-closed）；既存夾（不經 mkdir）不受此限。"""
    if not name or name != Path(name).name or name in (".", ".."):
        return False
    if any(c in '<>:"/\\|?*' or ord(c) < 32 for c in name):
        return False
    if name[-1] in (".", " "):
        return False
    if name.split(".", 1)[0].rstrip(" ").upper() in _WIN_RESERVED_STEMS:
        return False
    return True


def physical_dup_key(p: str | Path) -> str:
    """dup 偵測用的**實體** canonical 鍵：resolve（跟隨 junction；不存在 → 非嚴格）後之父路徑＋`name_key`
    摺疊葉名。root 內 junction 別名（`Alias`→`Hub`）與 casefold/NFC 孿生（`Hub`/`hub`）在此鍵下同一
    （codex mcwd-g4 #1：Path/名字 exact 比對看不出同一實體夾 → 「多 local 配一 hub」防線被 junction 別名
    繞過 → 空舊夾 false tombstone）。resolve 失敗 → 退回原字串（該夾另由 `safe_project_dir`/掃描擋下）。
    bootstrap key_dups / scan build_plan dup guard / transfer `_rkey` 共用（單一真相源）。"""
    try:
        rp = Path(p).resolve()
    except OSError:
        return str(p)
    return f"{rp.parent}\x00{name_key(rp.name)}"


def is_reparse(p: str | Path) -> bool:
    """`p` 的**最終元件**是否為 reparse point（symlink／Windows junction 等），**不跟隨**。跨 OS：POSIX `S_ISLNK`；
    Windows `st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT`（涵蓋 symlink＋junction，因 Windows 無 `O_NOFOLLOW`、
    `is_symlink()` 對 junction 回 False）。缺檔／lstat 失敗 → False（不存在非 reparse；呼叫端另以「缺檔」語意處理）。
    供 leaf 檔（如 A15 `acks.json`、索引）在讀取前 fail-closed 擋掉被 redirect 的別名（與 `apply._read_index_bytes_nofollow`
    同一套 lstat 檢查，抽成共用 leaf 予免重複實作漂移）。"""
    try:
        st = os.lstat(p)
    except OSError:
        return False
    return bool(stat.S_ISLNK(st.st_mode)
                or (getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))


def within_root(root: str | Path, p: str | Path) -> bool:
    """`p` 解析（跟隨 symlink/junction）後是否落在 `root` 內。擋 reparse 把路徑導出信任根（讀/寫到 root 外）。
    不存在的 `p`（如 push --map 待建夾）以非嚴格 resolve 判其字面路徑仍在 root 內。resolve 失敗 → fail-closed False。"""
    try:
        rr, rp = Path(root).resolve(), Path(p).resolve()
    except OSError:
        return False
    return rp == rr or rr in rp.parents


def safe_project_dir(root: str | Path, d: str | Path) -> bool:
    """專案夾（hub/local/remote 側）是否安全在 `root` 內：**非 symlink** 且解析後（跟隨 junction）落在 root 內。
    擋兩類逃逸——① symlink 專案夾（可跨裝置/特殊檔）；② junction/reparse 指向 root **外**（resolve-then-contain）。
    root **內**的 junction 透明允許（resolve 後仍在 root 內）。"""
    if Path(d).is_symlink():
        return False
    return within_root(root, d)


def dir_scannable(d: str | Path | None) -> bool:
    """`d` 能否列舉（`iterdir` 不 raise）。供 fail-closed 判定：`glob`/`Path.glob` 對**存在但不可讀**（POSIX
    read-denied）的目錄會 **fail-open**（吞 PermissionError → 回空）→ 上層可能把「不可讀」誤當「空/全刪」而寫
    抑制 tombstone／錯基線／復活已刪（違反 A3）。呼叫端據此在寫入或信任「無 tombstone/無檔」前擋掉不可列舉夾
    （e2e gate9/10/11 的 read-denied-dir class）。不存在（含 None）→ True（真的沒有、非不可讀，由既有「空」語意處理）；
    存在但 `iterdir`/`stat` raise → False（fail-closed）。**leaf**（僅 pathlib），故 scan/tombstone/transfer 共用免循環。"""
    if d is None:
        return True
    try:
        p = Path(d)
        if not p.exists():
            return True
        for _ in p.iterdir():
            break
        return True
    except OSError:
        return False


def list_project_dirs(root: str | Path) -> tuple[list[Path], list[Path]]:
    """列 `root` 下的專案子目錄，依 `safe_project_dir` 分 (safe, unsafe)：safe=在 root 內的普通夾／root 內 junction；
    unsafe=symlink 或逃出 root 的 reparse。**所有**「iterdir 專案夾候選清單」都該經此過濾。root 不存在 → ([], [])。
    unsafe 夾**不讀其內容**（連 sidecar/cwd 都不碰）→ 一併堵住界外讀。"""
    root = Path(root)
    if not root.exists():
        return [], []
    safe: list[Path] = []
    unsafe: list[Path] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        (safe if safe_project_dir(root, d) else unsafe).append(d)
    return safe, unsafe
