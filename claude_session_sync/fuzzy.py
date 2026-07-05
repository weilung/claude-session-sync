"""fuzzy：memory「同事實、不同檔名」的**模糊近似候選偵測**（P2，最高風險——動「永不把兩則語意不同的
memory 判同」的 cardinal 不變量）。

定位（DESIGN §7「不同檔名、同一事實」+ HANDOFF P2）：
  - exact 層（`memory.plan_memory_pair` duty a/b）只認 frontmatter `name` **完全相同**的跨檔身分；兩台機器各自
    為同一件事取了**不同 slug**（如 `codex-run-stall-handling` vs `codex-stall-triage`）時，exact 層看不出關聯 →
    兩則都留、互不干涉（**安全、不丟資料**，但使用者不會被提醒「這兩則其實同一件事、可考慮合併」）。
  - 本模組補這個洞：以**純字面**（決定性、零第三方相依 → 跨機可重現；**不用 embedding/ML**）算 name slug 詞元
    + description 詞元的相似度，把疑似同一事實的**候選對**列出交人複核。

**極性鐵則（cardinal）**：本模組**永遠只建議、絕不裁定**。fuzzy 分數**不進** `classify`/`apply`/`sync`、**絕不**
  自動合併或改寫任何 memory——誤判（把兩則不同事實判「疑似同一」）在此**只多印一行提示**、零資料危害。真正的
  保留兩版／合併一律由使用者逐對放行後才發生（memory-merge 的 stage/interactive＝後續 Block B）。本模組本身
  **不做任何 I/O**（純函式；讀檔在 CLI，餵進 `FuzzyEntry`）。

**訊號選擇（evidence-based，2026-07-04 對使用者真實 memory 實測）**：name slug 詞元是**主**訊號（同一事實兩台常
  重用關鍵名詞〔`codex`、`stall`〕），description 詞元為**次**；**刻意不比 body**——實測「換句話說」的重複其 body
  字元 n-gram 幾乎不重疊（真重複 body 相似度≈0），反而不相干的兩則因共用領域詞彙 body 分數更高（訊號是反的）→
  比 body 只會增誤報。故只用 name+desc，權重 0.7/0.3；真重複與雜訊的天生間距窄（實測真重複≈0.29 vs 最高雜訊≈0.11）
  → 閾值保守 + **一律 advisory + 人工確認** 是數字逼出來的必要條件，非潔癖。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import scan

# name/desc 加權與預設閾值（evidence-based，見模組 docstring）。閾值可由 CLI `--fuzzy-threshold` 覆寫供校準。
WEIGHT_NAME = 0.7
WEIGHT_DESC = 0.3
DEFAULT_THRESHOLD = 0.25

# name slug 分詞：slug 合法字元為 [A-Za-z0-9_.-]（見 `memory._SLUG_RE`）→ 以 - _ . 斷詞。
_SLUG_SPLIT = re.compile(r"[-_.]+")
# description 分詞：拉丁/數字連續段 + 個別 CJK 字（description 常中英混）。CJK bigram 可提升召回，屬未來調校、
# 現以單字保守（union 較大 → Jaccard 偏低 → 偏少誤報，符合 advisory 安全方向）。
_LATIN_RUN = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[㐀-鿿]")


def _norm(s: str) -> str:
    """caseless + Unicode 正規化（復用 `scan._name_key`＝NFC∘casefold∘NFC，全 codebase 單一正規化真相源——與
    檔名別名／memory-merge 路徑包含判定同源，免各自實作漂移）。"""
    return scan._name_key(s)


def name_tokens(name: str | None) -> frozenset[str]:
    """name slug → 正規化詞元集。None/空 → 空集。"""
    if not name:
        return frozenset()
    return frozenset(t for t in _SLUG_SPLIT.split(_norm(name)) if t)


def desc_tokens(desc: str | None) -> frozenset[str]:
    """description → 正規化詞元集（拉丁/數字段 + 個別 CJK 字）。None/空 → 空集。"""
    if not desc:
        return frozenset()
    low = _norm(desc)
    return frozenset(_LATIN_RUN.findall(low)) | frozenset(_CJK.findall(low))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """|a∩b| / |a∪b|。任一側空 → 0.0（無可比詞元 → 不判相似，fail-open-to-not-similar＝安全方向）。"""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass(frozen=True)
class FuzzyEntry:
    """一個 memory 檔的 fuzzy 比對輸入（由 CLI 讀檔後建；本模組不做 I/O）。

    `name`＝frontmatter `name` slug（`memory.MemoryDoc.name`；非 fm_ok → None）；`description`＝frontmatter
    `description`（非 fm_ok/無/非字串 → None）。"""

    filename: str
    name: str | None
    description: str | None


@dataclass(frozen=True)
class FuzzyCandidate:
    """一對疑似同一事實的候選（advisory）。`a`/`b`＝檔名（決定性排序，`a` 較小）。"""

    project_key: str
    a: str
    b: str
    name_a: str | None
    name_b: str | None
    score: float
    name_sim: float
    desc_sim: float
    shared_name_tokens: tuple[str, ...]


def similarity(a: FuzzyEntry, b: FuzzyEntry) -> tuple[float, float, float]:
    """兩檔加權相似度。回 (score, name_sim, desc_sim)。純 name+desc（不碰 body，見模組 docstring）。"""
    name_sim = jaccard(name_tokens(a.name), name_tokens(b.name))
    desc_sim = jaccard(desc_tokens(a.description), desc_tokens(b.description))
    return (WEIGHT_NAME * name_sim + WEIGHT_DESC * desc_sim, name_sim, desc_sim)


def find_candidates(project_key: str, entries: list[FuzzyEntry], *,
                    threshold: float = DEFAULT_THRESHOLD) -> list[FuzzyCandidate]:
    """列出**不同檔名**且相似度 ≥ threshold 的疑似同一事實候選對。

    **排除已由 exact 層處理者**：兩檔 frontmatter `name` **完全相同**（且皆可判）＝ exact cross-file-identity，已是
    `memory-merge` 衝突 → 不重複列（fuzzy 專補 exact 認不出的「不同 name」洞）。其餘（含一/兩側 name 不可判）照算
    ——desc 仍可能命中。**同一檔的別名拼寫**（`_name_key` 相同：僅大小寫/NFC-NFD 不同）先去重（非「兩檔」；保排序後
    第一個，決定性）——否則第三檔會與別名兩者各配一次、重複列同一組。決定性：`a` ≤ `b`（raw 檔名）、排序 score 高→低
    同分依檔名 → 跨機/跨次結果逐位元組一致。"""
    uniq: list[FuzzyEntry] = []
    seen: set[str] = set()
    for e in sorted(entries, key=lambda e: scan._name_key(e.filename)):
        k = scan._name_key(e.filename)
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    out: list[FuzzyCandidate] = []
    for i in range(len(uniq)):
        for j in range(i + 1, len(uniq)):
            ea, eb = uniq[i], uniq[j]
            if ea.name and eb.name and ea.name == eb.name:
                continue  # exact cross-file-identity（exact 層已處理 → 不重複列）
            score, name_sim, desc_sim = similarity(ea, eb)
            if score < threshold:
                continue
            if eb.filename < ea.filename:   # 決定性：c.a ≤ c.b（raw 檔名，與顯示/最終排序鍵一致）
                ea, eb = eb, ea
            shared = tuple(sorted(name_tokens(ea.name) & name_tokens(eb.name)))
            out.append(FuzzyCandidate(
                project_key, ea.filename, eb.filename, ea.name, eb.name,
                round(score, 4), round(name_sim, 4), round(desc_sim, 4), shared))
    out.sort(key=lambda c: (-c.score, c.a, c.b))
    return out
