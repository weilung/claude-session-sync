"""Tombstone（刪除標記）+ coverage epoch。hub `<proj>/.tombstones/`，append-only。

依據 DESIGN 附錄 A3/A17.1/A17.3 + PLAN v0.5 §2.7（codex r3 bootstrap/coverage、r3 digest 含 coverage）：
  - conditional **suppress**：只抑制復活，**永不自動刪 local**；預設**永不自動 GC**（A17.3）。
  - 偵測一律查 **hub tombstone**，不依賴 per-machine state（A17.1）。
  - 未 initialized（無 `_coverage.json`）的 project → 上層應 blocked，除非 `--bootstrap`（codex r3）。
  - `tombstone_dir_digest` **含 `_coverage.json`**、排除 temp/lock（供決策快照偵測 epoch 變動，codex r3）。

P1a：讀 + digest（+ 提供 bootstrap/標記用的簡單 atomic write 原語）；conditional suppress 套用是 P1b。
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import atomicio
from .pathsafe import dir_scannable, safe_project_dir

SCHEMA_VERSION = 1
TOMB_DIR = ".tombstones"


class UnsafeTombstonesDir(OSError):
    """`<proj>/.tombstones` 是 symlink 或逃逸專案夾（指向界外）。拒絕跟隨——否則讀界外 coverage/tombstone 當
    決策輸入、或寫 coverage/tombstone 到界外（e2e gate3 #3）。子類 OSError → 寫入端 raise、呼叫端既有 except
    OSError 捕捉；讀取端 fail-closed（coverage → None ⇒ 專案視為未初始化 ⇒ 上層 blocked，不復活/不自動套用）。"""
COVERAGE_FILE = "_coverage.json"
_TOMB_SUFFIX = ".deleted.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def raw_file_digest(path: str | os.PathLike) -> str | None:
    """檔 bytes 的 sha256（**原始位元組**，非語意 content hash）。對任何可讀檔都可算（含 JSON 壞行/
    空白/0-byte）；讀不到 → None。

    base_hash 與條件式 suppress 用**同一基準**（raw bytes）：bootstrap 記 base_hash 用它（codex r9-4），
    P1c suppress 比對現存側也用它（同 hash 空間才可比）。保守取捨——純編碼往返（CRLF/BOM）會讓 raw 不等
    → 轉 conflict 交人，**寧可多問、不靜默復活也不靜默丟更新**（A3）。"""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def local_machine_id() -> str:
    return socket.gethostname() or "unknown"


def tombstones_dir(project_dir: str | os.PathLike) -> Path:
    return Path(project_dir) / TOMB_DIR


def _tombstones_ok(project_dir: str | os.PathLike) -> bool:
    """`.tombstones` 是否安全在 project_dir 內（非 symlink、resolve 後不逃逸）。不存在（首寫前）→ 安全（字面在內）。"""
    return safe_project_dir(project_dir, tombstones_dir(project_dir))


def tombstones_enumerable(project_dir: str | os.PathLike) -> bool:
    """`.tombstones/` 是否**安全且可完整列舉**——供**不 gate on coverage** 的消費者（transfer）在信任
    「`read_tombstones` 回傳的集合是完整的」之前檢查。False 有兩因，皆須 fail-closed（否則漏刪除標記 → 復活已刪，A3）：
      ① `_tombstones_ok` False（`.tombstones` 是 symlink/逃逸）→ read_tombstones 回 {}，但那是**拒讀界外**、非「真的沒有」；
      ② 不可列舉（POSIX read-denied）→ read_tombstones 的 glob **fail-open** 漏標記。
    主 sync/resolve 由 `read_coverage`（內含本檢查）→ `is_initialized` 擋；transfer 無 coverage gate，直接用本函式（e2e gate12）。"""
    return _tombstones_ok(project_dir) and dir_scannable(tombstones_dir(project_dir))


def _atomic_write_json(path: Path, obj: dict) -> None:
    # `.tombstones` 逃逸專案夾（symlink/junction 指界外）→ **拒寫界外**（e2e gate3 #3）；path.parent=.tombstones、
    # path.parent.parent=專案夾。走 atomicio（fsync + 讀回驗），確保不寫出半截/未落地的 tombstone/coverage（codex r11-3）。
    tdir = Path(path).parent
    if not safe_project_dir(tdir.parent, tdir):
        raise UnsafeTombstonesDir(f".tombstones 為 symlink 或逃逸專案夾，拒絕寫入：{tdir}")
    atomicio.atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


# ── coverage ─────────────────────────────────────────────────────────────

@dataclass
class Coverage:
    initialized: bool
    epoch: int
    bootstrap_time: str | None
    machine: str | None


def read_coverage(project_dir: str | os.PathLike) -> Coverage | None:
    # `.tombstones/` symlink/逃逸（e2e gate3 #3，不讀界外假 coverage）或**不可列舉**（e2e gate11 finding1，glob
    # fail-open 漏刪除標記 → 復活）→ 回 None（→ is_initialized False → 專案 blocked，覆蓋 build_plan/apply/resolve
    # 等所有 coverage-gated 路徑）。transfer 不 gate on coverage，另在 _apply_one 直接用 `tombstones_enumerable`。
    if not tombstones_enumerable(project_dir):
        return None
    p = tombstones_dir(project_dir) / COVERAGE_FILE
    if not p.exists() or p.is_symlink():   # leaf：_coverage.json 為 symlink → 不信界外 coverage（e2e gate4 #1）
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    # 嚴格驗型別、fail-closed：壞/竄改的 coverage 不可被當成「已 bootstrap」而放行單邊複製（codex r13-2，
    # 同 config force_unsafe_lock="false" 的陷阱——bool("false")=True）。任何不符 → 回 None（視為未初始化）。
    if not isinstance(d, dict):
        return None
    init, epoch = d.get("initialized"), d.get("epoch")
    if not isinstance(init, bool):
        return None
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        return None
    bt, mc = d.get("bootstrap_time"), d.get("machine")
    if not (bt is None or isinstance(bt, str)) or not (mc is None or isinstance(mc, str)):
        return None
    return Coverage(initialized=init, epoch=epoch, bootstrap_time=bt, machine=mc)


def is_initialized(project_dir: str | os.PathLike) -> bool:
    cov = read_coverage(project_dir)
    return bool(cov and cov.initialized)


def write_coverage(project_dir: str | os.PathLike, epoch: int = 1,
                   machine: str | None = None, when: str | None = None) -> None:
    _atomic_write_json(
        tombstones_dir(project_dir) / COVERAGE_FILE,
        {
            "schema_version": SCHEMA_VERSION,
            "initialized": True,
            "epoch": epoch,
            "bootstrap_time": when or now_iso(),
            "machine": machine or local_machine_id(),
        },
    )


# ── tombstones ───────────────────────────────────────────────────────────

@dataclass
class Tombstone:
    kind: str          # "session" | "memory"
    target: str        # sessionId 或 memory 檔名
    base_hash: str | None
    machine: str | None
    time: str | None
    identity: str | None = None  # **memory 專屬**：刪除時的 frontmatter `name`（跨檔身分，A14/§7.2.3）。
    #   讓「已刪事實換檔名復活」可被偵測（present 檔的 frontmatter name 命中此值、即使檔名不同 → 不復活，
    #   P1d Block 2b duty b）。session 恆 None（無 frontmatter 身分）。schema 末端 + default → 向後相容。


def _mem_file(name: str) -> str:
    safe = name.replace("/", "_").replace("\\", "_")
    return f"memory-{safe}.deleted.json"


def is_tombstone_safe_name(name: str) -> bool:
    """memory 檔名能否與其 tombstone 檔名**無損 round-trip**。`_mem_file` 對斜線/反斜線有損 sanitize（兩者在
    不同 OS 皆可能是路徑分隔）→ 含這些字元的名稱無法由 tombstone 檔名還原身分：read 端 `target == ftarget`
    會把合法刪除標記判成「corrupt 於錯誤身分」，而真實檔仍無 tombstone → 可能復活（codex P1d gate）。
    真實 memory 檔名為 slug（無分隔字元）；含者由上層 blocked（不複製、不寫 tombstone），徹底解＝可逆檔名
    編碼（留後續）。判據刻意綁定 `_mem_file` 的 sanitize 字元集，避免兩處漂移。"""
    return "/" not in name and "\\" not in name


def _sess_file(sid: str) -> str:
    return f"{sid}.deleted.json"


def _filename_identity(base: str) -> tuple[str, str] | None:
    """由 tombstone 檔名（去 .deleted.json 後的 base）推身分。`<sid>`→session；`memory-<safe>`→memory。"""
    if base.startswith("memory-"):
        return ("memory", base[len("memory-"):])
    return ("session", base) if base else None


def _valid_tombstone(path: Path) -> Tombstone | None:
    """嚴格解析一個 tombstone 檔。回有效 Tombstone 或 None（壞/語意不符）。

    **檔名命名空間為準**（檔名=身分，spike-3）：`<sid>.deleted.json`=session、`memory-*`=memory。
    內容 `kind` 必須**符合檔名命名空間**（否則 `secret.deleted.json` 內容寫 `{"kind":"memory"}` 會繞過
    session 比對、讓 secret 復活，codex r13 fail-closed）。**且內容 `target` 必須精確 == 檔名身分 `ftarget`**
    （session=檔名 sid、memory=`memory-` 與 `.deleted.json` 之間那段）——session 與 memory 一律如此（統一規則）。
    否則 `memory-secret.md.deleted.json` 寫 `{"target":"other.md"}` 會被當 `other.md` 的有效 tombstone，而
    `secret.md` 既無有效 tombstone 也不進 corrupt → 單邊 `secret.md` 復活（P1d Block 2 起 memory tombstone 進
    classify，須與 session 對稱 fail-closed，codex P1d-r1）。
    用 `== ftarget`（非 `_mem_file(target) == 檔名`）才**完整**：後者會放行 sanitize 撞名的非扁平 target（如
    `target="a/b.md"` 映射回 `memory-a_b.md...` → 卻記成 `("memory","a/b.md")`，真扁平檔 `a_b.md` 仍無 tombstone，
    codex P1d-r2）。真實 memory 檔名恆扁平（檔名不含路徑分隔），故 `target == ftarget` 不會誤殺合法 tombstone；
    含斜線或反斜線的 target 一律落 corrupt。可逆檔名編碼留後續。
    """
    if Path(path).is_symlink():
        return None   # leaf：symlink tombstone → 不跟隨讀界外（→ 落 corrupt_tombstone_targets/blocked，fail-closed，e2e gate4 #1）
    fid = _filename_identity(path.name[: -len(_TOMB_SUFFIX)])
    if fid is None:
        return None
    fkind, ftarget = fid
    try:
        o = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(o, dict):
        return None
    kind, target = o.get("kind"), o.get("target")
    if kind != fkind or not isinstance(target, str) or not target:
        return None  # 內容 kind 必須符合檔名命名空間；target 須非空字串
    if target != ftarget:
        return None  # 內容 target 與檔名身分不符（竄改/半寫/sanitize 撞名）→ 損壞、fail-closed（session r13 + memory P1d-r2）
    # base_hash/machine/time 須 **None 或字串**（fail-closed，codex gate7 F2）：下游（memory-merge `_short`/`_disp`、
    # session suppress 比對）皆假設字串；`"base_hash": 123` 等型別錯的 tombstone 會讓 `_short` 切片整數而崩 dry-run/
    # 提示詞。型別不符 → 回 None（落 corrupt_tombstone_targets → 上層 blocked-tombstone-corrupt，不放行給 merge）。
    for v in (o.get("base_hash"), o.get("machine"), o.get("time")):
        if not (v is None or isinstance(v, str)):
            return None
    # identity（**memory 專屬**跨檔身分，A14）：僅 memory kind 接受非空字串、否則 None（與 MemoryDoc.name 對稱——
    # 空/缺/型別錯一律當「無身分」，不 fail-closed〔tombstone 仍是合法的檔名鍵刪除標記、只是不參與 identity 配對〕；
    # session kind 一律 None〔無 frontmatter 身分，codex P1d gate2〕）。**slug 形驗證在 memory.py 消費端**（非 slug
    # → 當不可判），此處只做型別/命名空間把關。Block 3 寫 memory tombstone 時恆填刪除檔的 name slug。
    ident = o.get("identity")
    identity = (ident if isinstance(ident, str) and ident.strip() else None) if fkind == "memory" else None
    return Tombstone(kind=fkind, target=target, base_hash=o.get("base_hash"),
                     machine=o.get("machine"), time=o.get("time"), identity=identity)


def read_tombstones(project_dir: str | os.PathLike) -> dict[tuple[str, str], Tombstone]:
    """回 {(kind, target): Tombstone}。只收**嚴格有效**者；壞/語意不符者個別略過（由 corrupt_… 阻擋）。"""
    if not _tombstones_ok(project_dir):
        return {}   # .tombstones 逃逸 → 不讀界外（專案已由 coverage gate blocked；e2e gate3 #3）
    d = tombstones_dir(project_dir)
    out: dict[tuple[str, str], Tombstone] = {}
    if not d.exists():
        return out
    for p in sorted(d.glob("*" + _TOMB_SUFFIX)):
        t = _valid_tombstone(p)
        if t is not None:
            out[(t.kind, t.target)] = t
    return out


def corrupt_tombstone_targets(project_dir: str | os.PathLike) -> set[tuple[str, str]]:
    """回「存在但非嚴格有效」的 tombstone 之**檔名推定身分**（內容壞/型別錯/session 身分不符）。

    供上層把「壞掉的刪除標記」當**阻擋**而非「沒有標記」——否則半截/竄改/檔名內容不符的
    `<sid>.deleted.json` 會被當作「無 tombstone」而讓單邊檔復活（codex r11-3 / r12，fail-closed）。
    """
    if not _tombstones_ok(project_dir):
        return set()   # .tombstones 逃逸 → 不讀界外（e2e gate3 #3）
    d = tombstones_dir(project_dir)
    out: set[tuple[str, str]] = set()
    if not d.exists():
        return out
    for p in sorted(d.glob("*" + _TOMB_SUFFIX)):
        if _valid_tombstone(p) is not None:
            continue
        fid = _filename_identity(p.name[: -len(_TOMB_SUFFIX)])
        if fid is not None:
            out.add(fid)
    return out


def session_tombstone_path(project_dir, sid: str) -> Path:
    """該 session tombstone 的完整路徑（命名與 write_session_tombstone 一致）。供回報/定位用。"""
    return tombstones_dir(project_dir) / _sess_file(sid)


def memory_tombstone_path(project_dir, name: str) -> Path:
    """該 memory tombstone 的完整路徑（命名與 write_memory_tombstone 一致）。供回報/定位用。"""
    return tombstones_dir(project_dir) / _mem_file(name)


def find_session_tombstone(project_dir, sid: str) -> Tombstone | None:
    return read_tombstones(project_dir).get(("session", sid))


def find_memory_tombstone(project_dir, name: str) -> Tombstone | None:
    return read_tombstones(project_dir).get(("memory", name))


def write_session_tombstone(project_dir, sid: str, base_hash: str | None,
                            machine: str | None = None, when: str | None = None) -> None:
    _atomic_write_json(
        tombstones_dir(project_dir) / _sess_file(sid),
        {
            "schema_version": SCHEMA_VERSION, "kind": "session", "target": sid,
            "base_hash": base_hash, "machine": machine or local_machine_id(),
            "time": when or now_iso(),
        },
    )


def write_memory_tombstone(project_dir, name: str, base_hash: str | None,
                           machine: str | None = None, when: str | None = None,
                           identity: str | None = None) -> None:
    """寫 memory tombstone。`identity` = 刪除檔的 frontmatter `name`（跨檔身分，A14/§7.2.3）——供偵測
    「換檔名復活」（present 檔 name 命中此值即使檔名不同 → 不復活）。Block 3 由刪除 doc 的 `.name` 帶入；
    無 name 的 memory（fm 壞/無 name）→ identity=None，僅靠檔名鍵 tombstone 追蹤（退回 Block 2 行為）。"""
    _atomic_write_json(
        tombstones_dir(project_dir) / _mem_file(name),
        {
            "schema_version": SCHEMA_VERSION, "kind": "memory", "target": name,
            "base_hash": base_hash, "machine": machine or local_machine_id(),
            "time": when or now_iso(), "identity": identity,
        },
    )


# A15 ack 帳本檔名（放 `.tombstones/` 內、但**呈現層**、非決策相關）→ 必須排除於決策 digest 之外（見下）。
# 字面常量（非 import acks，避免 acks→tombstone 反向循環）；`test_tombstone` 有漂移守衛測試釘住此名。
_ACKS_FILE = "acks.json"


def tombstone_dir_digest(project_dir: str | os.PathLike) -> str:
    """整個 .tombstones/ 的 canonical digest（含 `_coverage.json`、排除 temp/lock/acks.json）。
    供決策快照偵測 tombstone/epoch 在交易中被改（codex r3/C4）。"""
    d = tombstones_dir(project_dir)
    items: list[list[str]] = []
    if _tombstones_ok(project_dir) and d.exists():   # .tombstones 逃逸 → 不 iterdir 界外、視為空 digest（e2e gate3 #3）
        for p in sorted(d.iterdir()):
            if p.is_symlink() or not p.is_file() or p.name.endswith((".tmp", ".lock")):
                continue   # symlink leaf → 不 hash 界外目標（e2e gate4 #1；已由 _valid_tombstone 落 corrupt）
            if p.name == _ACKS_FILE:
                continue   # A15 ack 帳本＝**純呈現層**，不得進決策快照（否則並發 `doctor --ack` 令 apply 對無關
                           #   session 誤判 skipped-changed＝ack 改了 apply 行為，違反呈現層不變量，fresh gate g1 Medium）
            items.append([p.name, hashlib.sha256(p.read_bytes()).hexdigest()])
    return hashlib.sha256(
        json.dumps(items, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
