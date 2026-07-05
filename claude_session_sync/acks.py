"""ack 帳本：對「工具永遠無法自動解決」的 blocked 項（damaged / casefold-collision / identity-collision）
記錄「已審閱」，讓 doctor/sync 不再反覆回報（DESIGN 附錄 A15「blocked 收斂出口」）。

**安全鐵則**：
  1. **純呈現層**：ack 只抑制「回報」，**絕不改變分類**——acked 的 damaged 仍永不同步、acked 的 collision 仍永不
     自動合併。本帳本從不進 `build_plan`/`classify`/`apply`，故結構上不可能把 blocked 變成 auto-apply。呈現層
     （`format_plan` / `doctor.diagnose`）以 `AckView` 隱藏/降級 acked 項，分類與寫入路徑一律看不到本模組。
  2. **fingerprint 綁定**：ack 記 `(kind, identity, fingerprint)`。現況 fingerprint 不符（damaged 檔內容改、
     撞名集合變）→ 視為**未** ack、照常重報——不遮蓋新的/變動過的問題。
  3. **fail-closed 讀**：帳本缺 → 無 ack（全部照常回報，`ok=True`）；壞/讀不到 → 忽略整本（`ok=False`，呼叫端
     警告，仍全部照常回報）；壞條目 → 跳過該條、不毒化整本。`.tombstones/` 為 symlink/逃逸 → **不信任、不
     suppress**（回空、`ok=True`）。任一路徑都**只會少 suppress、不會多 suppress**。
  4. **A3 不丟**：ack 只寫一個 hub 側 per-project JSON（`<proj>/.tombstones/acks.json`），**絕不動 session/
     memory 檔**。落點在 `.tombstones/`（已被 scan 排除、不會被當 session/專案）；寫走 atomicio 原子寫 + 專屬鎖。

範圍（v1，session 側）：casefold-collision（A9 檔名撞名，兩份都是真 session）、damaged / blocked-damaged-source
（壞 JSONL）、identity-collision（同 uuid 異 hash 的內容身分衝突）。memory 側對應項留 follow-on。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import atomicio, pathsafe, scan, tombstone

SCHEMA_VERSION = 1
ACKS_FILE = "acks.json"

# ackable 的 plan action → ack kind。casefold-collision 以 casefold key 為身分（一項含整組 sid）；
# damaged / identity-collision 以 sid 為身分（指紋看兩側檔 bytes）。
_ACTION_KIND: dict[str, str] = {
    "blocked-casefold-collision": "casefold-collision",
    "blocked-damaged-source": "damaged",
    "damaged": "damaged",
    "identity-collision": "identity-collision",
}
_KINDS = frozenset(_ACTION_KIND.values())
# 可 ack 的 plan action 字串（供 apply.format_report 呈現層過濾）。**單一真相源在 `scan.ACKABLE_ACTIONS`**（scan 被
# acks 依賴、不可反向 import）；`_ACTION_KIND` 的鍵須與之一致（test_acks 有漂移守衛）。
ACKABLE_ACTIONS = scan.ACKABLE_ACTIONS


class UnsafeAcksDir(OSError):
    """`<proj>/.tombstones` 是 symlink 或逃逸專案夾（指界外）→ 拒寫 acks（否則寫到信任根外）。"""


@dataclass(frozen=True)
class AckItem:
    """一個可 ack 的 blocked 項（doctor / format_plan / ack 寫入的**單一真相源**，由 `ackable_from_plan` 產）。"""
    project: str                    # hub 專案夾名（pk）
    hub_dir: str                    # hub 專案夾路徑；ledger 落點 = <hub_dir>/.tombstones/acks.json
    kind: str                       # casefold-collision | damaged | identity-collision
    identity: str                   # collision: casefold key；damaged/identity: sid
    fingerprint: str | None         # 現況指紋（見 fingerprint_*）；None＝不可綁定內容（讀不到）→ 不可 ack、不被隱藏（g6）
    session_ids: tuple[str, ...]    # 此項涵蓋的 sid（collision=整組；其餘=單一）——供呈現層隱藏對應行
    label: str                      # 顯示用短標籤


@dataclass
class Ledger:
    """載入後的 acks.json。`by_key` 以 **(kind, identity, fingerprint) 三元組**為鍵（同一 (kind,identity) 可有多個
    fingerprint 並存，g2）；`ok=False` 表帳本損壞/讀不到（已忽略，呼叫端警告）。"""
    by_key: dict[tuple[str, str, str], dict] = field(default_factory=dict)
    ok: bool = True
    path: Path | None = None


# ── fingerprint ─────────────────────────────────────────────────────────────

def fingerprint_collision(names, local_files=None, hub_files=None) -> str | None:
    """撞名項指紋 = 撞名集（排序拼法）＋**各撞名檔兩側 raw bytes digest**。新拼法加入/移除 → 集變 → 指紋變；
    某撞名檔內容改（變 damaged／換成別的 session）→ digest 變 → 指紋變 → 重報。**為何也綁內容**（g4）：
    `classify_session` 的撞名閘**先於** damaged 閘 → 撞名檔之一變壞仍分類為 collision，若 fp 只看名稱，collision ack
    會遮蓋「撞名檔變 damaged」這個新情況；綁內容使「撞名檔變了」重新提示。**回 `None`** 若某撞名檔 present 但讀不到
    （不可綁定內容 → 不列為 ackable，fail-closed，g5 Medium）。`*_files` 由 `scan._session_files` 提供（已排除 symlink）；
    未給（如非比對用的直接呼叫）→ 視為兩側皆無檔。"""
    lf, hf = local_files or {}, hub_files or {}
    parts = ["\n".join(sorted(names))]
    for n in sorted(names):
        fpf = fingerprint_files(lf.get(n), hf.get(n))
        if fpf is None:
            return None                         # 某撞名檔讀不到 → 不可綁定 → 不可 ack（fail-closed）
        parts.append(fpf)
    return "cf:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def fingerprint_files(*paths: Path | None) -> str | None:
    """damaged / identity-collision 指紋 = 兩側檔 raw bytes digest 的組合。任一側內容改/消失 → 指紋變 → 重報。
    **回 `None`** 若某 present 檔讀不到（unreadable，`raw_file_digest`→None）→ 呼叫端（ackable_from_plan）視為
    **不可綁定內容 → 不列為 ackable、永遠照報**（fail-closed，g5 Medium：不可綁定則 fp 無法反映內容變動，read-denied
    檔內容變會被舊 ack 遮蓋）。缺檔（None path）→ "-"（可綁定為「該側無此檔」）。path 僅由 `scan._session_files`
    提供（已排除 symlink），故 `raw_file_digest`（會跟隨 symlink）不會讀到界外檔。"""
    parts: list[str] = []
    for p in paths:
        if p is None:
            parts.append("-")                       # 該側無此檔（可綁定）
        else:
            d = tombstone.raw_file_digest(p)
            if d is None:
                return None                         # present 但讀不到 → 不可綁定內容（fail-closed）
            parts.append(d)
    return "fs:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ── 從 plan 抽 ackable 項（單一真相源）─────────────────────────────────────────

def ackable_from_plan(plan) -> list[AckItem]:
    """從 `SyncPlan` 抽出所有可 ack 的 session 側 blocked 項。**只含有 hub_dir 的專案**（ack 帳本 hub 側；
    local-only 專案尚未上 hub、還不是同步問題，不納入）。撞名依 casefold key 併組（整組一項）。"""
    out: list[AckItem] = []
    for pp in plan.projects:
        if not pp.hub_dir:
            continue
        pk = Path(pp.hub_dir).name
        coll: dict[str, set[str]] = {}
        filebased: list[tuple[str, str]] = []   # (sid, kind) for damaged / identity-collision
        for s in pp.sessions:
            kind = _ACTION_KIND.get(s.action)
            if kind is None:
                continue
            if kind == "casefold-collision":
                coll.setdefault(s.session_id.casefold(), set()).add(s.session_id)
            else:
                filebased.append((s.session_id, kind))
        if not coll and not filebased:
            continue
        # collision 與 damaged/identity 皆需讀撞名/壞檔內容算指紋（g4：collision fp 亦綁內容）→ 有 ackable 項就讀。
        # 用 `_session_files`（已排除 symlink）取真實檔路徑，確保指紋只看工具本就會處理的實體檔。
        local_files = scan._session_files(Path(pp.local_dir)) if pp.local_dir else {}
        hub_files = scan._session_files(Path(pp.hub_dir))
        # 不可綁定（讀不到內容）→ fp=None：**仍列出**（不 skip）。fingerprint=None → is_acked 恆 False → compute_ack_view
        # 不會隱藏該 (pk,sid)（fail-closed，g6：若整個 skip 掉，同 hub 另一 view 對同 sid 的 ack 會讓此 sid 看似「所有涵蓋
        # 項都已 ack」而誤藏這個不可綁定行）。`_doctor_ack` 另會濾掉 fp=None 者（不可 ack、無從綁定內容變動）。
        for cf, sids in sorted(coll.items()):
            names = sorted(sids)
            out.append(AckItem(pk, pp.hub_dir, "casefold-collision", cf,
                               fingerprint_collision(names, local_files, hub_files), tuple(names),
                               "/".join(n[:8] for n in names)))
        for sid, kind in sorted(filebased):
            out.append(AckItem(pk, pp.hub_dir, kind, sid,
                               fingerprint_files(local_files.get(sid), hub_files.get(sid)), (sid,), sid[:8]))
    return out


# ── 帳本讀（lock-free、fail-closed）────────────────────────────────────────────

def load_ledger(hub_project_dir) -> Ledger:
    """讀 `<hub_project_dir>/.tombstones/acks.json`。fail-closed：缺 → 空 `ok=True`；壞/讀不到 → 空 `ok=False`；
    `.tombstones` 不安全 → 空 `ok=True`（不信任、不 suppress）。**任一情況都只會少 suppress，不會多 suppress**。"""
    tdir = tombstone.tombstones_dir(hub_project_dir)
    path = tdir / ACKS_FILE
    if not tombstone._tombstones_ok(hub_project_dir):
        return Ledger({}, True, path)          # symlink/逃逸 .tombstones → 不信任其內容、不 suppress
    if pathsafe.is_reparse(path):
        # `.tombstones` 是真夾但 `acks.json` **本身**是 symlink/reparse → `read_bytes()` 會跟隨讀界外／被植入的帳本
        # → 不信任、不 suppress（fail-closed，g1 High；leaf 防線，補 `_tombstones_ok` 只管父夾）。
        return Ledger({}, True, path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Ledger({}, True, path)          # 尚無帳本 → 無 ack（正常）
    except OSError:
        return Ledger({}, False, path)         # 讀不到（權限等）→ fail-closed 忽略
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return Ledger({}, False, path)         # 壞 JSON → 忽略整本
    version = obj.get("version") if isinstance(obj, dict) else None
    # `type(version) is int`：**拒 bool/float**——Python 中 `True == 1`、`1.0 == 1` 皆真，若只寫 `version == 1`
    # 則 `{"version": true}` / `{"version": 1.0}` 會被當合法版本放行 → 壞帳本可 suppress 真問題（R1 High#1，
    # 對稱 tombstone `_valid_tombstone` 的型別 fail-closed）。
    if not (type(version) is int and version == SCHEMA_VERSION and isinstance(obj.get("acks"), list)):
        return Ledger({}, False, path)         # 版本不符/型別錯/結構壞 → 忽略整本
    by_key: dict[tuple[str, str, str], dict] = {}
    for rec in obj["acks"]:
        if not isinstance(rec, dict):
            continue                            # 壞條目跳過（不毒化整本）
        k, i, fp = rec.get("kind"), rec.get("identity"), rec.get("fingerprint")
        if isinstance(k, str) and isinstance(i, str) and isinstance(fp, str) and k in _KINDS:
            by_key[(k, i, fp)] = rec            # 以 **(kind,identity,fingerprint) 三元組**為鍵：同一 (kind,identity)
            #   可有多個 fp 並存（同 hub 專案被多個 local 夾映射時 damaged 的內容 fp 不同，g2）——不互蓋。
    return Ledger(by_key, True, path)


def is_acked(ledger: Ledger, kind: str, identity: str, fingerprint: str | None) -> bool:
    """該 (kind, identity, fingerprint) 三元組是否在帳本內（**fp-exact**：指紋不符＝內容/撞名集已變 → 未 ack）。
    `fingerprint=None`（不可綁定內容）→ **恆 False**（fail-closed：讀不到內容者不可能『已 ack』、也不該被隱藏，g6）。"""
    return fingerprint is not None and (kind, identity, fingerprint) in ledger.by_key


# ── ack view（呈現層過濾：format_plan / doctor 共用）────────────────────────────

@dataclass
class AckView:
    """呈現層過濾視圖。`hidden[pk]` = 該專案要隱藏的 session_id 集（見 compute_ack_view 的 fail-safe 規則）；
    `corrupt_projects` = 帳本損壞的專案（呼叫端警告）。隱藏行數由各 renderer 自行計。"""
    hidden: dict[str, set[str]] = field(default_factory=dict)
    corrupt_projects: list[str] = field(default_factory=list)


def compute_ack_view(plan) -> AckView:
    """對 plan 的每個 ackable 項查對應專案帳本（每專案讀一次），算出要隱藏的 session_id。純讀、lock-free。

    **fail-safe 隱藏**：某 `(pk, sid)` 只在「**涵蓋它的所有 ackable 項都 fp-exact 已 ack**」時才隱藏——否則不藏。
    因同一 hub 專案可被多個 local 夾映射（兩 cwd 綁定／兩 clone），同一 sid 的 damaged 內容 fp 可不同 → 兩個
    AckItem 共用 `(pk, sid)`；若只 ack 其一就用 `(pk,sid)` 藏，會誤藏另一個未 ack 的（遮蓋真問題，g2 High）。
    寧可多顯示已 ack 項，也不誤藏未 ack 的同 sid 項。（collision 的 fp 只看名稱集、跨 view 相同，本規則對它無副作用。）"""
    view = AckView()
    by_dir: dict[str, list[AckItem]] = {}
    for it in ackable_from_plan(plan):
        by_dir.setdefault(it.hub_dir, []).append(it)
    for hub_dir, its in by_dir.items():
        led = load_ledger(hub_dir)
        pk = its[0].project
        if not led.ok:
            view.corrupt_projects.append(pk)
        sid_all_acked: dict[str, bool] = {}     # sid → 涵蓋它的**所有**項是否都已 ack（AND）
        for it in its:
            acked = is_acked(led, it.kind, it.identity, it.fingerprint)
            for sid in it.session_ids:
                sid_all_acked[sid] = sid_all_acked.get(sid, True) and acked
        hide = {sid for sid, ok in sid_all_acked.items() if ok}
        if hide:
            view.hidden.setdefault(pk, set()).update(hide)
    return view


# ── 帳本寫（加鎖 read-modify-write、atomic）─────────────────────────────────────

@dataclass
class UpdateResult:
    added: list[str] = field(default_factory=list)      # 新 ack / 更新指紋的 label
    removed: list[str] = field(default_factory=list)     # 取消 ack 的 label
    unchanged: list[str] = field(default_factory=list)   # 已 ack 且指紋相符（無變更）
    replaced_corrupt: bool = False                       # 原帳本損壞、已以新內容取代（呼叫端告知）


def update_ledger(hub_project_dir, *, add=(), remove=(), lock_timeout_s: float = 5.0) -> UpdateResult:
    """加鎖 read-modify-write 一個專案帳本。`add`：`list[AckItem]`（以 (kind,identity,fingerprint) 三元組為鍵——
    同 (kind,identity) 不同 fp **並存不互蓋**，g2）；`remove`：`list[(kind, identity, fingerprint)]`（load_ledger 暴露
    的三元組鍵）。鎖內重讀（並發 ack 合併、不互蓋）；原子寫。`.tombstones` 不安全 → raise。"""
    tdir = tombstone.tombstones_dir(hub_project_dir)
    if not tombstone._tombstones_ok(hub_project_dir):
        raise UnsafeAcksDir(f".tombstones 為 symlink 或逃逸專案夾，拒絕寫入 acks：{tdir}")
    path = tdir / ACKS_FILE
    res = UpdateResult()
    lock = atomicio.FileLock(path).acquire_blocking(timeout_s=lock_timeout_s)
    try:
        led = load_ledger(hub_project_dir)     # 鎖內重讀
        res.replaced_corrupt = not led.ok      # 損壞帳本會被本次寫入取代（原本就已被忽略、不 suppress 任何項）
        by_key = dict(led.by_key)
        for it in add:
            key = (it.kind, it.identity, it.fingerprint)
            if key in by_key:
                res.unchanged.append(it.label)
                continue
            by_key[key] = {
                "kind": it.kind, "identity": it.identity, "fingerprint": it.fingerprint,
                "label": it.label, "acked_at": tombstone.now_iso(),
                "acked_by": tombstone.local_machine_id(),
            }
            res.added.append(it.label)
        for key in remove:
            rec = by_key.pop(key, None)
            if rec is not None:
                res.removed.append(rec.get("label") or key[1])
        _write_ledger(path, by_key)
        return res
    finally:
        lock.release()


def _write_ledger(path: Path, by_key: dict[tuple[str, str, str], dict]) -> None:
    acks = sorted(by_key.values(),
                  key=lambda r: (r.get("kind", ""), r.get("identity", ""), r.get("fingerprint", "")))
    obj = {"version": SCHEMA_VERSION, "acks": acks}
    atomicio.atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))
