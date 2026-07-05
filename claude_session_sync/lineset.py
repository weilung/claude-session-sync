"""SessionShape：把一檔算成「身分集合 + multiset + 順序 + root-set + genuine leaf + active-tip」。

依據 DESIGN 附錄 A1/A7/A11 + 附錄 B（B2 active-tip=最後一條 last-prompt.leafUuid、
leaf 排除工具 fan-out 與 sidechain；B3 root-set 含 system 根、先濾 uuid 行再判根）。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .canonical import FileState, Line, LoadResult, load, load_bytes

# 揮發性的 session 簿記行（每個 prompt/狀態變動就變），**不算對話內容**：
# 比較同一性時要排除，否則每延伸一則就因 last-prompt 改變而被誤判成 fork。
# 內容性的 no-uuid 行（summary / isCompactSummary）不在此列，仍納入比較。
VOLATILE_META_TYPES = frozenset(
    {
        "last-prompt",
        "mode",
        "permission-mode",
        "ai-title",
        "custom-title",
        "agent-name",
        "file-history-snapshot",
    }
)

# 揮發 meta 中**使用者/AI 可見的標題**：比較同一性時雖排除（每次延伸不該誤判 fork），但「ff 覆蓋對側」
# 時不可**靜默丟棄**（codex r19）——覆蓋會丟失對側既有標題 → 須交人決策，不自動套用。
PRESERVE_META_TYPES = frozenset({"ai-title", "custom-title"})


def _counts_for_compare(line: Line) -> bool:
    """此行是否納入「比較用」多重集合：uuid 行一律算；no-uuid 行只算非揮發性 meta。"""
    return bool(line.uuid) or (line.type not in VOLATILE_META_TYPES)


@dataclass
class SessionShape:
    state: FileState
    lines: list[Line]                       # 全部 ok 行（依序）
    uuids: set[str]                         # 出現過的 uuid
    multiset: Counter                       # 全部行身分 -> 次數（含揮發 meta，供 debug/union）
    compare_multiset: Counter               # 比較用多重集合（排除揮發 meta，A1+H2）
    order: list[tuple]                      # 行身分依檔序
    parent_map: dict[str, str | None]       # uuid -> parentUuid
    roots: list[Line]                       # 對話根（uuid 行、parent 為 null 或檔內找不到）
    genuine_leaves: list[Line]             # 真 tip 候選（排除 fan-out / sidechain）
    active_tip: str | None                  # 最後一條 last-prompt.leafUuid
    same_uuid_diff: set[str]                # 同檔內同 uuid 不同 hash（舊行被改寫 → damaged 訊號）
    uuid_hashes: dict[str, set[str]]        # uuid -> 該檔出現過的 canon_hash 集（供跨檔改寫偵測）
    has_bad: bool

    @property
    def is_damaged(self) -> bool:
        """檔級狀態壞、有壞 JSON 行、或同 uuid 不同 hash → damaged。"""
        return self.state.is_damaged or self.has_bad or bool(self.same_uuid_diff)

    @property
    def is_empty(self) -> bool:
        return len(self.lines) == 0

    @property
    def newest_genuine_leaf(self) -> Line | None:
        """genuine leaf 中 timestamp 最晚者（給 active-tip 交叉驗用）。"""
        leaves = [ln for ln in self.genuine_leaves if ln.ts]
        if not leaves:
            return self.genuine_leaves[-1] if self.genuine_leaves else None
        return max(leaves, key=lambda ln: ln.ts)


def is_ancestor(shape: SessionShape, anc: str | None, desc: str | None) -> bool:
    """anc 是否為 desc 的祖先（含相等）。沿 parent_map 從 desc 往上走。"""
    if anc is None or desc is None:
        return False
    seen: set[str] = set()
    cur: str | None = desc
    while cur is not None and cur not in seen:
        if cur == anc:
            return True
        seen.add(cur)
        cur = shape.parent_map.get(cur)
    return False


def analyze_result(res: LoadResult) -> SessionShape:
    ok = res.ok_lines
    uuids = {ln.uuid for ln in ok if ln.uuid}

    multiset: Counter = Counter(ln.identity for ln in ok)
    compare_multiset: Counter = Counter(ln.identity for ln in ok if _counts_for_compare(ln))
    order = [ln.identity for ln in ok]

    parent_map: dict[str, str | None] = {}
    by_uuid_hashes: dict[str, set[str]] = {}
    for ln in ok:
        if ln.uuid:
            parent_map[ln.uuid] = ln.parent
            if ln.canon_hash is not None:
                by_uuid_hashes.setdefault(ln.uuid, set()).add(ln.canon_hash)
    same_uuid_diff = {u for u, hs in by_uuid_hashes.items() if len(hs) > 1}

    # 對話根 = uuid 行且 parent 為 null 或 parent 不在檔內（含 compact 產生的 system 根）。
    roots = [ln for ln in ok if ln.uuid and (ln.parent is None or ln.parent not in uuids)]

    # genuine leaf = uuid 未被任何行當 parent，且非 sidechain、非工具 fan-out。
    used_as_parent = {ln.parent for ln in ok if ln.parent}
    genuine_leaves = [
        ln
        for ln in ok
        if ln.uuid and ln.uuid not in used_as_parent and not ln.is_sidechain and not ln.is_tool_fanout
    ]

    # active tip = 最後一條 last-prompt 行的 leafUuid。
    active_tip: str | None = None
    for ln in ok:
        if ln.type == "last-prompt" and isinstance(ln.obj, dict) and ln.obj.get("leafUuid"):
            active_tip = ln.obj["leafUuid"]

    return SessionShape(
        state=res.state,
        lines=ok,
        uuids=uuids,
        multiset=multiset,
        compare_multiset=compare_multiset,
        order=order,
        parent_map=parent_map,
        roots=roots,
        genuine_leaves=genuine_leaves,
        active_tip=active_tip,
        same_uuid_diff=same_uuid_diff,
        uuid_hashes=by_uuid_hashes,
        has_bad=res.has_bad,
    )


def analyze(path: str) -> SessionShape:
    return analyze_result(load(path))


def analyze_bytes(data: bytes) -> SessionShape:
    """從 bytes 算 SessionShape（不碰檔案）。供 transfer 把寫出的 bytes 綁定到分類決策。"""
    return analyze_result(load_bytes(data))
