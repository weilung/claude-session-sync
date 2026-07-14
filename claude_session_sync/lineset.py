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


def is_conversation_line(line: Line) -> bool:
    """**「什麼行算對話 DAG 的真節點」的單一真相源** = uuid 行、且**非揮發 meta**。

    揮發 meta（last-prompt/mode/title…）是簿記、不是對話節點；真實 CC 檔不會在其上放 uuid，但 schema
    未禁止（`canonical` 對任何 type 都收 uuid）→ 曾可被當成 genuine leaf（codex g2/g3），也曾可被當成
    smaller 側的「游標」而讓 ff 過關（codex g8：只檢查 `active_tip in uuids` ＝ 一手立規則、一手繞過它）。

    **sidechain / 工具 fan-out 行不在排除之列**——它們是**真實的對話節點**，只是不能當「葉子候選」。
    實證（2026-07-14 使用者真實 hub 檔 a9f7f783）：其 `last-prompt.leafUuid` 指向的正是一條 `type=user`
    **且帶 `toolUseResult`** 的工具結果行（`is_tool_fanout` 為真）→ 若把 fan-out 也排除在「可當游標」之外，
    會把本塊要修的真實案例反過來擋死。游標指向側鏈/離枝節點的風險由 `_lagging_tip` 的祖先守衛負責。
    """
    return bool(line.uuid) and line.type not in VOLATILE_META_TYPES


def meta_node_uuids(lines: list[Line]) -> set:
    """帶 uuid 的**揮發 meta 行**的 uuid 集 —— 即「被宣告為非對話節點」的那些（union 不搬運它們）。
    真實 CC 檔為零（實測 402 檔、54,187 條 meta 行無一帶 uuid）；schema 未禁止，故仍須處理。"""
    return {ln.uuid for ln in lines if ln.uuid and ln.type in VOLATILE_META_TYPES}


def has_meta_dag_link(lines: list[Line]) -> bool:
    """DAG 是否靠**簿記行**串接：有任何行以「帶 uuid 的揮發 meta 行」為 parent。

    這是結構異常：我們一邊宣告簿記行不是對話節點（不可當 leaf、不可當游標、union 會丟掉它），一邊卻
    讓祖先鏈**穿過**它來證明「純延伸」（codex g9）——同一條規則兩處不一致。union 已對此 fail-closed
    （丟掉簿記行會造成孤兒 → 退回人工），ff 也必須拒絕，否則「延伸」的證明建立在一個我們自己認定
    不存在的連結上。"""
    metas = meta_node_uuids(lines)
    return bool(metas) and any(ln.parent in metas for ln in lines if ln.parent)


def is_genuine_leaf(line: Line, used_as_parent: set) -> bool:
    """可當對話 **tip** 的行 = 對話真節點、未被任何行當 parent、且非 sidechain、非工具 fan-out。

    兩處各自複製此述詞就會漂移：classify 用這裡的定義決定 ff、session_merge 用自己的定義選 union tip
    → 同一檔兩種「葉子」（codex g3 實證：union 會挑中 meta 行當 tip、寫出指向 metadata 的游標）。
    """
    return (
        is_conversation_line(line)
        and line.uuid not in used_as_parent
        and not line.is_sidechain
        and not line.is_tool_fanout
    )


def genuine_leaves_of(lines: list[Line]) -> list[Line]:
    """依 `is_genuine_leaf` 取一組行的 genuine leaves（used_as_parent 就地算，供 union 後的行序列使用）。"""
    used_as_parent = {ln.parent for ln in lines if ln.parent}
    return [ln for ln in lines if is_genuine_leaf(ln, used_as_parent)]


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


def has_parent_cycle(parent_map: dict[str, str | None]) -> bool:
    """parent 鏈是否含環（沿 parentUuid 往上走重複造訪即環）。**單一真相源**：`session_merge` 早已拒環
    （不可 union），但 `classify` 原本不查 → 自我指向/成環的畸形檔仍可能走上 auto-ff（codex g4）。
    兩處共用同一述詞，姿態才一致：結構壞掉的檔一律交人，不自動落地。

    **O(n)**（記憶化）：每個節點只走一次——已判定「無環」的節點不重走（`safe`），本次路徑上的節點記在
    `on_path`，再度踏上即成環。原始寫法對每個起點各自重走整條鏈＝O(n²)：實測使用者最大的真實對話
    （2,026 uuid）要 447ms，而 classify 每個 superset 配對都會呼叫兩次 → 必須降階。"""
    safe: set[str] = set()
    for start in parent_map:
        if start in safe:
            continue
        path: list[str] = []
        on_path: set[str] = set()
        cur: str | None = start
        while cur is not None and cur in parent_map and cur not in safe:
            if cur in on_path:
                return True
            on_path.add(cur)
            path.append(cur)
            cur = parent_map[cur]
        safe.update(path)      # 這條鏈走到底沒成環 → 鏈上全部標記安全，之後不再重走
    return False


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

    genuine_leaves = genuine_leaves_of(ok)   # 單一真相源（見 `is_genuine_leaf`）

    # active tip = 最後一條 last-prompt 行的 leafUuid。
    # （classify 的「游標 vs 葉子孰先寫入」判定直接掃 `lines` 的 last-prompt 行，不另存索引——存索引只認得
    #   帶 leafUuid 的行，會漏掉「缺 leafUuid 的游標行」這個隱形缺口。）
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
