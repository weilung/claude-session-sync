"""doctor：維護工具——唯讀診斷 + `--rebuild-state` + `--break-lock`（DESIGN §8.5/§9/A6/A15）。

- **診斷（無參）**：掛載/FS 可靠度/state 狀態/per-project coverage/同側 casefold 撞名/lock 概況。**純唯讀**。
- **`--rebuild-state`**：state 損壞/遺失時由**磁碟**重建（§8.5）。hub 側基線（known_sessions + hub_fingerprint）
  對所有 **已 coverage-initialized** 專案無條件重建；local 側（local_sessions/bindings/local_dir_bindings）
  需 `--map`（無 `_project.json` 時 git 指紋無法配對，決定 #7 不弱猜）→ 未 map 的專案 local 基線留空（下次
  sync 對該專案 present=hub 走 blocked-no-local-baseline，fail-closed，不復活）。**永不**讀寫/重建 tombstone
  （只從 hub 讀以排除已刪 sid），故 rebuild 不丟 tombstone、不復活已刪（§14 DoD）。preview 預設、`--yes` 落地。
- **`--break-lock`**：列出 `*.lock`；`--yes` 只移除「**同 host 且 PID 已死**」的 stale 鎖（移除前再驗一次）。
  跨 host / 仍存活 / 無法解析 → **不自動刪**（網路 FS 不可信 PID/時鐘，A6），列出交人工確認後手動刪。
"""
from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import acks, anomaly, atomicio, scan, state as state_mod, tombstone
from .config import Config
from .state import State

_LOCK_SUFFIX = ".lock"


def _safe_name(name: str) -> bool:
    """`--map` 夾名須是 root 底下單一安全夾名（非空、無分隔、非 . / ..、非絕對），擋逃出信任根（比照 bootstrap）。"""
    return bool(name) and name == Path(name).name and name not in (".", "..")


def _safe_dir(root: Path, d: Path) -> bool:
    """專案夾須真的在 root 內（非 symlink、解析後落在 root 內）→ 委派 `scan._safe_project_dir`（單一真相源，
    與 transfer/bootstrap 共用同一把逃逸檢查；e2e 整合審消除各自實作的重複與漂移）。"""
    return scan._safe_project_dir(root, d)


def hub_project_dirs(hub_root) -> list[Path]:
    """hub 底下**安全**（非 symlink/逃逸）的專案夾，排序。供 A15 ack/unack/show 列舉 ledger（與 diagnose 同一把
    `_safe_dir` 過濾，不跟隨逃逸夾）。hub 不存在/非目錄 → 空。"""
    hub_root = Path(hub_root)
    if not hub_root.is_dir():
        return []
    return sorted(d for d in hub_root.iterdir() if d.is_dir() and _safe_dir(hub_root, d))


# ── 診斷（唯讀）────────────────────────────────────────────────────────────

@dataclass
class DoctorReport:
    lines: list[str] = field(default_factory=list)
    problems: int = 0

    def ok(self, msg: str) -> None:
        self.lines.append(f"  ✓ {msg}")

    def warn(self, msg: str) -> None:
        self.lines.append(f"  ⚠ {msg}")
        self.problems += 1

    def info(self, msg: str) -> None:
        self.lines.append(f"  · {msg}")

    def head(self, msg: str) -> None:
        self.lines.append(f"\n{msg}")

    def text(self) -> str:
        return "\n".join(self.lines) if self.lines else "（無）"


def _assess(report: DoctorReport, label: str, d: Path) -> None:
    """**唯讀**檢查存在/目錄/可寫（`os.access`，不寫探測檔，故 diagnose 真正無副作用，codex r-doctor-1）。
    FS crash-safe 可靠度需寫探測，留給實際 sync 的 assess_fs 警告，doctor 不在此寫任何東西。"""
    if not d.exists():
        report.warn(f"{label}：不存在 {d}")
    elif not d.is_dir():
        report.warn(f"{label}：非目錄 {d}")
    elif os.access(d, os.W_OK):
        report.ok(f"{label}：存在可寫 {d}")
    else:
        report.warn(f"{label}：無寫入權限 {d}")


