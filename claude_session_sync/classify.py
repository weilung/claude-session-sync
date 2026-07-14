"""§4.1 分類：安全閘 → identity-collision → identical/ff/superset-branch/fork。

依據 DESIGN §4.1 + 附錄 B + PLAN v0.4 §2.3，並納入 codex round 4（程式碼審查）修正：
  - **fast-forward 必須是「單一新 genuine tip 的純延伸」**：bigger 恰有一個 genuine leaf、
    它由 smaller 的 tip 延伸而來、active_tip 指向它、且確有新增 uuid 行。多 genuine leaf /
    只差內容 no-uuid / compact 子樹 → superset-branch（不可自動 ff）。
  - **active_tip 必須解析到「存在的 genuine leaf」**；指向 missing/fan-out/sidechain → 不可 ff。
    唯一例外＝**smaller 側的「落後指標」**（見 `_lagging_tip`）：smaller 只有一個 genuine leaf、
    指標存在於檔內且是該葉子的祖先 → 取該葉子當 tip（bigger 側不放寬）。
  - **跨檔同 uuid 不同 hash → DAMAGED**（歷史行被改寫/損壞，不可當普通 fork 進合併）。
  - 多個非-system 對話根（任一側）→ needs-decision（結構異常/已被合併過）。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .lineset import (
    PRESERVE_META_TYPES,
    VOLATILE_META_TYPES,
    SessionShape,
    has_meta_dag_link,
    has_parent_cycle,
    is_ancestor,
    is_conversation_line,
)


class Klass(str, Enum):
    IDENTICAL = "identical"
    FAST_FORWARD = "fast-forward"
    SUPERSET_BRANCH = "superset-branch"
    FORK = "fork"
    DAMAGED = "damaged"
    IDENTITY_COLLISION = "identity-collision"
    NEEDS_DECISION = "needs-decision"


@dataclass
class Classification:
    klass: Klass
    direction: str | None  # 'hub->local' | 'local->hub' | None
    reason: str
    metadata_differs: bool = False  # IDENTICAL 但揮發 meta（title 等）不同 → 由上層決定是否提示


def _genuine_tip(shape: SessionShape) -> str | None:
    """回唯一可信賴的 genuine tip uuid；無法確定（指標無效/多葉無指標）回 None。"""
    leaf_uuids = {leaf.uuid for leaf in shape.genuine_leaves}
    if shape.active_tip is not None:
        # active_tip 必須是「存在的 genuine leaf」，否則視為無效（不可 ff）
        return shape.active_tip if shape.active_tip in leaf_uuids else None
    if len(leaf_uuids) == 1:
        return next(iter(leaf_uuids))
    return None


def _lagging_tip(shape: SessionShape) -> str | None:
    """**smaller 側專用**的 tip：`_genuine_tip` 之外，容許「落後但可證明無歧義」的 active_tip。

    情境（2026-07-14 實機 a9f7f783，非罕見）：hub 快照取自「助理已回覆、使用者尚未送出下一則」的
    時刻 → 檔內最後一條 `last-prompt.leafUuid` 指向那則 user 訊息，而它在**同一檔內**已有子節點 →
    非葉 → `_genuine_tip` 判無效 → 明明是純延伸的 ff 被誤擋成 superset-branch（每次 sync 重報、
    逼人工介入）。這是「session 結束→備份→resume 續寫→再同步」的常態，不是壞檔。

    **關鍵反例（codex g1 fresh gate）**：光看「指標指向葉子的祖先」**分不出**兩件事——
      (a) 過期：送出 u3 → 檔寫 last-prompt(u3) → 助理回覆 u4 追加在後 → 指標自然落後；
      (b) **刻意 rewind**：u4 已存在，使用者主動把游標退回 u3（放棄 u4 那一枝，準備改由 u3 重新分岔）。
    兩者的圖形完全相同。若把 (b) 當 (a) 放行 → ff local->hub **覆蓋 hub**，hub 那個刻意的游標被改成
    local 的 tip ＝ 靜默丟掉使用者的 rewind 意圖（classify 本來就對「內容同、active_tip 異」判
    needs-decision，可見游標是有意義的使用者狀態、不是可棄的簿記）。
    分辨依據＝**append-only 行序**：(a) 的 last-prompt 寫在葉子**出現之前**（寫下當刻葉子還不存在，
    構造上不可能在表達「放棄該葉子」）；(b) 的 last-prompt 寫在葉子**之後**。故有條件 4。

    僅在**可證明只是落後**時回退（四條全要，任一不成立 → None＝維持不可 ff）：
      1. 恰一個 genuine leaf —— smaller 的「哪一枝是活的」不可能猜錯（無分枝歧義）。
      2. active_tip 在本檔內、且它的每一條出現都是**對話真節點**（`is_conversation_line`：非揮發 meta）
         —— ① 排除 dangling（`is_ancestor` 會沿 parent_map 走到「根的檔外父親」，光靠它擋不掉）；
         ② 排除「游標指向簿記行」——既已宣告 meta 行不是對話節點，就不能一手立規則、一手讓它當游標
         過關（codex g8）。**不排除 sidechain/fan-out**：實機真實游標就指向一條帶 toolUseResult 的
         工具結果行（fan-out），排除它會把本塊要修的案例反過來擋死。
      3. active_tip 是該唯一葉子的祖先 —— 排除游標指向離枝節點（側鏈／fan-out 兄弟／另一條真枝）。
      4. 葉子首次出現之後（含該行本身）**不得有任何 `last-prompt` 行** —— 排除刻意 rewind（見上）。
         這一條刻意寫成「掃整段」而非「比較 active_tip 的位置」，一次涵蓋三種形：
           · 游標行寫在葉子之後 ＝ 刻意 rewind（codex g1）；
           · 游標行**就是**葉子行（畸形：帶 uuid 的 last-prompt 被當成葉）→ 相等不證明任何事（codex g2）；
           · 葉子之後的 last-prompt **缺 `leafUuid`** → 對 `active_tip` 隱形，但它證明「葉子存在後游標
             仍被碰過」→ 無法再證明只是落後（codex g6）。
         葉子若重複出現取**最早**一次（保守：只要游標可能寫在葉子存在之後，就不放行）。
         實測：使用者 402 個真實檔、11,161 條 last-prompt **無一** 缺 leafUuid，此掃描對真實檔零成本。

    **只放寬 smaller**：bigger（＝將被採納、可能覆蓋對側的那一枝）仍走 `_genuine_tip` 嚴格判定。
    """
    tip = _genuine_tip(shape)
    if tip is not None:
        return tip
    if shape.active_tip is None or len(shape.genuine_leaves) != 1:   # (1) 無指標 / 分枝歧義
        return None
    # (2) 游標必須落在本檔的**對話真節點**上：排除 dangling（檔內無此 uuid）與簿記行（帶 uuid 的揮發
    # meta）。**不排除 sidechain/fan-out**——它們是真節點（實機：真實游標就指向一條帶 toolUseResult 的
    # 工具結果行）；游標若指向離枝節點，由下面的祖先守衛(3)負責擋。
    # 用 `all` 而非 `any`：同 uuid 若重複出現且其中一條是簿記行 → 可疑 → 保守擋下。
    tip_lines = [ln for ln in shape.lines if ln.uuid == shape.active_tip]
    if not tip_lines or not all(is_conversation_line(ln) for ln in tip_lines):
        return None
    leaf = shape.genuine_leaves[0].uuid
    if not is_ancestor(shape, shape.active_tip, leaf):   # (3) 指到別的分枝 → 不可信
        return None
    # (4) 葉子首現之後（含該行）不得有任何 last-prompt 行 → 證明「寫下游標時葉子尚不存在」＝指標只是落後。
    # 掃整段（而非只比對 active_tip 的位置）才能一併蓋住「缺 leafUuid 的隱形游標行」與「游標行本身被當成
    # 葉子」的畸形；反之只比對位置會留下這兩個缺口。
    leaf_first = next((i for i, ln in enumerate(shape.lines) if ln.uuid == leaf), None)
    if leaf_first is None or any(ln.type == "last-prompt" for ln in shape.lines[leaf_first:]):
        return None
    return leaf


def _nonsystem_roots(shape: SessionShape) -> list:
    return [r for r in shape.roots if r.type != "system"]


def _classify_superset(bigger: SessionShape, smaller: SessionShape, direction: str) -> Classification:
    """bigger ⊋ smaller：判 fast-forward vs superset-branch vs needs-decision。"""
    # parent 鏈成環（含自我指向）＝結構壞掉：ff 的全部推理（祖先鏈、唯一枝、游標落後）都建立在 DAG 上，
    # 環一出現即不成立 → fail-closed 交人（codex g4；`session_merge` 早已拒環，此處補上同一姿態）。
    if has_parent_cycle(bigger.parent_map) or has_parent_cycle(smaller.parent_map):
        return Classification(Klass.NEEDS_DECISION, None, "parent 鏈含環/自我指向（結構異常）→ 不可自動 ff")

    # DAG 靠**簿記行**串接（對話行以帶 uuid 的揮發 meta 行為 parent）＝結構異常：ff 的「純延伸」證明
    # （`is_ancestor`）會**穿過**一個我們自己宣告為「非對話節點」的連結（union 甚至會丟掉它 → 孤兒 →
    # 退回人工）。同一條規則不能在 union 執行、在 ff 放行 → fail-closed 交人（codex g9）。
    # 實測：402 個真實檔、54,187 條揮發 meta 行**無一帶 uuid** → 此拒絕對真實檔零成本。
    if has_meta_dag_link(bigger.lines) or has_meta_dag_link(smaller.lines):
        return Classification(
            Klass.NEEDS_DECISION, None, "對話行以簿記行（帶 uuid 的揮發 meta）為 parent（結構異常）→ 不可自動 ff",
        )

    # 新增的「非-system 根」= 疑似注入不相關對話 → 不可自動套用（H4 / codex r3）
    if any(r.type != "system" for r in bigger.roots if r.uuid not in smaller.uuids):
        return Classification(Klass.NEEDS_DECISION, None, "superset 含新增非-system 根（疑似注入不相關對話）")

    big_tip = _genuine_tip(bigger)
    if big_tip is None:
        return Classification(
            Klass.NEEDS_DECISION, None,
            "active-tip 無法解析到單一存在的 genuine leaf（指向 missing/fan-out/sidechain 或多葉無指標）",
        )

    extra_uuids = bigger.uuids - smaller.uuids
    # 「新增」必須含**真內容行**，不能只多出簿記行（codex g5）：`_counts_for_compare` 對有 uuid 的行一律
    # 計入比較集合，所以一條**帶 uuid 的揮發 meta 行**（schema 未禁止）就足以讓「對話完全相同、只有游標
    # 不同」的兩檔變成 superset → 繞過上游「內容同、active_tip 異 → needs-decision」的游標守衛 → 被當
    # ff 覆蓋對側、靜默改掉游標。**只認非揮發 meta 的新行**才算延伸（sidechain/fan-out 仍算真內容 →
    # 「只多了子代理側鏈、主鏈 tip 未前進」這種合法無損延伸不受影響，不製造新噪音）。
    extra_content = any(
        ln.uuid in extra_uuids and ln.type not in VOLATILE_META_TYPES for ln in bigger.lines
    )
    small_tip = _lagging_tip(smaller)   # smaller 容許落後指標（無歧義才回退）；bigger 不放寬
    # fast-forward 嚴格條件（codex r4）：有新增**內容**行、bigger 恰一個 genuine leaf、
    # 它就是 active tip、且由 smaller 的 tip 延伸而來。
    if (
        extra_content
        and len(bigger.genuine_leaves) == 1
        and small_tip is not None
        and is_ancestor(bigger, small_tip, big_tip)
    ):
        # ff local->hub 會**覆蓋** hub(smaller)；若 hub 有 local(bigger) 缺的標題行（custom/ai-title），
        # 覆蓋會靜默丟使用者設定 → 交人決策（codex r19；hub->local 走 keep-both 不覆蓋、不丟）。
        if direction == "local->hub":
            bigger_ids = set(bigger.order)
            dropped = [ln for ln in smaller.lines
                       if ln.type in PRESERVE_META_TYPES and ln.identity not in bigger_ids]
            if dropped:
                return Classification(
                    Klass.NEEDS_DECISION, None,
                    "ff 覆蓋 hub 會丟失 hub 端標題（custom-title/ai-title）→ 交人決策（不靜默丟使用者設定）",
                )
        return Classification(Klass.FAST_FORWARD, direction, "純延伸：唯一 genuine leaf 由 active tip 延伸")
    return Classification(
        Klass.SUPERSET_BRANCH, None,
        "整行集合包含但非純延伸（多 genuine leaf / 僅內容 no-uuid 差異 / compact 子樹），不可自動 ff",
    )


def classify(local: SessionShape, hub: SessionShape) -> Classification:
    # 1) 安全閘：檔級 damaged / 壞行 / 單檔內同 uuid 異 hash
    if local.is_damaged or hub.is_damaged:
        which = "+".join(w for w, s in (("local", local), ("hub", hub)) if s.is_damaged)
        return Classification(Klass.DAMAGED, None, f"damaged: {which}（壞檔/壞行/同檔同 uuid 異 hash）")

    # 2) 身分基礎：兩側都要有對話 uuid
    if not local.uuids or not hub.uuids:
        return Classification(Klass.NEEDS_DECISION, None, "至少一側無對話 uuid 行，無法建立同一性")

    common = local.uuids & hub.uuids
    if not common:
        return Classification(Klass.IDENTITY_COLLISION, None, "零共同 uuid（撞 sessionId / 錯夾 / 不同對話）")

    # 3) 跨檔同 uuid 不同 hash → DAMAGED（歷史行被改寫/損壞，不可當普通 fork）
    for u in common:
        lh, hh = local.uuid_hashes.get(u, set()), hub.uuid_hashes.get(u, set())
        if lh and hh and lh.isdisjoint(hh):
            return Classification(Klass.DAMAGED, None, f"跨檔同 uuid 不同 hash（{u[:8]} 歷史行被改寫/損壞）")

    # 4) 多個非-system 對話根（任一側）→ 結構異常（已被合併過 / 注入），交人
    if len(_nonsystem_roots(local)) > 1 or len(_nonsystem_roots(hub)) > 1:
        return Classification(Klass.NEEDS_DECISION, None, "出現多個非-system 對話根（結構異常），交人決策")

    # 5) 比較（排除揮發 meta 的多重集合）
    la, lb = local.compare_multiset, hub.compare_multiset
    if la == lb:
        if local.active_tip == hub.active_tip:
            meta = local.multiset != hub.multiset
            note = "行多重集合相同且 active-tip 相同" + ("（但揮發 meta 不同）" if meta else "")
            return Classification(Klass.IDENTICAL, None, note, metadata_differs=meta)
        return Classification(Klass.NEEDS_DECISION, None, "內容相同但 active-tip 不同（指標歧異）")

    hub_superset = la <= lb
    local_superset = lb <= la
    if hub_superset and not local_superset:
        return _classify_superset(hub, local, "hub->local")
    if local_superset and not hub_superset:
        return _classify_superset(local, hub, "local->hub")

    return Classification(Klass.FORK, None, "互有對方沒有的行、有共同祖先（fork）")
