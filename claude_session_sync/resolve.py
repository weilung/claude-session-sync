"""resolve：互動式解決 fork / superset-branch（union 或 keep-both）。DESIGN §6.6 的「停下來問」。

兩種使用者選擇（皆**無損、C3-safe**，只寫 keep-both 新檔、絕不覆蓋任何既有檔）：
  - **UNION**：呼叫 `session_merge` 把兩枝行級併成一檔（標 chosen tip），寫成 local 端 keep-both 新檔。
  - **KEEP_BOTH**：把**對側（hub）分枝**以 keep-both 新檔名帶進 local（原本只在 hub，現在 local 也能 resume）。
  - **SKIP**：不動。

原 fork 兩檔（兩側同 sid）一律**保留**——本工具永不自動刪；要收斂成單一 session 需使用者自行刪除原檔
（刪除→tombstone 是另一塊）。故 union/keep-both 後該 sid 仍會被視為 fork，直到原檔被處理。

互動只在使用者明確要求時跑（`--interactive`）；非互動 apply 仍只回報 needs-decision（既有行為不變）。
決策由可注入的 `Decider` 回呼提供（CLI 用 stdin、測試用 stub），故核心邏輯可測、不綁 TUI。
`conflict-delete-vs-update` 不在此處理（屬刪除衝突，非分枝 fork）。

安全紀律：每 session 取 hub 側路徑鎖 → 持鎖**重新分類**確認仍是 fork/superset（tombstone 期間出現→不 union）
→ 讀兩側、merge、寫 local keep-both（O_EXCL）。新檔不覆蓋任何東西，故不需 C4 覆蓋快照；鎖足以序列化。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from . import anomaly, atomicio, scan, state as state_mod, tombstone
from .lineset import analyze
from .session_merge import LeafCandidate, MergeOutcome, merge_sessions, render_jsonl

# 互動可解的分枝分類（其餘類別不在此處理）。
RESOLVABLE = frozenset({"fork", "superset-branch"})


class Choice(str, Enum):
    UNION = "union"
    KEEP_BOTH = "keep-both"
    SKIP = "skip"


@dataclass
class Decision:
    choice: Choice
    chosen_tip: str | None = None   # UNION 用；merge 自動選不出（缺 ts/並列）時必須由此指定


@dataclass
class ResolveContext:
    """交給 decider 的資訊：sid、分類、預先嘗試的 union 結果（含可選 tip 候選）。"""
    session_id: str
    action: str
    union_outcome: MergeOutcome           # MERGED / NEEDS_DECISION（要 tip）/ FALLBACK（不能 union）
    union_reason: str
    leaves: list[LeafCandidate] = field(default_factory=list)


@dataclass
class ResolveOutcome:
    session_id: str
    result: str          # union-merged / kept-both / skipped / union-unavailable / skipped-changed /
                         # skipped-locked / skipped-stale / error
    detail: str
    path: str | None = None


@dataclass
class ResolveReport:
    outcomes: list[ResolveOutcome] = field(default_factory=list)
    halted: bool = False
    halt_reason: str | None = None

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for o in self.outcomes:
            c[o.result] = c.get(o.result, 0) + 1
        return c

    @property
    def had_error(self) -> bool:
        """互動解決中有寫入錯誤（disk full/權限/keep-both 名用罄）→ CLI 須非零退出（codex r23）。"""
        return any(o.result == "error" for o in self.outcomes)


Decider = Callable[[ResolveContext], Decision]


def _safe_analyze(path: Path):
    """analyze 但容忍 race（檔在 exists 檢查後被刪/chmod/截斷）：OSError → None（呼叫端視為 skipped）。"""
    try:
        return analyze(str(path))
    except OSError:
        return None


def _resolve_one(
    sid: str, action: str, *, local_dir: Path, hub_dir: Path, project_key: str,
    decider: Decider, machine: str | None, lock_timeout_s: float, state_path,
) -> ResolveOutcome:
    local_file = local_dir / f"{sid}.jsonl"
    hub_file = hub_dir / f"{sid}.jsonl"
    # leaf symlink 防線（e2e gate2 #2）：兩側 .jsonl 若為 symlink（可能指夾外）→ 不讀/寫（既有分類已略過 symlink
    # session；此為 resolve 獨立讀寫路徑的防線，含 plan→resolve TOCTOU）。
    if local_file.is_symlink() or hub_file.is_symlink():
        return ResolveOutcome(sid, "skipped-changed", "session 檔為 symlink（疑逃逸），略過")

    # ── Phase A（唯讀、**不持鎖**）：算 union preview 交 decider。
    # 刻意不在持鎖時等待人類輸入（A6/A10：網路 FS 上長時間持鎖會擋住他機）。
    ls, hs = _safe_analyze(local_file), _safe_analyze(hub_file)
    if ls is None or hs is None:
        return ResolveOutcome(sid, "skipped-changed", "session 已不在/讀不到兩側（race），略過")
    preview = merge_sessions(ls, hs)
    decision = decider(ResolveContext(sid, action, preview.outcome, preview.reason, list(preview.leaves)))

    # decider 契約驗證（外部回呼，須防呆，codex r23）：choice 必須是 Choice、chosen_tip 必須 None|str。
    if not isinstance(decision.choice, Choice):
        return ResolveOutcome(sid, "skipped", f"decider 回傳未知選擇 {decision.choice!r}，未處理")
    if decision.chosen_tip is not None and not isinstance(decision.chosen_tip, str):
        return ResolveOutcome(sid, "skipped",
                              f"decider chosen_tip 型別不合（須 str|None）：{decision.chosen_tip!r}")
    if decision.choice == Choice.SKIP:
        return ResolveOutcome(sid, "skipped", "使用者選擇暫不處理")
    if decision.choice == Choice.UNION:
        if preview.outcome == MergeOutcome.FALLBACK:
            return ResolveOutcome(sid, "union-unavailable", f"無法 union（退回挑選）：{preview.reason}")
        if preview.outcome == MergeOutcome.NEEDS_DECISION and not decision.chosen_tip:
            return ResolveOutcome(sid, "union-unavailable", f"union 需指定 tip 但未提供：{preview.reason}")

    # ── Phase B（持鎖）：鎖內重讀 coverage/tombstone、重新分類確認仍 fork → 由**鎖內現況**重讀寫。
    try:
        lock = atomicio.FileLock(hub_file).acquire_blocking(timeout_s=lock_timeout_s)
    except atomicio.StaleLock as e:
        return ResolveOutcome(sid, "skipped-stale", f"鎖疑似陳舊，交人工：{e}")
    except atomicio.LockError as e:
        return ResolveOutcome(sid, "skipped-locked", f"取鎖逾時，略過：{e}")
    try:
        # 信任邊界：decider 暫停期間 coverage 可能被移除/損壞 → 鎖內**重讀**，已非 initialized 則不寫（codex r23）。
        cov = tombstone.is_initialized(hub_dir)
        if not cov:
            return ResolveOutcome(sid, "skipped-changed", "專案已非 initialized（coverage 消失），不寫")
        if local_file.is_symlink() or hub_file.is_symlink():   # 鎖內 leaf symlink 重驗（TOCTOU，e2e gate2 #2）
            return ResolveOutcome(sid, "skipped-changed", "session 檔為 symlink（疑逃逸/TOCTOU），略過")
        cur_lf = local_file if local_file.exists() else None
        cur_hf = hub_file if hub_file.exists() else None
        tombs = tombstone.read_tombstones(hub_dir)
        corrupt = tombstone.corrupt_tombstone_targets(hub_dir)
        coll = scan.casefold_collisions_for(local_dir, hub_dir)
        cur_state = state_mod.load_or_none(state_path)
        known = cur_state.known_sessions.get(project_key) if cur_state else None
        has_baseline = bool(cur_state and project_key in cur_state.known_sessions)
        cur = scan.classify_session(
            sid, cur_lf, cur_hf, both=True, coverage_initialized=cov,
            tombs=tombs, corrupt=corrupt, known=known, has_baseline=has_baseline,
            is_collision=sid.casefold() in coll,
        )
        if cur.action not in RESOLVABLE:
            return ResolveOutcome(sid, "skipped-changed",
                                  f"重新分類已非可解 fork（現為 {cur.action}），請重跑")

        if decision.choice == Choice.KEEP_BOTH:
            dest = atomicio.write_keep_both(local_file, hub_file.read_bytes(), machine=machine)
            return ResolveOutcome(sid, "kept-both", "hub 分枝已另存 local keep-both（可 resume）", str(dest))
        if decision.choice != Choice.UNION:  # 防呆：未知選擇不落到 UNION 寫入路徑（已於 Phase A 擋下，雙保險）
            return ResolveOutcome(sid, "skipped", f"未知選擇 {decision.choice}，未處理")

        # UNION：鎖內**重讀重 merge**（反映現況；期間 source 變得無法 union → 安全退為 union-unavailable）。
        ls2, hs2 = _safe_analyze(local_file), _safe_analyze(hub_file)
        if ls2 is None or hs2 is None:
            return ResolveOutcome(sid, "skipped-changed", "鎖內讀取失敗（race），略過")
        merged = merge_sessions(ls2, hs2, chosen_tip=decision.chosen_tip)
        if merged.outcome != MergeOutcome.MERGED:
            why = "需指定 tip" if merged.outcome == MergeOutcome.NEEDS_DECISION else "退回挑選"
            return ResolveOutcome(sid, "union-unavailable", f"無法 union（{why}）：{merged.reason}")
        dest = atomicio.write_keep_both(local_file, render_jsonl(merged.objs), machine=machine)
        return ResolveOutcome(sid, "union-merged",
                              f"兩枝已 union（tip={merged.chosen_tip[:8]}），另存 local keep-both", str(dest))
    except (atomicio.AtomicWriteError, OSError) as e:
        return ResolveOutcome(sid, "error", f"寫入失敗（已中止該檔，未污染目標）：{e}")
    finally:
        lock.release()


def resolve_plan(
    plan: scan.SyncPlan, *, hub_root, state, state_path,
    decider: Decider, machine: str | None = None, lock_timeout_s: float = 5.0,
) -> ResolveReport:
    """對 plan 內所有 fork/superset-branch session 跑互動解決。回 ResolveReport。"""
    report = ResolveReport()
    halts = [f"{a.code}: {a.message}" for a in anomaly.check(state, Path(hub_root)) if a.severity == "halt"]
    if halts:
        report.halted = True
        report.halt_reason = "; ".join(halts)
        return report

    for pp in plan.projects:
        if not pp.hub_dir or not pp.local_dir or not pp.coverage_initialized:
            continue
        hub_dir, local_dir = Path(pp.hub_dir), Path(pp.local_dir)
        # 逃逸重驗（TOCTOU：plan 後專案夾被換成 symlink/junction 逃出 root）→ 不讀/寫界外（e2e gate2 #1）。
        # apply_plan 已擋，但互動 resolve 是**獨立**寫入路徑（Phase A 讀兩側、Phase B 寫 local keep-both），須各自守。
        if not scan._safe_project_dir(Path(hub_root), hub_dir) or \
                not scan._safe_project_dir(local_dir.parent, local_dir):
            for sp in pp.sessions:
                if sp.action in RESOLVABLE:
                    report.outcomes.append(ResolveOutcome(
                        sp.session_id, "skipped", "專案夾是 symlink 或逃逸信任根 → 不互動處理（不讀/寫界外）"))
            continue
        for sp in pp.sessions:
            if sp.action not in RESOLVABLE:
                continue
            report.outcomes.append(_resolve_one(
                sp.session_id, sp.action, local_dir=local_dir, hub_dir=hub_dir, project_key=hub_dir.name,
                decider=decider, machine=machine, lock_timeout_s=lock_timeout_s, state_path=state_path,
            ))
    return report


def format_report(report: ResolveReport) -> str:
    lines: list[str] = []
    if report.halted:
        lines.append(f"[HALT] {report.halt_reason}")
        return "\n".join(lines)
    for o in report.outcomes:
        lines.append(f"  - {o.session_id[:8]}: [{o.result}] {o.detail}"
                     + (f"  → {o.path}" if o.path else ""))
    c = report.counts()
    if c:
        lines.append("\n互動解決摘要：" + "；".join(f"{k}={v}" for k, v in sorted(c.items())))
    return "\n".join(lines) if lines else "（無 fork/superset 需互動解決）"
