"""§4.1 分類：安全閘 → identity-collision → identical/ff/superset-branch/fork。

依據 DESIGN §4.1 + 附錄 B + PLAN v0.4 §2.3，並納入 codex round 4（程式碼審查）修正：
  - **fast-forward 必須是「單一新 genuine tip 的純延伸」**：bigger 恰有一個 genuine leaf、
    它由 smaller 的 tip 延伸而來、active_tip 指向它、且確有新增 uuid 行。多 genuine leaf /
    只差內容 no-uuid / compact 子樹 → superset-branch（不可自動 ff）。
  - **active_tip 必須解析到「存在的 genuine leaf」**；指向 missing/fan-out/sidechain → 不可 ff。
  - **跨檔同 uuid 不同 hash → DAMAGED**（歷史行被改寫/損壞，不可當普通 fork 進合併）。
  - 多個非-system 對話根（任一側）→ needs-decision（結構異常/已被合併過）。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .lineset import PRESERVE_META_TYPES, SessionShape, is_ancestor


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


def _nonsystem_roots(shape: SessionShape) -> list:
    return [r for r in shape.roots if r.type != "system"]


def _classify_superset(bigger: SessionShape, smaller: SessionShape, direction: str) -> Classification:
    """bigger ⊋ smaller：判 fast-forward vs superset-branch vs needs-decision。"""
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
    small_tip = _genuine_tip(smaller)
    # fast-forward 嚴格條件（codex r4）：有新增 uuid 行、bigger 恰一個 genuine leaf、
    # 它就是 active tip、且由 smaller 的 tip 延伸而來。
    if (
        extra_uuids
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