def diagnose(local_root, hub_root, state_path, config: Config | None = None) -> DoctorReport:
    """唯讀健康檢查。不改任何檔。"""
    local_root, hub_root = Path(local_root), Path(hub_root)
    r = DoctorReport()

    r.head("掛載 / 檔案系統")
    _assess(r, "hub", hub_root)
    _assess(r, "local", local_root)
    _assess(r, "state 目錄", Path(state_path).parent)

    r.head("state")
    try:
        st = state_mod.load_or_none(state_path)
        if st is None:
            r.info("state.json 不存在（首次同步前正常；已同步過則異常 → 可 --rebuild-state）")
        else:
            r.ok(f"state.json 正常（{len(st.known_sessions)} 專案 known、{len(st.local_sessions)} 專案 local 基線）")
    except state_mod.StateCorruptError as e:
        st = None
        r.warn(f"state.json 損壞：{e} → 可 doctor --rebuild-state")

    if st is not None and hub_root.exists():
        for a in anomaly.check(st, hub_root):
            (r.warn if a.severity == "halt" else r.info)(f"anomaly {a.code}: {a.message}")

    r.head("hub 專案")
    if hub_root.is_dir():
        for hd in sorted(d for d in hub_root.iterdir() if d.is_dir() and _safe_dir(hub_root, d)):
            stems = list(scan._session_files(hd))
            cov = "已bootstrap" if tombstone.is_initialized(hd) else "未bootstrap"
            dup = scan._collision_casefolds(stems, [])
            note = f"，⚠ casefold 撞名 {len(dup)}" if dup else ""
            tn = len(tombstone.read_tombstones(hd))
            # A15：diagnose **只 surface ack 記錄數 / 壞帳本警告，不據此降級撞名**。diagnose 是 hub-only 檢查、看不到
            # local 端，無法安全驗證 merged 撞名的 ack 是否仍成立——若「acked 後 local 又新增同 casefold 拼法」，
            # 該撞名在 sync 已因指紋不符重報，但 diagnose 用舊 hub 集合仍命中舊 ack → 誤把真撞名降級（R1 High#2）。
            # 故 diagnose 誠實計為問題；ack-aware 隱藏交給看得到 merged 證據的 sync/status 與 doctor --show-acked。
            led = acks.load_ledger(hd)
            if led.by_key:
                note += f"（{len(led.by_key)} 筆 ack；sync 依此隱藏、doctor --show-acked 可查）"
            r.info(f"{hd.name}：{len(stems)} session、{cov}、tombstone {tn}{note}")
            if dup:
                r.problems += 1
            if not led.ok:
                r.warn(f"{hd.name}：acks.json 損壞（已忽略、全部照常回報）")
    else:
        r.warn("hub 不存在，無法列專案")

    r.head("鎖（*.lock）")
    # 只遞迴掃 hub（per-session 鎖在那）+ **明確的** state 鎖檔；不遞迴掃 state 整個父夾（否則會列到
    # 該夾下無關的 *.lock，codex r-doctor-3）。
    locks = find_locks([hub_root], [Path(str(state_path) + _LOCK_SUFFIX)])
    if not locks:
        r.ok("無殘留鎖")
    for lk in locks:
        (r.warn if lk.status in ("stale", "foreign", "unparseable") else r.info)(
            f"[{lk.status}] {atomicio._disp(lk.path)}（host={atomicio._disp(lk.host)} pid={atomicio._disp(lk.pid)}）")
    return r


# ── 鎖檢查 / break-lock ─────────────────────────────────────────────────────

@dataclass
class LockEntry:
    path: Path
    status: str          # held（同host存活）/ stale（同host已死）/ foreign（他host）/ unparseable
    host: str | None
    pid: int | None
    token: str | None = None   # 列出時捕捉的唯一憑證；break-lock unlink 前據此確認仍是同一把鎖（擋 check→unlink race）


