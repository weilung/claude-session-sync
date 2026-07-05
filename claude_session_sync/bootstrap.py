"""bootstrap：首次同步建基線（決定 #9 + codex r3 信任邊界）。掃兩邊現況 → 寫 coverage + state baseline。

**不複製、不刪 session/memory 資料**：只寫 per-project `_coverage.json`（initialized）+ state（known/
local/bindings/hub_fingerprint）+ 被 `--ignore` 的單邊檔的 suppress tombstone。

信任邊界（codex r3）：現存**單邊**檔在 bootstrap 後會被視為「可匯入」（下次 sync 複製到對側）。故落地前
必須印出**完整 baseline diff** 並由使用者確認；不想傳播的「刪除殘留」以 `--ignore` 排除——
排除 = 寫一條 suppress tombstone（檔留原地、永不複製到對側），不刪檔。

**P1d Block 3a：memory 基線（A17.1 對稱 session）。** 同時掃 `<proj>/memory/` 寫 `known_memory`/`local_memory`
基線——否則 `memory.classify_memory` 對單邊 memory 一律 `blocked-no-baseline`（刻意 fail-closed）。`--ignore`
也涵蓋 memory 檔名（與 sid 命名空間天然不交集：sid=UUID、memory=`*.md`）→ 寫 memory suppress tombstone
（base=正規化 `content_hash`、identity=frontmatter `name`，對稱 Block 2b 契約；走 per-project memory 鎖，與
未來 memory apply 互斥）。`memory/` 根是 symlink（`UnsafeMemoryDir`）→ 不建該專案 memory 基線（fail-closed，
下次 sync 仍 blocked-no-baseline，不誤把指向空/錯夾當「memory 全空」而傳播刪除）。

P1b 範圍：只 bless **已配對**（兩側夾都在且 git 指紋相符）或 **--map 明示**的專案；判不出一律 skip 待
`--map`（決定 #7，不猜跨 OS 編碼夾名）。空 hub 的首推請用 `--map <local夾名>=<hub夾名>`。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import anomaly, atomicio, memory, scan, state as state_mod, tombstone
from .anomaly import Anomaly
from .state import State


class BootstrapChanged(RuntimeError):
    """確認後、落地前，磁碟現況已與所確認的 baseline diff 不符 → 拒絕落地，請重跑（codex r9-1）。"""


def _safe_hub_name(name: str) -> bool:
    """--map 的 hub 目標必須是 hub_root 底下的**單一安全夾名**：非空、不含分隔、非 . / ..、非絕對路徑。
    擋 `../outside`、絕對路徑等逃出 hub 信任根（codex r9-3）。"""
    return bool(name) and name == Path(name).name and name not in (".", "..")


@dataclass
class ProjectBaseline:
    local_dir: str | None
    hub_dir: str | None
    project_key: str | None       # hub 夾名（known_sessions / bindings 的 key）
    cwd: str | None
    both: list[str] = field(default_factory=list)
    local_only: list[str] = field(default_factory=list)
    hub_only: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    # memory（檔名集，diff 同 session；Block 3a）。mem_unsafe=memory/ 根是 symlink → 不建記憶基線。
    mem_both: list[str] = field(default_factory=list)
    mem_local_only: list[str] = field(default_factory=list)
    mem_hub_only: list[str] = field(default_factory=list)
    mem_ignored: list[str] = field(default_factory=list)
    mem_unsafe: bool = False
    status: str = "mapped"        # mapped / skipped-<reason>

    @property
    def importable(self) -> list[str]:
        """bootstrap 後會被複製到對側的單邊 session 檔（已扣除 ignored）。"""
        ig = set(self.ignored)
        return sorted((set(self.local_only) | set(self.hub_only)) - ig)

    @property
    def mem_importable(self) -> list[str]:
        """bootstrap 後會被複製到對側的單邊 memory 檔（已扣除 mem_ignored）。"""
        ig = set(self.mem_ignored)
        return sorted((set(self.mem_local_only) | set(self.mem_hub_only)) - ig)


@dataclass
class BootstrapPlan:
    projects: list[ProjectBaseline]
    anomalies: list[Anomaly]

    @property
    def halt(self) -> bool:
        return any(a.severity == "halt" for a in self.anomalies)

    @property
    def mapped(self) -> list[ProjectBaseline]:
        return [p for p in self.projects if p.status == "mapped"]


def _stems(d: Path | None) -> set[str]:
    return set(scan._session_files(d).keys()) if d else set()


def _mem_names(proj_dir: Path | None) -> tuple[set[str], bool]:
    """專案夾下 `memory/` 的 memory 檔名集 + unsafe 旗標。回 (names, unsafe)。

    unsafe=True 表 `memory/` 根是 symlink（`UnsafeMemoryDir`）→ 上層不建該專案記憶基線（fail-closed）。
    其它 OSError（不可讀夾）刻意**不吞**、向上拋（fail-stop，與 `list_memory_files` 一致——把不可讀誤當
    「memory 全空」會看似大量刪除）。proj_dir=None / memory 夾不存在 → (空集, False)。"""
    if proj_dir is None:
        return set(), False
    try:
        return set(memory.list_memory_files(memory.memory_dir(proj_dir)).keys()), False
    except memory.UnsafeMemoryDir:
        return set(), True


def scan_baseline(
    local_root, hub_root, state: State | None, *,
    identity_fn=None, mappings: dict[str, str] | None = None, ignore: set[str] | None = None,
) -> BootstrapPlan:
    """唯讀算出每個專案的 baseline diff（不寫任何檔）。供顯示 + 確認。

    mappings：{local 夾名 → hub 夾名} 明示對應（覆寫 git 指紋解析，供空 hub 首推 / 指紋判不出時）。
    """
    local_root, hub_root = Path(local_root), Path(hub_root)
    mappings = mappings or {}
    ignore = ignore or set()
    resolve = identity_fn or scan._git_identity

    anomalies = anomaly.check(state, hub_root)
    if any(a.severity == "halt" for a in anomalies):
        return BootstrapPlan(projects=[], anomalies=anomalies)

    # 逃逸專案夾過濾（e2e gate G-Low + xgrp #4）：symlink/逃出 root 的 reparse 夾不讀/不寫（連 sidecar/cwd 都不碰）。
    # root 內 junction 允許（resolve 仍在 root 內）。與 build_plan/transfer/doctor 同一把 `_safe_project_dir`。
    hub_dirs, _hub_unsafe = scan._list_project_dirs(hub_root)
    local_dirs, local_unsafe = scan._list_project_dirs(local_root)

    projects: list[ProjectBaseline] = []
    for ld in local_unsafe:   # 逃逸 local 夾 → skipped-unsafe（不從 root 外夾建基線/bless）
        projects.append(ProjectBaseline(
            local_dir=str(ld), hub_dir=None, project_key=None, cwd=None, status="skipped-unsafe"))
    for ld in local_dirs:
        # --map 明示優先；否則走 git 指紋解析（hub_dirs 已濾逃逸，git 解析不會讀到不安全 hub 夾 sidecar）。
        if ld.name in mappings:
            if _safe_hub_name(mappings[ld.name]):
                hub_dir = hub_root / mappings[ld.name]
                status = "mapped"
            else:
                hub_dir, status = None, "skipped-bad-map"  # 逃出 hub 信任根 → 拒絕
        else:
            st, hub_dir = resolve(ld, hub_dirs)
            status = "mapped" if st == "match" else f"skipped-{st}"

        # hub 專案夾逃逸檢查（--map 目標或 git 解析到的 hub 夾若為既存 symlink/junction 逃出 hub_root）→ 不建基線、
        # 不寫 tombstone/coverage 到 root 外（e2e xgrp #4）。待建的空 hub 夾（不存在）resolve 字面仍在 root 內 → 放行。
        if status == "mapped" and hub_dir is not None and not scan._safe_project_dir(hub_root, hub_dir):
            hub_dir, status = None, "skipped-unsafe"

        cwds = scan._project_cwds(ld)
        cwd = next(iter(cwds)) if len(cwds) == 1 else None
        if status == "mapped" and len(cwds) > 1:
            status = "skipped-multi-cwd"  # 夾名有損混入多 cwd → 不可建單一綁定

        # 不可列舉夾 fail-stop（e2e gate11 finding2）：local/hub 專案夾存在但不可讀（POSIX read-denied）→ `_stems`
        # 的 glob **fail-open** 回空 → baseline 漏掉現存 session → 日後真正刪除認不出、hub 檔被復活。故不建基線、標
        # skipped-unreadable（fail-closed；memory 基線 `_mem_names`/list_memory_files 本就 fail-stop 一致）。可讀但
        # 真的空 → 照常建（空基線語意不變）。放在 `_stems`/`_project_cwds` 讀之前總擋。
        if not scan._dir_scannable(ld) or (hub_dir is not None and not scan._dir_scannable(hub_dir)):
            projects.append(ProjectBaseline(
                local_dir=str(ld), hub_dir=str(hub_dir) if hub_dir else None,
                project_key=None, cwd=cwd, status="skipped-unreadable"))
            continue

        local_s = _stems(ld)
        hub_s = _stems(hub_dir) if (hub_dir and hub_dir.exists()) else set()
        both = sorted(local_s & hub_s)
        local_only = sorted(local_s - hub_s)
        hub_only = sorted(hub_s - local_s)
        single = set(local_only) | set(hub_only)
        # memory 基線（檔名 diff，對稱 session；mem_unsafe → 任一側 memory/ 根是 symlink，不建記憶基線）。
        local_m, lmu = _mem_names(ld)
        hub_m, hmu = _mem_names(hub_dir if (status == "mapped" and hub_dir) else None)
        mem_both = sorted(local_m & hub_m)
        mem_local_only = sorted(local_m - hub_m)
        mem_hub_only = sorted(hub_m - local_m)
        mem_single = set(mem_local_only) | set(mem_hub_only)
        projects.append(ProjectBaseline(
            local_dir=str(ld),
            hub_dir=str(hub_dir) if hub_dir else None,
            project_key=hub_dir.name if (status == "mapped" and hub_dir) else None,
            cwd=cwd,
            both=both, local_only=local_only, hub_only=hub_only,
            ignored=sorted(single & ignore),
            mem_both=mem_both, mem_local_only=mem_local_only, mem_hub_only=mem_hub_only,
            mem_ignored=sorted(mem_single & ignore), mem_unsafe=(lmu or hmu),
            status=status,
        ))

    # 多個 local 撞同一 hub project_key、或同一 cwd → 落地會互覆/誤綁 → 全數 skip（不挑）（codex r9-3）。
    mapped = [p for p in projects if p.status == "mapped"]
    key_dups = {k for k, n in Counter(p.project_key for p in mapped).items() if n > 1}
    cwd_dups = {c for c, n in Counter(p.cwd for p in mapped if p.cwd is not None).items() if n > 1}
    for p in mapped:
        if p.project_key in key_dups:
            p.status = "skipped-dup-key"
        elif p.cwd is not None and p.cwd in cwd_dups:
            p.status = "skipped-dup-cwd"
    return BootstrapPlan(projects=projects, anomalies=anomalies)


def format_baseline(plan: BootstrapPlan) -> str:
    lines: list[str] = []
    for a in plan.anomalies:
        lines.append(f"[{a.severity.upper()}] {a.code}: {a.message}")
    if plan.halt:
        lines.append("→ halt 級異常，bootstrap 中止。")
        return "\n".join(lines)
    lines.append("bootstrap baseline 預覽（**不複製、不刪**；確認後現存單邊檔將於下次 sync 視為可匯入）：")
    for p in plan.projects:
        head = p.local_dir or p.hub_dir
        if p.status != "mapped":
            lines.append(f"\n專案 {head}  [{p.status}]  → 跳過（需 --map）")
            continue
        lines.append(f"\n專案 {head}  → hub={p.project_key}  cwd={p.cwd}")
        lines.append(f"  兩側皆有：{len(p.both)}；local-only：{len(p.local_only)}；hub-only：{len(p.hub_only)}")
        if p.importable:
            lines.append(f"  ⚠ 將被視為可匯入（複製到對側）：{', '.join(s[:8] for s in p.importable)}")
        if p.ignored:
            lines.append(f"  · 已忽略（寫 suppress tombstone、不傳播）：{', '.join(s[:8] for s in p.ignored)}")
        if p.mem_unsafe:
            lines.append("  · ⚠ memory/ 根是 symlink → 跳過記憶基線（請改為實體目錄後重跑）")
        else:
            lines.append(f"  記憶：兩側 {len(p.mem_both)}；local-only {len(p.mem_local_only)}；"
                         f"hub-only {len(p.mem_hub_only)}")
            if p.mem_importable:
                lines.append(f"  ⚠ 記憶將被視為可匯入：{', '.join(p.mem_importable)}")
            if p.mem_ignored:
                lines.append(f"  · 記憶已忽略（寫 suppress tombstone）：{', '.join(p.mem_ignored)}")
    if not plan.mapped:
        lines.append("\n（無已配對專案可建基線；請用 --map <local夾名>=<hub夾名> 明示對應）")
    return "\n".join(lines)


def _revalidate(plan: BootstrapPlan) -> None:
    """落地前重掃每個 mapped 專案，與**所確認**的 diff 比對；不符即中止（codex r9-1：擋確認後冒出的檔
    被悄悄 bless）。bootstrap 是明確的一次性操作，重掃-比對足以擋住確認後的漂移。"""
    for p in plan.mapped:
        cur_local = _stems(Path(p.local_dir)) if p.local_dir else set()
        cur_hub = _stems(Path(p.hub_dir)) if (p.hub_dir and Path(p.hub_dir).exists()) else set()
        if cur_local != set(p.both) | set(p.local_only) or cur_hub != set(p.both) | set(p.hub_only):
            raise BootstrapChanged(
                f"專案 {p.project_key} 自確認後內容已變（有檔新增/移除）——請重跑 bootstrap 再確認。"
            )
        # memory drift（mem_unsafe 的專案不建記憶基線 → 不比；重掃變 unsafe 由下方集合不符觸發 abort）。
        if not p.mem_unsafe:
            cur_lm, lmu = _mem_names(Path(p.local_dir)) if p.local_dir else (set(), False)
            cur_hm, hmu = _mem_names(Path(p.hub_dir)) if p.hub_dir else (set(), False)
            if (lmu or hmu or cur_lm != set(p.mem_both) | set(p.mem_local_only)
                    or cur_hm != set(p.mem_both) | set(p.mem_hub_only)):
                raise BootstrapChanged(
                    f"專案 {p.project_key} 記憶自確認後已變（檔案增減或 memory/ 變 symlink）——請重跑 bootstrap。"
                )


def apply_baseline(
    plan: BootstrapPlan, hub_root, state_path, *,
    machine: str | None = None, lock_timeout_s: float = 5.0,
) -> dict:
    """確認後落地。次序刻意為 **tombstone → state baseline(加鎖) → coverage(最後)**：coverage 是
    「此專案可開始匯入」的信任邊界 go 訊號，放最後 → 若 state 提交失敗，專案仍 uninitialized（sync 續 block），
    不會出現「已 initialized 但無 baseline」的危險半成品（codex r9-2）。不複製、不刪 session 資料。"""
    if plan.halt:
        raise RuntimeError("plan 含 halt 異常，拒絕 apply")
    hub_root = Path(hub_root)
    # 落地前重檢掛載/存在性（scan 與 apply 之間 hub 可能消失/掛錯）——否則會在錯的 FS 上建空夾並 bless（codex r11-5）。
    rehalt = [a for a in anomaly.check(None, hub_root) if a.severity == "halt"]
    if rehalt:
        raise RuntimeError("落地前重檢異常：" + "; ".join(f"{a.code}: {a.message}" for a in rehalt))
    # 落地前**先**重驗逃逸（先於 `_revalidate` 的讀取，e2e gate3 #4）：scan 與 apply 間夾可能被換成 symlink/junction；
    # hub 是寫入目標、local 是讀取來源，逃逸都不可跟隨（否則 `_revalidate` 會先讀界外樹再拒＝讀-escape）。任一不符 → 拒絕。
    for p in plan.mapped:
        hd = Path(p.hub_dir)
        if not scan._safe_project_dir(hub_root, hd):
            raise BootstrapChanged(
                f"專案 {p.project_key} 的 hub 夾自確認後成 symlink/逃逸 hub_root → 拒絕落地（不寫中繼到信任根外），請重跑。")
        if not scan._safe_project_dir(hd, hd / tombstone.TOMB_DIR):   # .tombstones 逃逸 → 不寫 coverage/tombstone 到界外（e2e gate3 #3）
            raise BootstrapChanged(
                f"專案 {p.project_key} 的 .tombstones 為 symlink/逃逸 → 拒絕落地（不寫中繼到界外），請重跑。")
        if p.local_dir is not None and not scan._safe_project_dir(Path(p.local_dir).parent, Path(p.local_dir)):
            raise BootstrapChanged(
                f"專案 {p.project_key} 的 local 夾自確認後成 symlink/逃逸 → 拒絕落地，請重跑。")
    _revalidate(plan)

    tombstoned: list[str] = []
    mem_tombstoned: list[str] = []
    # 1) 建 hub 夾（空 hub 首推）+ ignored 單邊檔 → suppress tombstone（檔留原地、永不複製）。
    for p in plan.mapped:
        hub_dir = Path(p.hub_dir)
        hub_dir.mkdir(parents=True, exist_ok=True)
        local_dir = Path(p.local_dir) if p.local_dir else None
        for sid in p.ignored:
            src = (hub_dir / f"{sid}.jsonl") if sid in p.hub_only else (
                (local_dir / f"{sid}.jsonl") if local_dir else None)
            base = tombstone.raw_file_digest(src) if (src and src.exists()) else None
            # 與 apply 共用同一把 per-session 鎖（hub 側路徑），讓 tombstone 寫與 apply 的 gate 互斥
            # ——否則 tombstone 可能在 apply gate 之後、copy 之前才出現而復活已抑制的 session（codex r10-4）。
            lk = atomicio.FileLock(hub_dir / f"{sid}.jsonl").acquire_blocking(timeout_s=lock_timeout_s)
            try:
                tombstone.write_session_tombstone(hub_dir, sid, base_hash=base, machine=machine)
            finally:
                lk.release()
            tombstoned.append(f"{p.project_key}:{sid}")
        # ignored memory → suppress tombstone（base=正規化 content_hash、identity=frontmatter name，對稱
        # Block 2b 契約）。走 **per-project memory 鎖**（hub 側 `.tombstones/memory`），與未來 memory apply 的
        # gate 互斥（同 session：tombstone 寫須在 apply 取鎖前後互斥，免復活已抑制 memory）。mem_unsafe 不寫。
        mem_ig = [n for n in p.mem_ignored if not p.mem_unsafe]
        if mem_ig:
            hub_mdir = memory.memory_dir(hub_dir)
            local_mdir = memory.memory_dir(local_dir) if local_dir else None
            mlk = atomicio.FileLock(
                tombstone.tombstones_dir(hub_dir) / "memory").acquire_blocking(timeout_s=lock_timeout_s)
            try:
                for name in mem_ig:
                    if not tombstone.is_tombstone_safe_name(name):
                        continue  # 含路徑分隔的不可逆檔名 → 不寫 tombstone（sync classify 另以 blocked-unsupported-name 擋）
                    src = (hub_mdir / name) if name in p.mem_hub_only else (
                        (local_mdir / name) if local_mdir else None)
                    doc = memory.load_memory(src) if (src and src.exists()) else None
                    base = memory.content_hash(doc) if doc else None  # damaged → None（tombstone 仍擋傳播，走 conflict）
                    identity = doc.name if doc else None              # frontmatter name slug（無 → None）
                    tombstone.write_memory_tombstone(hub_dir, name, base_hash=base,
                                                     machine=machine, identity=identity)
                    mem_tombstoned.append(f"{p.project_key}:mem:{name}")
            finally:
                mlk.release()

    # 2) state baseline：一次加鎖 RMW，保留未 bootstrap 的專案條目。known/bindings 取自**所確認**的
    #    plan（非重新 glob），避免確認後冒出的檔被納入 baseline。
    ignore_by_pk = {p.project_key: set(p.ignored) for p in plan.mapped}
    fp = anomaly.hub_fingerprint(hub_root)

    def _mutate(s: State) -> None:
        s.hub_fingerprint = fp
        for p in plan.mapped:
            ig = ignore_by_pk.get(p.project_key, set())
            # known = 確認當下 hub 現況（both ∪ hub_only）- ignored（local-only 待 sync 複製後才記為已知）
            s.known_sessions[p.project_key] = (set(p.both) | set(p.hub_only)) - ig
            # local baseline = 確認當下 local 現況（both ∪ local_only）- ignored（對稱刪除追蹤起點，P1c）。
            # 之後本機刪除某 local session 時，下次 sync 才能分辨「本機刪除」與「對側新檔」。
            s.local_sessions[p.project_key] = (set(p.both) | set(p.local_only)) - ig
            # memory 基線（對稱 session；A17.1）。**mem_unsafe 不建**——否則把 symlink 指向的空/錯夾當「memory
            # 全空」基線，下次 sync 會把對側 memory 當「本機已刪」而寫抑制 tombstone。不建 → 該專案 memory 維持
            # blocked-no-baseline（fail-closed），待改實體目錄重 bootstrap。**空集 ≠ 缺欄位**（後者才是 migration）。
            if not p.mem_unsafe:
                mig = set(p.mem_ignored)
                s.known_memory[p.project_key] = (set(p.mem_both) | set(p.mem_hub_only)) - mig
                s.local_memory[p.project_key] = (set(p.mem_both) | set(p.mem_local_only)) - mig
            else:
                # re-bootstrap 一個曾有基線、現 memory/ 變 symlink 的專案：必須**清掉** stale 基線（pop），否則
                # 舊基線殘留會讓下次 sync 仍以為有可信基線 → 可能把 hub memory 當「本機已刪」寫抑制 tombstone
                # 蓋掉真實 memory（codex 3a-R1 #1 high）。pop 後退回 no-baseline（fail-closed），待改實體目錄重建。
                s.known_memory.pop(p.project_key, None)
                s.local_memory.pop(p.project_key, None)
            if p.cwd is not None:
                s.bindings[p.cwd] = p.project_key
            if p.local_dir is not None:
                # 夾名綁定：供「session 全刪 → 空夾無 cwd」時仍能配對偵測刪除（codex r25）。
                s.local_dir_bindings[Path(p.local_dir).name] = p.project_key

    state_mod.update_under_lock(_mutate, state_path, lock_timeout_s=lock_timeout_s)

    # 3) coverage 最後寫（信任邊界 go 訊號）：新 epoch（re-bootstrap 視為新紀元，連帶令舊決策快照失效）。
    blessed: list[str] = []
    for p in plan.mapped:
        hub_dir = Path(p.hub_dir)
        prev = tombstone.read_coverage(hub_dir)
        epoch = (prev.epoch + 1) if prev else 1
        tombstone.write_coverage(hub_dir, epoch=epoch, machine=machine)
        blessed.append(p.project_key or "")

    return {"blessed_projects": blessed, "tombstoned": tombstoned,
            "mem_tombstoned": mem_tombstoned, "hub_fingerprint": fp}
