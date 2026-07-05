"""apply：把 dry-run SyncPlan 逐 session 安全落地（P1b 自動套用僅 identical/paired-ff/copy）。

依據 PLAN v0.8 §3 資料流第 7 步（A12 逐 session 交易）+ §2.8（C3/C4）+ 決定 #8：
  每 session：取鎖(hub 側路徑為鍵，跨機共用 hub 也序列化) → 重算決策快照子集、與 plan 時比對，
  不一致即中止該檔（決策已過期）→ 重算 hub 指紋擋掛錯碟 → 執行 → 原子寫+讀回驗 → state per-session CAS。

C3 結構性保證：**絕不覆蓋 local 既有 JSONL**。寫 local 只有兩種——copy-to-local（sid 在 local 不存在
→ 建新檔）與 ff hub->local（local 已有 → 一律改寫檔名 keep-both，不碰原檔）。寫 hub 才允許覆蓋。

非自動類別（superset-branch/fork/needs-decision/damaged/collision/blocked-*/suppressed）只回報，不寫
（互動合併/刪除是 P1c）。
"""
from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from . import acks, anomaly, atomicio, memory, scan, state as state_mod, tombstone
from .config import Config
from .snapshot import DecisionSnapshot, compute_decision_snapshot
from .state import State

# 會自動落地的分類（其餘只回報）。local-deleted = 偵測到本機刪除，自動寫 hub tombstone（P1c）。
AUTO_ACTIONS = frozenset({"identical", "fast-forward", "copy-to-hub", "copy-to-local", "local-deleted"})
# memory auto 動作（無 fast-forward；**刻意不含** conflict-cross-file-identity / blocked-tombstone-no-identity
# → 一律 needs-decision、reported，不自動寫，A14/§7.2.3，P1d Block 3b-2）。
MEM_AUTO_ACTIONS = frozenset({"identical", "copy-to-hub", "copy-to-local", "local-deleted"})

_IDX_CHANGED = object()  # MEMORY.md 重讀失敗的哨兵（≠ 任何 str/None）→ 保守視為已變動、不覆蓋（Block 3c）。


def _is_unfollowable_reparse(p: Path) -> bool:
    """True → **不可跟隨**、跳過/不寫索引（symlink，或 cloud/未知/不可判定 reparse → fail-closed）；False → 普通檔/夾
    或 directory **junction**（跟隨——使用者刻意同機共用，CLAUDE_CONFIG_DIR 模型）。委派 `memory.reparse_kind`
    （精確 reparse-tag 分類，與 `memory.list_memory_files` 同一真相源，fresh gate ccdir-g1）：

    - junction（MOUNT_POINT）→ "junction" → 跟隨（索引照常維護到真實共用夾）；
    - symlink → "symlink" → 擋（非同機共用機制、可跨裝置/特殊檔逃逸；`_read_index_bytes_nofollow` 另守 leaf）；
    - OneDrive/cloud 佔位、未知 reparse、lstat 失敗 → "other" → 擋（fail-closed，避免寫到雲端/非預期目標，g1 Medium）；
    - 缺檔 → "none" → False（不存在非 reparse）。索引是便利性衍生資料，任何不確定一律退讓不寫。"""
    return memory.reparse_kind(p) in ("symlink", "other")


def _reparse_safe_symlink_names_cf(d: Path | None) -> set[str]:
    """`d` 內 symlink leaf 的 `scan._name_key` 集，供 symlink-alias 偵測（e2e gate7/gate8）。**reparse-aware root**：
    `d` 為 symlink/cloud/未知 reparse 根 → **不跟隨**、回空集（`_is_unfollowable_reparse`，fail-closed；memory/ 根
    可能是 symlink）；directory junction 根跟隨（CLAUDE_CONFIG_DIR 同機共用模型）。實際列舉委派
    `scan._symlink_name_keys`（單一真相源，與 transfer 共用；`scan._name_key`=NFC+casefold，涵蓋 exact／
    casefold-alias `A.md`／NFC-NFD-alias `café.md`）。"""
    if d is None or _is_unfollowable_reparse(d):
        return set()
    return scan._symlink_name_keys(d)