def _classify_lock(lock_path: Path) -> LockEntry:
    """讀一個 .lock 檔判定狀態。借用 FileLock 的 _read_info/_is_stale（同套規則，不漂移）。"""
    resource = str(lock_path)[: -len(_LOCK_SUFFIX)]
    fl = atomicio.FileLock(resource)
    info = fl._read_info()
    tok = info.token
    if info.host is None and info.pid is None:
        return LockEntry(lock_path, "unparseable", info.host, info.pid, tok)
    if fl._is_stale(info):
        return LockEntry(lock_path, "stale", info.host, info.pid, tok)
    status = "held" if info.host == atomicio._local_host() else "foreign"
    return LockEntry(lock_path, status, info.host, info.pid, tok)


def find_locks(scan_roots, lock_paths=()) -> list[LockEntry]:
    """分類鎖：`scan_roots` **遞迴**找 *.lock（hub）；`lock_paths` 為**明確**的 .lock 檔（如 state 鎖，避免
    遞迴掃到無關目錄，codex r-doctor-3）。排序穩定、去重。"""
    out: list[LockEntry] = []
    seen: set[Path] = set()
    candidates: list[Path] = []
    for root in scan_roots:
        root = Path(root)
        if not root.is_dir():
            continue
        # **不 rglob**（rglob 會遞迴進巢狀逃逸 junction、讀界外目錄 entries，e2e gate2 #5 / gate3 #5）：工具的鎖只在
        # 兩個已知位置——專案夾根（`<sid>.jsonl.lock`）與 `.tombstones/`（`memory.lock` 等）。只掃安全專案夾的這兩處
        # （`_list_project_dirs` 已濾逃逸；`.tombstones` 另驗非逃逸），並以 `_within_root` 過濾。
        safe_dirs, _ = scan._list_project_dirs(root)
        for sd in safe_dirs:
            candidates.extend(p for p in sorted(sd.glob("*" + _LOCK_SUFFIX)) if scan._within_root(root, p))
            tdir = sd / tombstone.TOMB_DIR
            if scan._safe_project_dir(sd, tdir):
                candidates.extend(p for p in sorted(tdir.glob("*" + _LOCK_SUFFIX)) if scan._within_root(root, p))
        # root 頂層直屬的 .lock（不在專案夾內）：glob（非 rglob）+ 過濾。
        candidates.extend(p for p in sorted(root.glob("*" + _LOCK_SUFFIX)) if scan._within_root(root, p))
    candidates.extend(Path(lp) for lp in lock_paths)   # 明確鎖（state 鎖）為信任、不過濾
    for p in candidates:
        if p in seen or not p.is_file():
            continue
        seen.add(p)
        out.append(_classify_lock(p))
    return out


@dataclass
class BreakReport:
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)   # (path, 原因)
    errors: list[str] = field(default_factory=list)  # 移除失敗（呼叫端據此非零退出，codex r-doctor-4）
    lines: list[str] = field(default_factory=list)


