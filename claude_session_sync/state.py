"""state.json：已知 session/project 指紋、hub fingerprint、跨路徑綁定（A17.4），含完整性校驗。

依據 DESIGN §4(state)/§8.5 + 附錄 A17.4 + PLAN v0.5 §2.6：
  - schema version + checksum；**present-but-corrupt 與 missing 嚴格分開**——壞檔保守要求確認，
    不退化成「首次同步」（§8.5）。
  - 跨路徑 local-cwd ↔ hub-project 綁定持久化（A17.4）。

P1b：save 改走 atomicio（fsync+讀回驗）；新增 **per-session 加鎖 read-modify-write(CAS)**——
逐 session commit、絕不批次覆蓋；持鎖期間重讀最新 state 再加上本次 delta，故並發 commit 不互蓋。
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import atomicio, config

SCHEMA_VERSION = 1


class StateCorruptError(Exception):
    """state.json 存在但損壞（schema/checksum 不符）。呼叫端須保守處理、不可當首次同步。"""


def default_state_path() -> Path:
    return config.default_config_path().with_name("state.json")


def _checksum(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


@dataclass
class State:
    hub_fingerprint: str | None = None
    known_sessions: dict[str, set[str]] = field(default_factory=dict)   # project_key -> **hub** 端 sessionId 集
    local_sessions: dict[str, set[str]] = field(default_factory=dict)   # project_key -> **local** 端 sessionId 集（對稱刪除追蹤，P1c）
    known_memory: dict[str, set[str]] = field(default_factory=dict)     # project_key -> **hub** 端 memory 檔名集
    local_memory: dict[str, set[str]] = field(default_factory=dict)     # project_key -> **local** 端 memory 檔名集（對稱刪除追蹤，P1d）
    bindings: dict[str, str] = field(default_factory=dict)              # local_cwd -> hub_project_key (A17.4)
    local_dir_bindings: dict[str, str] = field(default_factory=dict)    # local 夾名 -> hub_project_key（供 session 全刪、無 cwd 可解析的空夾仍能配對，P1c）
    schema_version: int = SCHEMA_VERSION
    path: str | None = None

    def _payload(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "hub_fingerprint": self.hub_fingerprint,
            "known_sessions": {k: sorted(v) for k, v in self.known_sessions.items()},
            "local_sessions": {k: sorted(v) for k, v in self.local_sessions.items()},
            "known_memory": {k: sorted(v) for k, v in self.known_memory.items()},
            "local_memory": {k: sorted(v) for k, v in self.local_memory.items()},
            "bindings": dict(self.bindings),
            "local_dir_bindings": dict(self.local_dir_bindings),
        }


def load_or_none(path: str | os.PathLike | None = None) -> State | None:
    """檔不存在 → None（真首次同步）。檔在但壞 → raise StateCorruptError（不可當首次）。"""
    p = Path(path) if path is not None else default_state_path()
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise StateCorruptError(f"state.json 無法解析：{e}") from e
    if not isinstance(raw, dict) or "_checksum" not in raw:
        raise StateCorruptError("state.json 結構不符或缺 checksum")
    payload = {k: v for k, v in raw.items() if k != "_checksum"}
    if _checksum(payload) != raw["_checksum"]:
        raise StateCorruptError("state.json checksum 不符（可能損壞/被竄改）")
    try:
        return State(
            hub_fingerprint=payload.get("hub_fingerprint"),
            known_sessions={k: set(v) for k, v in payload.get("known_sessions", {}).items()},
            # 舊 state 缺 local_sessions → .get(...,{}) 給空（clean migration，首次 apply 後由 re-glob 填）。
            local_sessions={k: set(v) for k, v in payload.get("local_sessions", {}).items()},
            known_memory={k: set(v) for k, v in payload.get("known_memory", {}).items()},
            # 舊 state 缺 local_memory → 空（migration）；has_local_memory_baseline 以「pk 是否在此 dict」判（空集≠缺欄位）。
            local_memory={k: set(v) for k, v in payload.get("local_memory", {}).items()},
            bindings=dict(payload.get("bindings", {})),
            local_dir_bindings=dict(payload.get("local_dir_bindings", {})),
            schema_version=int(payload.get("schema_version", SCHEMA_VERSION)),
            path=str(p),
        )
    except (TypeError, ValueError, AttributeError) as e:
        raise StateCorruptError(f"state.json 欄位型別不符：{e}") from e


def save(state: State, path: str | os.PathLike | None = None) -> str:
    """原子寫（atomicio：同目錄 temp + fsync + rename + 讀回驗）。回寫出的路徑。

    注意：本函式**不加鎖**——並發場景請走 `commit_session` / `update_under_lock`（持鎖 RMW），
    否則「load→改→save」之間另一 process 的 commit 會被覆蓋掉。
    """
    p = Path(path) if path is not None else Path(state.path or default_state_path())
    payload = state._payload()
    doc = {**payload, "_checksum": _checksum(payload)}
    atomicio.atomic_write_text(p, json.dumps(doc, ensure_ascii=False, indent=2))
    state.path = str(p)
    return str(p)


def _resolve_path(path: str | os.PathLike | None) -> Path:
    return Path(path) if path is not None else default_state_path()


def update_under_lock(
    mutate: Callable[[State], None],
    path: str | os.PathLike | None = None,
    *,
    lock_timeout_s: float = 5.0,
) -> State:
    """加鎖 read-modify-write：取 state 鎖 → 重讀**最新** state → 套用 mutate(delta) → 原子寫。

    並發安全的關鍵在「持鎖期間重讀」：即便呼叫端手上的 State 已過期，這裡仍以磁碟最新內容為基底，
    故兩個 process 各加一個 session 不會互蓋（CAS 等價，PLAN §2.6）。state 壞檔 → 拋 StateCorruptError，
    不靜默當首次同步覆蓋。鎖取不到（逾時/stale）→ 拋 atomicio.LockError/StaleLock，不靜默 proceed。
    """
    p = _resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock = atomicio.FileLock(p).acquire_blocking(timeout_s=lock_timeout_s)
    try:
        st = load_or_none(p) or State(path=str(p))
        mutate(st)
        save(st, p)
        return st
    finally:
        lock.release()


def commit_session(
    project_key: str,
    sid: str,
    path: str | os.PathLike | None = None,
    *,
    cwd: str | None = None,
    hub_fingerprint: str | None = None,
    lock_timeout_s: float = 5.0,
) -> State:
    """逐 session 提交（加鎖 RMW）：把 sid 記入 known_sessions[project_key]；可選同時記跨路徑綁定
    （cwd→project_key，A17.4）與 hub_fingerprint。回提交後的 State。"""
    def _mutate(st: State) -> None:
        st.known_sessions.setdefault(project_key, set()).add(sid)
        if cwd is not None:
            st.bindings[cwd] = project_key
        if hub_fingerprint is not None:
            st.hub_fingerprint = hub_fingerprint

    return update_under_lock(_mutate, path, lock_timeout_s=lock_timeout_s)


def commit_memory(
    project_key: str,
    name: str,
    path: str | os.PathLike | None = None,
    *,
    lock_timeout_s: float = 5.0,
) -> State:
    """逐 memory 提交（加鎖 RMW、additive）：把檔名記入 known_memory[project_key]（hub 基線已知，P1d）。
    對稱 `commit_session`；不動 binding/fingerprint（那些是 session/專案層級）。回提交後的 State。"""
    def _mutate(st: State) -> None:
        st.known_memory.setdefault(project_key, set()).add(name)

    return update_under_lock(_mutate, path, lock_timeout_s=lock_timeout_s)


def reconcile_local_memory_presence(
    project_key: str,
    present_names,
    tombstoned,
    path: str | os.PathLike | None = None,
    *,
    lock_timeout_s: float = 5.0,
    require_baseline: bool = False,
) -> State:
    """更新 local_memory[project_key]（memory apply 末由 re-glob 結果呼叫，對稱 `reconcile_local_presence`，P1d）。
    新值 = present_names（寫入後 local memory 現況）∪ pending；pending = **鎖內最新** baseline 中「已不在 local、
    且尚無 memory tombstone」者（未落地的本機刪除，保留以免下次當新檔復活）。已成功落地 tombstone 的檔名（∈
    tombstoned）才從 baseline 移除（往後由 tombstone 閘保護）。加鎖 RMW，只動 local_memory[pk]。

    `require_baseline`（apply 傳 True）：鎖內若**最新** state 已無此 pk 的 local memory 基線（並發 doctor
    --rebuild-state 或 migration 移除）→ **不重建**。否則會把 has_local_baseline 守衛（取自呼叫時可能 stale 的
    state）失效後仍憑當前磁碟建空基線 → 下次 hub-only memory 當新檔復活（e2e Pass2 Medium）。"""
    present = set(present_names)
    tombset = set(tombstoned)

    def _mutate(st: State) -> None:
        if require_baseline and project_key not in st.local_memory:
            return  # 鎖內最新 state 已無此專案 local memory 基線 → fail-closed 不重建（避免復活）
        prev = st.local_memory.get(project_key, set())
        pending = {n for n in prev if n not in present and n not in tombset}
        st.local_memory[project_key] = present | pending

    return update_under_lock(_mutate, path, lock_timeout_s=lock_timeout_s)


def reconcile_local_presence(
    project_key: str,
    present_stems,
    tombstoned,
    path: str | os.PathLike | None = None,
    *,
    lock_timeout_s: float = 5.0,
    require_baseline: bool = False,
) -> State:
    """更新 local_sessions[project_key]（apply 末由 re-glob 結果呼叫，P1c）。新值 =
        present_stems（寫入後 local 現況）∪ pending，
    pending = **鎖內最新** baseline 中「已不在 local、且尚無 tombstone」者 = 未落地的本機刪除。

    pending 為何由**鎖內 disk baseline** 算（非呼叫端傳入的 stale 集）：並發另一 sync 可能已在 disk
    baseline 保留了某個 tombstone 寫失敗的 pending 刪除；若本 process 以自己的 stale 快照盲覆寫，會把它
    抹掉 → 下次當「新 hub 檔」復活（codex r24-4）。故在鎖內以最新 `prev` 計算、合併。

    pending **不以 hub 是否在場為條件**：local-deleted 的 tombstone 若寫失敗（error/skip），即便 hub 檔
    此刻恰好不在，也須保留該 sid 於 baseline，否則 hub 檔稍後復現（無 tombstone）會被當新檔復活（codex r24-3）。
    已成功落地 tombstone 的 sid（∈tombstoned）才從 baseline 移除（往後由 tombstone 閘保護）。
    與 known_sessions 的 additive commit 不同，這是 baseline 取代式更新；加鎖 RMW，只動 local_sessions[pk]，
    其餘（known_sessions / 別 pk）一律保留。dry-run **不**呼叫（apply 專用）。

    `require_baseline`（apply 傳 True）：鎖內若**最新** state 已無此 pk 的 local 基線（並發 doctor --rebuild-state
    或 migration 移除）→ **不重建**。否則會令 apply 的 has_local_baseline 守衛（取自呼叫時 stale state）失效後仍憑
    當前磁碟建空基線 → 下次 present=hub 走 copy-to-local 而非 blocked-no-local-baseline ＝ 復活（e2e Pass2 Medium）。"""
    present = set(present_stems)
    tombset = set(tombstoned)

    def _mutate(st: State) -> None:
        if require_baseline and project_key not in st.local_sessions:
            return  # 鎖內最新 state 已無此專案 local 基線 → fail-closed 不重建（避免復活）
        prev = st.local_sessions.get(project_key, set())
        pending = {sid for sid in prev if sid not in present and sid not in tombset}
        st.local_sessions[project_key] = present | pending

    return update_under_lock(_mutate, path, lock_timeout_s=lock_timeout_s)
