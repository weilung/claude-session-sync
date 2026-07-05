"""merge：memory 衝突的**安全保留兩版**（approach A）+ `memory-merge` 提示詞產生器。

依據 DESIGN §7.1 / §7.3 / §9 + PLAN §2.9（memory 列）+ HANDOFF「先 A」：
  - **偵測**三類 memory 衝突（沿用 `scan.build_plan` 的 memory 計畫）：
      `conflict-content`（同檔名兩側內容不同）/ `conflict-cross-file-identity`（同 frontmatter `name`
      落多檔名）/ `conflict-delete-vs-update`（一方刪除、另一方改過）。
  - **安全保留兩版到 `memory/` 之外**（`$XDG_CACHE_HOME/claude-session-sync/merge/`，DESIGN §7.1）：暫存區**不在**
      `~/.claude/projects/<proj>/memory/`、**不在** hub → `list_memory_files` 掃不到、不會被當新 memory 同步擴散
      （DoD §14：`.merge` 不外洩）。**只讀**正式 memory、**絕不**寫回 `memory/`（A3/§7.3：暫存清理由工具負責、
      不授權 AI 刪；合併寫回交使用者）。
  - **`memory-merge` 提示詞產生器**：把兩版包成給 Claude 的合併提示詞。⚠ **明文外洩警告**（§7.3）——把兩版貼進
      Claude 對話，prompt 會進 session JSONL → 下次 sync 同步到 hub ＝ 敏感資訊從 memory 擴散進 transcript。故本
      工具**只**輸出到 stdout 或本機暫存（皆不同步）、**不自動餵 Claude**，並支援使用者先刪減敏感段（編輯暫存檔）。

**獨立指令、不併進例行 `sync`**（memory 的衝突/AI/洩漏流程不混進 session 同步）。真正 AI 合併 + 模糊近似比對留 P2。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_from_bytes

from . import atomicio, memory, scan, tombstone
from .state import State

# 三類「需 memory-merge 處理」的衝突動作（皆非自動套用；apply 對它們只回報）。其餘 blocked-*/suppressed/local-deleted
# 是 fail-closed 或刪除閘，不在此（它們不是「兩版待合併」而是「不確定/已決」）。
CONFLICT_ACTIONS = frozenset({
    "conflict-content", "conflict-cross-file-identity", "conflict-delete-vs-update",
})

# fuzzy（P2 Block B）：「同事實、不同檔名」的**模糊近似**候選，經使用者**明確放行**後才轉成的衝突類型。
# **刻意不列入 CONFLICT_ACTIONS**（cardinal：fuzzy 分數永不進 classify/apply/sync/nudge——那些只認 CONFLICT_ACTIONS；
# `conflicts_from_plan` 只擷取 plan 動作，而 `classify_memory` 永遠不產 FUZZY_KIND）。FUZZY_KIND 的衝突**只**由
# `cli._cmd_memory_merge_fuzzy` 在 `--stage/--interactive` 放行後於記憶體建構、餵給 `stage_conflict`（與一般衝突共用
# 同一條 leak-safe 暫存路徑）；plan/apply 這條路產不出它 → 結構上不可能自動保留/合併任何未放行的候選。
FUZZY_KIND = "conflict-fuzzy-identity"

META_FILE = "CONFLICT.json"   # 暫存區每個衝突夾的 provenance/中繼資料（非 memory，不同步）
PROMPT_FILE = "PROMPT.md"     # 產生的合併提示詞（本機暫存、不同步）
DONE_FILE = ".done"           # 完成標記（**最後**寫）；缺它＝上次中途失敗的殘缺暫存（codex gate F3）
SCHEMA_VERSION = 1

_KIND_ZH = {
    "conflict-content": "同檔名兩側內容不同",
    "conflict-cross-file-identity": "同一 frontmatter name 出現在多個檔名",
    "conflict-delete-vs-update": "一方刪除、另一方更新",
    FUZZY_KIND: "疑似同一事實（模糊近似、不同檔名；使用者放行）",
}

# 明文外洩警告（§7.3）：CLI 與 PROMPT.md 共用同一段，確保使用者每次都看到。
LEAK_WARNING = (
    "⚠ 明文外洩警告：memory 是明文。若把下列內容貼進 Claude 對話來合併，該 prompt 會被寫進 session JSONL，\n"
    "  下次 `sync` 就連同同步到 hub ＝ 原本只在 memory 的敏感資訊擴散進 transcript（且 transcript 難以事後清除）。\n"
    "  → 本工具只把兩版保留到本機快取（memory/ 之外、不同步）或印到 stdout；**絕不自動餵給 Claude**。\n"
    "  → 合併前請自行刪減敏感段落（直接編輯暫存檔），並考慮在**不會被同步的拋棄式專案**裡進行合併。"
)


# ── 暫存區（memory/ 之外）─────────────────────────────────────────────────────

def merge_root() -> Path:
    """衝突暫存根＝`$XDG_CACHE_HOME/claude-session-sync/merge`（無 XDG → `~/.cache/...`，DESIGN §7.1）。
    刻意放 `memory/` 與 hub **之外**：`list_memory_files` 掃不到 → 不會被當新 memory 同步擴散（DoD：`.merge` 不外洩）。"""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "claude-session-sync" / "merge"


_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_COMPONENT = 200   # 保守 < NAME_MAX（常見 255 bytes）；percent-encode 後純 ASCII → len(out)==byte 數


def _disp(s: str | None) -> str:
    """顯示用字串淨化：剔控制字元（含換行/CR/tab，POSIX 檔名可含 → 否則破壞單行/markdown 結構）+ 中和 lone
    surrogate（非 UTF-8 檔名經 surrogateescape 解出 → 否則 `.encode("utf-8")` 在寫 PROMPT.md/CONFLICT.json 時
    raise UnicodeEncodeError、令 memory-merge 崩潰，對稱 memory._index_title_text/Block 3c surrogate 洞）。**只用於
    顯示/中繼**——保留的原始 bytes（staged 版本檔）永遠原樣、不淨化。"""
    if not s:
        return s or ""
    return _CTRL_RE.sub("", s).encode("utf-8", "replace").decode("utf-8")


def _key_disp(key: str | None) -> str:
    """顯示用的衝突鍵。fuzzy 的 `key` 內部以 NUL 接兩檔名（`a\\x00b`，NUL 是唯一保證不出現在檔名的分隔符 → 分割
    單射）；顯示時把 NUL 換成「 ↔ 」再過 `_disp`（一般 key 無 NUL、不受影響）——否則 `_disp` 逕自剔除控制字元會把
    兩檔名黏成一串。"""
    return _disp((key or "").replace("\x00", " ↔ "))


def _fence_for(text: str) -> str:
    """為內容選一條夠長的 code fence（≥4 個 `，且比內容中最長的連續 ` 還長 1）——memory 正文常含 ``` code
    block，固定長度 fence 會被內容中的 ``` 提前關閉。CommonMark：關閉 fence 長度須 ≥ 開啟。"""
    longest = cur = 0
    for ch in text:
        cur = cur + 1 if ch == "`" else 0
        longest = max(longest, cur)
    return "`" * max(4, longest + 1)


def _safe_component(s: str) -> str:
    """把字串**injective**（一對一、可逆）轉成單一 FS-safe 路徑元件。percent-encode `os.fsencode(s)`（同
    `memory._index_link_target`：RFC3986 unreserved `A-Za-z0-9-._~` 保留 → slug/檔名原樣可讀；空白/`/`/`:`/`?`/
    `+`/控制字元/非 ASCII → %XX）。**必須 injective**（codex R1 High）：`_` 取代式是 many-to-one——`a:b.md` 與
    `a?b.md` 會撞同名，令第二個衝突被誤判 already-staged、或同組版本檔互蓋而靜默丟失。`os.fsencode` 還原非 UTF-8
    檔名的原始 bytes（surrogateescape）再逐 byte 編碼，永不破壞路徑或 raise。`.`/`..`/空（percent-encode 不碰 `.`）
    仍須擋路徑穿越 → 前綴 `_`（仍 injective：真實元件不會以 `_` 前綴恰好等於 `_.`/`_..`）。"""
    out = quote_from_bytes(os.fsencode(s), safe="")
    if not out:
        out = "%"                          # 空輸入 sentinel（不應發生；"%" 非 quote 正常輸出 → 仍 injective）
    elif set(out) <= {"."}:                 # ".", ".." → FS-special：逐點轉 %2E（"%2E" 非 quote 正常輸出 → injective）
        out = out.replace(".", "%2E")
    if len(out) > _MAX_COMPONENT:
        # percent-encode 對非 ASCII 檔名最多膨脹 3x → **合法** memory 檔名也可能超過 FS NAME_MAX、令
        # atomic_create「file name too long」失敗而無法保留（codex R2 Medium）。截斷可讀前綴 + 全名 sha1
        # （bounded 且仍 injective——不同原名 digest 不同；碰撞＝sha1 部分碰撞，可忽略，同 codebase hash 信任）。
        # 原始檔名完整存於 CONFLICT.json（`_disp(v.filename)`），故截斷夾名不需可逆。
        digest = hashlib.sha1(os.fsencode(s)).hexdigest()[:20]
        out = out[: _MAX_COMPONENT - len(digest) - 1] + "~" + digest
    return out


def staging_dir(root: Path, conflict: "MemoryConflict") -> Path:
    """某衝突的暫存夾（各段 sanitized）。content/delete-vs-update 用**檔名**鍵 → `<root>/<pk>/<filename>`；
    cross-file-identity 用 **frontmatter identity** 鍵（slug，可能長得像檔名如 `notes.md`）→ 放獨立 `by-name/`
    子層 `<root>/<pk>/by-name/<identity>`。**分開命名空間**杜絕「identity=="x.md" 撞檔名 "x.md"」令兩個不同
    衝突共用同夾 → 第二個被當 already-staged 而靜默丟失（自審補洞）。"""
    pk = _safe_component(conflict.project_key)
    if conflict.kind == FUZZY_KIND:
        # fuzzy 候選鍵＝**一對**檔名（`a\x00b`，a≤b）→ 兩檔名各自 sanitize 成**獨立路徑段**放 `fuzzy/` 命名空間
        # （`<root>/<pk>/fuzzy/<safe(a)>/<safe(b)>`）。兩層而非單層 join 才對 (a,b) **單射**（FS 分隔符保證分段，
        # 單層 join 會因 `_safe_component` 保留 `_`/`.` 而可能撞名）。`.md` 保證檔名 sanitize 後含 `.` → 與字面
        # `fuzzy`/`by-name` 命名空間永不撞（memory 檔名一律 `*.md`）。
        a, _, b = conflict.key.partition("\x00")
        return Path(root) / pk / "fuzzy" / _safe_component(a) / _safe_component(b)
    leaf = _safe_component(conflict.key)
    if conflict.kind == "conflict-cross-file-identity":
        return Path(root) / pk / "by-name" / leaf
    return Path(root) / pk / leaf


def _norm_parts(p: Path) -> list[str]:
    """路徑各段的 **caseless + Unicode 正規化** 比對鍵（復用 `scan._name_key`＝NFC∘casefold∘NFC）。

    供 case-/normalization-insensitive 的包含判定：macOS 預設 APFS **大小寫不敏感**、且對檔名做 Unicode 正規化，
    但 `PosixPath` 比對大小寫/正規化**敏感**、`resolve()` 又保留輸入拼寫 → 同一實體的不同拼寫（僅大小寫、或
    NFC/NFD、或兩者）在 `==`/`is_relative_to` 下看似互不包含 → 暫存根「其實在 hub 內」卻漏判 → 明文兩版落進
    同步區外洩（mmfrom-g4 High）。逐段正規化後前綴比對可認出同一實體；case-sensitive FS 上「僅拼寫不同的相異
    夾」會被多判重疊（fail-closed，只多拒不外洩，與 cardinal DoD 同向）。與 memory 檔名別名判定共用同一正規化真相源。"""
    return [scan._name_key(part) for part in Path(p).parts]


def _paths_overlap_ci(a: Path, b: Path) -> bool:
    """a、b 任一等於或在另一之下（**caseless + 正規化不敏感**、逐段前綴）。輸入須已 `resolve()`（絕對、正規化）。

    等價於 `a==b or a.is_relative_to(b) or b.is_relative_to(a)`，但比對走 `_norm_parts`（見其 docstring 的 mmfrom-g4
    理由）：對正規化後的兩段串，較短者為較長者前綴 ⟺ 兩路徑存在包含關係。"""
    ap, bp = _norm_parts(a), _norm_parts(b)
    n = min(len(ap), len(bp))
    return ap[:n] == bp[:n]


def unsafe_staging_root(root: Path, forbidden: list[Path]) -> str | None:
    """暫存根是否**不安全**（會破壞「memory/ 之外、不外洩」鐵則，codex R1 High）。回不安全原因或 None（安全）。

    `XDG_CACHE_HOME` 是使用者環境變數、不可盲信：相對路徑（落在當前 cwd、不可預期）、或位於 hub / local 根
    **之內**（→ 寫進受同步區 → 兩版被當新 memory 擴散）皆須 fail-closed 拒絕。`forbidden`＝[hub, local_root]
    （memory 夾恆在這兩根之下，故涵蓋所有 memory/ 樹）。兩邊都 `resolve()`（跟隨 symlink → 擋「root 被 symlink
    進 hub」）後比對；root==forbidden 或 root 在 forbidden 之下 → 不安全。resolve 失敗 → fail-closed 視為不安全。"""
    if not Path(root).is_absolute():
        return f"暫存根非絕對路徑（XDG_CACHE_HOME 相對路徑不可預期）：{root}"
    try:
        rr = Path(root).resolve()
    except OSError as e:
        return f"暫存根無法解析（保守視為不安全）：{e}"
    for f in forbidden:
        try:
            fr = Path(f).resolve()
        except OSError as e:
            # **fail-closed**（不 continue）：解析不了某受同步樹就無法**證明**暫存不在其內 → 保守拒絕（mmfrom-g3
            # High）。非存在/離線路徑走 resolve(strict=False) 不 raise（僅 symlink-loop/權限/exotic FS 才 raise）→
            # 尋常未掛載 remote 不會誤拒；真的 raise＝FS 異常，寧可拒也不放行可能重疊的寫入。
            return f"受同步區路徑無法解析（{f}）→ 無法證明暫存不重疊，保守拒絕：{e}"
        # **雙向**重疊都拒（gate7 F1 High）：root 在 hub/local 內 → 暫存直接落同步區；**或** hub/local 在 root
        # **內** → 某 per-conflict dest（`<root>/<pk>/<key>`）可能正好落進 hub/local（如 hub==root/projA）→ 一樣外洩。
        # 數學上：若 root 與 forbidden 互不包含，則 root 底下任何 dest 都不可能在 forbidden 內（共同祖先須是其一）。
        # 比對走 **caseless + 正規化不敏感**（mmfrom-g4 High）：macOS 預設 APFS 大小寫不敏感 + resolve 保留拼寫 →
        # `is_relative_to` 大小寫敏感會漏判「同一實體不同拼寫」的重疊 → 暫存落進 hub 外洩。見 `_paths_overlap_ci`。
        if _paths_overlap_ci(rr, fr):
            return (f"暫存根與受同步區（{fr}）重疊 → 兩版可能落進同步區被當新 memory 擴散；"
                    "請把 XDG_CACHE_HOME 設到 hub/local 之外（互不包含）。")
    return None


# ── 資料模型 ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ConflictVersion:
    """衝突中的一個版本。真實 memory 版本帶 `data`/`text`（已**一次讀入**，避免 stage 時 re-read 的 TOCTOU 與
    hash 漂移）；`is_tombstone` 版本只帶刪除標記中繼（無檔內容）。`label`＝來源側（`local`/`hub`/`local+hub`，
    或 `tombstone`）。"""

    label: str
    filename: str
    content_hash: str | None
    text: str | None = None
    data: bytes | None = None
    is_tombstone: bool = False
    base_hash: str | None = None
    identity: str | None = None
    machine: str | None = None
    time: str | None = None


@dataclass(frozen=True)
class MemoryConflict:
    project_key: str
    kind: str                      # CONFLICT_ACTIONS 之一
    key: str                       # 暫存夾名基底（檔名或 identity）
    versions: tuple[ConflictVersion, ...]
    reason: str
    notes: tuple[str, ...] = ()    # 退化警告（plan 後某側讀不到→保留不完整，codex gate F2）；非空 → CLI 非零提醒重跑。

    def staged_versions(self) -> list[ConflictVersion]:
        """有實體內容、會落成暫存檔的版本（排除 tombstone 標記與讀不到內容者）。"""
        return [v for v in self.versions if not v.is_tombstone and v.data is not None]


@dataclass
class StageResult:
    conflict: MemoryConflict
    dest: Path
    status: str                    # would-stage | staged | already-staged | degraded | incomplete | stale | error | empty
    files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ── 讀取（no-follow，只讀正式 memory）──────────────────────────────────────────

def _read_nofollow(mdir: Path, filename: str) -> bytes | None:
    """讀 `mdir/filename` 的 bytes，**不跟隨 symlink、不卡在 FIFO/device**（對稱 apply 的索引讀）。

    **父夾 + 最終元件雙重 no-follow**（codex R1 Medium + gate2 High）：O_NOFOLLOW 只擋**最終**元件，且
    **Windows 無 O_NOFOLLOW**（`getattr(...,0)`＝0）→ 完全不擋。故對**父夾與最終檔都先 `is_symlink` 明確
    lstat**（no-throw，cross-OS；ENOENT/權限/ELOOP 一律當不安全 → None），再開最終元件加 O_NOFOLLOW（POSIX
    再保險）+ O_NONBLOCK + fstat `S_ISREG`。否則 `build_plan` 後某側檔/`memory/` 根被換成 symlink → 跟隨讀到
    夾外檔 → 複製進快取/提示詞外洩（Windows 尤其，因 O_NOFOLLOW 失效）。**有界殘留**：lstat 與 os.open 間的
    µs 窗（同 Block 3c 父夾 symlink，受非對抗模型約束、不上 POSIX-only dir_fd；此處只讀進本機快取、危害更小）。
    缺檔/讀錯/非普通檔/symlink 一律 None（呼叫端略過該版本、記 note；保留是便利性、不為它崩）。"""
    p = Path(mdir) / filename
    try:
        if Path(mdir).is_symlink() or p.is_symlink():   # 父夾或最終檔為 symlink → fail-closed（cross-OS lstat）
            return None
    except OSError:
        return None
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    try:
        fd = os.open(p, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def _collect_file_versions_sides(filename: str,
                                 sides: list[tuple[str, Path | None]]) -> list[ConflictVersion]:
    """讀某檔名在**給定各側**的內容，**依正規化 content_hash 去重**（不同側位元雖異但正規化相同 → 合成單一
    `a+b` 版本，避免假兩版）。damaged（hash=None）每側獨立保留（無法證明相同）。每側只讀一次；`mdir=None` 的側
    → 跳過（不讀、不崩）。`sides`＝`[(label, mdir), ...]`（label 進版本 label）；session/cross-file 衝突固定
    local+hub 兩側，fuzzy 依「**列出時該檔實際出現的側**」限定（見 `fuzzy_conflict`）。"""
    by_hash: dict[str, list] = {}    # hash_key -> [data, text, [sides], content_hash]
    order: list[str] = []
    for label, mdir in sides:
        if mdir is None:
            continue
        data = _read_nofollow(mdir, filename)
        if data is None:
            continue
        doc = memory.load_memory_bytes(data)
        h = memory.content_hash(doc)
        key = h if h is not None else f"\x00damaged:{label}"  # damaged 不合併、每側獨立
        if key in by_hash:
            by_hash[key][2].append(label)
        else:
            by_hash[key] = [data, doc.text, [label], h]
            order.append(key)
    out: list[ConflictVersion] = []
    for key in order:
        data, text, side_lbls, h = by_hash[key]
        out.append(ConflictVersion(label="+".join(side_lbls), filename=filename,
                                   content_hash=h, text=text, data=data))
    return out


def _collect_file_versions(filename: str, local_mdir: Path | None,
                           hub_mdir: Path | None) -> list[ConflictVersion]:
    """讀某檔名在 local/hub 兩側（session/cross-file 衝突用；`conflicts_from_plan`/`_cross_file_conflicts` 呼叫）。
    薄包裝 `_collect_file_versions_sides`（單一讀取/去重真相源；行為與改動前逐位元組一致）。"""
    return _collect_file_versions_sides(filename, [("local", local_mdir), ("hub", hub_mdir)])


def _stage_safe_mdir(mdir: Path | None) -> Path | None:
    """stage-time TOCTOU 重驗（e2e gate2 #3 同類；R1 High）：list→stage 間，memory/ **之上的專案夾**可能被換成
    symlink/junction **逃逸信任根** → 經 memory/ 讀到界外 bytes（`_read_nofollow` 只守 memory/ 夾與 leaf、**不**守其上
    的專案夾；junction 在 Windows 非 symlink → `mdir` 自身 lstat 也認不出）→ 界外明文被保留進快取/PROMPT ＝ 外洩。
    故讀前重驗**專案夾**（＝mdir 父夾，root＝再上一層）：逃逸 → 回 None（該側視為缺 → 讀不到 → degraded note）。與
    `conflicts_from_plan` 讀前 `_safe_project_dir` 重驗兩側專案夾**對稱**（單一真相源 `scan._safe_project_dir`）。

    **範圍界定（重要）**：本檢查守的是**專案夾**逃逸。`memory/` 夾本身若是 **directory junction** 則**刻意跟隨**
    （`_read_nofollow` 只擋 symlink、不擋 junction）——這是既定的 CLAUDE_CONFIG_DIR/ccdir 政策（2026-06-30 使用者拍板）：
    memory/ junction＝使用者刻意的**同機多帳號共用**（方式1），`list_memory_files` 與現存 `conflicts_from_plan` 讀取
    路徑一律跟隨、與此對稱。故「掃描時真實 memory/ 於放行後被換成指向界外的 junction」屬 out-of-model 有界 TOCTOU
    殘留（持久 junction＝合法共用；換掉＝非對抗模型外），與 exact memory-merge 同立場，不在此另擋（否則會破壞方式1）。"""
    if mdir is None:
        return None
    proj = Path(mdir).parent
    return mdir if scan._safe_project_dir(proj.parent, proj) else None


def _revalidate_sides(sides: list[tuple[str, Path | None]]) -> list[tuple[str, Path | None]]:
    """讀前重驗每側**專案夾**（`_stage_safe_mdir`）；逃逸側其 mdir → None（→ 讀不到 → degraded），label 保留供診斷。"""
    return [(label, _stage_safe_mdir(mdir)) for label, mdir in sides]


def fuzzy_conflict(project_key: str, a: str, a_sides: list, b: str, b_sides: list,
                   *, reason: str) -> MemoryConflict:
    """把一對**使用者已放行**的模糊候選（兩個不同檔名，呼叫端保證 `a ≤ b`）讀成 `MemoryConflict`（kind=FUZZY_KIND）
    供 `stage_conflict` 保留兩版。**每檔只從其計分來源讀**（`a_sides`/`b_sides`＝`[(label, mdir), ...]`，CLI 綁**單一
    計分側**——見 `_run_fuzzy_stage`/`score_src`）——**不**回退/probe 另一側的同名檔（g2 High：否則放行後計分側被刪、
    另一側剛好有**無關**同名檔 → 靜默保留錯內容並標記完成＝靜默替換）。讀前重驗專案夾（`_revalidate_sides`→
    `_stage_safe_mdir`，防專案夾逃逸讀界外，R1 High）；同一條 no-follow leak-safe 讀；只讀正式 memory、絕不寫回。

    **只在使用者放行後呼叫**（`--stage` 全部 / `--interactive` 逐對）——本函式不做放行判斷（cardinal：放行是使用者
    的、不是分數的）。某檔於放行後其計分來源讀不到（刪除/改名/專案夾逃逸/來源缺）→ 退化 note → `stage_conflict` 不寫
    `.done`、CLI 非零、提示重跑（絕不靜默把「只剩一檔」或「別側無關同名檔」當完整）。key＝`a\\x00b`（見 `staging_dir`）。"""
    va = _collect_file_versions_sides(a, _revalidate_sides(a_sides))
    vb = _collect_file_versions_sides(b, _revalidate_sides(b_sides))
    missing = [fn for fn, vs in ((a, va), (b, vb)) if not vs]
    notes = ((f"以下檔於列出後讀不到、保留不完整（請重跑）：{', '.join(missing)}",) if missing else ())
    return MemoryConflict(project_key, FUZZY_KIND, f"{a}\x00{b}", tuple(va + vb), reason, notes)


def _both_side_identities(filename: str, local_mdir: Path, hub_mdir: Path) -> set[str]:
    """某檔名在**兩側**的 frontmatter `name` slug 集（兩側各取一次）。cross-file 歸組須看兩側——同檔名兩側
    name 可能不同（一側改名、一側未改），只看先讀到的一側會把該檔錯歸、漏掉真正的合併群（codex R1 Medium）。"""
    out: set[str] = set()
    for mdir in (local_mdir, hub_mdir):
        data = _read_nofollow(mdir, filename)
        if data is not None:
            nm = memory.load_memory_bytes(data).name
            if nm:
                out.add(nm)
    return out


def _tombstone_versions(name: str, hub_dir: Path,
                        versions: list[ConflictVersion]) -> list[ConflictVersion]:
    """delete-vs-update 的**所有**相關刪除標記版本（codex R1 Medium：不可只取第一個）。同檔名 memory tombstone
    + 所有 identity 命中現存版本 frontmatter name 的**別檔名** memory tombstone（多次換檔名刪除、tombstone 永不
    GC → 真實可能有多筆）。全部附上，否則提示詞會藏掉某些刪除/base hash → 使用者誤判。依 target 去重、排序求決定性。"""
    out: list[ConflictVersion] = []
    seen: set[str] = set()

    def _add(tb) -> None:
        if tb.target in seen:
            return
        seen.add(tb.target)
        out.append(ConflictVersion(label="tombstone", filename=tb.target, content_hash=None,
                                   is_tombstone=True, base_hash=tb.base_hash, identity=tb.identity,
                                   machine=tb.machine, time=tb.time))

    direct = tombstone.find_memory_tombstone(hub_dir, name)
    if direct is not None:
        _add(direct)
    idents = {nm for v in versions if v.data is not None
              for nm in [memory.load_memory_bytes(v.data).name] if nm}
    if idents:
        for (k, _t), tb in sorted(tombstone.read_tombstones(hub_dir).items()):
            if k == "memory" and tb.identity in idents and tb.target != name:
                _add(tb)
    return out


def _cross_file_conflicts(pk: str, entries: list, local_mdir: Path,
                          hub_mdir: Path) -> list[MemoryConflict]:
    """把 cross-file-identity 的逐檔 plan 條目歸組成衝突（同一事實拆成多檔 → 一起合併）。

    **連通分量歸組**（codex R1 Medium）：以「檔名」為節點、共享任一 frontmatter `name`（兩側皆計）為邊，做
    union-find；每個連通分量＝一個衝突。能正確處理「a.md 在 local 是 xname、在 hub 是 yname，b.md 是 yname」
    這種跨側分歧——a.md 與 b.md 應同組（只看單側會把 yname 的合併群拆散、漏檔）。identity 全不可判的檔自成
    一組（以檔名為鍵）。"""
    ids: dict[str, set[str]] = {m.name: _both_side_identities(m.name, local_mdir, hub_mdir)
                                for m in entries}
    reason = {m.name: m.reason for m in entries}
    parent: dict[str, str] = {fn: fn for fn in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_ident: dict[str, str] = {}
    for fn, fids in ids.items():
        for i in fids:
            if i in by_ident:
                parent[find(fn)] = find(by_ident[i])
            else:
                by_ident[i] = fn
    comps: dict[str, list[str]] = {}
    for fn in ids:
        comps.setdefault(find(fn), []).append(fn)
    # **專案級退化偵測**（gate3 F1 + gate4 F1，silent-loss 防護）。merge 由**現況**重建分組，可能與 plan 的 T0
    # 快照分歧；分歧時倖存成員的組會看似「完整」被寫 `.done` → 下次 already-staged 卡住缺版本。兩個 plan 不變量
    # 用來**偵測分歧**（無須把整組塞進 MemoryPlan）：
    #   ① plan 只在某成員「規劃時可讀」才標 cross-file → 此刻讀不到（`missing`）＝分歧；
    #   ② plan 只在某檔 frontmatter name **與別檔共享**（≥2 檔）才標 cross-file → 故每個 cross-file 成員此刻
    #      **至少應與另一檔同組**；若任一連通分量是**單檔**（singleton）＝其同名夥伴的 name 已改/消失（gate4 F1，
    #      改名而非讀不到的分歧），分組已裂。
    # 任一分歧 → **整個專案的 cross-file 組全部退化**（無法得知裂出成員原屬哪組）：不寫 .done、CLI 非零、提示重跑；
    # 下次現況與 plan 一致（成員可讀、分組復原）才落 .done。
    collected: list[tuple[str, list[str], list[ConflictVersion], list[str]]] = []
    project_diverged = False
    for fnames in comps.values():
        fnames = sorted(fnames)
        comp_ids = sorted({i for fn in fnames for i in ids[fn]})
        key = comp_ids[0] if comp_ids else fnames[0]   # identity 全不可判 → 退回檔名
        if len(fnames) < 2:                # singleton＝同名夥伴已改名/消失（plan 保證 ≥2）→ 分歧（gate4 F1）
            project_diverged = True
        versions: list[ConflictVersion] = []
        missing: list[str] = []
        for fn in fnames:
            vs = _collect_file_versions(fn, local_mdir, hub_mdir)
            if not vs:
                missing.append(fn)         # 規劃時在、此刻讀不到（symlink 抽換/刪除）→ 分歧（gate3 F1）
                project_diverged = True
            versions.extend(vs)
        collected.append((key, fnames, versions, missing))
    out: list[MemoryConflict] = []
    for key, fnames, versions, missing in collected:
        notes: tuple[str, ...] = ()
        if missing:
            notes = (f"以下檔於規劃後讀不到、保留不完整（請重跑 sync）：{', '.join(missing)}",)
        elif project_diverged:
            notes = ("本專案跨檔分組於規劃後分歧（成員讀不到或已改名）、分組可能不完整（請重跑 sync）",)
        if versions or notes:              # 有版本或有退化警告都 emit——**絕不靜默丟**（gate F1/F2）
            out.append(MemoryConflict(pk, "conflict-cross-file-identity", key, tuple(versions),
                                      reason[fnames[0]], notes))
    return sorted(out, key=lambda c: c.key)


# ── 偵測 ─────────────────────────────────────────────────────────────────────

def conflicts_from_plan(plan: scan.SyncPlan, *, project: str | None = None) -> list[MemoryConflict]:
    """從**已建好的** SyncPlan 抽 memory 衝突（純擷取、不重建 plan）。CLI 先 build_plan → 檢查 halt（掛錯碟等
    異常 surface），再呼叫本函式；測試走 `find_conflicts` 便利包裝。只看**兩側皆綁定**的專案——衝突（content/
    cross-file/delete-vs-update）唯有 hub+local 都在時才產生（單邊是 copy/blocked，不是衝突）。"""
    out: list[MemoryConflict] = []
    for pp in plan.projects:
        if not (pp.local_dir and pp.hub_dir):
            continue
        pk = Path(pp.hub_dir).name
        if project is not None and pk != project:
            continue
        # 逃逸重驗（TOCTOU：build_plan 後專案夾被換成逃逸 junction）→ 不從界外讀 memory 進暫存/prompt（e2e gate2 #3）。
        # `_read_nofollow` 只守 memory/ 夾與最終檔、**不**守其上的專案夾 junction，故在此重驗專案夾（單一真相源）。
        ldir, hdir = Path(pp.local_dir), Path(pp.hub_dir)
        if not scan._safe_project_dir(ldir.parent, ldir) or not scan._safe_project_dir(hdir.parent, hdir):
            continue
        conf = [m for m in pp.memories if m.action in CONFLICT_ACTIONS]
        if not conf:
            continue
        local_mdir = memory.memory_dir(pp.local_dir)
        hub_mdir = memory.memory_dir(pp.hub_dir)
        cross = [m for m in conf if m.action == "conflict-cross-file-identity"]
        out.extend(_cross_file_conflicts(pk, cross, local_mdir, hub_mdir))
        for m in conf:
            if m.action == "conflict-cross-file-identity":
                continue
            versions = list(_collect_file_versions(m.name, local_mdir, hub_mdir))
            real = [v for v in versions if not v.is_tombstone]
            notes: list[str] = []
            if m.action == "conflict-delete-vs-update":
                tvs = _tombstone_versions(m.name, Path(pp.hub_dir), versions)
                versions.extend(tvs)
                if not real:   # delete-vs-update 規劃時必有現存版本；此刻讀不到 → 退化（gate F2）
                    notes.append("現存版本於規劃後讀不到、保留不完整（請重跑 sync）")
                if not tvs:    # delete-vs-update 規劃時必有 tombstone；此刻 re-discover 不到（現存檔改名/改寫令
                    #            identity 不再命中已刪 identity tombstone，gate4 F2）→ 刪除側會被靜默漏掉 → 退化、
                    #            不寫 .done、不靜默把「只剩現存內容」當完整（merge 由現況 re-discover、非信 plan 的殘留）。
                    notes.append("刪除標記於規劃後對不上（現存檔疑改名/改寫）、刪除側可能漏掉、保留不完整（請重跑 sync）")
            elif len(real) < 2:   # conflict-content 規劃時兩側皆在且相異；不足兩版 → 某側讀不到/已變（gate F2）
                notes.append("預期兩側內容、但某側於規劃後讀不到或已變、保留不完整（請重跑 sync）")
            # **絕不靜默丟**：即使 versions 空（全讀不到）也 emit（帶退化 note），由 stage 回 empty + 警告。
            out.append(MemoryConflict(pk, m.action, m.name, tuple(versions), m.reason, tuple(notes)))
    return out


def find_conflicts(local_root, hub_root, state: State | None, *, project: str | None = None,
                   identity_fn=None) -> list[MemoryConflict]:
    """便利包裝：build_plan + `conflicts_from_plan`（halt 時 plan.projects 為空 → 回 []；CLI 另行 surface halt）。"""
    plan = scan.build_plan(local_root, hub_root, state, identity_fn=identity_fn)
    return conflicts_from_plan(plan, project=project)


def unscannable_memory_projects(plan: scan.SyncPlan, *, project: str | None = None) -> list[str]:
    """memory **無法掃描**的兩側皆綁定專案（`memory/` 根 symlink/不可讀）。供 CLI surface——否則 memory 被跳過時
    memory-merge 會把「沒掃到」誤報成「無衝突」並回 0（gate2 F3）。回 `"<pk>（…）"` 字串清單。

    **以 plan 結構化旗標 `memory_scan_failed` 為準**（gate4 F3）：plan 在 T0 記錄了「這專案 memory 沒掃」，**不可**
    被此刻 FS recheck 成功（transient 失敗已恢復）抹掉——否則就用一份「漏掃 memory」的 plan 回報無衝突。recheck
    （`list_memory_files`）只是**補充**目前仍不可掃者，與旗標**聯集**。"""
    out: list[str] = []
    for pp in plan.projects:
        if not (pp.local_dir and pp.hub_dir):
            continue
        pk = Path(pp.hub_dir).name
        if project is not None and pk != project:
            continue
        if pp.memory_scan_failed:   # 信任 plan 的 T0 跳過事實（即使現在 recheck 會成功）
            out.append(f"{pk}（規劃時 memory 未掃描）")
        for side, d in (("local", pp.local_dir), ("hub", pp.hub_dir)):   # 補充：目前仍不可掃者
            if not scan._safe_project_dir(Path(d).parent, Path(d)):   # 專案夾逃逸 → 不讀界外（e2e gate2 #3）
                out.append(f"{pk}（{side}：專案夾為 symlink/逃逸信任根）")
                continue
            try:
                memory.list_memory_files(memory.memory_dir(d))
            except memory.UnsafeMemoryDir:
                out.append(f"{pk}（{side}：memory/ 根為 symlink）")
            except OSError as e:
                out.append(f"{pk}（{side}：memory 夾讀取失敗 {e.__class__.__name__}）")
    return out


# ── 保留兩版（暫存，approach A）────────────────────────────────────────────────

def planned_staged_names(conflict: MemoryConflict) -> list[str]:
    """預覽：此衝突會落成哪些暫存檔名（`<label>__<filename>`，sanitized）。"""
    return [_safe_component(f"{v.label}__{v.filename}") for v in conflict.staged_versions()]


def _conflict_fingerprint(conflict: MemoryConflict) -> str:
    """衝突**本質內容**的決定性指紋（project_key + kind + key + 各版本 (filename, 內容指紋) + 各 tombstone (target,
    base, identity)）。供 `.done` 暫存的**陳舊偵測**（gate5 F1）：同一檔名鍵的衝突會隨時間**換 kind 或換內容**（content
    →delete-vs-update、或兩側被改成新內容），若只看 .done 就回 already-staged，會用舊證據遮蓋新衝突。指紋變→stale。

    版本內容指紋：有正規化 `content_hash` 用它；**damaged（content_hash=None，如 delete-vs-update 的損壞現存側）
    退回 raw bytes 的 sha256**（gate6 F1）——否則兩段不同的損壞 bytes 都成 `None`、指紋不變 → 換內容卻誤判
    already-staged。排序**已解析的條目字串**（含 raw sha）求決定性。surrogatepass 容非 UTF-8 檔名不崩。"""
    items: list[str] = []
    for v in conflict.versions:
        if v.is_tombstone:
            items.append(f"T:{v.filename}:{v.base_hash}:{v.identity}")
        elif v.content_hash:
            items.append(f"V:{v.filename}:{v.content_hash}")
        elif v.data is not None:
            items.append(f"V:{v.filename}:raw:{hashlib.sha256(v.data).hexdigest()}")
        else:
            items.append(f"V:{v.filename}:None")
    # project_key 納入（e2e-g2）：暫存夾 <merge>/<pk>/… 理應 per-pk 隔離，但兩個大小寫/正規化折疊後相同的相異 pk 在
    # 不敏感的快取 FS 上會撞成同一實體夾 → 若指紋省 pk，不同專案同檔名/內容的衝突會誤判 already-staged 靜默略過（同一
    # pk 內 pk 為常量、same/stale 判定不變；跨 pk 撞夾時才生效區分）。fuzzy 端另有 pk 折疊護欄先擋，這是共用層縱深防禦。
    parts = [f"pk:{conflict.project_key}", f"kind:{conflict.kind}", f"key:{conflict.key}", *sorted(items)]
    return hashlib.sha256("\n".join(parts).encode("utf-8", "surrogatepass")).hexdigest()


def _conflict_meta(conflict: MemoryConflict, staged: dict[int, str]) -> dict:
    # 顯示/中繼欄位一律過 _disp（檔名可含控制字元/surrogate → 否則 json.dumps→encode 崩潰）。hash 為 hex、安全。
    versions = []
    for i, v in enumerate(conflict.versions):
        versions.append({
            "label": _disp(v.label), "filename": _disp(v.filename), "content_hash": v.content_hash,
            "is_tombstone": v.is_tombstone, "staged_file": staged.get(i),
            "base_hash": v.base_hash, "identity": _disp(v.identity),
            "machine": _disp(v.machine), "time": _disp(v.time),
        })
    return {
        "schema_version": SCHEMA_VERSION, "project_key": _disp(conflict.project_key),
        "kind": conflict.kind, "key": _key_disp(conflict.key), "reason": _disp(conflict.reason),
        "staged_time": tombstone.now_iso(),
        "fingerprint": _conflict_fingerprint(conflict),       # 陳舊偵測基準（gate5 F1）
        "complete": not conflict.notes,                       # 退化（某側讀不到）→ 不完整（gate2 F1）
        "notes": [_disp(n) for n in conflict.notes],          # 退化警告持久化進中繼（gate2 F1）
        "versions": versions,
    }


def _completed_match(dest: Path, conflict: MemoryConflict) -> str | None:
    """leaf 已完成（有 `.done`）時，比對暫存的 fingerprint 與**目前**衝突（gate5 F1）。回 `same`（同一衝突 →
    already-staged 幂等）/ `stale`（衝突已換 kind/內容 → 暫存證據過時，不可當已處理）/ None（無 `.done` → 未完成）。
    `.done` 在但 CONFLICT.json 讀不到/壞 → 保守當 `stale`（無法確認 → 不沿用舊暫存）。"""
    if not os.path.exists(atomicio.os_path(dest / DONE_FILE)):   # os_path：深 staging 路徑長路徑安全
        return None
    try:
        stored = json.loads(atomicio.read_text(dest / META_FILE)).get("fingerprint")
    except (OSError, ValueError):
        return "stale"
    return "same" if stored == _conflict_fingerprint(conflict) else "stale"


class UnsafeStagingPath(OSError):
    """暫存路徑中（root→dest 任一層）含 symlink/junction/reparse → 拒絕（防寫入被重導進 hub/memory，codex gate F1 + e2e Pass1）。"""


def _claim_staging_dir(root: Path, dest: Path) -> str:
    """**逐層 no-follow** 建立 `root→dest`，回 `claimed`（本次新建 leaf）/`already-staged`（leaf 已是真實夾）。
    任一層為 symlink/**junction**/reparse/非目錄 → `raise UnsafeStagingPath`（junction 在 Windows 非 symlink、
    須以 `reparse_kind` 才擋得到，e2e Pass1 High）。

    為何必要（codex gate F1 High）：`unsafe_staging_root` 只驗**根**；但 `<root>/<pk>/<key>` 的**每層子路徑**若有
    既存 **symlink**（如 `merge/<proj>` → hub/memory），`mkdir(parents=True)` + 後續寫入會**跟隨它**把兩版/PROMPT.md
    寫進受同步區 → 外洩。故 root 以下**逐層** `os.mkdir` + `lstat` 驗證皆真實目錄、非 symlink。`root` 本身及其祖先
    鏈已由 CLI `unsafe_staging_root` 的 `resolve()`（跟隨所有 symlink）驗在 hub/local 之外，故 root 用 `mkdir(parents)`
    建即可（不在此 no-follow 檢——允許使用者把整個 cache symlink 到別處的安全位置）。**有界殘留**：本函式驗完到
    `atomic_create` 寫入間的 µs 窗，dest 仍可能被換成 symlink（同 Block 3c 父夾 symlink；受非對抗模型約束、不上
    POSIX-only dir_fd）。"""
    root, dest = Path(root), Path(dest)
    # os_path：暫存路徑常 >260（<pk>/<key> 各 ~200 巢狀）→ Windows 走 \\?\ 繞過 MAX_PATH（reparse_kind 已內建）。
    os.makedirs(atomicio.os_path(root), exist_ok=True)   # root + 祖先（CLI 已 resolve 驗在 hub/local 之外）
    parts = dest.relative_to(root).parts
    cur = root
    claimed_leaf = False
    for i, part in enumerate(parts):
        cur = cur / part
        try:
            os.mkdir(atomicio.os_path(cur))    # 非 FileExistsError 的 OSError 自然向上拋（呼叫端轉 error）
            if i == len(parts) - 1:
                claimed_leaf = True
        except FileExistsError:
            pass
        # 拒 **任何** reparse point（symlink / **junction** / cloud / 未知）＋非目錄（防重導）。**junction 必須也拒**
        # （e2e Pass1 High）：`os.path.islink` 在 Windows 對 junction 回 False（junction 非 symlink），舊檢查會放行
        # 指向正式 memory/hub 的 junction → 兩版/PROMPT.md 寫穿進同步區＝明文外洩。此處是**工具自有 staging**，ccdir
        # 政策明定其 reparse 一律 fail-closed（與 memory/ 夾**跟隨** junction 相反）→ 用 `reparse_kind != "none"` 全拒。
        if memory.reparse_kind(cur, long_path=True) != "none" or not os.path.isdir(atomicio.os_path(cur)):
            raise UnsafeStagingPath(f"暫存路徑含 symlink/junction/reparse 或非目錄：{cur}")
    return "claimed" if claimed_leaf else "already-staged"


def stage_conflict(conflict: MemoryConflict, *, root: Path | None = None,
                   apply: bool = False) -> StageResult:
    """把衝突兩版安全保留到暫存夾（approach A）。

    **claim-the-dir 幂等**：逐層 no-follow 佔用 `<root>/<pk>/<key>`（`_claim_staging_dir`，拒 symlink 路徑，gate F1）；
    leaf 已存在 → 看**完成標記** `.done`：有 → `already-staged`（不覆蓋，保護使用者刪減/合併的內容，§7.3）；無 →
    `incomplete`（上次中途失敗的殘缺暫存，gate F3）→ 報失敗 + 指示刪除重跑，**不**當成已完成。每個版本走
    `atomic_create_bytes`（O_EXCL）；全部寫完才寫 `.done`。**只讀正式 memory、只寫暫存**——永不碰 `memory/`、永不刪
    任何檔（A3）。`apply=False` → would-stage 預覽。無可保留內容 → `empty`。`conflict.notes`（plan 後退化）一律
    併入結果並使 CLI 非零（gate F2）。"""
    root = Path(root) if root is not None else merge_root()
    dest = staging_dir(root, conflict)
    base_notes = list(conflict.notes)
    staged_versions = conflict.staged_versions()
    if not staged_versions:
        return StageResult(conflict, dest, "empty", [],
                           base_notes + ["無可保留的版本內容（全部讀不到/損壞）"])
    if not apply:
        # 預覽也比對 fingerprint（gate5 F1）：陳舊（衝突已換 kind/內容）顯示 stale，與 apply 一致、不誤報 already。
        state = _completed_match(dest, conflict)
        status = {"same": "already-staged", "stale": "stale"}.get(state, "would-stage")
        extra = [f"暫存內容與目前衝突不符（衝突已變）；請刪除 {dest} 後重跑"] if state == "stale" else []
        return StageResult(conflict, dest, status, planned_staged_names(conflict), base_notes + extra)
    try:
        claim = _claim_staging_dir(root, dest)
    except UnsafeStagingPath as e:
        return StageResult(conflict, dest, "error", [], base_notes + [f"暫存路徑不安全（拒絕寫入）：{e}"])
    except OSError as e:
        return StageResult(conflict, dest, "error", [], base_notes + [f"建立暫存夾失敗：{e}"])
    if claim == "already-staged":
        # `.done` 在 → 比對 fingerprint：同一衝突＝already-staged 幂等；**已換 kind/內容＝stale**（不可用舊證據
        # 遮蓋新衝突，gate5 F1）。`.done` 不在 → incomplete（上次中途失敗）。皆不覆蓋使用者已刪減/合併的內容。
        state = _completed_match(dest, conflict)
        if state == "same":
            return StageResult(conflict, dest, "already-staged", [], base_notes)
        if state == "stale":
            return StageResult(conflict, dest, "stale", [], base_notes
                               + [f"暫存內容與目前衝突不符（衝突已換 kind/內容）；請刪除 {dest} 後重跑"])
        return StageResult(conflict, dest, "incomplete", [], base_notes
                           + [f"暫存夾殘缺（上次中途失敗，缺 {DONE_FILE}）；請刪除 {dest} 後重跑"])
    written: list[str] = []
    notes: list[str] = list(base_notes)
    staged_map: dict[int, str] = {}
    ok = True
    for i, v in enumerate(conflict.versions):
        if v.is_tombstone or v.data is None:
            continue
        sname = _safe_component(f"{v.label}__{v.filename}")
        try:
            atomicio.atomic_create_bytes(dest / sname, v.data, long_path=True)   # 深 staging 路徑 → \\?\
            written.append(sname)
            staged_map[i] = sname
        except FileExistsError:
            # dest 是本次新佔用的空夾 + _safe_component injective → 同名衝突屬真實異常（並發/不該發生）。
            # 標成失敗（含「失敗」→ CLI 非零退出），不靜默略過丟版本（codex R1 High：collision 須當 error）。
            notes.append(f"{sname}: 暫存檔意外已存在，寫入失敗（未覆蓋；請回報）")
            ok = False
        except (OSError, atomicio.AtomicWriteError) as e:
            notes.append(f"{sname}: 寫入失敗 {e}")
            ok = False
    degraded = bool(conflict.notes)   # plan 後某側讀不到 → 保留不完整（gate2 F1）
    try:
        atomicio.atomic_write_text(
            dest / META_FILE, json.dumps(_conflict_meta(conflict, staged_map), ensure_ascii=False, indent=2),
            long_path=True)   # 深 staging 路徑 → \\?\
        atomicio.atomic_write_text(dest / PROMPT_FILE, build_prompt(conflict), long_path=True)
        # 只有「全部寫成功**且非退化**」才落 `.done`（gate2 F1）：退化暫存缺 .done → 下次偵測為 incomplete、
        # 不會被當 already-staged 而永久卡住缺檔；提示詞/中繼已帶退化警告，使用者知道要刪除重跑補齊。
        if ok and not degraded:
            atomicio.atomic_write_text(dest / DONE_FILE, "", long_path=True)
    except (OSError, atomicio.AtomicWriteError) as e:
        notes.append(f"中繼/提示詞寫入失敗 {e}")
        ok = False
    status = "error" if not ok else ("degraded" if degraded else "staged")
    return StageResult(conflict, dest, status, written, notes)


# ── 合併提示詞（明文外洩警告）─────────────────────────────────────────────────

def build_prompt(conflict: MemoryConflict) -> str:
    """產生給 Claude 的合併提示詞（含明文外洩警告抬頭）。純文字、只組裝已讀入的版本內容；不寫任何檔。
    呼叫端決定輸出到 stdout 或本機暫存（皆不同步）——**絕不**由本工具自動送進 Claude。"""
    lines: list[str] = ["<!-- claude-session-sync memory-merge 提示詞（本機產生；勿回寫到任何會被同步的位置）-->",
                        LEAK_WARNING, "",
                        "# 任務：合併衝突的 Claude memory", "",
                        f"- 專案：{_disp(conflict.project_key)}",
                        f"- 衝突類型：{_KIND_ZH.get(conflict.kind, conflict.kind)}（`{conflict.kind}`）",
                        f"- 鍵：{_key_disp(conflict.key)}",
                        f"- 偵測原因：{_disp(conflict.reason)}", ""]
    if conflict.notes:   # 退化警告（某側讀不到、保留不完整）持久化進提示詞（gate2 F1），合併者須知本次不完整。
        lines.append("- ⚠ **本次保留不完整**（請刪除暫存夾後重跑 sync 補齊；勿據此最終定案）：")
        for n in conflict.notes:
            lines.append(f"    - {_disp(n)}")
        lines.append("")
    tombs = [v for v in conflict.versions if v.is_tombstone]
    if tombs:
        # **列出所有** tombstone（codex R1 Medium：多筆換檔名刪除全數呈現，不可只顯示第一筆 → 否則藏掉某些刪除）。
        lines.append("- ⚠ 有一方/多方曾**刪除**此記憶。刪除常是移除過期/錯誤/敏感資訊——請評估該尊重刪除、"
                     "還是保留更新版；不確定就交人。已刪版本：")
        for t in tombs:
            lines.append(f"    - `{_disp(t.filename)}`（base hash `{_short(t.base_hash)}`"
                         + (f"、identity `{_disp(t.identity)}`" if t.identity else "")
                         + (f"、來源 `{_disp(t.machine)}`" if t.machine else "")
                         + (f"、時間 `{_disp(t.time)}`" if t.time else "") + "）")
        lines.append("")
    if conflict.kind == FUZZY_KIND:
        # fuzzy：兩檔是**近似比對疑似**同一事實（不同檔名、非確定），連提示詞都須守 advisory——先要求確認、允許判「其實
        # 不同」→ 不合併（保留兩則各自的檔），不可假設一定同一則。
        lines += [
            "下面是兩個**不同檔名**的 memory，被近似比對標為**疑似**同一事實（**尚未確認**）。請**先判斷它們是否真是"
            "同一件事**：",
            "- 若**其實是兩件不同的事**（只是用詞或主題相近）→ **不要合併**，說明理由、維持兩則各自獨立即可；",
            "- 若**確是同一件事** → 把它們**合併成單一 memory `.md`**：保留所有**不同的事實**、不遺漏任一版本獨有內容；"
            "不杜撰未出現的資訊；保留 frontmatter（`name`/`description`/`metadata`），衝突欄位取較完整者、必要時在正文"
            "註明出處；兩版**矛盾**（非互補）就標出來、兩種說法都留、交人決定。",
            "",
            "請先明確說出「同一件事 / 不同的事」的判斷；若判定合併，再**只輸出**最終 `.md` 內容（含 frontmatter）。"
            "完成後本工具不會自動寫回 `memory/`——請自行覆蓋正式檔再重跑 `sync` 傳播（合併時記得刪掉多餘的那個舊檔）。",
            "",
        ]
    else:
        lines += [
            "下面是同一則記憶的多個版本（已從本機與 hub 各自保留到暫存區）。請把它們**合併成單一 memory `.md`**：",
            "- 保留所有**不同的事實**，不要遺漏任一版本獨有的內容；",
            "- 不要杜撰或推測未出現的資訊；",
            "- 保留 frontmatter（`name`/`description`/`metadata`）；衝突欄位取較完整者，必要時在正文註明出處；",
            "- 若兩版**矛盾**（非互補），不要自行裁定——標出來、兩種說法都保留、交人決定。",
            "",
            "合併後請**只輸出**最終 `.md` 內容（含 frontmatter）。完成後本工具不會自動寫回 `memory/`；"
            "請自行覆蓋正式檔再重跑 `sync` 傳播。",
            "",
        ]
    n = 0
    for v in conflict.versions:
        if v.is_tombstone:
            continue
        n += 1
        head = f"## 版本 {n}：{_disp(v.label)}（`{_disp(v.filename)}`"
        head += f"，hash `{_short(v.content_hash)}`）" if v.content_hash else "，內容損壞/無法解碼）"
        lines.append(head)
        if v.text is None:
            lines.append("（無法解碼或已損壞——請改看暫存檔原始 bytes。）")
        else:
            fence = _fence_for(v.text)         # 動態長度，防內容中的 ``` 提前關閉 fence
            lines.append(fence + "md")
            lines.append(v.text)
            lines.append(fence)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _short(h: str | None) -> str:
    return (h[:12] if h else "?")


# ── CLI 報告 ──────────────────────────────────────────────────────────────────

def format_conflicts(conflicts: list[MemoryConflict], *, root: Path | None = None,
                     apply: bool = False) -> str:
    """dry-run / 預覽報告：列每個衝突 + 各版本 + 暫存目標路徑。"""
    root = Path(root) if root is not None else merge_root()
    lines: list[str] = []
    for c in conflicts:
        dest = staging_dir(root, c)
        lines.append(f"\n● {_disp(c.project_key)} / {_key_disp(c.key)}  [{_KIND_ZH.get(c.kind, c.kind)}]")
        lines.append(f"  原因：{_disp(c.reason)}")
        for v in c.versions:
            if v.is_tombstone:
                lines.append(f"    - tombstone（已刪除；base `{_short(v.base_hash)}`"
                             + (f"，identity `{_disp(v.identity)}`" if v.identity else "") + "）")
            else:
                tag = "" if v.content_hash else "（損壞/無法解碼）"
                lines.append(f"    - {_disp(v.label)}：`{_disp(v.filename)}`  hash `{_short(v.content_hash)}`{tag}")
        for n in c.notes:
            lines.append(f"  ⚠ {_disp(n)}")
        verb = "暫存於" if apply else "將暫存於"
        # dest 的根來自 XDG_CACHE_HOME（POSIX 可含非 UTF-8 bytes/控制字元）→ 過 _disp，免預覽輸出崩 strict stdout
        # 或破壞單行（g4 Low，同 `_print_stage(res.dest)` 一類）。
        lines.append(f"  {verb}：{_disp(str(dest))}")
    return "\n".join(lines)