def break_locks(scan_roots, lock_paths=(), *, apply: bool) -> BreakReport:
    """列出鎖；apply=True 時**只**移除「同 host 且 PID 已死」的 stale 鎖。跨 host / 存活 / 無法解析一律保留，
    列出交人工（不自動信 PID/時鐘，A6）。

    移除前**再讀一次**且要求「仍是 stale **且 token 與列出時相同**」才 unlink——`os.unlink(path)` 刪的是
    路徑當下的檔、非剛才驗過的那個 inode（與 `FileLock.release` 的 token 身分核對同一紀律）。

    **單一 break-lock 呼叫在此已完全安全**：foreign stale 鎖只會被 break_locks 移除（`FileLock.acquire`
    遇 stale 一律 raise、**永不自動奪鎖**；`release` 只憑 token 刪自己那把）。故只要沒有第二個 break-lock
    介入，check→unlink 間該路徑不會被清空、writer 也無法在原地 O_EXCL 重建活鎖 → unlink 必定只刪到剛驗過
    的那把 stale 鎖（即使被排程 preempt 任意久亦然）。
    **有界殘留（單一操作者約束）**：若**同一 hub 同時跑兩個 `break-lock --yes`**——B 在 A 的 check→unlink 窗內
    （此窗可被排程任意拉長、**非** µs）刪掉 stale 鎖、writer C 立刻重取一把活鎖，A 之後的 unlink 可能誤刪 C 的
    活鎖＝雙 writer。故 break-lock 是**單一操作者的復原指令**：勿並行、勿於 sync 進行中執行（CLI 另有提醒；
    docs 原就要求「確認無其他同步在跑」）。此殘留與整套 O_EXCL+token 鎖在跨機網路 hub 上的可靠度同界，
    非對抗、可由操作紀律避免——codex breaklock-r1/r2 之 High 收斂取捨（未上 hot-path maintenance lock）。"""
    rep = BreakReport()
    for lk in find_locks(scan_roots, lock_paths):
        # 顯示用（中和 malformed 鎖的 surrogate/控制字元，免 print 崩）；unlink/removed/kept/errors 仍用**原始** lk.path。
        pa, h, pd = atomicio._disp(lk.path), atomicio._disp(lk.host), atomicio._disp(lk.pid)
        if lk.status == "stale" and apply:
            resource = str(lk.path)[: -len(_LOCK_SUFFIX)]
            fl = atomicio.FileLock(resource)
            fresh = fl._read_info()
            # 仍 stale 且仍是「剛才列出的同一把鎖」（token 相同）才刪；否則疑被重取成活鎖 → 不動。
            if fl._is_stale(fresh) and fresh.token == lk.token:
                try:
                    os.unlink(lk.path)
                    rep.removed.append(str(lk.path))
                    rep.lines.append(f"  ✓ 已移除 stale 鎖：{pa}（host={h} pid={pd} 已死）")
                    continue
                except OSError as e:
                    rep.errors.append(str(lk.path))
                    rep.lines.append(f"  ⚠ 移除失敗：{pa}：{atomicio._disp(e)}")
            else:
                rep.lines.append(f"  · 取消移除（鎖狀態已變，疑被重取）：{pa}")
        verb = "可移除（--yes）" if lk.status == "stale" else "保留（人工確認後手動刪）"
        if lk.status == "stale" and not apply:
            rep.kept.append(str(lk.path))
            rep.lines.append(f"  · [stale] {pa}（host={h} pid={pd} 已死）→ {verb}")
        elif lk.status != "stale":
            rep.kept.append(str(lk.path))
            reason = {"held": "同機進程持有中（存活）", "foreign": "他機持有，無法判存活",
                      "unparseable": "內容無法解析"}.get(lk.status, lk.status)
            rep.lines.append(f"  · [{lk.status}] {pa}（host={h} pid={pd}）→ 保留：{reason}")
    if not rep.lines:
        rep.lines.append("  ✓ 無殘留鎖")
    return rep


# ── rebuild-state ───────────────────────────────────────────────────────────

@dataclass
class RebuildResult:
    state: State
    lines: list[str] = field(default_factory=list)
    fatal: bool = False   # hub 不存在/非目錄 → 無法重建，呼叫端**不可**落地（否則寫出空 state）


def _live_stems(stems_dir: Path, tomb_dir: Path | None = None) -> set[str]:
    """stems_dir 現有 session stem 扣掉**已 tombstone** 者（基線只記活的；tombstone 永不重建、只讀）。

    tombstone 為 **hub 所有**（codex r-doctor-2）：hub 側 stems_dir==tomb_dir；**local 側 tomb_dir 須傳對應
    hub 夾**（local 夾本身沒有 tombstone，否則 local 端會漏扣 hub 已刪者）。"""
    tomb_dir = tomb_dir if tomb_dir is not None else stems_dir
    stems = set(scan._session_files(stems_dir))
    tombs = {t for (k, t) in tombstone.read_tombstones(tomb_dir) if k == "session"}
    return stems - tombs


