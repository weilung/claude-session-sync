"""存在性異常偵測（§8.5）：任何 apply 前阻斷，擋「掛錯碟/空掛載/hub 突變/已知 session 大量消失」。

依據 DESIGN §8.5 + PLAN v0.8 §2.9 + codex r6 必補②（已知 session 大量消失偵測）：
靠**存在性**而非百分比門檻去抓「整體不對」——掛載點不在 / hub 夾名指紋變 → halt；
**已知 session 大量從 hub 消失**（夾名沒變但內容被清/部分同步/誤掛）也 halt。

約定（全工具共用）：state.known_sessions 的 key = **hub 專案夾名**（hub_root 底下的編碼夾名），
bindings 的 value 亦同。anomaly 才能由 project_key 直接定位 hub_root/<project_key>。

P1a 起：mount + hub-fingerprint；P1b 加 known-session 大量消失（coarse 安全網，預設高門檻避免誤殺）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .pathsafe import safe_project_dir   # leaf；逃逸 pk 夾不讀界外（e2e gate2 #4，無循環）
from .state import State

# 大量消失門檻（coarse 安全網；寧可偶爾要使用者確認，也不靜默吃掉「掛錯/被清空」）。
DISAPPEAR_MIN_KNOWN = 8       # **全體**已知 session 總數低於此值不觸發全域判定（樣本太小）
DISAPPEAR_FRAC = 0.6         # 消失比例達此值 → halt（全域或單專案）
PROJECT_VANISH_MIN = 4       # **單專案**已知數達此值即納入個別判定（避免大專案稀釋掉小專案被整夾清空）


@dataclass
class Anomaly:
    code: str
    message: str
    severity: str  # "halt" | "warn"


def hub_fingerprint(hub_root: str | Path) -> str:
    """hub 的存在性指紋：所有專案夾名（排序）的雜湊。掛錯碟 → 夾名集合大不同 → 指紋變。"""
    root = Path(hub_root)
    names = sorted(d.name for d in root.iterdir() if d.is_dir()) if root.exists() else []
    return hashlib.sha256(json.dumps(names, ensure_ascii=False).encode("utf-8")).hexdigest()


def known_session_set_hash(state: State | None) -> str:
    """所有 project_key→已知 sessionId 集合的穩定雜湊（供 anomaly 快照比對 project 集合突變）。"""
    if state is None:
        return hashlib.sha256(b"none").hexdigest()
    payload = {k: sorted(v) for k, v in sorted(state.known_sessions.items())}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _hub_session_stems(hub_dir: Path) -> set[str]:
    if not hub_dir.exists():
        return set()
    return {p.stem for p in hub_dir.glob("*.jsonl") if p.is_file() and not p.name.startswith(".")}


def detect_disappearance(
    state: State | None, hub_root: str | Path, *,
    min_known: int = DISAPPEAR_MIN_KNOWN, frac: float = DISAPPEAR_FRAC,
    project_min: int = PROJECT_VANISH_MIN,
) -> Anomaly | None:
    """已知 session 是否從 hub **大量**消失（夾名指紋沒抓到的「內容被清/部分同步/誤掛」）。

    只看 hub 端（local 消失靠 tombstone 流程處理，最致命的掛錯碟由此處 + fingerprint 涵蓋）。觸發 =
      - **單專案**：known≥project_min 且該專案消失比例 ≥ frac（避免大專案稀釋掉小專案整夾被清，codex r8）；
      - **或全域**：全體 known≥min_known 且全體消失比例 ≥ frac。
    比例用精確分數比較（非 int() floor，否則 50% 會誤觸 60% 門檻，codex r8）。回 None 表無異常。
    """
    if state is None or not state.known_sessions:
        return None
    root = Path(hub_root)
    total_known = 0
    total_missing = 0
    flagged: list[str] = []
    for pk, known in state.known_sessions.items():
        if not known:
            continue
        if not safe_project_dir(root, root / pk):
            continue  # 逃逸 pk 夾（symlink/junction 出 hub_root）→ 不 glob 界外（e2e gate2 #4）；其異常由 build_plan skipped-unsafe surface
        present = _hub_session_stems(root / pk)
        missing = len(set(known) - present)
        total_known += len(known)
        total_missing += missing
        if len(known) >= project_min and missing / len(known) >= frac:
            flagged.append(pk)
    global_hit = total_known >= min_known and total_known > 0 and total_missing / total_known >= frac
    if not flagged and not global_hit:
        return None
    parts: list[str] = []
    if flagged:
        parts.append(f"{len(flagged)} 個專案各自大量消失")
    if global_hit:
        parts.append(f"全體 {total_missing}/{total_known} 不在 hub")
    return Anomaly(
        "known-sessions-vanished",
        "已知 session 大量從 hub 消失（" + "；".join(parts) + "）。"
        "疑似掛錯碟/hub 被清空/部分同步——請確認 hub 正確後 `bootstrap` 重建基線，不自動處理。",
        "halt",
    )


def check(state: State | None, hub_root: str | Path) -> list[Anomaly]:
    root = Path(hub_root)
    if not root.exists() or not root.is_dir():
        return [Anomaly("mount-missing", f"hub 掛載點不存在或非目錄：{root}", "halt")]
    out: list[Anomaly] = []
    cur = hub_fingerprint(root)
    if state is not None and state.hub_fingerprint and state.hub_fingerprint != cur:
        out.append(Anomaly("hub-fingerprint-changed", "hub 指紋改變（可能掛錯碟/專案集合突變）", "halt"))
    disappeared = detect_disappearance(state, root)
    if disappeared is not None:
        out.append(disappeared)
    return out


# ── 跨側 presence / identity 安全 predicates（session-scan 與 memory-scan 共用，單一真相源避免漂移）──
# 這兩個 predicate 不是 halt-anomaly 本身，而是餵給 classify 的安全判據（大量消失 → 不自動傳播刪除；
# casefold 撞名 → 跨 OS aliasing 風險）。原本在 scan.py，P1d memory 也要用同一套 → 上提到此 leaf 模組，
# scan 以原名（`is_bulk_local_deletion` / `_collision_casefolds` 別名）re-export 維持既有呼叫端不變。

# present-empty 偵測的最小樣本：曾有 ≥ 此數的 local 項、現一個都不在 → 疑掛錯碟/整夾被清。
# 單一項刪到空（known==1）仍視為正常刪除（floor=2 才觸發），避免常見小專案誤擋。
_MOUNT_UNCONFIRMED_MIN = 2


def is_bulk_local_deletion(local_known: set | None, local_stems: set[str]) -> bool:
    """本專案 local 是否「大量消失」——疑掛錯碟/內容被清，而非使用者逐一刪除 → 該專案所有 `local-deleted`
    改判 `blocked-bulk-local-deletion`（交人、**不自動寫 tombstone**）。session（sid）與 memory（檔名）共用。

    這是刪除偵測最危險處：false-positive 會寫 tombstone 去**抑制對側真實項（session/memory，跨機資料可用性
    損失）**。觸發條件（任一）：
      (a) **掛載無法確認**：曾有 ≥2 個 local 項，但**一個都不在**現況（present 空集）。最危險的掛錯碟情境——
          夾名靠 binding/git 對上、內容卻是別碟（known 集與現況零交集）即被此擋下，連 frac 樣本量不足的小專案
          也涵蓋（codex r24-2）。單一項刪到空（known==1）仍信任。
      (b) **大量比例消失**：known ≥ project_min 且消失比例 ≥ frac（仿 anomaly hub 側、精確分數比較）。
    保守取捨——寧可大量/可疑消失時多問一次，也不在掛錯碟/被清空時靜默寫 tombstone。**仍有殘留**：掛載已被
    現存項確認（present 非空）的「部分」消失會被當正常刪除而寫 tombstone；此為 feature 本旨（傳播使用者刪除）
    的刻意取捨，且 harm 受限——A3 保證永不刪 hub/對側 local，tombstone 僅抑制再傳播、可逆（移除即復原）。"""
    if not local_known:
        return False
    known = set(local_known)
    present = known & set(local_stems)
    if not present and len(known) >= _MOUNT_UNCONFIRMED_MIN:
        return True  # (a) 掛載無法確認
    return len(known) >= PROJECT_VANISH_MIN and (len(known) - len(present)) / len(known) >= DISAPPEAR_FRAC


def collision_casefolds(a_stems, b_stems, keyfn=None) -> set[str]:
    """casefold 撞名集（A9，跨 OS 碰撞風險）。**合併兩側**後看每個折疊鍵是否對到 >1 種拼法——這同時
    涵蓋同側重複與**跨側** case-only 變體（local `ABC` + hub `abc`，case-sensitive 機器上各自不重複，但落到
    Windows/exFAT 會 alias，且兩者鎖路徑不同→不互斥）（codex r11-4）。session 傳 sid 集、memory 傳檔名集。

    `keyfn`＝折疊鍵函式，預設 `str.casefold`（session sid＝ASCII UUID，NFC/NFD 不適用 → 維持既有行為、零變動）。
    **memory 傳 `pathsafe.name_key`（NFC∘casefold∘NFC）**——否則同一檔名的 NFC 與 NFD 兩種拼法（跨平台撰寫 memory
    常見）不被判撞名，會各自當獨立檔雙向 copy（norm-sensitive FS）或在 norm-insensitive FS 上 aliased 覆蓋（e2e-r1
    Finding 2；memory 檔名配對按位元組精確、唯此折疊鍵能認出正規化別名）。回傳集合以 `keyfn` 為鍵——**呼叫端的
    `x in <此集合>` 判定必須用同一 `keyfn(x)`**（memory 端已改 `name_key(name) in collisions`）。"""
    kf = keyfn or str.casefold
    by_cf: dict[str, set[str]] = {}
    for s in set(a_stems) | set(b_stems):
        by_cf.setdefault(kf(s), set()).add(s)
    return {cf for cf, spellings in by_cf.items() if len(spellings) > 1}
