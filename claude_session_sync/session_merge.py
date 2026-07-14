"""session_merge：fork/superset 兩枝的**決定性、無損** union（H2/H3 + DESIGN A1/§6.6）。

語意（PLAN v0.8 §2.4，N1：行級 union 非語意合併）：
  1. 共同前綴 = 兩枝**依檔序逐行比 line-identity 到第一個 divergence**（uuid 行比 (uuid,hash)、
     內容 no-uuid 比 content-hash）；揮發 meta（last-prompt/mode/title…）先濾掉、不參與也不保留。
  2. 共同前綴輸出「一次」。
  3. 各 branch 分歧段接在共同前綴後、依原序整段輸出：
     - uuid 行：(uuid,hash) **去重**（跨整檔；同 uuid 異 hash 應已被 classify 擋成 damaged）；
     - no-uuid 內容行（summary 等）：**不去重**——同段保留 multiplicity、跨段（A vs B）即使同 hash 也各留
       （屬不同枝的摘要）。
  4. chosen tip：使用者選或預設「**唯一**最新 timestamp 的 genuine leaf」；結尾 **append 一條新
     `last-prompt{leafUuid=chosen}`**，不沿用任一輸入尾端（防裸 rewind 落後的 stale tip 被寫死，B2）。
     chosen 必須是合併後存在的 genuine leaf；自動選只在「唯一最新」時成立，缺 ts / 並列最新 → needs-decision
     （A11：不以裸/並列 timestamp 自動拍板），交人帶 chosen_tip 重呼。
  5. 安全條件不成立（damaged / 零共同 uuid=collision / 多個非-system 根 / parent 環 / 無 leaf）
     → **退回挑選**（FALLBACK；上層改走 keep-local/keep-hub/keep-both）。

決定性 / commutative：兩枝分歧段以「分歧段 line-identity 序列」做 stable sort，故輸出**與 local/hub 標籤無關**；
每行用 `canonical.canon_dumps` 序列化，故同一 line-identity 在任何機器都產生相同 bytes（跨機 union 收斂）。
keep-both = 複製/落地時改檔名（檔名即身分，B6），不重寫內文 sessionId——由上層 atomicio 負責。

純標準庫、無 IO。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .canonical import Line, canon_dumps
from .lineset import (
    VOLATILE_META_TYPES,
    SessionShape,
    genuine_leaves_of,
    has_parent_cycle,
    meta_node_uuids,
)


class MergeOutcome(str, Enum):
    MERGED = "merged"                  # union 成功，objs 為合併後 JSONL 物件序列
    FALLBACK = "fallback"              # 安全條件不成立 → 退回挑選（keep-local/keep-hub/keep-both）
    NEEDS_DECISION = "needs-decision"  # chosen tip 非合併後 genuine leaf → 交人


@dataclass
class LeafCandidate:
    """合併後可當 tip 的 genuine leaf（供互動呈現：時間/身分）。"""
    uuid: str
    ts: str | None


@dataclass
class MergeResult:
    outcome: MergeOutcome
    reason: str
    objs: list[dict] | None = None          # MERGED 才有；含結尾新 last-prompt
    chosen_tip: str | None = None
    leaves: list[LeafCandidate] = field(default_factory=list)  # 可當 tip 的 genuine leaf（NEEDS_DECISION 時供互動挑選）


def _is_content_line(ln: Line) -> bool:
    """納入 union 的內容行 = **非揮發 meta 的行**（uuid 行、summary 等內容 no-uuid 行）。

    揮發 meta（last-prompt/mode/title…）不進 union，結尾另 append 一條新 last-prompt。
    **即使它畸形地帶了 uuid 也不進**（codex g3）：舊述詞 `bool(ln.uuid) or …` 會把帶 uuid 的 meta 行
    當內容搬進 union，並讓它成為 leaf 候選 → 自動選中它當 tip → 合併檔的游標指向一條 metadata。
    與 `lineset._counts_for_compare`（比較用；uuid 行一律計入）**刻意不同**：比較端計入 → 兩側若差這種
    畸形行仍會顯示為 fork/superset（交人、不靜默），但 union **產出**端不搬運簿記行。"""
    return ln.type not in VOLATILE_META_TYPES


def _idkey(ln: Line) -> tuple[str, str]:
    """可比較的 line-identity 鍵（避免 None 與 str 比較）。uuid 行=(uuid,hash)；no-uuid=("",hash)。"""
    return (ln.uuid or "", ln.canon_hash or "")


def _common_prefix_len(a: list[Line], b: list[Line]) -> int:
    k = 0
    while k < len(a) and k < len(b) and a[k].identity == b[k].identity:
        k += 1
    return k


_has_cycle = has_parent_cycle   # 單一真相源移至 lineset（classify 也要用）；此名保留供本模組既有呼叫端


def merge_sessions(
    local: SessionShape, hub: SessionShape, *, chosen_tip: str | None = None
) -> MergeResult:
    """union 兩個 SessionShape。回 MergeResult；不寫檔。"""
    # ── (5) 安全前置：damaged / 零共同 uuid → 退回挑選 ──────────────────────────
    if local.is_damaged or hub.is_damaged:
        return MergeResult(MergeOutcome.FALLBACK, "至少一側 damaged，不可 union（退回挑選）")
    if not local.uuids or not hub.uuids:
        return MergeResult(MergeOutcome.FALLBACK, "至少一側無對話 uuid，無法 union（退回挑選）")
    if not (local.uuids & hub.uuids):
        return MergeResult(MergeOutcome.FALLBACK, "零共同 uuid（無共同祖先 / collision），不可 union（退回挑選）")

    # 跨檔同 uuid 異 hash（歷史行被改寫）→ 不可 union。**必須在濾行之前**檢查（codex g7）：union 不搬揮發
    # meta 行，若某 uuid 在一側是 meta 行、另一側是真對話行，衝突就永遠到不了下方 `_emit` 的偵測 →
    # 防線消失。此處鏡射 `classify` 的同一判準（`uuid_hashes` 涵蓋所有 uuid 行、含 meta 行），
    # 使 `merge_sessions` 單獨呼叫時也守得住（正常路徑 classify 已先判 DAMAGED，此為第二層）。
    for u in local.uuids & hub.uuids:
        lh, hh = local.uuid_hashes.get(u, set()), hub.uuid_hashes.get(u, set())
        if lh and hh and lh.isdisjoint(hh):
            return MergeResult(
                MergeOutcome.FALLBACK,
                f"跨檔同 uuid 異 hash（{u[:8]} 歷史行被改寫），不可 union（退回挑選）",
            )

    a = [ln for ln in local.lines if _is_content_line(ln)]
    b = [ln for ln in hub.lines if _is_content_line(ln)]
    k = _common_prefix_len(a, b)

    # 兩枝分歧段以「分歧段 line-identity 序列」做 stable sort → 輸出與 local/hub 標籤無關（commutative）。
    seg_a, seg_b = a[k:], b[k:]
    first, first_seg, second_seg = (
        (a, seg_a, seg_b)
        if [_idkey(x) for x in seg_a] <= [_idkey(x) for x in seg_b]
        else (b, seg_b, seg_a)
    )

    # ── (1)(2)(3) 共同前綴一次 + 兩分歧段保序；uuid 去重、no-uuid 不去重 ──────────
    out: list[Line] = []
    emitted_uuid_hash: dict[str, str | None] = {}  # uuid -> 已輸出的 hash（偵測衝突）

    def _emit(ln: Line) -> bool:
        if ln.uuid:
            prev = emitted_uuid_hash.get(ln.uuid)
            if prev is not None:
                # 同 uuid 重複：同 hash → 安全去重（略過）；異 hash → 歷史行被改寫（damaged，本不該到這）
                return prev == ln.canon_hash
            emitted_uuid_hash[ln.uuid] = ln.canon_hash
        out.append(ln)
        return True

    for ln in first[:k]:           # 共同前綴（取 canonically-first 那側，bytes 一致）
        _emit(ln)
    for ln in first_seg:           # 先輸出的分歧段
        if not _emit(ln):
            return MergeResult(MergeOutcome.FALLBACK, f"同 uuid 異 hash（{ln.uuid[:8]} 歷史行被改寫），不可 union")
    for ln in second_seg:          # 後輸出的分歧段
        if not _emit(ln):
            return MergeResult(MergeOutcome.FALLBACK, f"同 uuid 異 hash（{ln.uuid[:8]} 歷史行被改寫），不可 union")

    # ── (5) 合併後結構檢查：斷鏈 / 多非-system 根 / parent 環 → 退回挑選 ─────────
    merged_uuids = {ln.uuid for ln in out if ln.uuid}
    parent_map = {ln.uuid: ln.parent for ln in out if ln.uuid}
    if _has_cycle(parent_map):
        return MergeResult(MergeOutcome.FALLBACK, "合併後 parent 鏈成環（不可解），退回挑選")

    # 被我們丟棄的揮發 meta 行若正是某內容行的父親 → union 會產出 parent 指向不存在 uuid 的**斷鏈**檔。
    # 「多非-system 根」只是這件事的**間接**訊號：若被丟的正好是根的父親，孤兒自己遞補成唯一的根 → 根數
    # 仍是 1 → 檢查漏接（codex g4 實證）。改為直接檢查「父親是否被我們丟掉」，精準 fail-closed。
    # 被丟棄的 uuid ＝ 帶 uuid 的揮發 meta 行（`meta_node_uuids`＝與 classify 共用的單一真相源；
    # 勿在此重新定義一次「什麼行不算對話節點」→ 又一次述詞漂移）。
    dropped_uuids = meta_node_uuids([*local.lines, *hub.lines])
    orphaned = [ln for ln in out if ln.parent and ln.parent in dropped_uuids]
    if orphaned:
        return MergeResult(
            MergeOutcome.FALLBACK,
            f"內容行的父親是被丟棄的揮發 meta 行（{orphaned[0].parent[:8]}）→ union 會斷鏈，退回挑選",
        )
    nonsystem_roots = [
        ln for ln in out
        if ln.uuid and (ln.parent is None or ln.parent not in merged_uuids) and ln.type != "system"
    ]
    if len(nonsystem_roots) > 1:
        return MergeResult(
            MergeOutcome.FALLBACK,
            "合併後出現多個非-system 對話根（疑似併入不相關對話），退回挑選",
        )

    # genuine leaves：用 lineset 的**單一真相源**述詞（勿再複製一份 → 會與 classify 的 ff 判定漂移，
    # codex g3 實證：舊的本地副本沒排除揮發 meta → union 挑中 meta 行當 tip）。
    leaf_lines = genuine_leaves_of(out)
    if not leaf_lines:
        return MergeResult(MergeOutcome.FALLBACK, "合併後無 genuine leaf，退回挑選")
    leaves = [LeafCandidate(ln.uuid, ln.ts) for ln in leaf_lines]
    leaf_uuids = {ln.uuid for ln in leaf_lines}

    # ── (4) chosen tip：使用者選或預設最新；append 新 last-prompt ─────────────────
    # 自動只在「唯一最新」時拍板；缺 ts / 並列最新 → needs-decision（A11：不以裸/並列 timestamp 自動選；
    # 防裸 rewind 落後或無法比較時被寫死錯 tip，B2）。上層互動再帶 chosen_tip 重呼。
    if chosen_tip is not None:
        if chosen_tip not in leaf_uuids:
            return MergeResult(
                MergeOutcome.NEEDS_DECISION,
                f"指定 tip {chosen_tip[:8]} 非合併後 genuine leaf（交人決策）",
                leaves=leaves,
            )
        chosen = chosen_tip
    else:
        if any(ln.ts is None for ln in leaf_lines):
            return MergeResult(
                MergeOutcome.NEEDS_DECISION,
                "部分 genuine leaf 缺 timestamp，無法自動選 tip（交人挑選）",
                leaves=leaves,
            )
        newest_ts = max(ln.ts for ln in leaf_lines)
        newest = [ln for ln in leaf_lines if ln.ts == newest_ts]
        if len(newest) != 1:
            return MergeResult(
                MergeOutcome.NEEDS_DECISION,
                "多個 genuine leaf 並列最新 timestamp，無法自動選 tip（交人挑選）",
                leaves=leaves,
            )
        chosen = newest[0].uuid

    objs = [ln.obj for ln in out if ln.obj is not None]
    sid = None
    for ln in leaf_lines:
        if ln.uuid == chosen and isinstance(ln.obj, dict):
            sid = ln.obj.get("sessionId")
            break
    tip_obj: dict = {"type": "last-prompt", "leafUuid": chosen}
    if sid is not None:
        tip_obj["sessionId"] = sid
    objs.append(tip_obj)

    return MergeResult(
        MergeOutcome.MERGED,
        "union 成功（共同前綴去重一次 + 兩分歧段保序 + 新 last-prompt 標 tip）",
        objs=objs,
        chosen_tip=chosen,
        leaves=leaves,
    )


def render_jsonl(objs: list[dict]) -> bytes:
    """把 union 後物件序列序列化成 canonical JSONL bytes（每行 canon_dumps + LF）。

    canonical 序列化 → 同一 line-identity 跨機 bytes 一致；落地用 atomicio.atomic_create_bytes
    寫到 keep_both_path（不覆蓋既有 local JSONL，C3）。"""
    return ("".join(canon_dumps(o) + "\n" for o in objs)).encode("utf-8")