def rebuild_state(
    local_root, hub_root, *, mappings: dict[str, str] | None = None,
    identity_fn=None,
) -> RebuildResult:
    """由磁碟重建一份**全新** State（不讀舊 state，故損壞亦可救）。hub 側無條件、local 側需 --map。
    永不讀寫 tombstone（只讀以排除已刪 sid）。回 RebuildResult（state + 預覽行）；落地由呼叫端加鎖寫。"""
    local_root, hub_root = Path(local_root), Path(hub_root)
    mappings = mappings or {}
    st = State()
    res = RebuildResult(state=st)

    if not hub_root.is_dir():
        res.fatal = True
        res.lines.append(f"⚠ hub 不存在或非目錄：{hub_root} → 無法重建（不寫 state）")
        return res
    st.hub_fingerprint = anomaly.hub_fingerprint(hub_root)

    # 排除 symlink/逃逸的專案夾（不從 root 外的夾建基線，codex r-doctor-2）。
    hub_dirs = [d for d in sorted(hub_root.iterdir()) if d.is_dir() and _safe_dir(hub_root, d)]
    initialized = {hd.name: hd for hd in hub_dirs if tombstone.is_initialized(hd)}
    for pk, hd in initialized.items():
        # 不可列舉 hub 夾 fail-stop（e2e gate11 finding2）：`_live_stems`→`_session_files` glob **fail-open** 回空 →
        # 基線漏現存 session → 日後復活。可讀但真空 → 照常。（.tombstones/ 不可列舉已由 is_initialized False 濾掉。）
        if not scan._dir_scannable(hd):
            res.lines.append(f"  ⚠ hub 專案夾不可列舉（權限）→ 略過基線（fail-closed）：{pk}")
            continue
        st.known_sessions[pk] = _live_stems(hd)
        res.lines.append(f"  · hub 基線 {pk}：known={len(st.known_sessions[pk])}")
    skipped = [hd.name for hd in hub_dirs if hd.name not in initialized]
    if skipped:
        res.lines.append(f"  · 略過未 bootstrap 的 hub 專案（不重建基線）：{', '.join(skipped)}")

    # local 側：僅 --map 明示（local夾名=hub夾名）。未 map → 該專案無 local 基線（下次 sync fail-closed）。
    # 先驗夾名安全（擋 ../ 絕對路徑逃出 root，codex r-doctor-3）+ 拒多 local 對同一 hub（避免互覆）。
    valid_map: dict[str, str] = {}
    for ln, hn in sorted(mappings.items()):
        if not _safe_name(ln) or not _safe_name(hn):
            res.lines.append(f"  ⚠ --map {ln}={hn}：夾名不安全（須單一夾名），略過")
            continue
        valid_map[ln] = hn
    dup_hub = {h for h, c in Counter(valid_map.values()).items() if c > 1}
    for local_name, hub_name in valid_map.items():
        if hub_name in dup_hub:
            res.lines.append(f"  ⚠ --map …={hub_name}：多個 local 對到同一 hub 夾 → 全數略過 local 基線")
            continue
        ld = local_root / local_name
        hd = initialized.get(hub_name)
        if hd is None:
            res.lines.append(f"  ⚠ --map {local_name}={hub_name}：hub 專案不存在或未 bootstrap，略過 local 基線")
            continue
        if not ld.is_dir() or not _safe_dir(local_root, ld) or not scan._dir_scannable(ld):
            res.lines.append(f"  ⚠ --map {local_name}={hub_name}：local 夾不存在/非目錄/symlink逃逸/不可列舉 {ld}，略過 local 基線")
            continue
        st.local_sessions[hub_name] = _live_stems(ld, hd)   # tombstone 由 **hub** 夾讀（codex r-doctor-2）
        st.local_dir_bindings[local_name] = hub_name
        cwds = scan._project_cwds(ld)
        if len(cwds) == 1:
            st.bindings[next(iter(cwds))] = hub_name
        res.lines.append(f"  · local 基線 {local_name}→{hub_name}：local={len(st.local_sessions[hub_name])}")

    no_local = sorted(set(initialized) - set(st.local_sessions))
    if no_local:
        res.lines.append("  · 無 local 基線（需 --map 才能對該專案雙向同步；否則 present=hub 走 "
                         f"blocked-no-local-baseline）：{', '.join(no_local)}")
    return res


def write_rebuilt_state(result: RebuildResult, state_path, *, lock_timeout_s: float = 5.0) -> str:
    """把重建的 State **覆寫**落地（加鎖；不讀舊內容，故損壞 state 亦可救）。回路徑。"""
    p = Path(state_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock = atomicio.FileLock(p).acquire_blocking(timeout_s=lock_timeout_s)
    try:
        return state_mod.save(result.state, p)
    finally:
        lock.release()
