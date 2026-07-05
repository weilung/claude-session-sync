"""transfer：跨群明確 `pull`/`push`（DESIGN §8.1 git-remote 心智模型）。

與例行 `sync` 的差別（決定：stateless 明確選擇式傳輸）：
  - **單向**：pull = remote hub → local；push = local → remote hub（另一條方向只回報、不寫）。
  - **跨群**：對象是**別群組的 hub**（config `[remotes]` 具名），不是 own_hub。
  - **可挑選**：`--session <id>` 點名單一 session；不給則該方向全部待傳（dry-run 預設先看）。
  - **無 per-remote 基線**：不為 remote 維護 known/local_sessions/coverage——使用者的**明確選擇**取代了
    例行 sync 用來分辨「新檔 vs 已刪」的基線推論。故無 has_baseline/bulk/local-deleted 那套。

**保留的安全性質（與 sync 同）**：classify(§4.1) 全套（identical/ff/fork/damaged/collision）+ **C3**（pull 寫
local 一律 O_EXCL 新建或 keep-both、絕不覆蓋 local 既有檔）+ **A3**（respect **remote** hub 的 session
tombstone：條件式 suppress/conflict，不跨群復活已刪）+ sidecar 同一性（git 指紋 / `--map`，判不出拒落地）
+ 鎖 remote 檔（跨機共用資源序列化）+ 來源 stable-read（擋 active session 半截）+ dry-run 預設。

**本版範圍**：兩側都須**身分可解析**（git 指紋或 `--map`）。單邊存在 → needs-map（回報、不傳）。
跨群**新建專案**（含 sidecar/`_project.json` 與跨 OS 夾名）留後續；`--map` 指定的目標夾不存在時 push 會
建（僅放 session，不寫 sidecar）。互動 fork union/keep-both（resolve.py 為 own-hub）跨群版亦留後續。

**威脅模型 / 同一性（誠實聲明，codex r-transfer-2）**：remote 是**使用者自己的別群組 hub**（家/公司），
非對抗性第三方；且 CLI 的 plan→apply 為**單次 in-process 呼叫**（相隔數秒）。專案同一性目前靠
**夾名配對**（`--map` / git 指紋）+ `_safe_remote_dir`（解析後須在 root 內、非 symlink，擋逃逸——這是真正
的安全性質）。`_project.json` sidecar digest 有捕捉就重驗（forward-compat），但工具**目前尚未寫** sidecar，
故多半為 'absent'：在此前提下「remote 夾被同名抽換且無 sidecar」之殘留風險**有界**——sid 是 UUID（跨專案
不碰撞）＋ C3（pull 不覆蓋 local）⇒ 至多「錯置一個新 session（copy）」，**絕不覆蓋/丟失**既有資料、可逆。
更強的跨群同一性（push 時寫 `_project.json`、per-remote 指紋）留後續。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import anomaly, atomicio, scan, tombstone
from .anomaly import Anomaly
from .classify import classify
from .lineset import SessionShape, analyze, analyze_bytes

PULL = "pull"
PUSH = "push"

# 會自動落地的傳輸動作（其餘只回報）。
AUTO_TRANSFER = frozenset({"transfer-copy", "transfer-ff"})
_WROTE_RESULTS = frozenset({"copied-to-local", "kept-both-local", "copied-to-remote", "applied-ff-remote"})


@dataclass
class TransferItem:
    session_id: str
    action: str          # transfer-copy/transfer-ff/identical/dest-newer/source-absent/needs-decision/
                         # suppressed-deleted/conflict-delete-vs-update/blocked-*/
    reason: str


@dataclass
class TransferProject:
    local_dir: str | None
    remote_dir: str | None
    identity: str        # match / needs-map / ambiguous / blocked-multi-cwd / skipped-bad-map /
                         # skipped-unsafe / remote-only
    items: list[TransferItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    remote_sidecar: str = "absent"   # plan 時 remote `_project.json` 的 digest（apply 鎖內重驗，擋夾被抽換）


@dataclass
class TransferPlan:
    direction: str       # pull | push
    remote: str          # remote 名（顯示用）
    anomalies: list[Anomaly]
    projects: list[TransferProject]

    @property
    def halt(self) -> bool:
        return any(a.severity == "halt" for a in self.anomalies)


@dataclass
class TransferOutcome:
    session_id: str
    action: str
    result: str          # copied-to-local/kept-both-local/copied-to-remote/applied-ff-remote/identical/
                         # skipped-changed/skipped-locked/skipped-stale/reported/error/halt
    detail: str
    path: str | None = None


@dataclass
class TransferReport:
    outcomes: list[TransferOutcome] = field(default_factory=list)
    halted: bool = False
    halt_reason: str | None = None
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for o in self.outcomes:
            c[o.result] = c.get(o.result, 0) + 1
        return c

    @property
    def wrote_anything(self) -> bool:
        return any(o.result in _WROTE_RESULTS for o in self.outcomes)

    @property
    def had_error(self) -> bool:
        return any(o.result == "error" for o in self.outcomes)


def _safe_name(name: str) -> bool:
    """`--map` 目標夾名須是 remote_root 底下單一安全夾名（擋逃出信任根，比照 bootstrap）。"""
    return bool(name) and name == Path(name).name and name not in (".", "..")


def _sidecar_digest(remote_dir: Path) -> str:
    """remote 專案 `_project.json` 的 raw digest（無檔 → 'absent'）。供 apply 鎖內重驗夾未被抽換成別專案。"""
    sc = remote_dir / "_project.json"
    if sc.is_symlink():   # symlink _project.json → 視為 absent（不跟隨讀界外 digest，e2e gate4 #2）
        return "absent"
    return tombstone.raw_file_digest(sc) or "absent"


def _resolve_pair(
    ld: Path, remote_dirs: list[Path], mappings: dict[str, str], remote_root: Path,
    resolve: Callable[[Path, list[Path]], tuple[str, Path | None]],
) -> tuple[str, Path | None]:
    """local 夾 → remote 夾：`--map`（local夾名=remote夾名）優先，否則 git 指紋。判不出 → needs-map。
    解析到的 remote 夾須通過 `_safe_remote_dir`（擋 symlink 逃逸）→ 否則 skipped-unsafe。"""
    if ld.name in mappings:
        tgt = mappings[ld.name]
        if not _safe_name(tgt):
            return ("skipped-bad-map", None)
        rd: Path | None = remote_root / tgt  # 夾可能尚不存在（push 建 / pull 視為空）
    else:
        status, rd = resolve(ld, remote_dirs)
        if status != "match":
            return (status, rd)
    if rd is not None and not scan._safe_project_dir(remote_root, rd):
        return ("skipped-unsafe", None)
    return ("match", rd)


def _classify_transfer(
    sid: str, lf: Path | None, rf: Path | None, *,
    direction: str, tombs: dict, corrupt: set | None = None, collision: bool = False,
    src_shape: SessionShape | None = None,
) -> TransferItem:
    """單一 session 的傳輸分類（plan 與 apply-鎖內重分類共用 → 不漂移）。

    tombs/corrupt = **remote** hub 的 tombstone（A3：跨群也不復活已刪）。direction 決定 source/dest 與
    哪個 ff 方向可寫。`src_shape`（apply 提供）= 已讀進的**來源 bytes** 算出的 shape：用它做分類使「寫出的
    bytes」與「分類決策」綁定同一份（push 來源未持鎖，避免寫出未經分類/半截的內容，codex r-transfer-1）。"""
    if collision:
        return TransferItem(sid, "blocked-casefold-collision",
                            "casefold 撞名 sessionId（跨 OS 碰撞風險，A9）")
    # A3 tombstone 閘**先於**配對：remote 已刪此 session → 不跨群復活（條件式 suppress / 交人）。
    if ("session", sid) in tombs:
        sp = scan._suppress_or_conflict(sid, lf, rf, tombs[("session", sid)])
        return TransferItem(sid, sp.action, "remote " + sp.reason)
    if corrupt and ("session", sid) in corrupt:
        return TransferItem(sid, "blocked-tombstone-corrupt",
                            "remote tombstone 損壞、無法確認是否已刪 → 阻擋（fail-closed）")
    # 來源側用 src_shape（綁定 bytes）若有；否則由檔分析。目的側一律由檔分析。
    if direction == PULL:
        remote_shape = src_shape if src_shape is not None else (analyze(str(rf)) if rf else None)
        local_shape = analyze(str(lf)) if lf else None
    else:
        local_shape = src_shape if src_shape is not None else (analyze(str(lf)) if lf else None)
        remote_shape = analyze(str(rf)) if rf else None
    src_eff = remote_shape if direction == PULL else local_shape
    dst_eff = local_shape if direction == PULL else remote_shape
    if src_eff is None:
        return TransferItem(sid, "source-absent", "來源側無此 session，無可傳")
    if dst_eff is None:
        # 目的側無 → 新檔複製。來源損壞/無身分不可散播（同 sync 單邊 copy 閘）。
        if src_eff.is_damaged or not src_eff.uuids:
            return TransferItem(sid, "blocked-damaged-source",
                                "來源檔損壞/無對話身分（0-byte/壞行/空/可能正在寫），不複製")
        return TransferItem(sid, "transfer-copy", f"{direction}：來源單邊新檔")
    # 兩側都有 → classify(local, remote)；c.direction 'local->hub'=local 較新、'hub->local'=remote 較新。
    c = classify(local_shape, remote_shape)
    k = c.klass.value
    if k == "identical":
        return TransferItem(sid, "identical", "兩側相同")
    if k == "fast-forward":
        local_newer = c.direction == "local->hub"
        if direction == PULL:
            return (TransferItem(sid, "transfer-ff", "remote 較新 → 帶進 local（keep-both，不覆蓋）")
                    if not local_newer else
                    TransferItem(sid, "dest-newer", "local 已較新，無可 pull"))
        return (TransferItem(sid, "transfer-ff", "local 較新 → 寫入 remote")
                if local_newer else
                TransferItem(sid, "dest-newer", "remote 已較新，無可 push"))
    if k in ("fork", "superset-branch", "needs-decision"):
        return TransferItem(sid, "needs-decision", c.reason)
    return TransferItem(sid, f"blocked-{k}", c.reason)  # damaged / identity-collision


def _plan_pair(local_dir: Path | None, remote_dir: Path | None, direction: str,
               session: str | None) -> list[TransferItem]:
    local = scan._session_files(local_dir)
    remote = scan._session_files(remote_dir)
    collisions = scan._collision_casefolds(local.keys(), remote.keys())
    tombs = tombstone.read_tombstones(remote_dir) if remote_dir else {}
    corrupt = tombstone.corrupt_tombstone_targets(remote_dir) if remote_dir else set()
    sids = set(local) | set(remote)
    if session is not None:
        sids &= {session}
    items: list[TransferItem] = []
    for sid in sorted(sids):
        items.append(_classify_transfer(
            sid, local.get(sid), remote.get(sid), direction=direction,
            tombs=tombs, corrupt=corrupt, collision=sid.casefold() in collisions))
    return items


def plan_transfer(
    direction: str, local_root, remote_root, *,
    remote_name: str = "", session: str | None = None,
    mappings: dict[str, str] | None = None,
    identity_fn: Callable[[Path, list[Path]], tuple[str, Path | None]] | None = None,
) -> TransferPlan:
    """跨群傳輸的 dry-run 計畫（不寫任何檔）。identity_fn 可注入（測試）；預設 git 指紋。"""
    assert direction in (PULL, PUSH)
    local_root, remote_root = Path(local_root), Path(remote_root)
    mappings = mappings or {}
    resolve = identity_fn or scan._git_identity
    anomalies = anomaly.check(None, remote_root)  # remote 掛載存在性（無 state → 不查指紋/消失）
    if any(a.severity == "halt" for a in anomalies):
        return TransferPlan(direction, remote_name, anomalies, [])

    # 逃逸專案夾過濾（e2e gate G-High/G-Low + xgrp #3）：symlink/逃出 root 的 reparse 夾不讀/不寫、不跟隨（連
    # 候選 remote 的 sidecar 都不碰）；root 內 junction resolve 後仍在 root 內 → 允許。與 build_plan/bootstrap 一致。
    remote_dirs, _remote_unsafe = scan._list_project_dirs(remote_root)
    local_dirs, local_unsafe = scan._list_project_dirs(local_root)

    projects: list[TransferProject] = []
    for ld in local_unsafe:   # 逃逸 local 夾 → 可見 skipped-unsafe（push 不洩漏界外、pull 不寫界外）
        projects.append(TransferProject(str(ld), None, "skipped-unsafe", [],
                                        ["local 專案夾是 symlink 或逃逸 local_root（拒絕跟隨，避免讀/寫到信任根外）"]))
    matched_remote: set[Path] = set()
    for ld in local_dirs:
        status, rd = _resolve_pair(ld, remote_dirs, mappings, remote_root, resolve)
        if rd:
            matched_remote.add(rd)
        if status != "match":
            projects.append(TransferProject(str(ld), str(rd) if rd else None, status, [],
                                            [f"未對應 remote 專案（{status}）；需 --map <local夾>=<remote夾>"]))
            continue
        projects.append(TransferProject(str(ld), str(rd), "match", _plan_pair(ld, rd, direction, session),
                                        remote_sidecar=_sidecar_digest(rd)))

    # 多個 local 夾對到**同一** remote 夾（--map 撞 target 或 git 多對一）→ 全數跳過，否則 push 會把不同
    # 專案的 session 合併進一個 remote 夾（比照 bootstrap dup-key guard，codex r-transfer-3）。
    # **比對 resolve 後的實體路徑**（非字串）：junction 別名（`--map a=real` + `--map b=alias`，alias 為指向 real
    # 的 junction）字串不同但實體同夾，只比字串會漏 → 兩專案仍合進同一實體夾（e2e xgrp #5）。
    matched = [p for p in projects if p.identity == "match"]

    def _rkey(rd: str | None) -> str:
        try:
            return str(Path(rd).resolve()) if rd else ""
        except OSError:
            return rd or ""   # 解析失敗 → 退回字串（該夾另會被 `_safe_project_dir` 擋下）

    keyed = [(p, _rkey(p.remote_dir)) for p in matched]
    dup_targets = {k for k, n in Counter(k for _, k in keyed).items() if n > 1}
    for p, k in keyed:
        if k in dup_targets:
            p.identity, p.items = "skipped-dup-target", []
            p.notes = ["多個 local 夾對到同一 remote 夾（含 junction 別名指向同實體）→ 全數跳過（避免把不同專案合併進一個 remote 夾）"]

    # pull：remote 有、無 local 對應 → 無法落地（不建死夾），列待 --map。
    if direction == PULL:
        for rd in remote_dirs:
            if rd in matched_remote:
                continue
            projects.append(TransferProject(None, str(rd), "remote-only", [],
                                            ["remote 有、local 無對應專案；需 --map 才能 pull"]))
    return TransferPlan(direction, remote_name, anomalies, projects)


def format_plan(plan: TransferPlan) -> str:
    arrow = "remote→local" if plan.direction == PULL else "local→remote"
    lines = [f"{plan.direction} ({arrow})  remote={plan.remote or '?'}"]
    for a in plan.anomalies:
        lines.append(f"[{a.severity.upper()}] {a.code}: {a.message}")
    if plan.halt:
        lines.append("→ halt 級異常，停止。")
        return "\n".join(lines)
    for pp in plan.projects:
        head = pp.local_dir or pp.remote_dir
        lines.append(f"\n專案 {head}  [{pp.identity}]")
        for n in pp.notes:
            lines.append(f"  · {n}")
        for it in pp.items:
            lines.append(f"  - {it.session_id[:8]}: {it.action} — {it.reason}")
        if pp.identity == "match" and not pp.items:
            lines.append("  （無可傳項）")
    return "\n".join(lines)


def _stable_read(path: Path) -> bytes | None:
    """讀兩次比對：不一致（來源正被 append，如 active session）或讀不到 → None（呼叫端略過）。
    pull 的 remote 來源已持鎖穩定；push 的 local 來源未持鎖，靠此擋半截檔（DESIGN §6.8）。"""
    try:
        a = path.read_bytes()
        b = path.read_bytes()
    except OSError:
        return None
    return a if a == b else None


def _apply_one(
    item: TransferItem, *, local_dir: Path, remote_dir: Path, remote_root: Path, local_root: Path,
    remote_sidecar: str, direction: str, machine: str | None, lock_timeout_s: float,
) -> TransferOutcome:
    sid, action = item.session_id, item.action
    local_file = local_dir / f"{sid}.jsonl"
    remote_file = remote_dir / f"{sid}.jsonl"

    # 取鎖（會 mkdir 父夾）前先擋 symlink/junction 逃逸（plan 後夾可能被換成 reparse，codex r-transfer-3 + e2e）。
    # **remote 與 local 兩側都檢**：local 側逃逸會令 push 讀 root 外真檔洩漏 / pull 寫穿到 root 外（e2e xgrp #3）。
    if not scan._safe_project_dir(remote_root, remote_dir) or not scan._safe_project_dir(local_root, local_dir):
        return TransferOutcome(sid, action, "skipped-changed", "remote/local 專案夾不安全（symlink/逃出信任根），中止")

    # 鎖 remote 檔（跨機共用資源；其 .lock 父夾 acquire 時會建 → push 到新 --map 夾亦可）。
    try:
        lock = atomicio.FileLock(remote_file).acquire_blocking(timeout_s=lock_timeout_s)
    except atomicio.StaleLock as e:
        return TransferOutcome(sid, action, "skipped-stale", f"鎖疑似陳舊，交人工：{e}")
    except atomicio.LockError as e:
        return TransferOutcome(sid, action, "skipped-locked", f"取鎖逾時，略過：{e}")
    try:
        # 鎖內**再驗** symlink/逃逸（TOCTOU：pre-lock 檢查後夾/檔可能被換成 reparse）+ 拒 symlink session 檔
        # ——確保 lock 後實際讀/寫的路徑解析後仍在 root 內，不沿 symlink 讀/寫到信任根外（codex r-transfer-1/3 + e2e）。
        # **remote 與 local 兩側 + 各自 session 檔**都驗（local 側補端到端整合審抓到的缺口，e2e xgrp #3）。
        if (not scan._safe_project_dir(remote_root, remote_dir) or remote_file.is_symlink()
                or not scan._within_root(remote_root, remote_file)
                or not scan._safe_project_dir(local_root, local_dir) or local_file.is_symlink()
                or not scan._within_root(local_root, local_file)):
            return TransferOutcome(sid, action, "skipped-changed", "remote/local 路徑不安全（symlink/逃出信任根），中止")
        # 不可信 tombstone/夾 fail-stop（e2e gate10/11/12，對稱主 sync；transfer 不 gate on coverage 故此處自檢）：
        #   ① remote/local 專案夾不可列舉（POSIX read-denied）→ `_session_files`/`_symlink_name_keys` fail-open 漏 alias；
        #   ② remote `.tombstones/` **不安全（symlink/逃逸）或不可列舉** → `read_tombstones` 回 {}（拒讀界外／glob
        #      fail-open）＝漏刪除標記 → transfer-copy 復活已刪 session（A3）。`tombstones_enumerable` 同時涵蓋兩者。
        # 先擋（不存在的 dest 夾＝pull 待建 → `_dir_scannable` 回 True，不誤擋）。
        if (not scan._dir_scannable(remote_dir) or not scan._dir_scannable(local_dir)
                or not tombstone.tombstones_enumerable(remote_dir)):
            return TransferOutcome(sid, action, "skipped-changed",
                                   "remote/local 專案夾不可列舉、或 remote .tombstones/ 不安全/不可列舉 → 不自動處理（tombstone/alias 偵測失效，fail-closed）")
        # symlink-alias 防線（e2e gate9 finding1，對稱主 sync apply 的 `_leaf_symlink`）：dest/來源夾若有 **casefold
        # 或 normalization-alias** 的 symlink leaf（大寫 UUID `ABC.jsonl`、NFD 名），`_session_files` 略過 → 看似
        # absent → transfer-copy/ff 會把不可信 alias symlink 當 absent 寫入/覆蓋。上面 exact `is_symlink()` 只擋原
        # 字面；此處以 `scan._name_key`（NFC+casefold）比對兩側夾的 symlink leaf 名（夾已驗 `_safe_project_dir`）。
        name_key = scan._name_key(f"{sid}.jsonl")
        if (name_key in scan._symlink_name_keys(remote_dir)
                or name_key in scan._symlink_name_keys(local_dir)):
            return TransferOutcome(sid, action, "skipped-changed",
                                   "remote/local 有 symlink-alias 同名 session 檔（不可信），中止")
        # remote 專案同一性：`_project.json` digest 自 plan 後變（夾被抽換成別專案）→ 中止（codex r-transfer-2，
        # forward-compat；工具目前未寫 sidecar 故多半 'absent'，殘留風險見模組 docstring 威脅模型）。
        if _sidecar_digest(remote_dir) != remote_sidecar:
            return TransferOutcome(sid, action, "skipped-changed",
                                   "remote 專案 _project.json 自 plan 後已變（疑夾被抽換），中止")
        # 讀來源 bytes **一次**：pull 源=remote（已持鎖穩定）、push 源=local（未鎖→stable-read 擋 active 半截）。
        cur_lf = local_file if local_file.exists() else None
        cur_rf = remote_file if remote_file.exists() else None
        src_file = cur_rf if direction == PULL else cur_lf
        data = _stable_read(src_file) if src_file else None
        if data is None:
            return TransferOutcome(sid, action, "skipped-changed", "來源內容讀不到/不穩定（active？），中止")

        # 鎖內重新分類**用同一份 bytes** 算來源 shape → 寫出的 bytes 與決策綁定（codex r-transfer-1）。
        tombs = tombstone.read_tombstones(remote_dir)
        corrupt = tombstone.corrupt_tombstone_targets(remote_dir)
        coll = scan.casefold_collisions_for(local_dir, remote_dir)
        cur = _classify_transfer(sid, cur_lf, cur_rf, direction=direction, tombs=tombs,
                                 corrupt=corrupt, collision=sid.casefold() in coll,
                                 src_shape=analyze_bytes(data))
        if cur.action != action:
            return TransferOutcome(sid, cur.action, "skipped-changed",
                                   f"重新分類已變（{action}→{cur.action}），請重跑")

        if direction == PULL:
            # C3：絕不覆蓋 local 既有檔。copy→O_EXCL 新建；ff（local 已有、remote 較新）→ keep-both。
            if action == "transfer-copy":
                try:
                    atomicio.atomic_create_bytes(local_file, data)
                    return TransferOutcome(sid, action, "copied-to-local", "remote 單邊新檔複製到 local",
                                           str(local_file))
                except FileExistsError:
                    dest = atomicio.write_keep_both(local_file, data, machine=machine)
                    return TransferOutcome(sid, action, "kept-both-local",
                                           "local 期間冒出同名檔 → keep-both 不覆蓋", str(dest))
            dest = atomicio.write_keep_both(local_file, data, machine=machine)  # transfer-ff
            return TransferOutcome(sid, action, "kept-both-local",
                                   "remote 較新但不覆蓋 local，另存 keep-both（resume 即可接續）", str(dest))
        # push：寫 remote（允許覆蓋；ff 為 local 較新的純延伸，classify 已擋會丟標題的情形）。
        atomicio.atomic_write_bytes(remote_file, data)
        result = "copied-to-remote" if action == "transfer-copy" else "applied-ff-remote"
        return TransferOutcome(sid, action, result, "已寫入 remote hub", str(remote_file))
    except (atomicio.AtomicWriteError, OSError) as e:
        return TransferOutcome(sid, action, "error", f"寫入失敗（已中止該檔，未污染目標）：{e}")
    finally:
        lock.release()


def apply_transfer(
    plan: TransferPlan, *, local_root, remote_root,
    machine: str | None = None, lock_timeout_s: float = 5.0,
) -> TransferReport:
    """落地跨群傳輸。只自動套用 transfer-copy/transfer-ff；其餘只回報。"""
    local_root, remote_root = Path(local_root), Path(remote_root)
    report = TransferReport()

    halts = [f"{a.code}: {a.message}" for a in anomaly.check(None, remote_root) if a.severity == "halt"]
    if not local_root.is_dir():
        halts.append(f"local-mount-missing: local 根不存在或非目錄：{local_root}")
    if halts:
        report.halted = True
        report.halt_reason = "; ".join(halts)
        return report

    for label, d in (("remote", remote_root), ("local", local_root)):
        a = atomicio.assess_fs(d)
        if not a.can_write:
            report.warnings.append(f"{label} 目標不可寫：{a.reason}")
        elif not a.reliable:
            report.warnings.append(f"{label} FS 不可靠（best-effort + 已保留 rvw+lock）：{a.reason}")

    # 不用整體 hub-fingerprint 比對（與 push 合法建夾相衝，codex r-transfer-4）；改靠 _apply_one 鎖內
    # 逐專案 `_project.json` digest + symlink 重驗，精準擋「夾被抽換」而不擋自己建的新夾。
    for pp in plan.projects:
        if pp.identity != "match" or pp.remote_dir is None or pp.local_dir is None:
            for it in pp.items:
                report.outcomes.append(TransferOutcome(it.session_id, it.action, "reported", it.reason))
            continue
        local_dir, remote_dir = Path(pp.local_dir), Path(pp.remote_dir)
        for it in pp.items:
            if it.action not in AUTO_TRANSFER:
                report.outcomes.append(TransferOutcome(it.session_id, it.action, "reported", it.reason))
                continue
            report.outcomes.append(_apply_one(
                it, local_dir=local_dir, remote_dir=remote_dir, remote_root=remote_root,
                local_root=local_root, remote_sidecar=pp.remote_sidecar, direction=plan.direction,
                machine=machine, lock_timeout_s=lock_timeout_s))
    return report


def format_report(report: TransferReport) -> str:
    lines: list[str] = []
    for w in report.warnings:
        lines.append(f"⚠ {w}")
    if report.halted:
        lines.append(f"[HALT] {report.halt_reason}")
    for o in report.outcomes:
        lines.append(f"  - {o.session_id[:8]}: [{o.result}] {o.action} — {o.detail}"
                     + (f"  → {o.path}" if o.path else ""))
    c = report.counts()
    if c:
        lines.append("\n摘要：" + "；".join(f"{k}={v}" for k, v in sorted(c.items())))
    return "\n".join(lines) if lines else "（無可傳輸項）"