def _read_index_bytes_nofollow(p: Path) -> bytes:
    """讀 MEMORY.md，**不跟隨 symlink/junction、不卡在 FIFO/device**（codex 塊末 fresh gate r5 Medium ＋ CI gate）：
    開檔前先 `os.lstat` **跨 OS** 擋 reparse point——POSIX `S_ISLNK`；Windows `st_file_attributes &
    FILE_ATTRIBUTE_REPARSE_POINT`（涵蓋 symlink ＋ junction）。此為 **leaf 防線**（idx_path 自身）；父夾 memory/
    為 **symlink/cloud/未知 reparse** 則由呼叫端 `_is_unfollowable_reparse` 擋（**junction 父夾刻意跟隨**＝CLAUDE_CONFIG_DIR 模型，見 memory.reparse_kind）——兩者互補（亦防 TOCTOU：父夾
    驗過後 leaf 被換）。因 Windows 無 `O_NOFOLLOW`（=0 no-op），函式**自身**必須擋、不能只靠它——
    GitHub Windows runner 開了開發者模式能建 symlink → 實跑揭露此盲點（本機無開發者模式則該測試 skip）。再以
    O_NOFOLLOW（POSIX 最終元件為 symlink → ELOOP，關 lstat→open 的 TOCTOU）+ O_NONBLOCK（FIFO 開啟不阻塞）+
    fstat `S_ISREG`（非普通檔→拒）開檔讀。防外部在 lstat 與 read 之間把 MEMORY.md 換成 symlink→FIFO/device，令
    apply 在 memory 已寫後卡死或讀到外部檔。缺檔 → `os.lstat` 照常 raise FileNotFoundError（呼叫端當「不存在」處理）。"""
    st = os.lstat(p)  # 不跟隨最終元件；缺檔 → FileNotFoundError（契約：呼叫端當不存在）
    if stat.S_ISLNK(st.st_mode) or (getattr(st, "st_file_attributes", 0)
                                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise OSError(errno.ELOOP, "MEMORY.md 是 symlink/junction（reparse point，不跟隨）")
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    fd = os.open(p, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "MEMORY.md 非普通檔（疑 symlink/FIFO/device）")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


_WROTE_RESULTS = frozenset({"applied-ff-hub", "copied-to-hub", "copied-to-local", "kept-both-local",
                            "tombstoned-local-deletion"})


@dataclass
class ApplyOutcome:
    session_id: str      # session=sessionId；memory=檔名（kind="memory"）
    action: str          # plan 的動作
    result: str          # identical / applied-ff-hub / copied-to-hub / copied-to-local / kept-both-local /
                         # tombstoned-local-deletion / skipped-changed / skipped-locked / skipped-stale /
                         # suppressed / reported / error / halt
    detail: str
    path: str | None = None
    committed: bool = True  # 寫入後 state 是否成功提交（False=已寫檔但 state 未落，須非零退出，codex r11-6）
    kind: str = "session"   # "session" | "memory"（供 format_report 顯示與計數區分，P1d Block 3b-2）
    project: str | None = None  # hub 專案夾名（A15：供 format_report **project-scoped** 隱藏 acked 項，不跨專案 flatten，g1 Low）


@dataclass
class ApplyReport:
    outcomes: list[ApplyOutcome]
    halted: bool = False
    halt_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    # presence reconcile（local_sessions/local_memory baseline 更新）失敗 → 須 CLI 非零退出（codex 3b2-R1 #3）：
    # 寫檔成功但 presence 基線沒落地時若靜默報成功，使用者在下次成功 sync 前刪掉剛 copy 的檔 → 下次當新檔復活。
    reconcile_failed: bool = False

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for o in self.outcomes:
            c[o.result] = c.get(o.result, 0) + 1
        return c

    @property
    def wrote_anything(self) -> bool:
        return any(o.result in _WROTE_RESULTS for o in self.outcomes)

    @property
    def had_error(self) -> bool:
        return any(o.result == "error" for o in self.outcomes)

    @property
    def had_uncommitted(self) -> bool:
        """有「檔已寫成但 state 未提交」的 session → 須讓 CLI 非零退出（誠實，codex r11-6）。"""
        return any(not o.committed for o in self.outcomes)


def _single_cwd(local_dir: Path | None) -> str | None:
    if local_dir is None:
        return None
    cwds = scan._project_cwds(local_dir)
    return next(iter(cwds)) if len(cwds) == 1 else None


def _verified_bytes(path: Path, expected_token: str) -> bytes | None:
    """讀 source bytes **一次**，比對其 sha 與快照當時記下的 digest；不符（含讀不到）→ None。

    把「實際寫出去的 bytes」綁定到通過檢查的快照（codex r10-3），杜絕「快照後 source 又變、寫進未分類
    內容」的窗。expected_token 形如 snapshot 的 'sha:<hex>'。"""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if "sha:" + hashlib.sha256(data).hexdigest() != expected_token:
        return None
    return data


def _commit_known(state_path, project_key: str, sid: str, lock_timeout_s: float) -> str | None:
    """成功寫入後記 sid 為已知（加鎖 CAS、additive）。**不**動 binding/fingerprint——binding 由 bootstrap
    建立，apply 期間若每 session 改 binding 會連動同專案其他 session 的 state_entry 而誤判快照過期。
    回 None=成功；否則回錯誤字串（檔已寫成、state 未落，呼叫端須標 committed=False，誠實非零退出）。"""
    try:
        state_mod.commit_session(project_key, sid, state_path, lock_timeout_s=lock_timeout_s)
        return None
    except Exception as e:  # noqa: BLE001 - 檔已安全寫入，state 提交失敗：降級回報、不假裝成功
        return str(e)


def _done(sid, action, result, base_detail, path, commit_err) -> ApplyOutcome:
    """組裝成功 outcome 並記 known：state 提交失敗則 committed=False 並把原因併入 detail（codex r11-6）。
    path 可為 None（如 identical 沒寫檔）。"""
    p = str(path) if path is not None else None
    if commit_err is None:
        return ApplyOutcome(sid, action, result, base_detail, p)
    return ApplyOutcome(sid, action, result,
                        base_detail + f"（state 未提交，下次 sync 自癒：{commit_err}）", p, committed=False)


def _apply_session(
    sp: scan.SessionPlan, *, local_dir: Path | None, hub_dir: Path, project_key: str, cwd: str | None,
    plan_snap: DecisionSnapshot, config: Config, state_path, hub_root: Path, base_fp: str,
    machine: str | None, lock_timeout_s: float,
) -> ApplyOutcome:
    sid, action = sp.session_id, sp.action
    hub_file = hub_dir / f"{sid}.jsonl"
    local_file = (local_dir / f"{sid}.jsonl") if local_dir else None

    # 取鎖：以 hub 側 session 路徑為鍵（hub 是跨機共用資源，序列化任何兩個 process 對同 sid 的動作）。
    try:
        lock = atomicio.FileLock(hub_file).acquire_blocking(timeout_s=lock_timeout_s)
    except atomicio.StaleLock as e:
        return ApplyOutcome(sid, action, "skipped-stale", f"鎖疑似陳舊，交人工處理：{e}")
    except atomicio.LockError as e:
        return ApplyOutcome(sid, action, "skipped-locked", f"取鎖逾時，略過：{e}")
    try:
        # leaf symlink 防線（e2e gate3 #1）：鎖內 reclassify 前先擋 symlink .jsonl（TOCTOU：plan/T1 後被換）→ 不讓
        # classify/snapshot 跟隨讀界外（既有 `src.is_symlink()` 在寫入前、太晚，讀已發生；此處提前到讀之前）。
        if (local_file is not None and local_file.is_symlink()) or hub_file.is_symlink():
            return ApplyOutcome(sid, action, "skipped-changed", "session 檔為 symlink（疑逃逸/TOCTOU），略過")
        # 1) 持鎖**重新分類**（用磁碟現況），動作+方向須與 plan 一致 → 否則決策已過期，中止（codex r10-2）。
        #    這擋住「plan 算 ff local->hub，但期間 hub 收到獨立資料變成 fork」仍照舊覆蓋 hub 的洞。
        cur_lf = local_file if (local_file and local_file.exists()) else None
        cur_hf = hub_file if hub_file.exists() else None
        cov = tombstone.is_initialized(hub_dir)
        tombs = tombstone.read_tombstones(hub_dir)
        corrupt = tombstone.corrupt_tombstone_targets(hub_dir)
        coll = scan.casefold_collisions_for(local_dir, hub_dir)
        cur_state = state_mod.load_or_none(state_path)  # 鎖內取磁碟最新 state（供 known/baseline 判定 + 快照）
        known = cur_state.known_sessions.get(project_key) if cur_state else None
        has_baseline = bool(cur_state and project_key in cur_state.known_sessions)
        # local-presence 對稱刪除：鎖內重算 local_known + has_local_baseline + bulk guard（由磁碟現況，反映
        # 期間更多 local 消失 → bulk 翻 True / 復現 → 重分類與 plan 不符 → skipped-changed，不誤寫 tombstone）。
        local_known = cur_state.local_sessions.get(project_key) if cur_state else None
        has_local_baseline = bool(cur_state and project_key in cur_state.local_sessions)
        cur_local_stems = set(scan._session_files(local_dir).keys()) if local_dir else set()
        bulk = scan.is_bulk_local_deletion(local_known, cur_local_stems)
        cur = scan.classify_session(
            sid, cur_lf, cur_hf, both=local_dir is not None,
            coverage_initialized=cov, tombs=tombs, corrupt=corrupt, known=known,
            has_baseline=has_baseline, is_collision=sid.casefold() in coll,
            local_known=local_known, bulk_local_deletion=bulk, has_local_baseline=has_local_baseline,
        )
        if not cov:  # 信任邊界：apply 中途若專案變 uninitialized → 不自動套用（F1 防線之一）
            return ApplyOutcome(sid, action, "blocked-uninitialized", "專案未 bootstrap → 不自動套用")
        if (cur.action, cur.direction) != (action, sp.direction):
            if cur.action == "suppressed-deleted":
                return ApplyOutcome(sid, action, "suppressed",
                                    "apply 中出現 hub tombstone → 不復活已刪 session")
            if cur.action == "conflict-delete-vs-update":
                # race：plan 後、鎖內重分類前出現 tombstone 且內容≠base。不寫，且**誠實surface 衝突**
                # （非泛用 suppressed），與 plan-time 衝突回報一致，保住「交人」訊號（codex r22）。
                return ApplyOutcome(sid, cur.action, "reported",
                                    "apply 中出現 hub tombstone 且內容≠base（刪除衝突）→ 不寫，交人決策（請重跑 sync）")
            return ApplyOutcome(sid, action, "skipped-changed",
                                f"重新分類已變（{action}→{cur.action}），請重跑 sync")

        # 2) 重算決策快照（含 config/_project.json/tomb-dir/state 條目），與 plan 時不符 → 中止。
        cur_snap = compute_decision_snapshot(
            session_id=sid, local_project_dir=local_dir, hub_project_dir=hub_dir,
            config=config, state=cur_state, project_key=project_key, cwd=cwd,
        )
        if cur_snap != plan_snap:
            return ApplyOutcome(sid, action, "skipped-changed", "決策輸入自 plan 後已變，請重跑 sync")
        if anomaly.hub_fingerprint(hub_root) != base_fp:
            return ApplyOutcome(sid, action, "halt", "hub 指紋在 apply 中改變（疑似掛錯碟），全面中止")

        # 任一成功的 auto 動作都「確認 hub 有此 sid」（identical/ff/copy 皆然）→ 一律記 known，
        # 否則未記 known 的 session 日後在 hub 被刪時，known-deleted 閘抓不到而復活（codex r17）。
        commit = lambda: _commit_known(state_path, project_key, sid, lock_timeout_s)

        if action == "identical":
            return _done(sid, action, "identical", "兩側相同，無需寫入", None, commit())

        if action == "local-deleted":
            # 本機已刪此 local session（hub 仍在）→ 寫 hub tombstone 通知對側；**絕不刪 hub**（A3 永久歸檔）。
            # base_hash = 鎖內 hub 現況 raw bytes：對側據此條件式 suppress（==base→不復活；≠base→delete-vs-update
            # 交人）。此處不複製任何 bytes，故跳過 _verified_bytes（無來源檔可寫）。snapshot 已守住「local 期間
            # 復現/hub 變動」（local_data absent→present 或 hub_data 變 → cur_snap≠plan_snap → 上面已 skipped）。
            base = tombstone.raw_file_digest(hub_file)
            tombstone.write_session_tombstone(hub_dir, sid, base_hash=base, machine=machine)
            return _done(sid, action, "tombstoned-local-deletion",
                         "本機刪除已寫 hub tombstone（hub 歸檔保留；對側下次 sync 起抑制）",
                         tombstone.session_tombstone_path(hub_dir, sid), commit())

        # 寫入前：把實際 bytes 綁定到通過檢查的快照（read-once + 比對 digest），不符即中止（codex r10-3）。
        # source 為 local 僅當：copy-to-hub，或 ff local->hub；其餘（ff hub->local / copy-to-local）source 為 hub。
        src_is_local = action == "copy-to-hub" or (action == "fast-forward" and sp.direction == "local->hub")
        src, expected = (local_file, cur_snap.local_data) if src_is_local else (hub_file, cur_snap.hub_data)
        # leaf 檔 symlink 防線（e2e gate2 #2 defense-in-depth）：source .jsonl 若為 symlink（TOCTOU：plan 後被換）→
        # read_bytes 會跟隨到夾外檔＝洩漏/污染。plan-time `_session_files` 已略過 symlink，此處守鎖內 TOCTOU。
        if src.is_symlink():
            return ApplyOutcome(sid, action, "skipped-changed", "source 為 symlink（疑逃逸/TOCTOU），中止")
        data = _verified_bytes(src, expected)
        if data is None:
            return ApplyOutcome(sid, action, "skipped-changed", "source 內容於寫入前改變，中止")

        if action == "fast-forward":
            if sp.direction == "local->hub":
                atomicio.atomic_write_bytes(hub_file, data)       # 覆蓋 hub（允許）+ 讀回驗
                return _done(sid, action, "applied-ff-hub", "純延伸寫入 hub", hub_file, commit())
            # hub->local：C3 絕不覆蓋 local 既有檔 → O_EXCL 只建不覆蓋的 keep-both（不重寫內文 sessionId）。
            dest = atomicio.write_keep_both(local_file, data, machine=machine)
            return _done(sid, action, "kept-both-local",
                         "hub 較新但不覆蓋 local，另存 keep-both（resume 即可接續）", dest, commit())

        if action == "copy-to-hub":
            atomicio.atomic_write_bytes(hub_file, data)
            return _done(sid, action, "copied-to-hub", "單邊新檔複製到 hub", hub_file, commit())

        if action == "copy-to-local":
            # C3：local 只**建**不覆蓋。O_EXCL 直接建；若期間冒出同名檔 → 改 keep-both，絕不覆蓋。
            try:
                atomicio.atomic_create_bytes(local_file, data)
            except FileExistsError:
                dest = atomicio.write_keep_both(local_file, data, machine=machine)
                return _done(sid, action, "kept-both-local", "local 期間冒出同名檔 → keep-both 不覆蓋", dest, commit())
            return _done(sid, action, "copied-to-local", "hub 單邊新檔複製到 local", local_file,
                          _commit_known(state_path, project_key, sid, lock_timeout_s))

        return ApplyOutcome(sid, action, "reported", sp.reason)
    except (atomicio.AtomicWriteError, OSError) as e:
        return ApplyOutcome(sid, action, "error", f"寫入失敗（已中止該檔，未污染目標）：{e}")
    finally:
        lock.release()


def _mem_copy_bytes(src: Path, expected_hash: str | None) -> bytes | None:
    """讀來源 memory bytes **一次**，要求其正規化 content_hash == 鎖內權威分類的 `src_hash`；不符/讀不到/損壞
    → None（呼叫端 skipped-changed）。把寫出的 bytes 綁定到分類所據（對稱 session 的 `_verified_bytes`）：杜絕
    auth 之後、寫入之前來源被改名/改內容（frontmatter name 變 → 撞別檔或撞 tombstone identity）仍照寫 ＝ 製造
    跨檔衝突或復活已刪 memory（codex P1d 3b2-R1 #1）。content_hash 涵蓋正規化 frontmatter（含 name）+ 正文，故
    任何語意改動（含改名）都會令 hash 不符而中止。"""
    if src.is_symlink():   # leaf symlink 防線（e2e gate3 #2）：TOCTOU 換成指界外的 symlink → 不讀/複製界外內容
        return None
    try:
        raw = src.read_bytes()
    except OSError:
        return None
    h = memory.content_hash(memory.load_memory_bytes(raw))
    if h is None or h != expected_hash:
        return None
    return raw


def _mem_done(name, action, result, base_detail, path, commit_err) -> ApplyOutcome:
    """組裝 memory 成功 outcome（kind=memory）；state 提交失敗則 committed=False 並併入原因（對稱 `_done`）。"""
    p = str(path) if path is not None else None
    if commit_err is None:
        return ApplyOutcome(name, action, result, base_detail, p, kind="memory")
    return ApplyOutcome(name, action, result,
                        base_detail + f"（state 未提交，下次 sync 自癒：{commit_err}）", p,
                        committed=False, kind="memory")


def _apply_project_memory(
    pp: scan.ProjectPlan, *, report: ApplyReport, hub_dir: Path, local_dir: Path | None,
    project_key: str, state_path, hub_root: Path, base_fp: str,
    machine: str | None, lock_timeout_s: float,
) -> bool:
    """逐專案 memory apply（append 到 report.outcomes；回傳 halted）。

    走 **per-project memory 鎖**（`<hub>/.tombstones/memory.lock`），非 session 的 per-file 鎖：memory 有跨檔身分
    （duty a/b），單檔決策依賴別檔在場 → 整組需對其他 sync process 原子化。鎖內：① 指紋守衛；② 重跑
    `scan._plan_memories` 取**權威**計畫（反映鎖內磁碟現況 + 最新 state）；③ 與 plan-time action 比對（漂移 →
    skipped-changed / suppressed / 衝突 surface）；④ 執行 auto（identical / copy-to-hub / copy-to-local〔C3 O_EXCL，
    撞名 keep-both〕/ local-deleted〔寫 memory tombstone：base=正規化 content_hash、identity=刪除 doc name；**絕不
    刪 hub**，A3〕）；⑤ 末 reconcile local_memory presence。**conflict-cross-file-identity / blocked-* / conflict-***
    皆非 MEM_AUTO → reported（不自動寫，A14）。寫出 bytes 採 read-once + 正規化 damaged 檢查（同一份 raw 既檢查
    又寫出，不寫未分類/半截內容）。"""
    autos = [m for m in pp.memories if m.action in MEM_AUTO_ACTIONS]
    non_autos = [m for m in pp.memories if m.action not in MEM_AUTO_ACTIONS]
    for m in non_autos:  # conflict-cross-file-identity / blocked-* / conflict-content 等：只回報
        report.outcomes.append(ApplyOutcome(m.name, m.action, "reported", m.reason, kind="memory"))
    # 純 hub-only（無 local 端）且無 auto → 無事可做。但**有 local 端時即使無 auto 也要持鎖 reconcile**（修 codex
    # gate #1：只有 conflict/blocked 的專案也須更新 local_memory 到磁碟現況，否則前次失敗留下的 stale 基線永不收斂）。
    if not autos and local_dir is None:
        return False

    try:
        lock = atomicio.FileLock(
            tombstone.tombstones_dir(hub_dir) / "memory").acquire_blocking(timeout_s=lock_timeout_s)
    except atomicio.StaleLock as e:
        for m in autos:
            report.outcomes.append(ApplyOutcome(m.name, m.action, "skipped-stale",
                                                f"memory 鎖疑陳舊，交人工：{e}", kind="memory"))
        if local_dir is not None:  # 沒能取鎖 → presence 未更新（可能仍 stale）→ 促重跑（修 gate #1）
            report.reconcile_failed = True
            report.warnings.append(f"{project_key}: memory 鎖疑陳舊，未更新 local_memory（請重跑 sync）：{e}")
        return False
    except atomicio.LockError as e:
        for m in autos:
            report.outcomes.append(ApplyOutcome(m.name, m.action, "skipped-locked",
                                                f"memory 取鎖逾時，略過：{e}", kind="memory"))
        if local_dir is not None:
            report.reconcile_failed = True
            report.warnings.append(f"{project_key}: memory 取鎖逾時，未更新 local_memory（請重跑 sync）：{e}")
        return False
    try:
        # ① 指紋守衛（掛錯碟）：與 session apply 同基準 base_fp。
        if anomaly.hub_fingerprint(hub_root) != base_fp:
            report.outcomes.append(ApplyOutcome("(memory)", "memory", "halt",
                                                "hub 指紋在 memory apply 中改變（疑掛錯碟），全面中止", kind="memory"))
            return True
        # ② 鎖內最新 state；apply 中途專案變 uninitialized → 不自動套用、不 reconcile（信任邊界，與 F1 一致）。
        cur_state = state_mod.load_or_none(state_path)
        if not tombstone.is_initialized(hub_dir):
            for m in autos:
                report.outcomes.append(ApplyOutcome(m.name, m.action, "blocked-uninitialized",
                                                    "專案未 bootstrap → memory 不自動套用", kind="memory"))
            return False
        has_local_baseline = bool(cur_state and project_key in cur_state.local_memory)
        local_mdir = memory.memory_dir(local_dir) if local_dir is not None else None
        hub_mdir = memory.memory_dir(hub_dir)

        # local/hub memory 夾內 symlink leaf 的 casefold 檔名集（一次算；迴圈不建 symlink 故不失效）。
        _local_msyms = _reparse_safe_symlink_names_cf(local_mdir)
        _hub_msyms = _reparse_safe_symlink_names_cf(hub_mdir)

        def _leaf_symlink(nm):
            """local 或 hub memory 夾中存在 symlink leaf 其**正規化鍵** `_name_key` == `_name_key(nm)` → 不可信：
            `list_memory_files` 略過它（memory.py:394）→ 該 name 在該側「看似 absent」→ 可能誤驅動 local-deleted
            （gate5；casefold-alias gate7；NFC/NFD-alias gate8）或把 hub symlink 當 absent 覆蓋（copy-to-hub，gate6#1）。
            對稱 session apply loop 對**每個 action** 檢查兩側 leaf。統一 fail-closed 略過（不寫/不覆蓋/不當刪除）。
            `_name_key` 同時堵 exact、casefold-alias（`A.md`）與 NFC/NFD-alias（`café.md`）；另保留 exact-path
            `is_symlink()`——iterdir 若因罕見 race 失敗回空集時仍守住確切名的 leaf（gate8 建議）。"""
            k = scan._name_key(nm)
            return (k in _local_msyms or k in _hub_msyms
                    or (local_mdir is not None and (local_mdir / nm).is_symlink())
                    or (hub_mdir / nm).is_symlink())

        def _commit(nm):
            try:
                state_mod.commit_memory(project_key, nm, state_path, lock_timeout_s=lock_timeout_s)
                return None
            except Exception as e:  # noqa: BLE001 - 檔已安全寫入，state 提交失敗：降級回報、不假裝成功
                return str(e)

        def _drift(nm, mp, cur):
            """鎖內權威分類 ≠ plan → 不寫、誠實 surface（含跨檔升級/中途 tombstone）。"""
            if cur is not None and cur.action == "suppressed-deleted":
                return ApplyOutcome(nm, mp.action, "suppressed",
                                    "apply 中出現 memory tombstone → 不復活已刪 memory", kind="memory")
            if cur is not None and cur.action == "conflict-delete-vs-update":
                return ApplyOutcome(nm, cur.action, "reported",
                                    "apply 中 memory tombstone 且內容≠base（刪除衝突）→ 交人（請重跑 sync）", kind="memory")
            cact = cur.action if cur is not None else "（已消失）"
            return ApplyOutcome(nm, mp.action, "skipped-changed",
                                f"memory 重新分類已變（{mp.action}→{cact}），請重跑 sync", kind="memory")

        # ③ 處理 auto 動作（兩階段）。**無 auto 時整段略過，但仍會走到末段 reconcile**（修 codex gate #1）。
        if autos:
            try:
                auth = {m.name: m for m in scan._plan_memories(
                    local_dir, hub_dir, state=cur_state, cov=True,
                    tombs=tombstone.read_tombstones(hub_dir),
                    corrupt=tombstone.corrupt_tombstone_targets(hub_dir))}
            except OSError:
                # memory/ 在 apply 中變 symlink（UnsafeMemoryDir ⊂ OSError）**或不可讀**（權限/陳舊掛載）→ degrade，
                # 不讓 OSError 逸出成 traceback（plan-time build_plan 已對稱 catch OSError；e2e Pass1 Medium）。
                for m in autos:
                    report.outcomes.append(ApplyOutcome(m.name, m.action, "skipped-changed",
                                                        "memory/ 在 apply 中變 symlink 或不可讀 → 不自動處理", kind="memory"))
                auth, autos = {}, []
            # **兩階段**（codex 3b2-R1 #2）：先 local-deleted（寫 tombstone）→ 重跑 auth → 再 copy/identical。
            # local-deleted 寫的 tombstone 改變「別檔」分類基準（identity=None 毒化全專案 copy；decidable → 換檔名復活
            # suppress/conflict）。沿用單趟 auth 邊寫邊用會讓後處理 copy 用過期 auth → 復活；故 tombstone 全寫完重分類。
            deletes = [m for m in autos if m.action == "local-deleted"]
            others = [m for m in autos if m.action != "local-deleted"]

            for mp in deletes:
                name = mp.name
                cur = auth.get(name)
                if cur is None or (cur.action, cur.direction) != (mp.action, mp.direction):
                    report.outcomes.append(_drift(name, mp, cur))
                    continue
                try:
                    # local/hub leaf symlink 防線（e2e gate5 local-deleted + gate3#2 hub-leaf，統一於 _leaf_symlink）：
                    # symlink leaf 被 list_memory_files 略過 → 看似 absent → 誤判 local-deleted。**不可信/讀不到的 leaf
                    # 絕不可**當「使用者確認刪除」而寫抑制 tombstone（fail-closed／A3——untrusted symlink 可指界外；亦不依
                    # 界外內容寫 base_hash/identity）。skip 不寫；末段 reconcile 因該 name 既不在 present（略過）又無
                    # tombstone → 留 pending、baseline 不收斂 → 下次 sync 續 blocked，直到使用者處理該 symlink（**絕不
                    # reconcile 掉**，見 state.reconcile_local_memory_presence 的 pending 語意）。
                    if _leaf_symlink(name):
                        report.outcomes.append(ApplyOutcome(name, mp.action, "skipped-changed",
                                                            "local/hub memory 檔為 symlink（疑逃逸/TOCTOU），不當作刪除、不寫 tombstone", kind="memory"))
                        continue
                    # 本機刪除此 local memory（hub 仍在）→ 寫 hub memory tombstone；**絕不刪 hub**（A3）。base=正規化
                    # content_hash、identity=刪除 doc frontmatter name（皆由鎖內 hub 檔讀；hub 僅 sync 寫、鎖內穩定）。
                    hub_doc = memory.load_memory(hub_mdir / name)
                    tombstone.write_memory_tombstone(
                        hub_dir, name, base_hash=memory.content_hash(hub_doc),
                        machine=machine, identity=hub_doc.name)
                    report.outcomes.append(_mem_done(
                        name, "local-deleted", "tombstoned-local-deletion",
                        "本機刪除已寫 memory tombstone（hub 保留；對側下次 sync 起抑制）",
                        tombstone.memory_tombstone_path(hub_dir, name), _commit(name)))
                except (atomicio.AtomicWriteError, OSError) as e:
                    report.outcomes.append(ApplyOutcome(name, mp.action, "error",
                                                        f"memory tombstone 寫入失敗（已中止該檔）：{e}", kind="memory"))

            # 重跑 auth：只要**嘗試過** local-deleted 就重讀磁碟（即使寫入回報失敗，atomic_write 可能 replace 後才
            # verify 失敗 raise，tombstone 已落地 → copy 必須看見它而被毒化/抑制，修 codex gate #3）。
            if deletes:
                try:
                    auth = {m.name: m for m in scan._plan_memories(
                        local_dir, hub_dir, state=state_mod.load_or_none(state_path), cov=True,
                        tombs=tombstone.read_tombstones(hub_dir),
                        corrupt=tombstone.corrupt_tombstone_targets(hub_dir))}
                except OSError:
                    # 同上：symlink（UnsafeMemoryDir）或不可讀 → degrade。此處在已寫 local-deleted tombstone 之後，
                    # tombstone 已 durable、A3-safe；末段 reconcile 另會因同 OSError 標 reconcile_failed → CLI 非零。
                    for m in others:
                        report.outcomes.append(ApplyOutcome(m.name, m.action, "skipped-changed",
                                                            "memory/ 在 apply 中變 symlink 或不可讀 → 不自動處理", kind="memory"))
                    others = []

            # **有界殘留（codex 3b2-R2 #1，刻意接受並記錄）**：copy 已綁定來源 bytes 到分類 hash（_mem_copy_bytes），
            # 但「跨檔 project-set」維度無法對**外部**寫入完全關閉——project memory 鎖只序列化其他 sync，claude/使用者
            # 不持此鎖。若 auth 後、寫入前 local 冒出**另一個同 frontmatter name 的 sibling 檔**，本 copy 仍會寫出
            # （該檔自身 hash 未變）。harm 有界且非 cardinal sin：至多把一個合法 memory 提早 copy（無復活/無 loss/無 C3
            # 覆蓋/無 A3 刪 hub），**下次 sync 必由 duty(a) 標成 conflict-cross-file-identity**。完全消除需擋外部寫入
            # （不可能）；與威脅模型（plan→apply 單次 in-process、非對抗 hub）一致，同 transfer.py 既有有界殘留立場。
            for mp in others:
                name = mp.name
                cur = auth.get(name)
                # 漂移守衛：鎖內權威分類須與 plan 一致（含跨檔升級 / 被剛寫 tombstone 毒化 → 不再是 copy → 不寫）。
                if cur is None or (cur.action, cur.direction) != (mp.action, mp.direction):
                    report.outcomes.append(_drift(name, mp, cur))
                    continue
                try:
                    # local/hub leaf symlink 防線（e2e gate6#1，對稱 deletes/session）：symlink leaf 被 list_memory_files
                    # 略過 → 看似 absent → copy-to-hub 會把不可信的 hub symlink 當 absent 覆蓋（os.replace 雖不跟隨、無界外
                    # 寫，仍不該悄悄替換使用者 symlink）；copy-to-local 撞 local symlink 同理不自動處理。fail-closed skip。
                    if _leaf_symlink(name):
                        report.outcomes.append(ApplyOutcome(name, mp.action, "skipped-changed",
                                                            "local/hub memory 檔為 symlink（疑逃逸/TOCTOU），不自動處理", kind="memory"))
                        continue
                    if mp.action == "identical":
                        report.outcomes.append(_mem_done(name, "identical", "identical",
                                                         "兩側相同（正規化後），無需寫入", None, _commit(name)))
                    elif mp.action == "copy-to-hub":
                        # 來源 local（使用者/claude 可寫）→ 綁定寫出 bytes 到分類所據（cur.src_hash），不符即中止。
                        raw = _mem_copy_bytes(local_mdir / name, cur.src_hash)
                        if raw is None:
                            report.outcomes.append(ApplyOutcome(name, mp.action, "skipped-changed",
                                                                "來源 memory 於分類後改名/改內容/損壞，中止", kind="memory"))
                        else:
                            dest = hub_mdir / name  # hub 允許覆蓋；分類保證 hub 無此名（sync 互斥於 memory 鎖）
                            atomicio.atomic_write_bytes(dest, raw)
                            report.outcomes.append(_mem_done(name, "copy-to-hub", "copied-to-hub",
                                                             "單邊新 memory 複製到 hub", dest, _commit(name)))
                    elif mp.action == "copy-to-local":
                        # 來源 hub；C3：local 只**建**不覆蓋（O_EXCL），撞名 → keep-both（不重寫內文）。同樣綁定 src_hash。
                        raw = _mem_copy_bytes(hub_mdir / name, cur.src_hash)
                        if raw is None:
                            report.outcomes.append(ApplyOutcome(name, mp.action, "skipped-changed",
                                                                "來源 memory 於分類後改名/改內容/損壞，中止", kind="memory"))
                        else:
                            dest = local_mdir / name
                            try:
                                atomicio.atomic_create_bytes(dest, raw)
                                report.outcomes.append(_mem_done(name, "copy-to-local", "copied-to-local",
                                                                 "hub 單邊新 memory 複製到 local", dest, _commit(name)))
                            except FileExistsError:
                                kp = atomicio.write_keep_both(dest, raw, machine=machine)
                                report.outcomes.append(_mem_done(name, "copy-to-local", "kept-both-local",
                                                                 "local 期間冒出同名 memory → keep-both 不覆蓋", kp, _commit(name)))
                except (atomicio.AtomicWriteError, OSError) as e:
                    report.outcomes.append(ApplyOutcome(name, mp.action, "error",
                                                        f"memory 寫入失敗（已中止該檔，未污染目標）：{e}", kind="memory"))

        # ④ 末：reconcile local_memory presence。**不論有無 auto 都跑**（只要 has_local_baseline + local_dir）：修
        # codex gate #1——只有 conflict/blocked 的專案也須把 local_memory 更新到磁碟現況，否則前次失敗留下的 stale
        # 基線永不收斂 → 復活。tombstoned 取**磁碟上所有有效 memory tombstone**（非僅本次寫的；對稱 session reconcile，
        # 修 codex gate #2——別台/前次的 tombstone + stale 基線才會收斂、bulk 簿記才不被污染）。migration 不悄悄建基線。
        if has_local_baseline and local_dir is not None:
            try:
                present = memory.list_memory_files(local_mdir).keys()
                tombstoned = {t for (k, t) in tombstone.read_tombstones(hub_dir) if k == "memory"}
                state_mod.reconcile_local_memory_presence(
                    project_key, present, tombstoned, state_path,
                    lock_timeout_s=lock_timeout_s, require_baseline=True)
            except memory.UnsafeMemoryDir:
                report.reconcile_failed = True
                report.warnings.append(
                    f"{project_key}: memory/ 變 symlink → 跳過 local_memory 追蹤更新（請重跑 sync 補基線）")
            except Exception as e:  # noqa: BLE001
                # 失敗不擋本次寫入，但**須 CLI 非零**（codex 3b2-R1 #3）：基線沒落地時刪掉剛 copy 的檔 → 下次復活。
                report.reconcile_failed = True
                report.warnings.append(
                    f"{project_key}: local_memory 追蹤更新失敗（檔已寫，但請重跑 sync 補基線）：{e}")

            # ⑤ MEMORY.md 索引機械重建（Block 3c，§7.4 + A14）。鎖內、reconcile 之後、以磁碟現況為據。只重寫工具
            # 自有 auto-block 內容；無標記的手寫索引/標記異常 → 保留原檔、僅警告（plan_index_rebuild 內判）。
            # 索引是**便利性非安全性質**：失敗只警告、**不** set reconcile_failed（不影響 exit code）——最壞索引過時，
            # 已另以警告提示，且不會丟 memory 檔本身（永不覆蓋手寫）。讀不開現有索引（解碼錯/權限）亦退讓不覆蓋。
            idx_path = local_mdir / memory.INDEX_FILE
            # **精確 reparse 分流：junction 跟隨、symlink/cloud/未知 拒絕**（CLAUDE_CONFIG_DIR 模型 + fresh gate
            # ccdir-g1，見 memory.reparse_kind / _is_unfollowable_reparse）：使用者多帳號以 directory junction 在同機
            # 刻意共用 memory/ → 索引照常維護到真實共用夾；④ reconcile 的 list_memory_files 同走 reparse_kind、兩步
            # 一致（不再是舊「偵測後拒絕」極性的 g1「root junction 基線污染」gap）。**拒絕**（do_index=False／④ raise
            # UnsafeMemoryDir）：① memory/ 根為 symlink → 連 `idx_path.read_bytes()` 都會**跟隨根**讀外部 MEMORY.md、
            #   破壞 no-follow（root symlink 在 ④ 已 raise UnsafeMemoryDir 警告、此處不重複）；② cloud/未知 reparse →
            #   可能寫到雲端/非預期目標（fail-closed，g1 Medium）；③ MEMORY.md 自身為 symlink → read 跟隨到外部、
            #   atomic rename 把 symlink 換成普通檔 → 破壞使用者把索引 symlink 到共享的設定。皆先 lstat 擋（寫前再驗防
            #   TOCTOU）。dangling junction（目標離線）由 ④/plan_index_rebuild 的 list_memory_files raise→已比照處理。
            cur_bytes: bytes | None = None
            cur_idx: str | None = None
            do_index = True
            if _is_unfollowable_reparse(local_mdir):
                do_index = False  # 根為 symlink/cloud/未知 reparse（不跟隨；junction 跟隨、不進此分支）
            elif _is_unfollowable_reparse(idx_path):
                do_index = False
                report.warnings.append(
                    f"{project_key}: MEMORY.md 為 symlink/cloud reparse 或無法 lstat → 跳過索引重建（不跟隨、不覆蓋使用者管理的索引設定）。")
            # **以 bytes 讀寫**（非 text mode）：text mode 會把 \r\n/\r 正規化成 \n，令 planner 看不到原始換行 →
            # 寫回 LF-only 會靜默改掉 auto-block **框外**手寫內容的行終止符（違反逐字保留），且重讀守衛也會漏掉
            # 純換行差異（codex R1 High）。故 read_bytes→手動 decode（保留 \r\n）、bytes 比對、atomic_write_bytes。
            if do_index:
                try:
                    cur_bytes = _read_index_bytes_nofollow(idx_path)
                    cur_idx = cur_bytes.decode("utf-8")
                except FileNotFoundError:
                    cur_bytes, cur_idx = None, None  # 不存在 → 走建新路徑
                except (OSError, UnicodeDecodeError) as e:
                    do_index = False
                    report.warnings.append(
                        f"{project_key}: 讀取現有 MEMORY.md 失敗，跳過索引重建（保留原檔不覆蓋）：{e}")
            if do_index:
                try:
                    res = memory.plan_index_rebuild(local_mdir, cur_idx)
                    if res.content is not None:
                        # 寫前 best-effort 重讀（bytes 比對，含行終止符）+ 重驗非 symlink：確認索引自分類後未被外部
                        # （claude/使用者）改動（覆蓋路徑無 O_EXCL 保護，不像 copy-to-local；read-verify-write 精神，
                        # 避免 clobber 手寫，A14）。仍有 sub-ms 殘留窗（重讀→rename），與「外部寫入不持 memory 鎖」同立場。
                        try:
                            latest = _read_index_bytes_nofollow(idx_path)
                        except FileNotFoundError:
                            latest = None
                        except OSError:
                            latest = _IDX_CHANGED  # 讀不回/symlink/特殊檔 → 視為已變動，保守略過不覆蓋
                        data = res.content.encode("utf-8")
                        # 寫前重驗 **memory/ 根 + MEMORY.md** 皆非 symlink/junction（codex fresh gate r6 High + R1）：leaf 的
                        # O_NOFOLLOW 不擋父夾被換成 symlink。**有界殘留**：父夾在此 lstat 與底層 os.open 間的 µs 窗
                        # 被換掉仍可能寫進 symlink 目標——但這與 atomicio 對**所有** memory/session/tombstone 寫入
                        # （copy-to-local 等亦走同樣 path-following 寫、寫的是更關鍵的真實資料）同一類有界殘留，受
                        # per-project 鎖序列化 + 非對抗外部寫入模型約束；索引是衍生資料、危害更小，不為它單獨上 POSIX-
                        # only dir_fd（與全 codebase 一致；對稱 transfer.py 既有有界殘留立場）。create 走 O_EXCL 仍不覆蓋。
                        if _is_unfollowable_reparse(local_mdir) or _is_unfollowable_reparse(idx_path) or latest != cur_bytes:
                            report.warnings.append(
                                f"{project_key}: MEMORY.md 於索引重建期間被改動，略過寫入（保留現檔，下次 sync 再重建）。")
                        elif cur_bytes is None:
                            # **建新走 O_EXCL 只建不覆蓋**（codex 塊末 fresh gate High）：缺檔時若窗內有人建了 markerless
                            # 手寫 MEMORY.md，atomic_write_bytes 的 os.replace 會覆蓋它（違反「markerless 絕不重寫」）。
                            # 改 atomic_create_bytes，撞名→FileExistsError→略過不覆蓋（C3 精神）。
                            try:
                                atomicio.atomic_create_bytes(idx_path, data)
                                report.outcomes.append(ApplyOutcome(
                                    memory.INDEX_FILE, "index", "index-created",
                                    "MEMORY.md 索引已建立", str(idx_path), kind="memory"))
                            except FileExistsError:
                                report.warnings.append(
                                    f"{project_key}: MEMORY.md 於索引建立期間被建立，略過寫入（保留現檔，下次 sync 再判）。")
                        else:
                            # 既有 auto-block → 覆寫（已由精確標記 + bytes 守衛 + 非 symlink 重驗確認是工具自有區）。
                            atomicio.atomic_write_bytes(idx_path, data)
                            report.outcomes.append(ApplyOutcome(
                                memory.INDEX_FILE, "index", "index-" + res.status,
                                "MEMORY.md 索引已重建", str(idx_path), kind="memory"))
                    if res.note:
                        report.warnings.append(f"{project_key}: {res.note}")
                except memory.UnsafeMemoryDir:
                    pass  # reconcile 已就 symlink 警告，不重複
                except (atomicio.AtomicWriteError, OSError, UnicodeError) as e:  # VerifyError⊂AtomicWriteError；
                    # UnicodeError backstop：surrogate 檔名等理論殘留，索引失敗只警告、絕不崩 apply（codex fresh gate）。
                    report.warnings.append(
                        f"{project_key}: MEMORY.md 索引重建失敗（不影響已寫 memory）：{e}")
    finally:
        lock.release()
    return False


def _assess_warnings(local_root: Path, hub_root: Path, state_path) -> list[str]:
    warns: list[str] = []
    targets = [("hub", hub_root), ("local", local_root), ("state", Path(state_path).parent)]
    for label, d in targets:
        a = atomicio.assess_fs(d)
        if not a.can_write:
            warns.append(f"{label} 目標不可寫：{a.reason}")
        elif not a.reliable:
            warns.append(f"{label} FS 不可靠（best-effort + 已保留 rvw+lock）：{a.reason}")
    return warns


def apply_plan(
    plan: scan.SyncPlan, *, local_root, hub_root, config: Config, state: State | None, state_path,
    machine: str | None = None, lock_timeout_s: float = 5.0,
) -> ApplyReport:
    """逐 session 安全落地。回報哪些寫了/略過/中止。任何 halt（前檢或 apply 中指紋變）→ 停、不再寫。"""
    local_root, hub_root = Path(local_root), Path(hub_root)
    report = ApplyReport(outcomes=[])

    # 首次同步（無 state）一律拒絕 --apply：必須先 bootstrap 建立並確認基線（決定 #9）。否則新/重建機器上
    # 若 hub 專案已被別台 bootstrap 過（有 coverage），git-matched 的殘留單邊檔會繞過信任邊界被複製（codex r15-1）。
    if state is None:
        report.halted = True
        report.halt_reason = "首次同步（state 不存在）：請先 `bootstrap` 建立並確認基線，再執行 --apply。"
        return report

    # 掛載/存在性前檢**先於** assess_fs（assess 不建目錄；先確認 hub/local 掛載都在，避免在裸 mountpoint 寫）。
    halts = [f"{a.code}: {a.message}" for a in anomaly.check(state, hub_root) if a.severity == "halt"]
    if not local_root.is_dir():
        halts.append(f"local-mount-missing: local 根不存在或非目錄：{local_root}")
    if halts:
        report.halted = True
        report.halt_reason = "; ".join(halts)
        return report

    report.warnings = _assess_warnings(local_root, hub_root, state_path)
    base_fp = anomaly.hub_fingerprint(hub_root)

    for pp in plan.projects:
        hub_dir = Path(pp.hub_dir) if pp.hub_dir else None
        local_dir = Path(pp.local_dir) if pp.local_dir else None
        # 逃逸重驗（TOCTOU：plan 後夾被換成 symlink/junction 逃出 root，或 build_plan 已標 skipped-unsafe〔空 sessions〕）
        # → 不讀/寫信任根外（e2e gate G-High；apply 是寫入邊界，於 build_plan 過濾外再擋一次，可見 blocked-unsafe 非靜默）。
        if (hub_dir is not None and not scan._safe_project_dir(hub_root, hub_dir)) or \
                (local_dir is not None and not scan._safe_project_dir(local_root, local_dir)):
            for sp in pp.sessions:
                report.outcomes.append(ApplyOutcome(sp.session_id, sp.action, "blocked-unsafe",
                                                    "專案夾是 symlink 或逃逸信任根 → 不自動處理（不讀/寫界外）"))
            for mp in pp.memories:
                report.outcomes.append(ApplyOutcome(mp.name, mp.action, "blocked-unsafe",
                                                    "專案夾是 symlink 或逃逸信任根 → memory 不自動處理", kind="memory"))
            continue
        project_key = hub_dir.name if hub_dir else None
        cwd = _single_cwd(local_dir)

        # F1 信任邊界：未綁定 hub 或專案未 bootstrap → 整個專案不自動套用（含 paired ff/identical）。
        cov = tombstone.is_initialized(hub_dir) if hub_dir else False
        if hub_dir is None or not cov:
            for sp in pp.sessions:
                if sp.action in AUTO_ACTIONS:
                    report.outcomes.append(ApplyOutcome(
                        sp.session_id, sp.action, "blocked-uninitialized",
                        "專案未 bootstrap（或無 hub 綁定）→ 不自動套用；請先 bootstrap"))
                else:
                    report.outcomes.append(ApplyOutcome(sp.session_id, sp.action, "reported", sp.reason,
                                                        project=project_key))
            for mp in pp.memories:  # memory 同信任邊界：未 bootstrap/無綁定 → 不自動套用
                if mp.action in MEM_AUTO_ACTIONS:
                    report.outcomes.append(ApplyOutcome(
                        mp.name, mp.action, "blocked-uninitialized",
                        "專案未 bootstrap（或無 hub 綁定）→ memory 不自動套用；請先 bootstrap", kind="memory"))
                else:
                    report.outcomes.append(ApplyOutcome(mp.name, mp.action, "reported", mp.reason, kind="memory"))
            continue

        # F2：plan-time(T1) 以**同一次讀**重新判定 action+snapshot 作為權威決策（不沿用 build_plan 的 T0），
        # _apply_session 在鎖內(T2)再算一次並比對 → T1→T2 視窗全程受守（codex r11-2）。
        tombs = tombstone.read_tombstones(hub_dir)
        corrupt = tombstone.corrupt_tombstone_targets(hub_dir)
        coll = scan.casefold_collisions_for(local_dir, hub_dir)
        known = state.known_sessions.get(project_key)
        has_baseline = project_key in state.known_sessions
        local_known = state.local_sessions.get(project_key)
        has_local_baseline = project_key in state.local_sessions
        # 不可列舉夾 fail-stop（e2e gate9 finding2 + gate10）：`_session_files`(glob) 對不可讀夾 **fail-open**（吞
        # PermissionError → 回空），`scan._symlink_name_keys` 對不可讀夾亦回空 → (a) 單一 known session 誤判
        # local-deleted 寫抑制 tombstone；(b) casefold/normalization **alias 偵測失效** → 寫出撞名檔（gate10：dest 夾
        # write+execute 但不可讀）。故 **local 或 hub** 專案夾存在但無法列舉 → 跳過**本專案**所有處理（session+memory），
        # 不寫/不 reconcile，fail-closed（可讀但真的空 → 仍走正常刪除偵測，語意不變）。對稱 memory list_memory_files。
        if ((local_dir is not None and not scan._dir_scannable(local_dir))
                or not scan._dir_scannable(hub_dir)):
            for sp0 in pp.sessions:
                report.outcomes.append(ApplyOutcome(
                    sp0.session_id, sp0.action, "skipped-unreadable",
                    "local/hub 專案夾無法列舉（權限/陳舊掛載）→ 不自動處理（不可讀不得誤當空/漏 alias）"))
            for mp0 in pp.memories:
                report.outcomes.append(ApplyOutcome(
                    mp0.name, mp0.action, "skipped-unreadable",
                    "local/hub 專案夾無法列舉（權限/陳舊掛載）→ memory 不自動處理", kind="memory"))
            continue
        bulk = scan.is_bulk_local_deletion(
            local_known, set(scan._session_files(local_dir).keys()) if local_dir else set())
        # local/hub 專案夾內 symlink leaf 的 casefold 檔名集（casefold-alias 偵測，e2e gate7；一次算，迴圈不建 symlink）。
        _local_ssyms = _reparse_safe_symlink_names_cf(local_dir)
        _hub_ssyms = _reparse_safe_symlink_names_cf(hub_dir)
        for sp0 in pp.sessions:
            sid = sp0.session_id
            lf = local_dir / f"{sid}.jsonl" if local_dir else None
            hf = hub_dir / f"{sid}.jsonl"
            # leaf symlink 防線（e2e gate3#1 exact + gate7 casefold-alias + gate8 NFC/NFD-alias）：plan 後／
            # case-sensitive·normalization FS 上 <sid>.jsonl 被換成 symlink（含 casefold-alias `ABC.jsonl` 或
            # normalization-alias）→ `_session_files` 略過、casefold 碰撞偵測只看**列出**名字亦漏 → classify/snapshot
            # 跟隨讀界外或誤判 local-deleted 寫 tombstone。改比對 `_name_key`（NFC+casefold，涵蓋 exact 與 alias）＋
            # 保留 exact-path `lf/hf.is_symlink()`（iterdir 罕見失敗仍守）；先於 classify_session。
            name_key = scan._name_key(f"{sid}.jsonl")
            if (name_key in _local_ssyms or name_key in _hub_ssyms
                    or (lf is not None and lf.is_symlink()) or hf.is_symlink()):
                report.outcomes.append(ApplyOutcome(sid, sp0.action, "skipped-changed",
                                                    "session 檔為 symlink（疑逃逸/TOCTOU/casefold·normalization-alias），略過"))
                continue
            plan_sp = scan.classify_session(
                sid, lf if (lf and lf.exists()) else None, hf if hf.exists() else None,
                both=local_dir is not None, coverage_initialized=cov, tombs=tombs, corrupt=corrupt,
                known=known, has_baseline=has_baseline, is_collision=sid.casefold() in coll,
                local_known=local_known, bulk_local_deletion=bulk, has_local_baseline=has_local_baseline,
            )
            if plan_sp.action not in AUTO_ACTIONS:
                report.outcomes.append(ApplyOutcome(sid, plan_sp.action, "reported", plan_sp.reason,
                                                    project=project_key))
                continue
            plan_snap = compute_decision_snapshot(
                session_id=sid, local_project_dir=local_dir, hub_project_dir=hub_dir,
                config=config, state=state, project_key=project_key, cwd=cwd,
            )
            outcome = _apply_session(
                plan_sp, local_dir=local_dir, hub_dir=hub_dir, project_key=project_key, cwd=cwd,
                plan_snap=plan_snap, config=config, state_path=state_path, hub_root=hub_root,
                base_fp=base_fp, machine=machine, lock_timeout_s=lock_timeout_s,
            )
            if outcome.result == "halt":
                report.outcomes.append(outcome)
                report.halted = True
                report.halt_reason = outcome.detail
                return report
            report.outcomes.append(outcome)

        # 專案末：更新 local-presence 追蹤（供下次 sync 偵測對稱刪除）。傳寫入後 local 現況（re-glob）+ 本專案
        # **已落地** tombstone 的 sid 給 reconcile_local_presence；新 baseline = 現況 ∪「鎖內最新 baseline 中
        # 未落地的本機刪除（不在現況、無 tombstone）」。pending 由 reconcile **鎖內依 disk baseline** 算，故：
        #   - tombstone 寫失敗的 sid 不被悄悄遺忘而復活（codex r24-3）；
        #   - 並發 sync 保留的 pending 不被本 process 的 stale 快照盲覆寫（codex r24-4）；
        #   - bulk-guard 觸發時整批無 tombstone → 全數留在 pending → baseline 不被未受信任現況覆蓋。
        # 只在本機已有**local 基線**時記（has_local_baseline）：migration 專案（無 local 基線）不可在此悄悄
        # 由當前 local 現況建立基線，否則下次 sync 會把 hub-only 檔當新檔復活（codex r24-1）；須重 bootstrap。
        # 失敗不擋本次寫入（staleness 由 tombstone 閘遮蔽，下次 sync 重算）。
        if has_local_baseline and local_dir is not None:
            try:
                present_stems = scan._session_files(local_dir).keys()
                tombstoned = {t for (k, t) in tombstone.read_tombstones(hub_dir) if k == "session"}
                state_mod.reconcile_local_presence(
                    project_key, present_stems, tombstoned, state_path,
                    lock_timeout_s=lock_timeout_s, require_baseline=True)
            except Exception as e:  # noqa: BLE001
                # 失敗不擋本次寫入，但**須 CLI 非零**（codex 3b2-R1 #3）：local-presence 基線沒落地時，使用者在
                # 下次成功 sync 前刪掉剛 copy 的檔 → 下次當新檔復活。非零促其重跑（重跑會補上基線）。
                report.reconcile_failed = True
                report.warnings.append(
                    f"{project_key}: local-presence 追蹤更新失敗（檔已寫，但請重跑 sync 補基線）：{e}")

        # 專案末 memory apply（走獨立 per-project memory 鎖；對稱 session 但跨檔身分需整組原子化，P1d Block 3b-2）。
        if _apply_project_memory(
            pp, report=report, hub_dir=hub_dir, local_dir=local_dir, project_key=project_key,
            state_path=state_path, hub_root=hub_root, base_fp=base_fp, machine=machine,
            lock_timeout_s=lock_timeout_s,
        ):
            report.halted = True
            report.halt_reason = "hub 指紋在 memory apply 中改變（疑掛錯碟），全面中止"
            return report
    return report


def format_report(report: ApplyReport, ack_view=None) -> str:
    """`ack_view`（`acks.AckView`，選配）＝**純呈現層過濾**：`sync --apply` 時隱藏已 `doctor --ack` 的 session 側
    damaged/collision `reported` 行（與 dry-run 的 `scan.format_plan` 一致，讓 apply 也真正不再重報，R1 Medium）。
    **只影響顯示**——apply 實際行為不變（blocked 本就只 report、不寫；三重護欄確保只隱藏 acked 的 reported+ackable 行）。"""
    lines: list[str] = []
    for w in report.warnings:
        lines.append(f"⚠ {w}")
    if report.halted:
        lines.append(f"[HALT] {report.halt_reason}")
        lines.append("→ 偵測到致命異常，已停止；可能已寫入部分 session（見上）。")
    hidden = ack_view.hidden if ack_view else {}
    n_hidden = 0
    for o in report.outcomes:
        # 四重護欄（結構上不可能誤藏）：僅 session 側（memory ack 留 follow-on）、僅 `reported`（apply 沒動它）、
        # 僅 ackable action、且 sid 在**該 outcome 所屬專案**的 acked 隱藏集內——**project-scoped**、不跨專案 flatten
        # （否則跨專案同 sid：A acked、B 未 ack 卻被 A 的 ack 誤藏，g1 Low）。project=None 的 outcome（memory/其他）不隱藏。
        if (o.kind != "memory" and o.result == "reported" and o.action in acks.ACKABLE_ACTIONS
                and o.project is not None and o.session_id in hidden.get(o.project, ())):
            n_hidden += 1
            continue
        label = f"memory {o.session_id}" if o.kind == "memory" else o.session_id[:8]
        lines.append(f"  - {label}: [{o.result}] {o.action} — {o.detail}"
                     + (f"  → {o.path}" if o.path else ""))
    if n_hidden:
        lines.append(f"  · （{n_hidden} 項 damaged/collision 已 acknowledged，未列出）")
    c = report.counts()
    if c:
        lines.append("\n摘要：" + "；".join(f"{k}={v}" for k, v in sorted(c.items())))
    return "\n".join(lines) if lines else "（無可套用項）"
