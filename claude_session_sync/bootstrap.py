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

from . import anomaly, atomicio, memory, pathsafe, scan, state as state_mod, tombstone
from .anomaly import Anomaly
from .state import State


class BootstrapChanged(RuntimeError):
    """確認後、落地前，磁碟現況已與所確認的 baseline diff 不符 → 拒絕落地，請重跑（codex r9-1）。"""


def _safe_hub_name(name: str) -> bool:
    """--map 的目標必須是 root 底下的**單一安全夾名**（擋 `../`、絕對路徑逃出信任根，codex r9-3）。hub 目標與
    還原模式的**待建 local 夾名**同一把尺（兩者都會被 mkdir）。委派 `pathsafe.safe_leaf_name`（單一真相源，
    另擋 Windows 保留名/非法字元/尾隨點空白的名實脫鉤別名，codex mcwd-r1 F3）。"""
    return pathsafe.safe_leaf_name(name)


def _resolve_map_target(hub_root: Path, hub_dirs: list[Path], unsafe_names: set[str],
                        target: str) -> tuple[Path | None, str]:
    """--map 的 hub 目標 → (hub_dir, "mapped") 或 (None, skip 狀態)。

    exact 命中安全夾 → 用該夾。casefold/NFC **alias** 命中（使用者打的大小寫/正規化與磁碟不同）→ canonical
    到**磁碟實際夾名**（codex mcwd-g1 #2：照使用者字串寫入會把 known_sessions/dirmap 掛在 `encr` 而磁碟是
    `EncR`——Windows `is_dir()` 過、`_bindings_first` 的 `hd.name == pk` exact 比對永遠失敗 → 剛 bless 的
    專案變 needs-map 死路）。alias 多重命中（case-sensitive FS 的孿生夾）→ 歧義 fail-closed。

    **unsafe（symlink/逃逸 reparse）夾同名/撞鍵 → fail-closed**（codex mcwd-g3 #2）：exact 命中 unsafe →
    skipped-unsafe（不可「好心」canonical 到 safe 孿生夾——使用者指的就是那個 unsafe 夾）；alias 鍵撞到
    unsafe → 一律 skipped-unsafe（Windows 上 mkdir 該名會 case-insensitive 開到 unsafe reparse＝寫穿界外）。

    皆無命中 → 待建新 hub 夾（空 hub 首推語意），沿用原字串。"""
    if not _safe_hub_name(target):
        return None, "skipped-bad-map"
    tk = scan._name_key(target)
    if target in unsafe_names or tk in {scan._name_key(u) for u in unsafe_names}:
        return None, "skipped-unsafe"
    for hd in hub_dirs:
        if hd.name == target:
            return hd, "mapped"
    aliases = [hd for hd in hub_dirs if scan._name_key(hd.name) == tk]
    if len(aliases) == 1:
        return aliases[0], "mapped"
    if len(aliases) > 1:
        return None, "skipped-map-collision"
    return hub_root / target, "mapped"


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
    cwds: list[str] = field(default_factory=list)   # 夾內觀察到的**全部** cwd（預覽攤開給使用者知情確認）
    asserted: bool = False        # 本次由使用者 --map 明示 → 落地寫 state.asserted_dirs（斷言整夾，2026-07-14）
    create_local: bool = False    # 還原模式：--map 指到不存在的 local 夾且 hub 專案在 → 落地時建空夾＋空 local 基線

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
    hub_dirs, hub_unsafe = scan._list_project_dirs(hub_root)
    hub_unsafe_names = {d.name for d in hub_unsafe}   # --map 目標撞 unsafe 夾 → fail-closed（mcwd-g3 #2）
    local_dirs, local_unsafe = scan._list_project_dirs(local_root)

    projects: list[ProjectBaseline] = []
    for ld in local_unsafe:   # 逃逸 local 夾 → skipped-unsafe（不從 root 外夾建基線/bless）
        projects.append(ProjectBaseline(
            local_dir=str(ld), hub_dir=None, project_key=None, cwd=None, status="skipped-unsafe"))
    for ld in local_dirs:
        # --map 明示優先；否則走 git 指紋解析（hub_dirs 已濾逃逸，git 解析不會讀到不安全 hub 夾 sidecar）。
        asserted = ld.name in mappings
        if asserted:
            # 目標經 canonical 解析（exact → casefold/NFC alias → 待建新夾；不安全名/unsafe 撞鍵/孿生歧義 → skip）。
            hub_dir, status = _resolve_map_target(hub_root, hub_dirs, hub_unsafe_names, mappings[ld.name])
        else:
            st, hub_dir = resolve(ld, hub_dirs)
            status = "mapped" if st == "match" else f"skipped-{st}"

        # hub 專案夾逃逸檢查（--map 目標或 git 解析到的 hub 夾若為既存 symlink/junction 逃出 hub_root）→ 不建基線、
        # 不寫 tombstone/coverage 到 root 外（e2e xgrp #4）。待建的空 hub 夾（不存在）resolve 字面仍在 root 內 → 放行。
        if status == "mapped" and hub_dir is not None and not scan._safe_project_dir(hub_root, hub_dir):
            hub_dir, status = None, "skipped-unsafe"

        cwds = scan._project_cwds(ld)
        cwd = next(iter(cwds)) if len(cwds) == 1 else None
        if status == "mapped" and len(cwds) > 1 and not asserted:
            # 夾名有損混入多 cwd → 不可自動建單一綁定。使用者 --map **明示斷言整夾**則放行（落地寫
            # asserted_dirs，sync 端 `_bindings_first` 憑此無視 cwd 數採夾名綁定）——「斷言 vs 弱猜」分界，
            # 重審 codex r26-1/C-r6-1 後的決定（2026-07-14）：r26-1 擋的是無人斷言的自動夾名配對，斷言不在此列。
            status = "skipped-multi-cwd"

        # 不可列舉夾 fail-stop（e2e gate11 finding2）：local/hub 專案夾存在但不可讀（POSIX read-denied）→ `_stems`
        # 的 glob **fail-open** 回空 → baseline 漏掉現存 session → 日後真正刪除認不出、hub 檔被復活。故不建基線、標
        # skipped-unreadable（fail-closed；memory 基線 `_mem_names`/list_memory_files 本就 fail-stop 一致）。可讀但
        # 真的空 → 照常建（空基線語意不變）。放在 `_stems`/`_project_cwds` 讀之前總擋。
        if not scan._dir_scannable(ld) or (hub_dir is not None and not scan._dir_scannable(hub_dir)):
            projects.append(ProjectBaseline(
                local_dir=str(ld), hub_dir=str(hub_dir) if hub_dir else None,
                project_key=None, cwd=cwd, status="skipped-unreadable", cwds=sorted(cwds)))
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
            status=status, cwds=sorted(cwds), asserted=(asserted and status == "mapped"),
        ))

    # 還原模式（決定 2026-07-14）：--map 指到**不存在的 local 夾** → 全新機器/災難還原。hub 專案夾須已存在
    # （兩側皆無 → 多半是拼錯，拒絕建出雙空死夾）；落地時建空 local 夾＋空 local 基線＋斷言夾名綁定，
    # 內容由下次 `sync --apply` 以 copy-to-local 拉下（空基線 → hub 檔=hub-only 可匯入，classify 語意不變）。
    present_names = {ld.name for ld in local_dirs} | {ld.name for ld in local_unsafe}
    present_keys = {scan._name_key(n) for n in present_names}
    synth_names = sorted(set(mappings) - present_names)
    # 多個 --map 待建夾**彼此** casefold/NFC 撞名（codex mcwd-g1 #1：`--map Proj=H1 --map proj=H2` 各自
    # 對現存夾都不撞 → 都放行 → apply 建了第一個、第二個 mkdir 才炸＝中途中止留半成品）→ 全數 skip。
    synth_dup_keys = {k for k, n in Counter(scan._name_key(n) for n in synth_names).items() if n > 1}
    for lname in synth_names:
        hname = mappings[lname]
        ldir = local_root / lname
        if not _safe_hub_name(lname):
            # 待建 local 夾名與 hub 目標同一把安全尺（含分隔/../絕對路徑 → mkdir 會逃出信任根）。
            projects.append(ProjectBaseline(
                local_dir=str(ldir), hub_dir=None, project_key=None, cwd=None, status="skipped-bad-map"))
            continue
        if scan._name_key(lname) in present_keys or scan._name_key(lname) in synth_dup_keys:
            # Windows 不分大小寫/正規化 FS：與現存夾（或另一個 --map 待建夾）casefold/NFC 撞名 → mkdir 會
            # 「開到」同一實體夾，基線名與磁碟名脫鉤（誤綁）→ 拒絕（改用磁碟上的實際夾名重跑 --map）。
            projects.append(ProjectBaseline(
                local_dir=str(ldir), hub_dir=None, project_key=None, cwd=None, status="skipped-map-collision"))
            continue
        hub_dir, hstatus = _resolve_map_target(hub_root, hub_dirs, hub_unsafe_names, hname)
        if hstatus != "mapped":
            projects.append(ProjectBaseline(
                local_dir=str(ldir), hub_dir=None, project_key=None, cwd=None, status=hstatus))
            continue
        if not hub_dir.is_dir():
            # 還原需 hub 專案**已存在**（兩側皆無 → 多半拼錯，拒建雙空死夾）。
            projects.append(ProjectBaseline(
                local_dir=str(ldir), hub_dir=str(hub_dir), project_key=None, cwd=None,
                status="skipped-map-no-hub"))
            continue
        if not scan._safe_project_dir(hub_root, hub_dir):
            projects.append(ProjectBaseline(
                local_dir=str(ldir), hub_dir=None, project_key=None, cwd=None, status="skipped-unsafe"))
            continue
        if not scan._dir_scannable(hub_dir):
            projects.append(ProjectBaseline(
                local_dir=str(ldir), hub_dir=str(hub_dir), project_key=None, cwd=None,
                status="skipped-unreadable"))
            continue
        hub_s = _stems(hub_dir)
        hub_m, hmu = _mem_names(hub_dir)
        projects.append(ProjectBaseline(
            local_dir=str(ldir), hub_dir=str(hub_dir), project_key=hub_dir.name, cwd=None,
            hub_only=sorted(hub_s), ignored=sorted(hub_s & ignore),
            mem_hub_only=sorted(hub_m), mem_ignored=sorted(hub_m & ignore), mem_unsafe=hmu,
            status="mapped", asserted=True, create_local=True))

    # 多個 local 撞同一 hub project_key、或同一 cwd → 落地會互覆/誤綁 → 全數 skip（不挑）（codex r9-3）。
    # 以**實體** canonical 鍵（resolve＋葉名 `_name_key` 摺疊）比對：casefold/NFC 孿生（`Hub`/`hub`，
    # codex mcwd-g1 #1）與 root 內 junction 別名（`Alias`→`Hub`，codex mcwd-g4 #1）exact 都不同、
    # 實體卻同一夾 → 只比字串/Path 會被繞過。
    mapped = [p for p in projects if p.status == "mapped"]
    key_dups = {k for k, n in Counter(
        pathsafe.physical_dup_key(p.hub_dir) for p in mapped).items() if n > 1}
    cwd_dups = {c for c, n in Counter(p.cwd for p in mapped if p.cwd is not None).items() if n > 1}
    for p in mapped:
        if pathsafe.physical_dup_key(p.hub_dir) in key_dups:
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
    _skip_hints = {
        # 修正舊誤導訊息：multi-cwd 的 skip 原本也印「需 --map」，但當時 --map 根本救不了（2026-07-13 實機中招）。
        # 兩個狀態字串是同一情境的兩條路：gate 擋下 mapped 者 = skipped-multi-cwd；git 身分解析自己判的
        # = blocked-multi-cwd → 前綴成 skipped-blocked-multi-cwd（真實世界無 --map 時走的是後者）。
        "skipped-multi-cwd": "夾內混多種 cwd；用 --map 明示斷言整夾可解",
        "skipped-blocked-multi-cwd": "夾內混多種 cwd；用 --map 明示斷言整夾可解",
        "skipped-map-no-hub": "--map 的 local 夾不存在且 hub 亦無此專案（還原需 hub 專案在；拼字？）",
        "skipped-map-collision": "--map 名稱 casefold/NFC 撞名（與現存夾／另一個 --map／hub 孿生夾）；請用磁碟上實際夾名",
        "skipped-bad-map": "--map 夾名不安全（須單一夾名，不可含分隔/../絕對路徑）",
    }
    for p in plan.projects:
        head = p.local_dir or p.hub_dir
        if p.status != "mapped":
            lines.append(f"\n專案 {head}  [{p.status}]  → 跳過（{_skip_hints.get(p.status, '需 --map')}）")
            continue
        cwd_disp = f"{len(p.cwds)} 種（--map 斷言整夾）" if len(p.cwds) > 1 else p.cwd
        lines.append(f"\n專案 {head}  → hub={p.project_key}  cwd={cwd_disp}")
        if len(p.cwds) > 1:
            for c in p.cwds:   # 斷言前把混入的 cwd 攤開（信任邊界：使用者須看見自己斷言了什麼）
                lines.append(f"    · cwd: {c}")
        if p.create_local:
            lines.append("  （local 夾不存在 → 落地將新建空夾＋空基線；內容由下次 `sync --apply` 從 hub 拉下）")
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
        if p.create_local and p.local_dir is not None:
            # 還原模式重驗（codex mcwd-r1 F2）：scan 時的「不存在＋不撞名」到 apply 可能已失效——期間冒出的
            # 同名夾（`_revalidate` 只比 stems/memory，空夾照樣通過）或 casefold/NFC 別名夾（Windows mkdir
            # exist_ok 會「開到」它）都會 bless 一個**未在預覽中確認**的夾／產生名實脫鉤的死綁定。一律中止重跑。
            # （local_root **本身**為 junction/symlink 屬多帳號共用的既定政策——使用者資料的 reparse 跟隨非拒絕，
            # 見 CLAUDE_CONFIG_DIR 塊 b29adda——root 指向哪是使用者自己的宣告，此處不重驗 root。）
            if not Path(p.hub_dir).is_dir():
                # 還原需 hub 專案在（scan 已驗）；apply 前 hub 消失 → `_revalidate` 對「預覽時就空的 hub」比不出
                # （missing→set()==set()）、step-1 mkdir 又會憑空重建 → bless 出兩側皆空的死基線（codex mcwd-g2 #2）。
                raise BootstrapChanged(
                    f"專案 {p.project_key} 的 hub 專案夾自確認後消失 → 還原需 hub 在，拒絕落地，請重跑。")
            ldir = Path(p.local_dir)
            if ldir.exists() or pathsafe.is_reparse(ldir):
                raise BootstrapChanged(
                    f"專案 {p.project_key} 的待建 local 夾自確認後已出現：{ldir} → 拒絕落地（未經預覽確認的夾不 bless），請重跑。")
            parent = ldir.parent
            if parent.exists():
                if not scan._dir_scannable(parent):
                    raise BootstrapChanged(
                        f"專案 {p.project_key} 的 local root 不可列舉 → 拒絕落地（無法排除撞名），請重跑。")
                try:
                    sib_keys = {scan._name_key(x.name) for x in parent.iterdir()}
                except OSError as e:
                    raise BootstrapChanged(
                        f"專案 {p.project_key} 的 local root 列舉失敗（{e.__class__.__name__}）→ 拒絕落地，請重跑。")
                if scan._name_key(ldir.name) in sib_keys:
                    raise BootstrapChanged(
                        f"專案 {p.project_key} 的待建 local 夾與現存夾 casefold/NFC 撞名 → 拒絕落地，"
                        f"請用磁碟上實際夾名重跑 --map。")
    _revalidate(plan)

    tombstoned: list[str] = []
    mem_tombstoned: list[str] = []
    # 1) 建 hub 夾（空 hub 首推）+ ignored 單邊檔 → suppress tombstone（檔留原地、永不複製）。
    for p in plan.mapped:
        hub_dir = Path(p.hub_dir)
        hub_dir.mkdir(parents=True, exist_ok=True)
        local_dir = Path(p.local_dir) if p.local_dir else None
        if p.create_local and local_dir is not None:
            # 還原模式：建空 local 夾（含全新機器缺失的 local_root 上層）。leaf 採**嚴格** exist_ok=False——
            # 上方重驗已確認不存在/不撞名，重驗到此的縫隙若仍冒出同名夾（並發），吞下等於 bless 未確認的夾（F2）。
            try:
                local_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                raise BootstrapChanged(
                    f"專案 {p.project_key} 的待建 local 夾在落地瞬間出現 → 中止（未經預覽確認的夾不 bless），請重跑。")
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
                dname = Path(p.local_dir).name
                # remap 撤舊（codex mcwd-g3 #1）：一個 hub 專案同時只綁**一個** local 夾。把指向同一 pk 的
                # **其他**夾名/cwd 舊 claims 撤掉——否則舊夾（空/multi-cwd → bootstrap 這輪 skip、不進 dup-key
                # guard）下次 sync 仍與新夾同配到此 hub：空舊夾會把 hub 檔判 local-deleted 寫 **false
                # tombstone**、舊夾內容誤 copy 進 hub（「多 local 對一 hub 全 skip」不變量被繞過）。
                for k in [k for k, v in s.local_dir_bindings.items()
                          if v == p.project_key and k != dname]:
                    del s.local_dir_bindings[k]
                    s.asserted_dirs.discard(k)
                for c in [c for c, v in s.bindings.items()
                          if v == p.project_key and c != p.cwd]:
                    del s.bindings[c]
                s.local_dir_bindings[dname] = p.project_key
                if p.asserted:
                    # --map 斷言整夾（2026-07-14）：sync 端憑此無視 cwd 數採夾名綁定。
                    s.asserted_dirs.add(dname)
                else:
                    # 非斷言的 re-bless（自動指紋配對）→ 撤下舊斷言：維持不變量「asserted ⇒ 現行 dirmap
                    # 是使用者親口斷言的那筆」，否則舊斷言會替**自動**配對背書（弱猜被升格，違反 r26-1 分界）。
                    s.asserted_dirs.discard(dname)

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
