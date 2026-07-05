"""scan：把基礎模組串成 dry-run 計畫（P1a 成品）。掃描→專案同一性→配對 session→分類→標單邊。

依據 DESIGN §6/§8 + PLAN v0.6 §2.9 / §3 資料流。**P1a 唯讀**：只產 SyncPlan、不寫檔。
  - 專案同一性：local 專案夾讀某 session 的 `cwd` → git 指紋 → 比對 hub `_project.json`（可注入 identity_fn 測）。
  - session 以**檔名**配對（B6）；成對 → classify；單邊 → 查 hub tombstone（suppress）/ coverage（未 init→blocked）/ 否則 copy。
  - first-run（無 state）標示；不在此寫 baseline（bootstrap 是 P1b）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import anomaly, memory, sidecar, tombstone
from .anomaly import Anomaly
# 跨側 presence/safety predicates 已上提到 anomaly（leaf，memory classify 也用）；以原名 re-export 維持
# 既有呼叫端不變（transfer/doctor 用 `scan._collision_casefolds`；apply/test 用 `scan.is_bulk_local_deletion`）。
from .anomaly import collision_casefolds as _collision_casefolds  # noqa: F401  (re-export)
from .anomaly import is_bulk_local_deletion  # noqa: F401  (re-export)
from .classify import classify
from .lineset import analyze
# 逃逸防線移至 leaf `pathsafe`（單一真相源；anomaly 等 leaf 也能用、免循環，e2e gate2）。以原底線名 re-export
# 維持既有 `scan._safe_project_dir`/`_within_root`/`_list_project_dirs` 呼叫端不變。
from .pathsafe import dir_scannable as _dir_scannable  # noqa: F401  (re-export；apply/transfer/bootstrap/doctor 用)
from .pathsafe import list_project_dirs as _list_project_dirs  # noqa: F401  (re-export)
from .pathsafe import safe_project_dir as _safe_project_dir  # noqa: F401  (re-export)
from .pathsafe import within_root as _within_root  # noqa: F401  (re-export)
from .pathsafe import name_key as _name_key  # noqa: F401  (re-export；NFC∘casefold∘NFC 單一真相源在 pathsafe，anomaly/memory 亦 import)
from .sidecar import MatchStatus
from .state import State

# local 端 session 根 = `<設定根>/projects`。設定根依 Claude Code 慣例取 `CLAUDE_CONFIG_DIR` env（多帳號／
# 設定不在預設位置者設此，與 Claude Code 本身一致）；未設（最普遍）→ 預設 `~/.claude`。由 CLI `--local-root`
# 覆寫。此路徑下的 junction/symlink 由 OS 透明跟隨——使用者多帳號常以 directory junction 在**同機**刻意共用
# `projects/`／`memory/`（免權限、讀寫透明＝同一夾），工具**信任** `CLAUDE_CONFIG_DIR` 指向的位置、不另偵測
# 共用別名（同份資料只配一個根、由使用者自己用 env 決定指向哪個帳號）。
def default_local_root() -> Path:
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(cfg_dir) if cfg_dir else Path.home() / ".claude"
    return base / "projects"


@dataclass
class SessionPlan:
    session_id: str
    action: str          # classify 類別值 或 copy-to-hub/copy-to-local/suppressed-deleted/
                         # conflict-delete-vs-update/blocked-uninitialized…
    direction: str | None
    reason: str


@dataclass
class ProjectPlan:
    local_dir: str | None
    hub_dir: str | None
    identity: str        # match/ambiguous/needs-map/hub-only/local-only
    coverage_initialized: bool
    sessions: list[SessionPlan] = field(default_factory=list)
    memories: list[memory.MemoryPlan] = field(default_factory=list)   # P1d Block 3b：memory 檔級計畫
    notes: list[str] = field(default_factory=list)
    memory_scan_failed: bool = False   # memory 規劃被跳過（memory/ symlink/不可讀）→ memories 不可信為「全部」
    #   （結構化旗標，非靠 note 文字；memory-merge 須據此 surface + 非零，不可 recheck 成功就抹掉 plan 的跳過事實，gate4 F3）


@dataclass
class SyncPlan:
    first_run: bool
    anomalies: list[Anomaly]
    projects: list[ProjectPlan]

    @property
    def halt(self) -> bool:
        return any(a.severity == "halt" for a in self.anomalies)


def _session_files(d: Path | None) -> dict[str, Path]:
    """專案夾下的 <sid>.jsonl（檔名 stem = sessionId）。略過 memory/、dotfiles 與 **symlink 檔**（後者對稱
    `memory.list_memory_files`：`secret.jsonl` symlink → 夾外檔會被當 session 讀/copy 進 hub＝洩漏，e2e gate2 #2；
    `is_symlink()` 先於 `is_file()`——is_file 會跟隨 symlink）。真實 session 為實體檔、不受影響。"""
    out: dict[str, Path] = {}
    if d and d.exists():
        for p in d.glob("*.jsonl"):
            if not p.is_symlink() and p.is_file() and not p.name.startswith("."):
                out[p.stem] = p
    return out


def _symlink_name_keys(d: Path | None) -> set[str]:
    """`d` 內 symlink leaf 的 `_name_key` 集，供 symlink-alias 偵測（e2e gate7/8/9）。`list_*_files` 略過 symlink →
    該 name「看似 absent」；exact-name guard 漏掉 casefold/normalization-alias（`A.md`、NFD `café.md`、大寫 UUID）→
    誤驅動 local-deleted tombstone / 把 alias symlink 當 absent 覆蓋。**呼叫端須先確保 `d` 安全**（apply 對 memory
    根以 `_is_unfollowable_reparse` 過濾後才呼叫；session/transfer 的專案夾已由 `_safe_project_dir` 驗非 symlink 根）。
    缺夾/讀不到 → 空集。"""
    if d is None:
        return set()
    try:
        return {_name_key(p.name) for p in Path(d).iterdir() if p.is_symlink()}
    except OSError:
        return set()




def _project_cwds(local_dir: Path) -> set[str]:
    """收集該專案夾**所有** session 的 cwd。夾名映射有損，同夾可能混入多個 cwd（§3.1/§8.2）。"""
    cwds: set[str] = set()
    for p in sorted(_session_files(local_dir).values()):
        for ln in analyze(str(p)).lines:
            if ln.obj and ln.obj.get("cwd"):
                cwds.add(ln.obj["cwd"])
                break  # 一個 session 取一個 cwd 即可
    return cwds


def _git_identity(local_dir: Path, hub_dirs: list[Path]) -> tuple[str, Path | None]:
    """預設同一性解析：local cwd 的 git 指紋 vs 各 hub `_project.json`。判不出 → needs-map（不猜）。"""
    cwds = _project_cwds(local_dir)
    if len(cwds) > 1:
        return ("blocked-multi-cwd", None)  # 同夾多 cwd → 不可挑第一個，整夾阻斷（C-r6-1）
    if not cwds:
        return ("needs-map", None)
    fp = sidecar.local_fingerprint(next(iter(cwds)))
    exact: list[Path] = []
    ambiguous = False
    for hd in hub_dirs:
        scd = sidecar.read_project_sidecar(hd)
        if not scd:
            continue
        status = sidecar.match(fp, scd).status
        if status == MatchStatus.MATCH:
            exact.append(hd)
        elif status == MatchStatus.AMBIGUOUS:
            ambiguous = True
    if len(exact) == 1:
        return ("match", exact[0])
    if len(exact) > 1 or ambiguous:
        return ("ambiguous", None)  # 多 exact 或 first-commit 相同不同 remote（fork/rename）→ 交人
    return ("needs-map", None)


def _bindings_first(
    resolve: Callable[[Path, list[Path]], tuple[str, Path | None]],
    state: State | None,
) -> Callable[[Path, list[Path]], tuple[str, Path | None]]:
    """先採 state 的持久綁定（cwd→hub project_key，由 bootstrap/`--map` 寫入，A17.4），再退回 git 指紋。

    使用者明示的綁定**優先於**啟發式 git 指紋。綁定指向的 hub 夾若當前不在（掛錯碟/被刪）→ needs-map，
    不憑空配對。多 cwd 同夾不採綁定（夾名有損，交原解析判 blocked-multi-cwd）。
    **空夾**（session 全刪 → 無 cwd 可解析身分）改採持久化的**夾名綁定**（local_dir_bindings），否則
    「刪到空」的專案無法配對 → 對稱刪除偵測抓不到（codex r25）。夾名為 FS 既有路徑、不做編碼弱猜（決定#7）。
    """
    bmap = (state.bindings if state else {}) or {}
    dirmap = (state.local_dir_bindings if state else {}) or {}
    if not bmap and not dirmap:
        return resolve

    def wrapped(local_dir: Path, hub_dirs: list[Path]) -> tuple[str, Path | None]:
        cwds = _project_cwds(local_dir)
        pk = None
        if len(cwds) == 1:
            pk = bmap.get(next(iter(cwds)))
        elif len(cwds) == 0 and not _session_files(local_dir):
            # **真正空夾**（無任何 session 檔）才用夾名綁定。有檔但讀不到 cwd（無 cwd 欄位/壞檔）→ **不**用
            # 夾名誤配（否則 local-only 真檔可能寫進錯 hub / 誤 tombstone）→ 交原解析判 needs-map（codex r26-1）。
            pk = dirmap.get(local_dir.name)
        if pk:
            for hd in hub_dirs:
                if hd.name == pk:
                    return ("match", hd)
            return ("needs-map", None)  # 綁定的 hub 夾不在 → 不憑空配對
        return resolve(local_dir, hub_dirs)  # 多 cwd / 無綁定 → 原解析（git 指紋 / blocked-multi-cwd）

    return wrapped


def _suppress_or_conflict(sid: str, lf: Path | None, hf: Path | None, tomb) -> SessionPlan:
    """tombstone 存在時的**條件式**判定（A3 delete-vs-update，P1c 消費 base_hash）：

    現存側內容（raw bytes digest）**全部 == base_hash** → suppress（不復活，尊重刪除）；
    否則（刪除後又被改 / 兩側內容不同 / base 不明）→ `conflict-delete-vs-update`：交人決策，
    **既不復活、也不靜默壓掉刪除後的更新**。兩種結果都非自動套用 → apply 不寫（r14-1 不復活仍成立）。
    """
    base = tomb.base_hash
    present = [p for p in (lf, hf) if p is not None]
    digests = [tombstone.raw_file_digest(p) for p in present]
    if base is not None and digests and all(d == base for d in digests):
        return SessionPlan(sid, "suppressed-deleted", None,
                           "hub tombstone 且現存內容==base → 不復活（A3）")
    return SessionPlan(sid, "conflict-delete-vs-update", None,
                       "hub tombstone 但現存內容≠base（刪除後又改/兩側不一/base 不明）"
                       "→ 交人，不復活也不丟更新（A3）")


def classify_session(
    sid: str, lf: Path | None, hf: Path | None, *,
    both: bool, coverage_initialized: bool, tombs: dict,
    is_collision: bool = False, corrupt: set | None = None, known: set | None = None,
    has_baseline: bool = True, local_known: set | None = None,
    bulk_local_deletion: bool = False, has_local_baseline: bool = True,
) -> SessionPlan:
    """單一 session 的分類（plan 與 apply-下重新分類共用同一套規則 → 不會漂移，codex r10-2）。

    lf/hf = 該 sid 在 local/hub 的檔路徑（None 表該側無）。both = 專案是否兩側皆綁定。
    corrupt = 該專案「壞掉的 tombstone」推定身分集（fail-closed，codex r11-3）。
    known = 該專案 state 已知**hub** session 集（hub baseline），用於分辨「新檔」vs「已知檔被刪」（codex r16）。
    has_baseline = **本機** state 是否已有此專案的基線（project_key ∈ known_sessions）。hub 的 coverage 是
      別台 bootstrap 也可能有；單邊 copy 必須本機自己對此專案 bootstrap 過，否則分不清「新檔」與「對側已刪」
      而可能復活刪除（codex r18）。
    local_known = 該專案 state 已知**local** session 集（local baseline，P1c）。用於 hub 側單邊檔的對稱判定：
      sid 曾在本機 local（∈local_known）但現已不在 → **本機刪除**（local-deleted），非新 hub 檔。
    has_local_baseline = **本機** state 是否已有此專案的 local 基線（project_key ∈ local_sessions）。與
      has_baseline（known/hub 基線）分開：舊 state（P1c 前）有 known 卻無 local_sessions → has_local_baseline
      =False（**migration**）。此時無法分辨「新 hub 檔」與「本機已刪」→ present=hub 一律 fail-closed
      `blocked-no-local-baseline`，**不 copy（避免靜默復活已刪）也不 tombstone**，待使用者重 bootstrap 建
      local 基線並確認可匯入差異（codex r24-1）。empty 的 local_sessions[pk]（has_local_baseline=True）≠ 無此
      欄位：前者是真基線（該專案 local 當時為空），後者才是 migration。
    bulk_local_deletion = 本專案 local 是否大量消失（疑掛錯碟/被清）→ local-deleted 改 blocked-bulk-local-deletion。
    """
    if is_collision:
        return SessionPlan(sid, "blocked-casefold-collision", None,
                           "casefold 撞名 sessionId（同側或跨側 case-only，跨 OS 碰撞風險，A9）")
    # tombstone 閘**先於**配對分類（codex r14-1）：刪除標記不論成對/單邊都該抑制，否則 tombstoned 的
    # session 若兩側都還在、且 local 是 hub 的 ff，會被當 fast-forward 寫回 hub＝復活已刪。
    if ("session", sid) in tombs:
        return _suppress_or_conflict(sid, lf, hf, tombs[("session", sid)])
    if corrupt and ("session", sid) in corrupt:
        return SessionPlan(sid, "blocked-tombstone-corrupt", None,
                           "tombstone 損壞、無法確認是否已刪 → 阻擋（fail-closed，不復活）")
    if lf and hf:
        c = classify(analyze(str(lf)), analyze(str(hf)))
        return SessionPlan(sid, c.klass.value, c.direction, c.reason)
    # 單邊存在
    present = "local" if lf else "hub"
    if not both:
        # 無對側綁定（hub-only / local-only / 未對應）→ 不知落到哪 → 拒絕落地（C-r6-2）
        return SessionPlan(sid, "blocked-unmapped", None,
                           "專案未對應到對側（需 --map / bootstrap），單邊不落地")
    if not coverage_initialized:
        return SessionPlan(sid, "blocked-uninitialized", None, "專案未 bootstrap，單邊檔不自動處理")
    if not has_baseline:
        return SessionPlan(sid, "blocked-no-baseline", None,
                           "本機未對此專案 bootstrap（hub 的 coverage 可能來自他機）→ 單邊檔不自動複製（避免復活刪除）")
    # present=hub 另需 **local 基線**：migration（舊 state 有 known、無 local_sessions）下無從分辨「新 hub 檔」
    # 與「本機已刪」→ fail-closed，不 copy（避免靜默復活）、不 tombstone，待重 bootstrap 建 local 基線（codex r24-1）。
    if present == "hub" and not has_local_baseline:
        return SessionPlan(sid, "blocked-no-local-baseline", None,
                           "本機無此專案 local 基線（疑舊 state 遷移）→ 單邊 hub 檔不自動處理，請重 bootstrap")
    # 「已知 session 單邊消失（無 tombstone）」的**對稱**偵測，方向決定信任與否：
    #  - hub 側消失（present=local，sid∈known）：hub 是永久歸檔、不該無故掉檔 → 不信任 → 交人
    #    （blocked-known-deleted），不可從 local copy 回 hub 復活（codex r16）。
    #  - local 側消失（present=hub，sid∈local_known）：使用者刪自己的 local 是正常操作 → 信任 → 寫 hub
    #    tombstone 通知對側（local-deleted，apply 寫；不刪 hub 歸檔，A3）。但**大量**消失（疑掛錯碟/被清）
    #    → 不自動寫、整批交人（blocked-bulk-local-deletion）——false-positive 會寫 tombstone 抑制真實 session。
    # 兩者都在 damaged 閘**之前**：tombstone/blocked 不需來源可讀（base_hash 走 raw bytes，壞檔亦可標記）。
    if present == "local" and sid in (known or set()):
        return SessionPlan(sid, "blocked-known-deleted", None,
                           "已知 session 在 hub 消失且無 tombstone（疑刪除，非新檔）→ 交人決策")
    if present == "hub" and sid in (local_known or set()):
        if bulk_local_deletion:
            return SessionPlan(sid, "blocked-bulk-local-deletion", None,
                               "本專案 local 大量消失（疑掛錯碟/被清空）→ 不自動寫 tombstone，整批交人確認")
        return SessionPlan(sid, "local-deleted", None,
                           "已知 local session 消失（本機刪除）→ 寫 hub tombstone 通知對側（不刪 hub 歸檔，A3）")
    # 單邊 copy 來源也要過損壞閘（codex r14-2）：0-byte/空白/解碼錯/壞行/無對話身分（含正在被寫的半截檔）
    # 不可原樣複製到對側，否則把壞檔散播出去。
    src = lf or hf
    shape = analyze(str(src))
    if shape.is_damaged or not shape.uuids:
        return SessionPlan(sid, "blocked-damaged-source", None,
                           "單邊來源檔損壞/無對話身分（0-byte/壞行/空/可能正在寫），不複製")
    action = "copy-to-hub" if present == "local" else "copy-to-local"
    return SessionPlan(sid, action, f"{present}->other", f"單邊新檔（{present}）")


def casefold_collisions_for(local_dir: Path | None, hub_dir: Path | None) -> set[str]:
    """該專案兩側**合併**的 casefold 撞名集（供 apply 下重新分類重算 collision）。"""
    return _collision_casefolds(_session_files(local_dir).keys(), _session_files(hub_dir).keys())


def plan_project_pair(
    local_dir: Path | None,
    hub_dir: Path | None,
    *,
    coverage_initialized: bool,
    tombs: dict | None = None,
    corrupt: set | None = None,
    known: set | None = None,
    has_baseline: bool = True,
    local_known: set | None = None,
    has_local_baseline: bool = True,
) -> list[SessionPlan]:
    """單一（已配對）專案：逐 session 產動作。成對 classify；單邊查 tombstone/coverage/known/local_known。"""
    tombs = tombs or {}
    both = local_dir is not None and hub_dir is not None
    local = _session_files(local_dir)
    hub = _session_files(hub_dir)
    collisions = _collision_casefolds(local.keys(), hub.keys())
    bulk = is_bulk_local_deletion(local_known, set(local.keys()))
    plans: list[SessionPlan] = []
    for sid in sorted(set(local) | set(hub)):
        plans.append(classify_session(
            sid, local.get(sid), hub.get(sid), both=both,
            coverage_initialized=coverage_initialized, tombs=tombs, corrupt=corrupt, known=known,
            has_baseline=has_baseline, is_collision=sid.casefold() in collisions,
            local_known=local_known, bulk_local_deletion=bulk, has_local_baseline=has_local_baseline,
        ))
    return plans


def _plan_memories(
    local_dir: Path | None, hub_dir: Path | None, *,
    state: State | None, cov: bool, tombs: dict, corrupt: set,
) -> list[memory.MemoryPlan]:
    """單一專案的 memory 檔級計畫（對稱 `plan_project_pair`，P1d Block 3b）。memory 基線取自 state 的
    `known_memory`/`local_memory`（與 session 的 known_sessions/local_sessions 對稱、各自獨立）。tombs/corrupt
    沿用同一份（plan_memory_pair 內部自行篩 kind=="memory"）。`memory.UnsafeMemoryDir` 由呼叫端 catch。"""
    pk = hub_dir.name if hub_dir else None
    mem_known = state.known_memory.get(pk) if (state and pk) else None
    mem_has_baseline = bool(state and pk and pk in state.known_memory)
    mem_local_known = state.local_memory.get(pk) if (state and pk) else None
    mem_has_local_baseline = bool(state and pk and pk in state.local_memory)
    return memory.plan_memory_pair(
        local_dir, hub_dir, coverage_initialized=cov, tombs=tombs, corrupt=corrupt,
        known=mem_known, has_baseline=mem_has_baseline,
        local_known=mem_local_known, has_local_baseline=mem_has_local_baseline)


def build_plan(
    local_root: str | Path,
    hub_root: str | Path,
    state: State | None,
    *,
    identity_fn: Callable[[Path, list[Path]], tuple[str, Path | None]] | None = None,
    memory_only: bool = False,
) -> SyncPlan:
    """完整 dry-run 計畫。identity_fn 可注入（測試）；預設用 git 指紋，並優先採 state 的持久綁定（A17.4）。

    `memory_only=True`：**只算 memory 檔級計畫**，跳過每個 session 的 `plan_project_pair`（`sessions=[]`）。
    供 `nudge` hook 助手（DESIGN §7.5「只比對 memory、不做重活」）在 SessionEnd/Start 輕量檢查記憶分歧用——
    避免對每個 session JSONL 做完整分類（最重的一段）。**memory 計畫與 session 無關、結果不受影響**。
    （身分解析仍需讀 session cwd 來配對 local↔hub 夾；完全免 session-parse 需夾名身分快路徑，留待需要時。）"""
    local_root, hub_root = Path(local_root), Path(hub_root)
    resolve = _bindings_first(identity_fn or _git_identity, state)
    anomalies = anomaly.check(state, hub_root)
    if any(a.severity == "halt" for a in anomalies):
        return SyncPlan(first_run=state is None, anomalies=anomalies, projects=[])

    # **逃逸專案夾過濾**（e2e gate G-High）：symlink 或逃出 root 的 reparse 專案夾一律不讀/不寫、不跟隨——否則主
    # sync 會把逃逸 local 夾的界外 session copy 進 hub（洩漏）、或把 hub 逃逸夾當本專案寫穿到界外。root 內 junction
    # （ccdir 多帳號刻意共用）resolve 後仍在 root 內 → 照常允許。與 transfer/bootstrap/doctor 同一把 `_safe_project_dir`。
    hub_dirs, hub_unsafe = _list_project_dirs(hub_root)
    local_dirs, local_unsafe = _list_project_dirs(local_root)

    projects: list[ProjectPlan] = []
    # 逃逸夾**可見回報** skipped-unsafe（非靜默丟）：使用者看得到、可改實體目錄；apply 對它們無 session/memory 可寫。
    for ld in local_unsafe:
        projects.append(ProjectPlan(
            local_dir=str(ld), hub_dir=None, identity="skipped-unsafe", coverage_initialized=False,
            notes=["local 專案夾是 symlink 或逃逸 local_root → 跳過（不讀/寫信任根外，請改實體目錄）"]))
    for hd in hub_unsafe:
        projects.append(ProjectPlan(
            local_dir=None, hub_dir=str(hd), identity="skipped-unsafe", coverage_initialized=False,
            notes=["hub 專案夾是 symlink 或逃逸 hub_root → 跳過（不讀/寫信任根外）"]))
    matched_hub: set[Path] = set()
    for ld in local_dirs:
        status, hub_dir = resolve(ld, hub_dirs)
        if hub_dir:
            matched_hub.add(hub_dir)
        cov = tombstone.is_initialized(hub_dir) if hub_dir else False
        tombs = tombstone.read_tombstones(hub_dir) if hub_dir else {}
        corrupt = tombstone.corrupt_tombstone_targets(hub_dir) if hub_dir else set()
        known = (state.known_sessions.get(hub_dir.name) if (state and hub_dir) else None)
        has_baseline = bool(state and hub_dir and hub_dir.name in state.known_sessions)
        local_known = (state.local_sessions.get(hub_dir.name) if (state and hub_dir) else None)
        has_local_baseline = bool(state and hub_dir and hub_dir.name in state.local_sessions)
        notes = [] if hub_dir else [f"未對應到 hub 專案（{status}）；需 --map / bootstrap"]
        mem_scan_failed = False
        try:
            mem_plans = _plan_memories(ld, hub_dir, state=state, cov=cov, tombs=tombs, corrupt=corrupt)
        except memory.UnsafeMemoryDir:
            mem_plans, mem_scan_failed = [], True
            notes.append("memory/ 根是 symlink → 已跳過記憶同步（請改實體目錄）")
        except OSError as e:
            # memory/ 夾不可讀（權限/陳舊掛載等）→ 不讓它崩掉整個 plan（否則 sync/status/memory-merge 全炸、
            # memory-merge 還來不及 surface 就 crash，gate3 F2）；跳過該專案 memory、記 note + 旗標，由 unscannable 回報。
            mem_plans, mem_scan_failed = [], True
            notes.append(f"memory/ 夾無法讀取（{e.__class__.__name__}）→ 已跳過記憶同步")
        projects.append(
            ProjectPlan(
                local_dir=str(ld), hub_dir=str(hub_dir) if hub_dir else None,
                identity=status,
                coverage_initialized=cov,
                sessions=([] if memory_only else
                          plan_project_pair(ld, hub_dir, coverage_initialized=cov, tombs=tombs,
                                            corrupt=corrupt, known=known, has_baseline=has_baseline,
                                            local_known=local_known, has_local_baseline=has_local_baseline)),
                memories=mem_plans,
                notes=notes,
                memory_scan_failed=mem_scan_failed,
            )
        )

    for hd in hub_dirs:
        if hd in matched_hub:
            continue
        cov = tombstone.is_initialized(hd)
        tombs = tombstone.read_tombstones(hd)
        corrupt = tombstone.corrupt_tombstone_targets(hd)
        known = state.known_sessions.get(hd.name) if state else None
        has_baseline = bool(state and hd.name in state.known_sessions)
        hub_notes = ["hub 有、local 無此專案"]
        mem_scan_failed = False
        try:
            mem_plans = _plan_memories(None, hd, state=state, cov=cov, tombs=tombs, corrupt=corrupt)
        except memory.UnsafeMemoryDir:
            mem_plans, mem_scan_failed = [], True
            hub_notes.append("memory/ 根是 symlink → 已跳過記憶同步")
        except OSError as e:   # 不可讀 memory/ 夾 → 不崩整 plan（gate3 F2）
            mem_plans, mem_scan_failed = [], True
            hub_notes.append(f"memory/ 夾無法讀取（{e.__class__.__name__}）→ 已跳過記憶同步")
        projects.append(
            ProjectPlan(
                local_dir=None, hub_dir=str(hd), identity="hub-only",
                coverage_initialized=cov,
                sessions=([] if memory_only else
                          plan_project_pair(None, hd, coverage_initialized=cov, tombs=tombs,
                                            corrupt=corrupt, known=known, has_baseline=has_baseline)),
                memories=mem_plans,
                notes=hub_notes,
                memory_scan_failed=mem_scan_failed,
            )
        )

    return SyncPlan(first_run=state is None, anomalies=anomalies, projects=projects)


# 「工具永遠無法自動解決、可由使用者 A15 acknowledge」的 blocked action 集（單一真相源；acks.ACKABLE_ACTIONS
# re-export 之——acks import scan、不可反向）。**呈現層隱藏必守此護欄**：只藏這些 action 的行，絕不因某 sid 被 ack
# 就連同一 sid 在另一 local view 的 copy-to-hub/fork 等**非 ackable** 行一起藏（fresh gate g3 High）。
ACKABLE_ACTIONS = frozenset({
    "blocked-casefold-collision", "blocked-damaged-source", "damaged", "identity-collision"})


def format_plan(plan: SyncPlan, ack_view=None) -> str:
    """把 SyncPlan 渲染成 status/dry-run 文字。

    `ack_view`（`acks.AckView`，選配）＝**純呈現層過濾**：隱藏使用者已 `doctor --ack` 的 damaged/collision 行、
    附「N 項已 acknowledged」摘要（DESIGN A15）。**只影響顯示**——分類（`build_plan`/`classify`）與寫入（`apply`）
    完全不看它，acked 項仍為 blocked、apply 一律不碰（結構上不可能因 ack 而 auto-apply）。"""
    lines: list[str] = []
    if plan.first_run:
        lines.append("⚠ 首次同步（無 state）：請先 `--bootstrap` 建基線；以下為唯讀預覽。")
    for a in plan.anomalies:
        lines.append(f"[{a.severity.upper()}] {a.code}: {a.message}")
    if plan.halt:
        lines.append("→ 偵測到 halt 級異常，停止（不進行任何寫入）。")
        return "\n".join(lines)
    for pp in plan.projects:
        head = pp.hub_dir or pp.local_dir
        lines.append(f"\n專案 {head}  [{pp.identity}{'' if pp.coverage_initialized else ', 未bootstrap'}]")
        for n in pp.notes:
            lines.append(f"  · {n}")
        pk = Path(pp.hub_dir).name if pp.hub_dir else None
        hidden = ack_view.hidden.get(pk, frozenset()) if (ack_view and pk) else frozenset()
        n_hidden = 0
        for s in pp.sessions:
            # 護欄：**同時**要求 sid 已 ack **且** action 為 ackable——否則同 sid 在另一 local view 的
            # copy-to-hub/fork 等非 ackable 行會被 ack 誤藏（漏顯示待寫入，g3 High）；對稱 apply.format_report。
            if s.session_id in hidden and s.action in ACKABLE_ACTIONS:
                n_hidden += 1
                continue   # 已 ack 的 ackable blocked 行 → 呈現層隱藏（分類仍 blocked、apply 不動）
            d = f" ({s.direction})" if s.direction else ""
            # 寫 local 既有檔的動作（codex r6）：P1a 只標示；P1b 不直接覆蓋
            writes_local = s.action == "copy-to-local" or s.direction == "hub->local"
            caveat = "  ⚠P1b：寫 local 前過 active 檢查，無法確認未開啟則 keep-both/quarantine" if writes_local else ""
            lines.append(f"  - {s.session_id[:8]}: {s.action}{d} — {s.reason}{caveat}")
        if not pp.sessions:
            lines.append("  （無 session）")
        if n_hidden:
            lines.append(f"  · （{n_hidden} 項 damaged/collision 已 acknowledged；doctor --show-acked 可查）")
        for m in pp.memories:
            md = f" ({m.direction})" if m.direction else ""
            lines.append(f"  - memory {m.name}: {m.action}{md} — {m.reason}")
    return "\n".join(lines) if lines else "（無可同步項）"
