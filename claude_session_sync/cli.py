"""CLI：`status`（唯讀）/ `sync`（dry-run 預設，`--apply` 安全寫入）/ `bootstrap`（建基線）。

dry-run 為預設。`--apply` 自動套用僅 identical/paired-ff/copy（其餘只回報，互動/刪除是 P1c）。
退出碼：0 正常；1 錯誤（前置/用法/寫入）；2 halt 級異常。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from . import acks as acks_mod
from . import apply as apply_mod
from . import atomicio as atomicio_mod
from . import bootstrap as bootstrap_mod
from . import config as config_mod
from . import doctor as doctor_mod
from . import fuzzy as fuzzy_mod
from . import memory as memory_mod
from . import merge as merge_mod
from . import resolve as resolve_mod
from . import scan
from . import state as state_mod
from . import transfer as transfer_mod
from .scan import default_local_root
from .session_merge import MergeOutcome


@dataclass
class Context:
    config: config_mod.Config
    hub: str
    local_root: str
    state_path: str | None
    state: state_mod.State | None


def _add_common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--hub", help="覆寫 own_hub 路徑（預設讀 config）")
    sp.add_argument("--local-root",
                    help="覆寫 local session 根（預設 $CLAUDE_CONFIG_DIR/projects，未設則 ~/.claude/projects）")
    sp.add_argument("--state", help="覆寫 state.json 路徑")


def _resolve_context(args) -> tuple[Context | None, str | None]:
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as e:
        return None, f"config.toml 損壞，保守中止：{e}"
    hub = args.hub or cfg.own_hub
    if not hub:
        return None, "尚未設定 own_hub：請先 `config set own-hub <path>` 或用 --hub。"
    try:
        st = state_mod.load_or_none(args.state)
    except state_mod.StateCorruptError as e:
        return None, f"state.json 損壞，保守中止（不可當首次同步）：{e}\n  可用 doctor --rebuild-state（P1c）。"
    local_root = args.local_root or str(default_local_root())
    state_path = args.state or str(state_mod.default_state_path())
    return Context(config=cfg, hub=hub, local_root=local_root, state_path=state_path, state=st), None


def _stdin_decider(ctx: resolve_mod.ResolveContext) -> resolve_mod.Decision:
    """CLI 互動 decider：對每個 fork/superset 問 [u]nion / keep-[b]oth / [s]kip。

    stdin 斷線（EOFError：行程被背景化/stdin 是管線且已耗盡）→ **安全跳過**、不 traceback——否則
    `sync --apply --interactive` 在 auto 項全部落地後才崩，exit 1 且吞掉整份報告（2026-07-14 實機中招：
    使用者的 `!` 指令被手動背景化）。比照 fuzzy decider 的 EOF→N。"""
    can_union = ctx.union_outcome != MergeOutcome.FALLBACK
    print(f"\n  fork/superset: {ctx.session_id[:8]}（{ctx.action}）")
    menu = (["[u] union 合併兩枝"] if can_union else [f"（union 不可用：{ctx.union_reason}）"]) + \
           ["[b] keep-both（把 hub 分枝帶進 local）", "[s] 跳過"]
    print("    " + "   ".join(menu))
    try:
        ans = input("    選擇 [u/b/s]: ").strip().lower()
    except EOFError:
        print("    （stdin 已斷 → 跳過）")
        return resolve_mod.Decision(resolve_mod.Choice.SKIP)
    if ans == "u" and can_union:
        tip = None
        if ctx.union_outcome == MergeOutcome.NEEDS_DECISION:
            print("    需選 active tip：")
            for i, lf in enumerate(ctx.leaves):
                print(f"      ({i}) {lf.uuid[:8]} ts={lf.ts}")
            try:
                tip = ctx.leaves[int(input("    tip 編號: ").strip())].uuid
            except (ValueError, IndexError, EOFError):
                print("    無效編號/輸入中斷 → 跳過")
                return resolve_mod.Decision(resolve_mod.Choice.SKIP)
        return resolve_mod.Decision(resolve_mod.Choice.UNION, chosen_tip=tip)
    if ans == "b":
        return resolve_mod.Decision(resolve_mod.Choice.KEEP_BOTH)
    return resolve_mod.Decision(resolve_mod.Choice.SKIP)


def _cmd_status_or_sync(args) -> int:
    ctx, err = _resolve_context(args)
    if err:
        print(err, file=sys.stderr)
        return 1
    assert ctx is not None
    plan = scan.build_plan(ctx.local_root, ctx.hub, ctx.state)
    ack_view = acks_mod.compute_ack_view(plan)   # A15：純呈現層過濾（隱藏已 ack 的 damaged/collision）
    print(scan.format_plan(plan, ack_view))
    if ack_view.corrupt_projects:
        print(f"⚠ acks.json 損壞（已忽略、全部照常回報）：{', '.join(sorted(ack_view.corrupt_projects))}",
              file=sys.stderr)
    if plan.halt:
        return 2
    interactive = getattr(args, "interactive", False)
    if getattr(args, "apply", False):
        report = apply_mod.apply_plan(  # 首次同步（無 state）會在此 halt，要求先 bootstrap
            plan, local_root=ctx.local_root, hub_root=ctx.hub, config=ctx.config,
            state=ctx.state, state_path=ctx.state_path,
        )
        print("\n=== apply ===")
        # g5 High：apply 報告用 **apply 後 fresh** ack_view（重讀磁碟重算），**不**沿用 dry-run(T0) 的 stale 視圖——
        # apply_plan 在 T2 重讀/重分類，若某 blocked 檔在 plan→apply 間變成新 blocked 情況，apply 已報告它、卻可能被
        # 舊 ack 遮蓋。acked 項為 blocked、apply 不寫其檔 → apply 後（T3）以現況重算指紋判定精確、不誤藏變化。
        report_ack_view = acks_mod.compute_ack_view(scan.build_plan(ctx.local_root, ctx.hub, ctx.state))
        print(apply_mod.format_report(report, report_ack_view))
        if report.halted:
            return 2
        resolve_error = False
        if interactive:  # 對 apply 只回報的 fork/superset 跑互動 union/keep-both
            rreport = resolve_mod.resolve_plan(
                plan, hub_root=ctx.hub, state=ctx.state, state_path=ctx.state_path,
                decider=_stdin_decider,
            )
            print("\n=== 互動解決 fork/superset ===")
            print(resolve_mod.format_report(rreport))
            if rreport.halted:
                return 2
            resolve_error = rreport.had_error  # 互動寫入錯誤也算未竟全功（誠實非零）
        if report.had_error or report.had_uncommitted or report.reconcile_failed or resolve_error:
            return 1
    elif interactive:
        print("\n（--interactive 需搭配 --apply 才會寫入；本次僅 dry-run，未處理 fork/superset）")
    return 0


def _parse_maps(items: list[str] | None) -> tuple[dict[str, str], str | None]:
    out: dict[str, str] = {}
    for it in items or []:
        if "=" not in it:
            return {}, f"--map 需 <local夾名>=<hub夾名> 格式：{it}"
        k, v = it.split("=", 1)
        if not k or not v:
            return {}, f"--map 兩側不可空：{it}"
        if k in out and out[k] != v:
            # --map 是斷言邊界（2026-07-14）：同一 local 夾兩個矛盾目標不可靜默 last-wins（codex mcwd-g4 #2）。
            return {}, f"--map 對同一 local 夾指定了矛盾目標：{k}={out[k]} 與 {k}={v}，請擇一"
        out[k] = v
    return out, None


def _cmd_bootstrap(args) -> int:
    ctx, err = _resolve_context(args)
    if err:
        print(err, file=sys.stderr)
        return 1
    assert ctx is not None
    mappings, merr = _parse_maps(args.map)
    if merr:
        print(merr, file=sys.stderr)
        return 1
    ignore = set(args.ignore or [])
    plan = bootstrap_mod.scan_baseline(
        ctx.local_root, ctx.hub, ctx.state, mappings=mappings, ignore=ignore
    )
    print(bootstrap_mod.format_baseline(plan))
    if plan.halt:
        return 2
    if not args.yes:
        print("\n（預覽）確認以上單邊檔可匯入後，加 --yes 落地基線（不複製/不刪 session）。")
        return 0
    if not plan.mapped:
        print("\n無可建基線的已配對專案；未寫入。", file=sys.stderr)
        return 1
    try:
        summary = bootstrap_mod.apply_baseline(plan, ctx.hub, ctx.state_path)
    except bootstrap_mod.BootstrapChanged as e:
        print(f"\n中止：{e}", file=sys.stderr)
        return 1
    print(f"\n已建基線：{len(summary['blessed_projects'])} 個專案"
          f"；session tombstone {len(summary['tombstoned'])} 條"
          f"；memory tombstone {len(summary.get('mem_tombstoned', []))} 條。")
    return 0


def _cmd_transfer(args, direction: str) -> int:
    """跨群 pull/push（DESIGN §8.1）：dry-run 預設、--apply 寫入；remote 名由 config `[remotes]` 解析。"""
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as e:
        print(f"config.toml 損壞，保守中止：{e}", file=sys.stderr)
        return 1
    name = args.from_remote if direction == transfer_mod.PULL else args.to_remote
    flag = "--from" if direction == transfer_mod.PULL else "--to"
    if not name:
        print(f"請指定 {flag} <remote 名稱>", file=sys.stderr)
        return 1
    remote_path = cfg.remotes.get(name)
    if not remote_path:
        have = ", ".join(sorted(cfg.remotes)) or "（無）"
        print(f"未知 remote '{name}'；先 `remote add {name} <path>`。現有：{have}", file=sys.stderr)
        return 1
    mappings, merr = _parse_maps(args.map)
    if merr:
        print(merr, file=sys.stderr)
        return 1
    local_root = args.local_root or str(default_local_root())
    plan = transfer_mod.plan_transfer(direction, local_root, remote_path, remote_name=name,
                                      session=args.session, mappings=mappings)
    print(transfer_mod.format_plan(plan))
    if plan.halt:
        return 2
    if getattr(args, "apply", False):
        report = transfer_mod.apply_transfer(plan, local_root=local_root, remote_root=remote_path)
        print("\n=== apply ===")
        print(transfer_mod.format_report(report))
        if report.halted:
            return 2
        if report.had_error:
            return 1
    return 0


def _cmd_remote(args) -> int:
    """管理跨群 remote hub（寫 config `[remotes]`）。"""
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as e:
        print(f"config.toml 損壞，保守中止：{e}", file=sys.stderr)
        return 1
    if args.remote_cmd == "list":
        if not cfg.remotes:
            print("（無 remote）")
        for n in sorted(cfg.remotes):
            print(f"{n} = {cfg.remotes[n]}")
        return 0
    # add
    cfg.remotes[args.name] = args.path
    config_mod.save(cfg)
    print(f"已加 remote {args.name} = {args.path}")
    return 0


def _print_stage(res: merge_mod.StageResult) -> None:
    head = {"staged": "已保留", "already-staged": "已存在（未覆蓋）", "would-stage": "將保留",
            "empty": "無內容可保留", "degraded": "不完整（某側讀不到，需重跑）",
            "incomplete": "殘缺（需重跑）", "stale": "暫存已過時（衝突已變，需重跑）",
            "error": "失敗"}.get(res.status, res.status)
    # project_key/key 可含控制字元/surrogate（非 UTF-8 檔名）→ 過 _disp，免 print 在嚴格 stdout 編碼下崩潰（codex R1 Low）。
    # key 用 _key_disp（fuzzy 的 `a\x00b` 顯示為「a ↔ b」，一般 key 無 NUL 不受影響）。dest 的根來自 XDG_CACHE_HOME
    # （POSIX 可含非 UTF-8 bytes）→ 亦過 _disp，免暫存成功後印結果才崩 strict stdout（g3 Low）。
    print(f"  ● {merge_mod._disp(res.conflict.project_key)} / {merge_mod._key_disp(res.conflict.key)} "
          f"[{head}] → {merge_mod._disp(str(res.dest))}")
    for f in res.files:
        print(f"      + {f}")
    if res.status == "staged":
        print(f"      + {merge_mod.META_FILE} / {merge_mod.PROMPT_FILE}")
    for n in res.notes:
        # notes 可含 raw 檔名（退化警告嵌 `missing` 檔名）→ 過 _disp，免 surrogate/非 UTF-8 檔名崩 strict stdout
        # （R1 Low；build_prompt/format_conflicts/_conflict_meta 早已 _disp notes，_print_stage 原為唯一漏網）。
        print(f"      · {merge_mod._disp(n)}")


def _dup_target_pks(plan, project: str | None = None) -> frozenset[str]:
    """被 ≥2 個 local 專案映到的 remote/hub 夾名（pk）。暫存夾名以 pk 為基底（`<merge>/<pk>/<key>`）→ 多對一時
    第二者撞夾被當 stale/already-staged，某專案兩版沒被獨立保留（對稱 `transfer` 的 skipped-dup-target，g1 Medium）。
    `project` 給定時只算該 pk（g2 Medium：否則 scoped 指令會被無關專案的 dup 誤觸警告/非零）。"""
    counts: dict[str, int] = {}
    for pp in plan.projects:
        # blocked-dup-local（scan 上游已擋、hub_dir=None）帶 dup_hub＝原會配到的 pk → 一樣計入，
        # 維持「撞夾 → 大聲警告＋非零」的既有回報（否則上游擋掉反而變靜默 exit 0，mcwd-g3 #1 修正的回歸）。
        pk = (Path(pp.hub_dir).name if (pp.local_dir and pp.hub_dir)
              else pp.dup_hub if pp.local_dir else None)
        if pk is None:
            continue
        if project is not None and pk != project:
            continue
        counts[pk] = counts.get(pk, 0) + 1
    return frozenset(pk for pk, c in counts.items() if c > 1)


def _own_hub_forbidden(local_root, cfg, override_hub) -> list[Path]:
    """暫存不可落入的**任一受同步樹**：local + **override --hub 與 config own_hub 兩者**（g2 High：兩者都是實體
    受同步 hub，不可用 `or` 互斥——override 只換本次操作對象、config own_hub 仍被例行 sync 同步）+ **所有** remote
    （g1 High：否則暫存落進某 remote 被那一群同步＝外洩）。落在任一之內 → `unsafe_staging_root` fail-closed 拒絕。"""
    forbidden = [Path(local_root)]
    for h in (override_hub, cfg.own_hub):
        if h:
            forbidden.append(Path(h))
    forbidden += [Path(p) for p in cfg.remotes.values()]
    return forbidden


def _emit_memory_conflicts(conflicts, unscannable, staging_forbidden, args, *,
                           dup_pks: frozenset[str] = frozenset()) -> int:
    """memory-merge 兩條路（own-hub / 跨群 --from）共用的**報告 + 保留兩版 + 提示詞**尾段（避免漂移）。
    `staging_forbidden`＝暫存根不可落入的**任一受同步樹**（local + own hub + 所有 remote）。`dup_pks`＝多對一撞夾的
    remote/hub 夾名 → 其衝突全數跳過（fail-closed，不讓第二專案版本被覆蓋/丟）＋非零。"""
    if dup_pks:
        print(f"⚠ 多個本機專案映到同一 remote/hub 夾（{', '.join(sorted(dup_pks))}）→ 暫存夾名會撞、"
              "已跳過該夾**所有**衝突（避免某專案兩版被覆蓋）；請一對一配對後重跑。")
        conflicts = [c for c in conflicts if c.project_key not in dup_pks]
    # memory 被跳過（memory/ 根 symlink 等）→ 不能把「沒掃到」誤當「無衝突」（gate2 F3）：surface + 非零。
    if unscannable:
        print("⚠ 下列專案的 memory 未被掃描（無法判斷是否有衝突），請修為實體目錄後重跑：")
        for u in unscannable:
            print(f"  - {u}")
    if not conflicts:
        if not unscannable and not dup_pks:
            print("（未偵測到 memory 衝突）")
        return 1 if (unscannable or dup_pks) else 0
    print(merge_mod.LEAK_WARNING)
    # 退化衝突（plan 後某側讀不到）/ memory 跳過 / 多對一撞夾 → 非零提醒重跑（gate F2/F3），不論模式。
    rc = 1 if (unscannable or dup_pks or any(c.notes for c in conflicts)) else 0
    if args.apply:
        # 暫存根（XDG_CACHE_HOME）不可盲信：相對路徑 / 落在任一受同步樹內 → fail-closed 拒絕寫入（避免外洩，codex
        # R1 High）。--from 時 forbidden 必含 **remote**，否則兩版可能落進 remote hub 被對方群當新 memory 擴散。
        bad = merge_mod.unsafe_staging_root(merge_mod.merge_root(), staging_forbidden)
        if bad:
            print(f"\n⚠ 拒絕保留兩版（暫存根不安全）：{bad}", file=sys.stderr)
            rc = 1
        else:
            print("\n=== 保留兩版到本機暫存（memory/ 之外，絕不同步）===")
            for c in conflicts:
                res = merge_mod.stage_conflict(c, apply=True)
                _print_stage(res)
                if res.status in ("error", "incomplete", "degraded", "stale") or any("失敗" in n for n in res.notes):
                    rc = 1
    if args.prompt_stdout:
        print("\n=== 合併提示詞（stdout；貼進 Claude 前請先刪減敏感段）===")
        for c in conflicts:
            print(merge_mod.build_prompt(c))
            print()
    if not args.apply and not args.prompt_stdout:
        print(merge_mod.format_conflicts(conflicts))
        print(f"\n共 {len(conflicts)} 個 memory 衝突。加 --apply 保留兩版到本機暫存（含 PROMPT.md）；"
              "--prompt-stdout 印合併提示詞到 stdout。")
    return rc


def _memory_merge_remote_identity(mappings: dict[str, str]):
    """跨群 memory-merge 的 local→remote 夾配對 identity_fn（餵 build_plan）：`--map`（local夾名=remote夾名）優先、
    否則 git 指紋（對稱 `transfer._resolve_pair`；工具尚未寫 `_project.json` sidecar → 無 --map 多半 needs-map，同
    transfer）。逃逸由 build_plan 的 `_list_project_dirs` + `merge.conflicts_from_plan` 的 `_safe_project_dir` 雙守。"""
    def resolve(local_dir: Path, remote_dirs: list[Path]) -> tuple[str, Path | None]:
        tgt = mappings.get(local_dir.name)
        if tgt is not None:
            for rd in remote_dirs:
                if rd.name == tgt:
                    return ("match", rd)
            return ("needs-map", None)   # 指定的 remote 夾當前不存在 → 不憑空配對
        return scan._git_identity(local_dir, remote_dirs)
    return resolve


def _cmd_memory_merge_remote(args) -> int:
    """跨群 `memory-merge --from <remote>`：偵測**本機 memory ↔ remote hub memory** 的衝突（conflict-content /
    remote-tombstone delete-vs-update；沿用 A3 尊重 remote tombstone、不跨群復活），保留兩版到本機暫存、產提示詞。
    **stateless**（無 per-remote 基線，對稱 transfer）：故單邊新檔＝blocked-no-baseline（非衝突、不 stage）、
    跨檔改名身分（cross-file-identity）此版**不偵測**（需基線語意；留 P2，同 transfer 的 stateless 殘留）。"""
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as e:
        print(f"config.toml 損壞，保守中止：{e}", file=sys.stderr)
        return 1
    name = args.from_remote
    remote_path = cfg.remotes.get(name)
    if not remote_path:
        have = ", ".join(sorted(cfg.remotes)) or "（無）"
        print(f"未知 remote '{name}'；先 `remote add {name} <path>`。現有：{have}", file=sys.stderr)
        return 1
    mappings, merr = _parse_maps(args.map)
    if merr:
        print(merr, file=sys.stderr)
        return 1
    local_root = args.local_root or str(default_local_root())
    # stateless（state=None，對稱 transfer）：衝突偵測不需基線（conflict-content=兩側皆在且異；delete-vs-update=
    # remote tombstone gate〕；memory_only 跳過 session；identity_fn 走 --map/git。halt（remote 未掛載）→ surface。
    plan = scan.build_plan(local_root, remote_path, None,
                           identity_fn=_memory_merge_remote_identity(mappings), memory_only=True)
    if plan.halt:
        print(scan.format_plan(plan))
        return 2
    conflicts = merge_mod.conflicts_from_plan(plan, project=args.project)
    unscannable = merge_mod.unscannable_memory_projects(plan, project=args.project)
    print(f"跨群 memory-merge（--from {name}）：偵測本機與 remote（{remote_path}）的 memory 衝突")
    # 誠實範圍聲明（codex R1 Medium）：stateless 跨群偵測「同檔名內容衝突」與「remote 刪除 vs 本機更新」，**不含**
    # 跨檔改名同一事實（cross-file-identity，需基線）→ 故「未偵測到衝突」不代表跨檔改名也沒有；別讓 partial 看似完整。
    print("（範圍：同檔名內容衝突 + remote 刪除衝突；不含跨檔改名同一事實〔留 P2〕，見 docs/memory-merge-from.md）")
    # 未配對的本機專案（needs-map/blocked-*）＝有 local 夾但無對應 remote 夾、**未比對** → **一律**印（不只在無衝突時，
    # codex R1 Medium：否則有衝突時 partial-scan 被包成完整掃描）；但受 `--project` 範圍限制（g1 Low：scoped 掃描不該
    # 報無關專案未配對）。提示補 --map。
    unpaired = [pp.local_dir for pp in plan.projects
                if pp.local_dir and not pp.hub_dir
                and (not args.project or Path(pp.local_dir).name == args.project)]
    if unpaired:
        print(f"（{len(unpaired)} 個本機專案未對應到 remote 夾、未比對；請用 --map 本機夾名=remote夾名 指定後重跑）")
    staging_forbidden = _own_hub_forbidden(local_root, cfg, args.hub)   # local + args.hub + cfg.own_hub + 所有 remote
    return _emit_memory_conflicts(conflicts, unscannable, staging_forbidden, args,
                                  dup_pks=_dup_target_pks(plan, args.project))


# 顯示一律過 merge._disp：memory 檔名/**專案夾名**在 POSIX 可含非 UTF-8 bytes（surrogateescape）或控制字元 →
# 直接 print 會 UnicodeEncodeError 崩潰（strict UTF-8 stdout）或破壞單行；比照 merge.format_conflicts 中和。


def _print_fuzzy_unscannable(unscannable: list[str]) -> None:
    """印 memory 未掃描警告（含內嵌 raw pk——fuzzy-g1 Low：此警告行也須過 _disp）。Block A 列出與 Block B 放行共用。"""
    if not unscannable:
        return
    print("⚠ 下列專案的 memory 未被掃描（無法判斷是否有近似候選），請修為實體目錄後重跑：")
    for u in sorted(set(unscannable)):
        print(f"  - {merge_mod._disp(u)}")


def _print_fuzzy_candidate_lines(candidates: list) -> None:
    """逐候選印 advisory 摘要行（pk 標頭 + 相似度 + 共享詞元 + 兩側 name；全過 _disp 防 surrogate 崩）。列出與
    --stage 前置檢視共用同一份顯示邏輯（surrogate 安全性單一真相源）。"""
    d = merge_mod._disp
    cur = None
    for c in candidates:
        if c.project_key != cur:
            cur = c.project_key
            print(f"\n● {d(c.project_key)}")
        print(f"  ~ {d(c.a)}  ↔  {d(c.b)}   相似度 {c.score:.2f}（name {c.name_sim:.2f} / desc {c.desc_sim:.2f}）")
        if c.shared_name_tokens:
            print(f"      共享 name 詞元：{d(', '.join(c.shared_name_tokens))}")
        print(f"      A: name={d(c.name_a) or '（無/不可判）'}")
        print(f"      B: name={d(c.name_b) or '（無/不可判）'}")


def _emit_fuzzy(candidates: list, unscannable: list[str], threshold: float) -> int:
    """fuzzy 唯讀列出（Block A）。**只印、不寫**。unscannable（memory 沒掃到）→ 非零（不把「沒掃到」誤當「無候選」，
    比照 `_emit_memory_conflicts`）；找到候選＝資訊性、非失敗 → 0。放行（保留兩版）走 --stage/--interactive。"""
    _print_fuzzy_unscannable(unscannable)
    if not candidates:
        if not unscannable:
            print(f"（未偵測到 memory 模糊近似候選；閾值 {threshold}）")
        return 1 if unscannable else 0
    print("=== memory 模糊近似候選（advisory；只提示、絕不自動合併、不寫任何檔）===")
    print(f"（純 name+description 字面比對、閾值 {threshold}；這只是「疑似同一事實」的提示，請自行檢視是否真同一件事。）")
    _print_fuzzy_candidate_lines(candidates)
    print(f"\n共 {len(candidates)} 對疑似重複（advisory）。fuzzy 永不自動合併——請自行檢視。要把兩版保留到本機暫存"
          "供合併，加 --stage（全部）或 --interactive（逐對確認）。")
    return 1 if unscannable else 0


def _fuzzy_reason(cand) -> str:
    """fuzzy 衝突的 reason 字串（顯示用、進 build_prompt/format 時再過 _disp；不進 fingerprint）。"""
    shared = "、".join(cand.shared_name_tokens) if cand.shared_name_tokens else "（無共享 name 詞元）"
    return (f"模糊近似候選（相似度 {cand.score:.2f}；name {cand.name_sim:.2f}/desc {cand.desc_sim:.2f}；"
            f"共享 name 詞元：{shared}）")


def _fuzzy_stdin_decider(cand) -> bool:
    """CLI 互動 decider（比照 `_stdin_decider` 樣式）：對一對模糊候選問「當同一則、保留兩版？」。**預設 N**（保守——
    不放行就不寫，守 cardinal）；EOF（無互動輸入）→ N。可由測試 monkeypatch `builtins.input`。"""
    d = merge_mod._disp
    print(f"\n  疑似同一事實：{d(cand.a)}  ↔  {d(cand.b)}   相似度 {cand.score:.2f}"
          f"（name {cand.name_sim:.2f} / desc {cand.desc_sim:.2f}）")
    if cand.shared_name_tokens:
        print(f"      共享 name 詞元：{d(', '.join(cand.shared_name_tokens))}")
    print(f"      A: name={d(cand.name_a) or '（無/不可判）'}")
    print(f"      B: name={d(cand.name_b) or '（無/不可判）'}")
    try:
        ans = input("    當成同一則、保留兩版供合併？[y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def _run_fuzzy_stage(args, candidates: list, unscannable: list[str], threshold: float,
                     score_src: dict, staging_forbidden: list) -> int:
    """Block B：把使用者**放行**的模糊候選導進 leak-safe 保留兩版（`--stage`＝全部 / `--interactive`＝逐對確認）。

    cardinal＝**放行才寫**（分數不裁定）＋**不外洩**（暫存根過 `unsafe_staging_root`、同一般衝突路徑）。每檔只從其
    **計分來源側**（`score_src[pk][檔名鍵]`＝單一 `(side, mdir)`）讀取，**不**回退/probe 別側同名檔（g2 High：杜絕靜默
    替換；亦天然免除多對一夾歧義）。全程只讀正式 memory、只寫 memory/ 外的暫存（A3）。"""
    interactive = args.interactive
    rc = 1 if unscannable else 0
    _print_fuzzy_unscannable(unscannable)
    if not candidates:
        if not unscannable:
            print(f"（未偵測到 memory 模糊近似候選；閾值 {threshold}）")
        return rc
    if interactive:
        print("=== 逐對確認模糊近似候選（放行才保留兩版；預設 N）===")
    else:   # --stage：先完整列出（scores+names）供檢視，再全部保留
        print("=== memory 模糊近似候選（將全部保留兩版供合併）===")
        print(f"（純 name+description 字面比對、閾值 {threshold}；保留只是把兩版存到本機暫存供你合併——請自行檢視是否真同一件事。）")
        _print_fuzzy_candidate_lines(candidates)
    approved: list = []
    for cand in candidates:
        if interactive and not _fuzzy_stdin_decider(cand):
            continue
        # 只從各檔的**計分來源側**讀（單一 (side, mdir)）；找不到來源 → 空 → fuzzy_conflict 判缺 → degraded。
        src = score_src.get(cand.project_key, {})
        a_src = src.get(scan._name_key(cand.a))
        b_src = src.get(scan._name_key(cand.b))
        approved.append(merge_mod.fuzzy_conflict(
            cand.project_key, cand.a, [a_src] if a_src else [], cand.b, [b_src] if b_src else [],
            reason=_fuzzy_reason(cand)))
    if not approved:
        print("\n（未放行任何候選；未保留任何內容。）")
        return rc
    if any(c.notes for c in approved):   # 放行後某檔讀不到/專案夾逃逸 → 退化 → 非零（比照 _emit_memory_conflicts；
        rc = 1                            # R1 Medium：否則「兩檔皆消失」→ stage_conflict 回 empty、rc 仍 0，誤報成功）
    # leak-safe：暫存根不可落在任一受同步樹（local/hub/所有 remote）→ 否則兩版+PROMPT.md 落同步區外洩（同一般衝突）。
    bad = merge_mod.unsafe_staging_root(merge_mod.merge_root(), staging_forbidden)
    if bad:   # bad 內嵌暫存根/受同步樹路徑（POSIX 可含 surrogate）→ 過 _disp，免拒絕訊息崩 strict stderr（g3 Low 同類）。
        print(f"\n⚠ 拒絕保留兩版（暫存根不安全）：{merge_mod._disp(bad)}", file=sys.stderr)
        return 1
    print("\n" + merge_mod.LEAK_WARNING)
    print("\n=== 保留兩版到本機暫存（memory/ 之外，絕不同步）===")
    for c in approved:
        res = merge_mod.stage_conflict(c, apply=True)
        _print_stage(res)
        # empty（兩檔皆讀不到）亦為失敗（R1 Medium）；degraded/incomplete/stale/error 與寫入失敗同。
        if res.status in ("error", "incomplete", "degraded", "stale", "empty") \
                or any("失敗" in n for n in res.notes):
            rc = 1
    if args.prompt_stdout:
        print("\n=== 合併提示詞（stdout；貼進 Claude 前請先刪減敏感段）===")
        for c in approved:
            print(merge_mod.build_prompt(c))
            print()
    return rc


def _cmd_memory_merge_fuzzy(args) -> int:
    """memory-merge --fuzzy：列「同事實、不同檔名」的模糊近似候選（Block A，唯讀 advisory）；使用者以 `--stage`
    （全部）/`--interactive`（逐對確認）**放行**後才把兩版保留到 memory/ 外的暫存（Block B）。

    **cardinal**：fuzzy 分數**永不裁定**——列出永不寫檔；保留兩版**只在使用者放行後**發生，且**只讀正式 memory、
    只寫 memory/ 外暫存、絕不碰 classify/apply/sync**（誤判在此至多多存一組暫存，零資料危害）。掃**兩側 memory 的
    聯集**（依 `_name_key` 去重；一側缺/不可掃仍就另一側算，近似重複可能只在單側），逐專案算候選。"""
    if getattr(args, "from_remote", None):
        print("跨群 fuzzy（--fuzzy --from）尚未實作；目前 --fuzzy 只支援 own-hub。", file=sys.stderr)
        return 1
    stage = getattr(args, "stage", False)
    interactive = getattr(args, "interactive", False)
    if getattr(args, "apply", False):   # fuzzy 的放行動詞是 --stage/--interactive，不是 --apply（避免誤用）。
        print("--fuzzy 的候選放行請用 --stage（全部）或 --interactive（逐對），不是 --apply。", file=sys.stderr)
        return 1
    if getattr(args, "prompt_stdout", False) and not (stage or interactive):
        print("--fuzzy 搭配 --prompt-stdout 需再加 --stage 或 --interactive（決定為哪些候選產生提示詞）。",
              file=sys.stderr)
        return 1
    ctx, err = _resolve_context(args)
    if err:
        print(err, file=sys.stderr)
        return 1
    assert ctx is not None
    threshold = args.fuzzy_threshold
    if not (0.0 <= threshold <= 1.0):   # 也擋 nan（任何比較皆 False → 落此）/inf/負/>1（codex r1 Low：nan 否則全印）
        print("--fuzzy-threshold 須為 0~1 之間的數（含邊界）", file=sys.stderr)
        return 1
    # memory_only：fuzzy 只看 memory、不需 session 分類（省最重的一段，比照 nudge/--from）。halt（掛錯碟等）→ surface。
    plan = scan.build_plan(ctx.local_root, ctx.hub, ctx.state, memory_only=True)
    if plan.halt:
        print(scan.format_plan(plan))
        return 2
    # pk 桶完整性（e2e-r1 F1 + g1 + g2）：不同專案的 memory 絕不可混進同一暫存命名空間。三類 fail-closed 跳過：
    # (a) ≥2 本機專案綁同一 hub 夾；(b) 某 local-only 專案名恰等於另一專案的 hub pk（local A→hub P 與 local-only P
    # 皆落 pk="P"，g1）——(a)(b) 皆「同一 raw pk 桶收到 >1 相異 local 或 hub 側」；(c) 兩個**相異 raw pk**（如 local "P"
    # + hub "p"）大小寫/正規化折疊後相同 → 各自獨立 by_pk 桶、但暫存夾 <merge>/<pk>/… 在**大小寫/正規化不敏感的快取
    # FS**（Windows NTFS 預設）上撞成同一實體夾、且 `_conflict_fingerprint` 已納 pk 但夾仍撞 → 混淆（g2）。用 fuzzy 自己
    # 的 pk 推導（hub 名優先、退 local 名）逐 pp 累計各 raw pk 的相異 local/hub 側 + 依 `scan._name_key` 分組折疊。
    _pk_locals: dict[str, set] = {}
    _pk_hubs: dict[str, set] = {}
    for _pp in plan.projects:
        # dup_hub（blocked-dup-local 保留的原 pk）優先：兩夾同綁一 hub 被上游擋後，仍須落同一 pk 桶才抓得到 dup。
        _pk = _pp.dup_hub or (Path(_pp.hub_dir).name if _pp.hub_dir
                              else (Path(_pp.local_dir).name if _pp.local_dir else None))
        if _pk is None:
            continue
        if _pp.local_dir:
            _pk_locals.setdefault(_pk, set()).add(str(Path(_pp.local_dir)))
        if _pp.hub_dir:
            _pk_hubs.setdefault(_pk, set()).add(str(Path(_pp.hub_dir)))
    _pks = set(_pk_locals) | set(_pk_hubs)
    _by_name_key: dict[str, set] = {}            # (c)：折疊鍵 → 相異 raw pk 集；>1 → 折疊撞（暫存夾在不敏感 FS 上會撞）
    for pk in _pks:
        _by_name_key.setdefault(scan._name_key(pk), set()).add(pk)
    dup_pks = frozenset(pk for pks in _by_name_key.values() if len(pks) > 1 for pk in pks) | frozenset(
        pk for pk in _pks if len(_pk_locals.get(pk, ())) > 1 or len(_pk_hubs.get(pk, ())) > 1)
    # **依專案夾名（pk）聚合兩側 memory**（codex r1 Medium）：同名的 local/hub 專案即使未經 identity 配對，其 memory 仍
    # 歸同一 pk 桶 → 跨側近似候選抓得到、且每 pk 只列一次（不重複標頭）。桶內依 `_name_key` 去重（同一檔別名拼寫）。
    # 放行時（Block B）需知每檔的**計分來源**以精確重讀：`score_src[pk][檔名鍵] = (side, memory夾)`＝**計分那一則
    # 所在的單一確切側/夾**（首見側）。放行後 stage **只**從這個確切來源讀 `cand.a`/`cand.b`——**不**回退別側、**不** probe
    # 別側的同名/別名檔（g2 High：綁「檔名鍵的所有側」仍會在計分側消失時讀到別側無關同名檔＝靜默替換；綁單一計分側
    # 才杜絕）。同時天然免除「多本機夾→同 pk」歧義（讀的是確切來源夾、非「某一側」，各檔各讀其來源）。
    by_pk: dict[str, dict[str, fuzzy_mod.FuzzyEntry]] = {}
    score_src: dict[str, dict[str, tuple[str, Path]]] = {}
    unscannable: list[str] = []
    for pp in plan.projects:
        pk = pp.dup_hub or (Path(pp.hub_dir).name if pp.hub_dir
                            else (Path(pp.local_dir).name if pp.local_dir else None))
        if pk is None:
            continue
        if args.project is not None and pk != args.project:
            continue
        if pk in dup_pks:   # 不同專案會混進同一暫存命名空間（同一 pk 桶，或大小寫/正規化折疊後暫存夾相撞）→ 跳過整個
            # pk（fail-closed；避免不同專案 memory 混淆/靜默取首見側/暫存撞名，e2e-r1 F1 + g1 + g2）
            unscannable.append(f"{pk}（此 pk 會與其他專案的 memory 混〔同一 pk 桶，或大小寫/正規化折疊後暫存夾相撞〕→ "
                               "已跳過；請確認專案夾名/配對後重跑）")
            continue
        entries = by_pk.setdefault(pk, {})           # _name_key(檔名) → entry（計分用；首見側）
        for d, side in ((pp.local_dir, "local"), (pp.hub_dir, "hub")):
            if not d:
                continue
            dd = Path(d)
            # 專案夾逃逸（symlink/junction 逃出信任根）→ 不從界外讀 memory（比照 merge.unscannable_memory_projects）。
            if not scan._safe_project_dir(dd.parent, dd):
                unscannable.append(f"{pk}（{side}：專案夾為 symlink/逃逸信任根）")
                continue
            mdir = memory_mod.memory_dir(d)
            try:
                files = memory_mod.list_memory_files(mdir)
            except memory_mod.UnsafeMemoryDir:
                unscannable.append(f"{pk}（{side}：memory/ 根為 symlink）")
                continue
            except OSError as e:
                unscannable.append(f"{pk}（{side}：memory 夾讀取失敗 {e.__class__.__name__}）")
                continue
            for fname in files:
                key = scan._name_key(fname)
                if key in entries:
                    continue   # 計分＋來源都只留首見側（另一側/別名拼寫同鍵視為同檔、不覆蓋來源綁定）
                # **父夾+最終檔 no-follow 讀**（codex r1 Medium）：list_memory_files 列舉時已排除 symlink leaf，但列舉→讀取
                # 間檔可被抽換成 symlink → 跟隨讀到界外檔；比照 merge._read_nofollow（單一真相源）。讀不到/被換 → 略過。
                data = merge_mod._read_nofollow(mdir, fname)
                if data is None:
                    continue
                doc = memory_mod.load_memory_bytes(data)
                desc = None
                if doc.fm_ok and doc.frontmatter:
                    dv = doc.frontmatter.get("description")
                    desc = dv if isinstance(dv, str) else None
                entries[key] = fuzzy_mod.FuzzyEntry(fname, doc.name, desc)
                score_src.setdefault(pk, {})[key] = (side, mdir)   # 綁定計分來源側（放行後只讀這裡，不猜別側）
    candidates: list = []
    for pk in sorted(by_pk):
        candidates.extend(fuzzy_mod.find_candidates(pk, list(by_pk[pk].values()), threshold=threshold))
    if not stage and not interactive:   # Block A：唯讀列出
        return _emit_fuzzy(candidates, unscannable, threshold)
    # Block B：使用者放行 → leak-safe 保留兩版。暫存不可落任一受同步樹（local + hub 兩者 + 所有 remote）。
    staging_forbidden = _own_hub_forbidden(ctx.local_root, ctx.config, args.hub)
    return _run_fuzzy_stage(args, candidates, unscannable, threshold, score_src, staging_forbidden)


def _cmd_memory_merge(args) -> int:
    """memory-merge（DESIGN §7.3 / §9）：偵測 memory 衝突 → 保留兩版到本機暫存（memory/ 之外、不同步）+ 報告
    + 產合併提示詞（明文外洩警告）。**獨立指令、不併進 sync**；真正 AI 合併留 P2。預設 dry-run。
    `--from <remote>` ＝跨群版（本機 ↔ remote hub，見 `_cmd_memory_merge_remote`）；不給則 own-hub。
    `--fuzzy` ＝唯讀模糊近似候選列出（Block A；只提示、不寫檔，見 `_cmd_memory_merge_fuzzy`）。"""
    if getattr(args, "fuzzy", False):
        return _cmd_memory_merge_fuzzy(args)
    if getattr(args, "stage", False) or getattr(args, "interactive", False):
        print("--stage / --interactive 僅用於 --fuzzy 的候選放行；一般 memory 衝突請用 --apply（保留兩版）或 "
              "--prompt-stdout（印提示詞）。", file=sys.stderr)
        return 1
    if getattr(args, "from_remote", None):
        return _cmd_memory_merge_remote(args)
    ctx, err = _resolve_context(args)
    if err:
        print(err, file=sys.stderr)
        return 1
    assert ctx is not None
    # 先 build_plan 並 surface halt（掛錯碟/指紋變等）——否則 find_conflicts 在 halt 時靜默回「無衝突」誤導使用者。
    plan = scan.build_plan(ctx.local_root, ctx.hub, ctx.state)
    if plan.halt:
        print(scan.format_plan(plan))
        return 2
    conflicts = merge_mod.conflicts_from_plan(plan, project=args.project)
    unscannable = merge_mod.unscannable_memory_projects(plan, project=args.project)
    # local + args.hub + cfg.own_hub + 所有 remote 都不可落入（g1/g2 High；ctx.hub=args.hub or own_hub 已被涵蓋）。
    staging_forbidden = _own_hub_forbidden(ctx.local_root, ctx.config, args.hub)
    return _emit_memory_conflicts(conflicts, unscannable, staging_forbidden, args,
                                  dup_pks=_dup_target_pks(plan, args.project))


def _doctor_show_acked(hub, project) -> int:
    """列出已 acknowledged 的 damaged/collision 項（A15）。純讀、不寫。"""
    dirs = doctor_mod.hub_project_dirs(hub)
    if project:
        dirs = [d for d in dirs if d.name == project]
    shown = False
    for hd in dirs:
        led = acks_mod.load_ledger(hd)
        if not led.ok:
            print(f"專案 {hd.name}：acks.json 損壞（已忽略、全部照常回報）")
            shown = True
            continue
        if not led.by_key:
            continue
        print(f"專案 {hd.name}：")
        for _key, rec in sorted(led.by_key.items()):   # key=(kind,identity,fingerprint) 三元組（g2）；顯示用 rec 欄位
            print(f"  · [{rec.get('kind')}] {rec.get('label') or rec.get('identity')}"
                  f"（{rec.get('acked_at', '?')} @ {rec.get('acked_by', '?')}）")
        shown = True
    if not shown:
        print("目前沒有任何 acknowledged 項。")
    return 0


def _group_by_hub_dir(items):
    """保序把 AckItem 依 hub_dir 分組（回 [(hub_dir, [items])]）。"""
    order: list[str] = []
    by_dir: dict[str, list] = {}
    for it in items:
        if it.hub_dir not in by_dir:
            by_dir[it.hub_dir] = []
            order.append(it.hub_dir)
        by_dir[it.hub_dir].append(it)
    return [(hd, by_dir[hd]) for hd in order]


def _doctor_ack(local_root, hub, state_path, project, *, apply) -> int:
    """把目前所有 damaged/collision blocked 項標記 acknowledged（A15）。預覽預設、--yes 落地。
    列舉走 read-only `build_plan`（單一真相源，指紋與 sync 一致）；state 損壞則請先 --rebuild-state。"""
    try:
        st = state_mod.load_or_none(state_path)
    except state_mod.StateCorruptError as e:
        print(f"state 損壞，無法列舉可 ack 項（請先 doctor --rebuild-state）：{e}", file=sys.stderr)
        return 1
    plan = scan.build_plan(local_root, hub, st)
    if plan.halt:
        print(scan.format_plan(plan))
        print("\n偵測到 halt 級異常，未進行 ack。", file=sys.stderr)
        return 2
    items = acks_mod.ackable_from_plan(plan)
    if project:
        items = [it for it in items if it.project == project]
    # 濾掉不可綁定內容者（fp=None：讀不到檔）——無從綁定內容變動 → 不可 ack（fail-closed，g6）；提示使用者。
    unbindable = [it for it in items if it.fingerprint is None]
    items = [it for it in items if it.fingerprint is not None]
    if unbindable:
        print(f"（{len(unbindable)} 項因讀不到內容無法 ack、將持續回報："
              f"{', '.join(sorted(f'{it.project}/{it.label}' for it in unbindable))}）")
    if not items:
        suffix = f"（--project {project}）" if project else ""
        print(f"目前沒有可 acknowledge 的 damaged/collision 項。{suffix}")
        return 0
    print("將 acknowledge 以下 damaged/collision 項（審閱後 sync/doctor 不再重報；內容/撞名集變動會自動重新提示）：")
    to_ack = {}   # hub_dir -> [AckItem]（僅需新增者）
    for hub_dir, group in _group_by_hub_dir(items):
        led = acks_mod.load_ledger(hub_dir)
        pk = group[0].project
        for it in group:
            new = not acks_mod.is_acked(led, it.kind, it.identity, it.fingerprint)
            print(f"  · [{pk}] {it.kind} {it.label} — {'新增' if new else '已 ack（略過）'}")
            if new:
                to_ack.setdefault(hub_dir, []).append(it)
    if not to_ack:
        print("（皆已 acknowledged，無新增。）")
        return 0
    if not apply:
        print("\n（預覽）加 --yes 寫入 ack 帳本。")
        return 0
    n, err = 0, False
    for hub_dir, group in to_ack.items():
        try:
            res = acks_mod.update_ledger(hub_dir, add=group)
            n += len(res.added)
        except (acks_mod.UnsafeAcksDir, atomicio_mod.LockError, atomicio_mod.AtomicWriteError, OSError) as e:
            print(f"⚠ 寫入 ack 失敗（{Path(hub_dir).name}）：{e}", file=sys.stderr)
            err = True
    print(f"\n已 acknowledge {n} 項。")
    return 1 if err else 0


def _doctor_unack(hub, project, *, apply) -> int:
    """取消 acknowledgement（A15）。ledger 驅動（含已無對應現況項的孤兒 ack 也能清）。預覽預設、--yes 落地。"""
    dirs = doctor_mod.hub_project_dirs(hub)
    if project:
        dirs = [d for d in dirs if d.name == project]
    targets = []   # (hub_dir, key, label)；key=(kind,identity,fingerprint) 三元組（g2）
    for hd in dirs:
        led = acks_mod.load_ledger(hd)
        for key, rec in sorted(led.by_key.items()):
            targets.append((hd, key, rec.get("label") or rec.get("identity") or key[1]))
    if not targets:
        print("目前沒有任何 acknowledged 項可取消。")
        return 0
    print("將取消以下 acknowledgement（之後 sync/doctor 會重新回報）：")
    for hd, key, label in targets:
        print(f"  · [{hd.name}] {key[0]} {label}")
    if not apply:
        print("\n（預覽）加 --yes 取消上列 ack。")
        return 0
    by_dir = {}
    for hd, key, _ in targets:
        by_dir.setdefault(hd, []).append(key)
    n, err = 0, False
    for hd, keys in by_dir.items():
        try:
            res = acks_mod.update_ledger(hd, remove=keys)
            n += len(res.removed)
        except (acks_mod.UnsafeAcksDir, atomicio_mod.LockError, atomicio_mod.AtomicWriteError, OSError) as e:
            print(f"⚠ 取消 ack 失敗（{hd.name}）：{e}", file=sys.stderr)
            err = True
    print(f"\n已取消 {n} 項 ack。")
    return 1 if err else 0


def _cmd_doctor(args) -> int:
    """維護工具。**不走 _resolve_context**（它對壞 state 會中止，而 --rebuild-state 正是要救壞 state）。"""
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as e:
        print(f"config.toml 損壞，保守中止：{e}", file=sys.stderr)
        return 1
    hub = args.hub or cfg.own_hub
    if not hub:
        print("尚未設定 own_hub：請先 `config set own-hub <path>` 或用 --hub。", file=sys.stderr)
        return 1
    local_root = args.local_root or str(default_local_root())
    state_path = args.state or str(state_mod.default_state_path())

    if args.break_lock:
        # 只遞迴掃 hub + **明確的** state 鎖檔（不遞迴掃 state 父夾，免刪到無關 *.lock，codex r-doctor-3）。
        state_lock = Path(str(state_path) + doctor_mod._LOCK_SUFFIX)
        rep = doctor_mod.break_locks([Path(hub)], [state_lock], apply=args.yes)
        print("\n".join(rep.lines))
        print("\n※ break-lock 為單一操作者復原指令：請勿並行執行、且勿於 sync 進行中執行"
              "（並行 break-lock 可能在 check→unlink 窗誤刪剛重取的活鎖＝雙 writer；見 doctor.break_locks 說明）。")
        if not args.yes and rep.kept:
            print("（預覽）加 --yes 移除上列同機已死的 stale 鎖（跨機/存活者不動）。")
        return 1 if rep.errors else 0

    if args.rebuild_state:
        mappings, merr = _parse_maps(args.map)
        if merr:
            print(merr, file=sys.stderr)
            return 1
        res = doctor_mod.rebuild_state(local_root, hub, mappings=mappings)
        print("rebuild-state 預覽（hub 側無條件、local 側僅 --map；永不碰 tombstone）：")
        print("\n".join(res.lines) if res.lines else "  （無可重建）")
        if res.fatal:   # hub 不存在/非目錄 → 不寫（否則覆成空 state，codex r-doctor-1）
            return 1
        if not args.yes:
            print("\n（預覽）加 --yes 落地（覆寫 state.json）。")
            return 0
        try:
            path = doctor_mod.write_rebuilt_state(res, state_path)
        except (atomicio_mod.LockError, atomicio_mod.AtomicWriteError, OSError) as e:
            # AtomicWriteError/VerifyError 非 OSError 子類（readback 驗證失敗會走這），須一併捕捉
            # → 誠實非零退出，不外拋 traceback（codex r-doctor-5）。
            print(f"\n寫入失敗：{e}", file=sys.stderr)
            return 1
        print(f"\n已重建 state：{path}")
        return 0

    if args.show_acked:
        return _doctor_show_acked(hub, args.project)
    if args.unack_all:
        return _doctor_unack(hub, args.project, apply=args.yes)
    if args.ack_all:
        return _doctor_ack(local_root, hub, state_path, args.project, apply=args.yes)

    rep = doctor_mod.diagnose(local_root, hub, state_path, cfg)
    print(rep.text())
    print(f"\n問題數：{rep.problems}")
    return 1 if rep.problems else 0


# nudge 只提示**某指令實際會處理**的 memory 分歧，避免反覆吵不可動作項（DESIGN §7.5「有分歧」的可動作解讀）。
# 兩桶各綁其處理指令的**單一真相源動作集**——若那些集合日後變動，nudge 自動跟隨、不漂移：
#   · 更新 = `sync --apply` 會自動寫入者 = `apply.MEM_AUTO_ACTIONS` 去掉 identical（identical＝無分歧）。
#   · 衝突 = `memory-merge` 會處理者 = `merge.CONFLICT_ACTIONS`。
# suppressed-deleted（已定案刪除、sync 不再動）/ blocked-*（工具無法自動解、靜音出口是 A15 ack）不在任一桶 → 不吵。
_NUDGE_UPDATE_ACTIONS = apply_mod.MEM_AUTO_ACTIONS - {"identical"}


def _nudge_summary(plan: scan.SyncPlan) -> str | None:
    """把計畫濃縮成一行提示（無可動作分歧→None）。只看 memory：更新（sync 自動同步）與衝突（交 memory-merge）。

    **只算 hub+local 兩側皆綁定的專案**（`pp.local_dir and pp.hub_dir`，g4 Medium）：這正是兩個處理指令唯一會
    實際動到 memory 的專案型態——`memory-merge`（`merge.conflicts_from_plan`）明文只碰兩側皆綁者；`sync` 的 memory
    auto-apply 也需 mapping+coverage，實測**未配對專案的每個 memory 動作都是 `blocked-unmapped`/`blocked-*`**（見
    `tests/test_nudge` 的 hub-only 探測），本就不在任一桶。故加此閘不改變任何可達狀態的計數，卻能防未來 classify
    若把某 auto/conflict 洩漏到未配對專案時 nudge 去吵一個沒指令能處理的項（hub-only `conflict-delete-vs-update` 即
    如此：build_plan 會產它、但 memory-merge 不碰 → 不可動作 → 不吵）。"""
    updates = conflicts = 0
    for pp in plan.projects:
        if not (pp.local_dir and pp.hub_dir):   # 未配對（hub-only/local-only/needs-map…）→ 無指令會處理 → 不吵
            continue
        for m in pp.memories:
            if m.action in _NUDGE_UPDATE_ACTIONS:
                updates += 1
            elif m.action in merge_mod.CONFLICT_ACTIONS:
                conflicts += 1
    if not updates and not conflicts:
        return None
    if updates and conflicts:
        return (f"claude-session-sync：記憶待同步（{updates} 更新、{conflicts} 衝突）"
                "→ 更新執行 `sync --apply`、衝突執行 `memory-merge`")
    if updates:
        return f"claude-session-sync：{updates} 個記憶更新待同步 → 執行 `sync --apply`"
    return f"claude-session-sync：{conflicts} 個記憶衝突待處理 → 執行 `memory-merge`"


def _compute_nudge(args) -> str | None:
    """唯讀算出 nudge 訊息。**掛載點不在/未設定/halt → None（不 nudge）**。任何例外由 `_cmd_nudge` 靜默吞。"""
    cfg = config_mod.load()                          # 壞 config → ConfigError → 上層靜默
    hub = args.hub or cfg.own_hub
    if not hub or not Path(hub).is_dir():            # 未設定 own_hub、或掛載點不在（G5 載體可有可無）→ 不 nudge
        return None
    st = state_mod.load_or_none(args.state)          # 壞 state → StateCorruptError → 上層靜默
    local_root = args.local_root or str(default_local_root())
    plan = scan.build_plan(local_root, hub, st, memory_only=True)   # 不做重活：跳過 session 分類
    if plan.halt:                                    # 掛錯碟/指紋異常等 → 交 routine sync/status 處理，nudge 不吵
        return None
    return _nudge_summary(plan)


def _cmd_nudge(args) -> int:
    """SessionEnd/SessionStart hook 助手（DESIGN §7.5）：**唯讀、fail-silent**，掛載點在才比對 memory，有分歧
    印一行提示。預設輸出 JSON `{"systemMessage": …}`（Claude Code 對使用者顯示；SessionEnd stdout 本身不顯示、
    但 systemMessage 通用顯示，SessionStart 亦可）；`--text` 改印純文字（手動執行/除錯用）。

    **絕不干擾 session 結束**：任何錯誤/未設定/掛載點不在/halt/無分歧 → 靜默 exit 0。**輸出也在 try 內**——
    否則 hook 子程序若 stdout 非 UTF-8（如 `PYTHONIOENCODING=ascii`/cp1252）印中文會拋 `UnicodeEncodeError`、
    以 traceback 非零退出＝破壞 fail-silent（codex R1 Medium）。JSON 用預設 `ensure_ascii=True`（純 ASCII 的
    `\\uXXXX`、任何 stdout 編碼都印得出、Claude Code JSON parser 還原成中文顯示）→ 主 hook 路徑最穩、提示不被
    默默吞掉；`--text` 原樣中文在無法編碼的終端會拋 → 由 try 靜默吞（手動除錯的邊角情況）。不寫、不鎖、不讀
    stdin（hook 傳的 JSON 不需要、也避免手動執行時卡等 stdin）。"""
    try:
        msg = _compute_nudge(args)
        if msg:
            line = msg if getattr(args, "text", False) else json.dumps({"systemMessage": msg})
            sys.stdout.write(line + "\n")
            sys.stdout.flush()   # flush 也在 try 內：延後到 flush 才觸發的編碼錯也要吞
    except BrokenPipeError:
        # hook supervisor 提早關掉我們的 stdout → 把 stdout fd 導向 devnull，避免直譯器**關閉時**再 flush 殘留
        # buffer 又撞破管、令行程以非零/「Exception ignored」收場（g1 Low）。**開的 fd 必 close**（finally），
        # 免每次呼叫漏一個 devnull fd（g2 Low）；fileno()/dup2 失敗（測試用非真實 fd）→ 由外層 except 吞。
        try:
            fd = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(fd, sys.stdout.fileno())
            finally:
                os.close(fd)
        except Exception:  # noqa: BLE001
            pass
        return 0
    except Exception:  # noqa: BLE001 — 建議性指令：任何失敗都不得吵/崩，一律靜默退出 0（hook 不可中斷 session）
        return 0
    return 0


def _utf8_output() -> None:
    """把 stdout/stderr 轉成 UTF-8。

    Windows 非 UTF-8 code page（如 cp950/Big5）下，stdout 一被導向管線或檔案，Python 就改用 locale 編碼，
    輸出裡的 `⚠` 等字元直接 UnicodeEncodeError → 整個指令炸掉（連唯讀的 status 也是）。
    """
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:  # noqa: BLE001 — 被替換成非 TextIOWrapper（測試捕捉/重導）→ 維持原樣，不讓它擋住指令。
            pass           # 寬接：本函式在 main() 最前、nudge 的 fail-silent 網之外，任何例外逃出都會破壞 hook 鐵則。


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    p = argparse.ArgumentParser(prog="claude-session-sync", description="跨機同步 Claude Code session/memory")
    sub = p.add_subparsers(dest="cmd", required=True)

    _add_common(sub.add_parser("status", help="顯示與 hub 的差異（唯讀）"))

    sp_sync = sub.add_parser("sync", help="同步（預設 dry-run；--apply 安全寫入）")
    _add_common(sp_sync)
    sp_sync.add_argument("--apply", action="store_true",
                         help="實際寫入（自動套用 identical/ff/copy；偵測到本機刪除寫 tombstone 通知對側）")
    sp_sync.add_argument("--interactive", action="store_true",
                         help="搭配 --apply：對 fork/superset 互動 union/keep-both（寫 keep-both 新檔，不覆蓋）")

    sp_bs = sub.add_parser("bootstrap", help="建立同步基線（首次同步；不複製/不刪）")
    _add_common(sp_bs)
    sp_bs.add_argument("--map", action="append", metavar="LOCAL=HUB", help="明示 local夾名→hub夾名（可重複）")
    sp_bs.add_argument("--ignore", action="append", metavar="SID", help="排除某 sid 不傳播（寫 suppress tombstone，可重複）")
    sp_bs.add_argument("--yes", action="store_true", help="確認落地（否則只預覽）")

    sp_pull = sub.add_parser("pull", help="跨群：remote hub → local（明確、可挑選；預設 dry-run）")
    _add_common(sp_pull)
    sp_pull.add_argument("--from", dest="from_remote", metavar="REMOTE", help="來源 remote 名（config [remotes]）")
    sp_pull.add_argument("--session", metavar="SID", help="只傳此 sessionId（不給則該方向全部）")
    sp_pull.add_argument("--map", action="append", metavar="LOCAL=REMOTE", help="明示 local夾名→remote夾名（可重複）")
    sp_pull.add_argument("--apply", action="store_true", help="實際寫入（僅 copy/ff；C3 不覆蓋 local）")

    sp_push = sub.add_parser("push", help="跨群：local → remote hub（明確、可挑選；預設 dry-run）")
    _add_common(sp_push)
    sp_push.add_argument("--to", dest="to_remote", metavar="REMOTE", help="目標 remote 名（config [remotes]）")
    sp_push.add_argument("--session", metavar="SID", help="只傳此 sessionId（不給則該方向全部）")
    sp_push.add_argument("--map", action="append", metavar="LOCAL=REMOTE", help="明示 local夾名→remote夾名（可重複）")
    sp_push.add_argument("--apply", action="store_true", help="實際寫入 remote hub（僅 copy/ff）")

    sp_remote = sub.add_parser("remote", help="管理跨群 remote hub")
    rsub = sp_remote.add_subparsers(dest="remote_cmd", required=True)
    r_add = rsub.add_parser("add", help="新增/覆寫一個 remote")
    r_add.add_argument("name")
    r_add.add_argument("path")
    rsub.add_parser("list", help="列出所有 remote")

    sp_doc = sub.add_parser("doctor", help="診斷 / --rebuild-state / --break-lock")
    _add_common(sp_doc)
    sp_doc.add_argument("--rebuild-state", action="store_true",
                        help="由磁碟重建 state（state 損壞救援；hub 側無條件、local 側需 --map；永不碰 tombstone）")
    sp_doc.add_argument("--break-lock", action="store_true",
                        help="移除同機已死的 stale 鎖（需 --yes；跨機/存活/無法解析者不動）")
    sp_doc.add_argument("--map", action="append", metavar="LOCAL=HUB",
                        help="rebuild-state 用：明示 local夾名→hub夾名 重建 local 基線（可重複）")
    sp_doc.add_argument("--ack-all", action="store_true",
                        help="把目前所有 damaged/collision blocked 項標記 acknowledged（需 --yes；審閱後 sync/doctor 不再重報）")
    sp_doc.add_argument("--unack-all", action="store_true",
                        help="取消所有 acknowledgement（需 --yes；之後重新回報）")
    sp_doc.add_argument("--show-acked", action="store_true", help="列出目前已 acknowledged 的 damaged/collision 項")
    sp_doc.add_argument("--project", metavar="HUB_DIR", help="ack/unack/show 只限此 hub 專案夾名")
    sp_doc.add_argument("--yes", action="store_true", help="確認落地（rebuild/break-lock/ack/unack 否則只預覽）")

    sp_nudge = sub.add_parser(
        "nudge", help="hook 助手：唯讀檢查 memory 分歧，有就印一行提示（給 SessionEnd/SessionStart hook 用）")
    _add_common(sp_nudge)
    sp_nudge.add_argument("--text", action="store_true",
                          help="印純文字（預設印 JSON systemMessage 供 Claude Code hook 對使用者顯示）")

    sp_mm = sub.add_parser("memory-merge", help="memory 衝突：保留兩版到本機暫存 + 產合併提示詞（明文外洩警告）")
    _add_common(sp_mm)
    sp_mm.add_argument("--from", dest="from_remote", metavar="REMOTE",
                       help="跨群：偵測本機 ↔ 此 remote hub 的 memory 衝突（config [remotes]；不給則 own-hub）")
    sp_mm.add_argument("--map", action="append", metavar="LOCAL=REMOTE",
                       help="搭配 --from：明示 local夾名→remote夾名配對（可重複；工具未寫 sidecar 故跨群多半需要）")
    sp_mm.add_argument("--project", metavar="HUB_DIR", help="只看此專案夾名（不給則全部）")
    sp_mm.add_argument("--apply", action="store_true",
                       help="把兩版保留到本機暫存（memory/ 之外、不同步；含 PROMPT.md）；否則只預覽")
    sp_mm.add_argument("--prompt-stdout", action="store_true",
                       help="把合併提示詞印到 stdout（不寫檔；貼進 Claude 前請先刪減敏感段）")
    sp_mm.add_argument("--fuzzy", action="store_true",
                       help="列出「同事實、不同檔名」的模糊近似候選（唯讀 advisory；只提示、不自動合併、不寫檔）")
    sp_mm.add_argument("--fuzzy-threshold", type=float, default=fuzzy_mod.DEFAULT_THRESHOLD,
                       metavar="F", help=f"fuzzy 相似度閾值（0~1，預設 {fuzzy_mod.DEFAULT_THRESHOLD}；越低越敏感、誤報越多）")
    # fuzzy 候選放行（Block B）：把使用者放行的候選導進 leak-safe 保留兩版（僅搭配 --fuzzy；一般衝突請用 --apply）。
    g_fuzzy = sp_mm.add_mutually_exclusive_group()
    g_fuzzy.add_argument("--stage", action="store_true",
                         help="搭配 --fuzzy：把**所有**候選保留兩版到本機暫存（含 PROMPT.md；memory/ 之外、不同步）")
    g_fuzzy.add_argument("--interactive", action="store_true",
                         help="搭配 --fuzzy：逐對確認「當同一則、保留兩版？」，只保留放行的候選")

    # nudge 是 fail-silent hook 助手：連 **argparse 用法錯誤/說明**（壞 hook 設定，如 `nudge --bogus`、誤含
    # `--help`）都不能非零/中斷 session、也不能污染輸出——parse_args 出錯會 `SystemExit(2)`、`--help` 會 SystemExit(0)
    # （皆在 dispatch 前、_cmd_nudge 的 try 包不到，g1 Medium）。故子指令是 nudge 時：把 argparse 的 usage/error/help
    # **stdout 與 stderr 都導進記憶體 StringIO**——error 走 stderr（g2 Medium：直觸 real stderr 可能編碼失敗/破管在
    # SystemExit 前逃出）、`--help`/`-h` 走 **stdout**（g3 Medium：非 JSON help 文字會污染 hook 讀到的 stdout、且若
    # stdout 已關會繞過 BrokenPipe handler）。**只包 parse_args**（dispatch 在外，正常 nudge 輸出不被吞）；攔 SystemExit
    # **與任何其他例外** → 退 0。（第一 token 必為子指令：本 parser 無子指令前的全域旗標。）
    raw = (sys.argv[1:] if argv is None else list(argv))
    nudge_mode = bool(raw) and raw[0] == "nudge"
    if nudge_mode:
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                args = p.parse_args(argv)
        except SystemExit:
            return 0
        except Exception:  # noqa: BLE001 — 壞參數解析的任何例外都不得中斷 session
            return 0
    else:
        args = p.parse_args(argv)
    if args.cmd == "bootstrap":
        return _cmd_bootstrap(args)
    if args.cmd == "pull":
        return _cmd_transfer(args, transfer_mod.PULL)
    if args.cmd == "push":
        return _cmd_transfer(args, transfer_mod.PUSH)
    if args.cmd == "remote":
        return _cmd_remote(args)
    if args.cmd == "doctor":
        return _cmd_doctor(args)
    if args.cmd == "memory-merge":
        return _cmd_memory_merge(args)
    if args.cmd == "nudge":
        return _cmd_nudge(args)
    return _cmd_status_or_sync(args)


if __name__ == "__main__":
    sys.exit(main())
