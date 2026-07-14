"""決策快照子集（C4）：plan 時擷取、apply 取鎖後重算比對；不一致即「決策已過期」→ 中止。

依據 PLAN v0.8 §2.8（codex r3/r4 C4）+ §3 資料流第 6/7 步。一個 per-session 子集，涵蓋這個分類決策
**所依賴的全部輸入**：兩側資料檔 + 各自 meta sidecar + 該 hub 專案 `_project.json` + config（own_hub/
remotes/force_unsafe_lock）+ 該 hub 專案 **tombstone 目錄 digest（含 `_coverage.json`）** + **該 session
在 state 的單一條目 hash**（不是整個 state，避免 per-session commit 自我失效）。

「hub/project fingerprint + known-session set + coverage」屬 **anomaly 重跑**（見 anomaly.py），與本
per-session 子集分開：本子集放整個 known-set 會被自己的逐 session commit 連動而誤判過期。

codex r8 兩個 critical 防護：
  ① 接 **專案夾 + sid**（非可為 None 的具體路徑）自行推導 `<sid>.jsonl`/`<sid>.meta.json`——否則 plan
     時 None 代表「這側沒檔」，apply 時還是 None，即使期間檔被建出來也偵測不到 → 覆蓋掉新檔。
  ② `_file_digest` **永不回 None**：以 absent/sha/err/nonreg 區分「不存在」與「存在但讀不到/非一般檔」，
     否則兩者都 None 會被當「沒變」而覆蓋未檢視過的資料。

純標準庫；不寫檔。
"""
from __future__ import annotations

import hashlib
import json
import os
import stat as statmod
from dataclasses import dataclass
from pathlib import Path

from . import tombstone
from .config import Config
from .state import State

META_SUFFIX = ".meta.json"


def _file_digest(path: Path | None) -> str:
    """檔狀態指紋，永不回 None。先 stat（不開 FIFO 以免阻塞），只對一般檔讀內容算 sha256：
      - absent          ：不存在
      - sha:<hex>       ：一般檔內容
      - nonreg:<ifmt>   ：存在但非一般檔（目錄/FIFO/symlink-to-dir…）
      - err:<errno>     ：存在但讀不到（權限/IO）
    四類彼此可辨，確保「plan 時沒檔、apply 時冒出檔（或變不可讀）」一定改變快照。
    """
    if path is None:
        return "absent"
    try:
        st = os.lstat(path)  # **no-follow**（e2e gate4 #2）：symlink leaf → S_ISLNK → 下方 nonreg（不 open 讀界外目標）；
        #                       一般檔 lstat==stat、行為不變。變動偵測仍有效（symlink swap → nonreg vs sha 改變 → 快照失效）。
    except FileNotFoundError:
        return "absent"
    except OSError as e:
        return f"err:{e.errno}"
    if not statmod.S_ISREG(st.st_mode):
        return f"nonreg:{statmod.S_IFMT(st.st_mode)}"   # symlink → S_ISLNK → 非 S_ISREG → 不 open 讀界外（e2e gate4 #2）
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return f"err:{e.errno}"
    return "sha:" + hashlib.sha256(data).hexdigest()


def config_hash(config: Config) -> str:
    """只納入會左右落地決策的欄位：own_hub / remotes / force_unsafe_lock（map=bindings 在 state 條目）。"""
    payload = {
        "own_hub": config.own_hub,
        "remotes": dict(sorted(config.remotes.items())),
        "force_unsafe_lock": config.force_unsafe_lock,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def state_entry_hash(state: State | None, project_key: str | None, sid: str, cwd: str | None,
                     local_dir_name: str | None = None) -> str:
    """**單一 session** 在 state 的條目指紋：是否已知（hub / local 基線）+ 該 cwd 的跨路徑綁定。

    刻意只取**本 sid 的成員性**、不納入整個集合——否則 apply 迴圈中對**其他** session 的 per-session
    commit 會改動集合、使本 session 的快照誤判過期（PLAN §2.8 明列的自我失效陷阱）。新增其他 sid 不改變
    「本 sid 是否在集合內」，故此 hash 在我方 additive commit 下穩定。`local_known` 與 `known` 對稱納入
    （供 local-deleted/copy-to-local 決策；local_sessions[pk] 的 baseline 取代只在專案末發生、不影響本 sid
    成員性於迴圈內的穩定性，P1c）。"""
    known = bool(state and project_key is not None and sid in state.known_sessions.get(project_key, set()))
    local_known = bool(state and project_key is not None and sid in state.local_sessions.get(project_key, set()))
    binding = state.bindings.get(cwd) if (state and cwd is not None) else None
    # 夾名綁定也納入：空夾（cwd=None）的身分解析改靠 local_dir_bindings，故其變動（並發 remap）須能令快照失效，
    # 與 cwd-binding 對稱（codex r26-2）。斷言旗標同理：asserted 夾（--map 斷言整夾，2026-07-14）的身分解析
    # 繞過 cwd 檢查，故斷言的授予/撤銷也須令快照失效。
    dir_binding = state.local_dir_bindings.get(local_dir_name) if (state and local_dir_name is not None) else None
    dir_asserted = bool(state and local_dir_name is not None and local_dir_name in state.asserted_dirs)
    payload = {"pk": project_key, "sid": sid, "known": known, "local_known": local_known,
               "binding": binding, "dir_binding": dir_binding, "dir_asserted": dir_asserted}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DecisionSnapshot:
    session_id: str
    local_data: str
    hub_data: str
    local_meta: str
    hub_meta: str
    project_sidecar: str
    config_hash: str
    tomb_dir_digest: str
    state_entry: str


def compute_decision_snapshot(
    *,
    session_id: str,
    local_project_dir: Path | None,
    hub_project_dir: Path | None,
    config: Config,
    state: State | None,
    project_key: str | None,
    cwd: str | None,
) -> DecisionSnapshot:
    """擷取/重算一個 session 的決策快照子集。plan 與 apply 各呼叫一次（傳同樣的專案夾），相等才可落地。

    傳**專案夾**而非具體檔路徑：本函式自行推導 `<sid>.jsonl`/`<sid>.meta.json`，使「期間冒出/消失/變不可讀」
    都能在 apply 重算時被偵測（codex r8 critical①）。某側無對應專案夾（未綁定）→ 該側恆 absent。
    """
    def in_dir(d: Path | None, name: str) -> Path | None:
        return (d / name) if d is not None else None

    sidecar_path = in_dir(hub_project_dir, "_project.json")
    tomb_digest = tombstone.tombstone_dir_digest(hub_project_dir) if hub_project_dir else ""
    return DecisionSnapshot(
        session_id=session_id,
        local_data=_file_digest(in_dir(local_project_dir, f"{session_id}.jsonl")),
        hub_data=_file_digest(in_dir(hub_project_dir, f"{session_id}.jsonl")),
        local_meta=_file_digest(in_dir(local_project_dir, f"{session_id}{META_SUFFIX}")),
        hub_meta=_file_digest(in_dir(hub_project_dir, f"{session_id}{META_SUFFIX}")),
        project_sidecar=_file_digest(sidecar_path),
        config_hash=config_hash(config),
        tomb_dir_digest=tomb_digest,
        state_entry=state_entry_hash(state, project_key, session_id, cwd,
                                     local_project_dir.name if local_project_dir is not None else None),
    )
