"""memory：Claude memory（`.md`）的檔級模型——frontmatter+正文解析、正規化內容 hash、identity。

依據 DESIGN §7.1 + A14 + A17.1 + PLAN v0.8 §2.9（memory 列）：
  - **內容 hash 正規化**（正文 + 穩定 frontmatter；排除易變欄位 → 避免假衝突，§7.1）。檔系統 mtime
    天然不入 hash（我們 hash 內容非 stat）；frontmatter 內的 per-session provenance（`originSessionId`）
    視為易變、不納入內容 hash——否則兩台各自獨立記下「同一事實」會因 provenance 不同被誤判成衝突。
  - **identity = frontmatter `name`**（exact-frontmatter 同一性，A14；P1d 只做精確，模糊近似留 P2）。
  - **零三方相依**：frontmatter 是 YAML 子集，自寫最小解析器；**超出子集 → fail-closed 退回整檔正規化
    hash**（不宣稱無法欄位級驗證的相等，寧可把可疑檔當「需比對」也不靜默判同）。

正規化沿用 `canonical`：`decode_bytes` 吸收 BOM/UTF-16（Windows 往返）、`canon_hash` 做 NFC+穩定鍵序
（跨 OS 一致）。三態 file-state 也沿用 `canonical.FileState`（0-byte/全空白/解碼錯 → damaged）。

P1d Block 1（唯讀核心，`canonical.py` 的 memory 對應）：解析 + hash + identity + 列舉。
P1d Block 2（**本檔下半**，`scan.classify_session` 的 memory 對應）：**唯讀**檔級 diff + classify——
  兩側以檔名配對、正規化 content_hash 比對、tombstone 閘（A17.1）、known/local baseline 對稱刪除偵測。
P1d Block 2b（**exact-frontmatter 跨檔身分**，DESIGN §7.2.3 / A14，memory 專屬、session 無對應、只做 exact）：
  在檔名配對之上，再以 frontmatter `name`（Block 1 唯一 slug identity）為鍵——(a) 同 name 落多檔名 → 跨檔同名
  conflict（`plan_memory_pair` 後置 pass）；(b) would-copy 單邊檔 name 命中別檔名的 memory tombstone identity →
  換檔名復活防護（`classify_memory` + `tomb_identities`，A14；tombstone identity 由 `tombstone.py` 持久化）。
  仍**不寫檔**：寫 memory tombstone / reconcile local_memory / MEMORY.md 索引 / memory-merge 暫存是 Block 3+
  （對稱 session：classify 先唯讀定案，bootstrap/apply 再落地）。模糊近似比對留 P2。
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_from_bytes, unquote

from . import atomicio, tombstone
from .anomaly import collision_casefolds, is_bulk_local_deletion
from .canonical import BOM, FileState, canon_hash, decode_bytes
from .pathsafe import name_key

INDEX_FILE = "MEMORY.md"  # 索引檔（由其餘 memory 機械重建，§7.4）——**不**當成一則 memory 參與 diff。
_FENCE = "---"


class UnsafeMemoryDir(OSError):
    """`<proj>/memory/` 目錄本身是 symlink（root）。**拒絕跟隨**——指向空/錯誤夾會讓 known local memory 看似
    被刪 → 驅動 local-deleted/suppress（單一 known 時 bulk guard 不觸發），一旦 apply wire 即抑制對側真實
    memory（codex P1d gate）；亦含 cloud/未知 reparse（OneDrive 佔位等）與**目標離線的 dangling junction**
    （fresh gate ccdir-g1：dangling 看似空夾→誤刪）。directory junction（目標可用）則**跟隨**、不 raise（見
    `reparse_kind`）。fail-stop（列舉階段就擋、不當成空夾），子類 OSError → 與不可讀夾的 propagate
    同一通道；上層（Block 3 build_plan）可 catch 當 per-project blocked，不必崩整個 sync。"""

# 內容 hash 排除的易變 frontmatter 鍵（任一巢狀層級遞迴剔除）。`originSessionId` 是 per-session
# provenance（同一事實在不同機/不同 session 會不同）→ 納入會造成假衝突（§7.1）。保守只列已知者；
# 不確定的欄位寧可保留（差異 → 多問一次），故此集刻意小。
VOLATILE_FM_KEYS = frozenset({"originSessionId"})


@dataclass
class MemoryDoc:
    """一個 memory `.md` 的檔級模型。

    state         : 三態 file-state（damaged → 不參與 union/index、由上層阻擋）。
    frontmatter   : 解析出的 frontmatter dict（**僅** fm_ok 時非 None）。
    fm_ok         : 有合法 `---` 圍欄**且**內容落在 YAML 子集內、解析成功。
    body          : 正規化後的正文（fm_ok 時為圍欄後內文；無/壞 frontmatter 時為整檔正規化文字）。
    text          : 解碼後的整檔原文（damaged → None；供後續 index/merge 沿用）。
    """

    state: FileState
    frontmatter: dict | None
    fm_ok: bool
    body: str
    text: str | None
    decode_error: str | None = None
    identity: str | None = None  # frontmatter `name` slug（`_fm_ok_identity`）。**只由 fm_ok 的完整 parse 取得**：
    #   唯完整 parse 能保證頂層 name 唯一（codex gate4）。非 fm_ok（出子集）→ None，跨檔身分留 P2（A14/A17.5）。

    @property
    def name(self) -> str | None:
        """identity = frontmatter `name` slug（exact）。**只由 fm_ok 的完整子集 parse 取得**——唯有掃完整段
        frontmatter 才能保證頂層 `name` 唯一（重複頂層 name 在 parser 已 fail-closed）；lenient 部分掃描無法保證
        後段無第二個頂層 name → 不可信（codex P1d gate4）。非 fm_ok（出子集 frontmatter）/ 非 slug name → None
        （不可判）；此時跨檔身分（duty a/b）退回：**duty b 復活**仍由 tombstone 在場的 `blocked-tombstone-no-identity`
        守住（資料安全），**duty a 跨檔重複**對非子集 frontmatter 留 P2（A14/A17.5；殘留＝可能重複、非 loss）。"""
        return self.identity


# ── 正規化 ────────────────────────────────────────────────────────────────

def _normalize_body(text: str) -> str:
    """正文正規化：只做**可證明安全**者——CRLF/CR→LF（行終止符、非內容）+ 去**單一** POSIX 檔尾 newline。
    **不**逐行 rstrip、**不**去前導空行、**不**去多個檔尾空行：Markdown 行尾兩空格＝hard break、code fence
    內尾隨空白、前導/EOF 空行（unclosed code block 內）皆可能有語意，會靜默丟資料（codex gate/gate2）。
    寧可留 cosmetic 差異成假衝突（安全方向），也不壓掉語意。NFC 由 `canon_hash` 處理。"""
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    return norm[:-1] if norm.endswith("\n") else norm  # 僅移除單一檔尾 newline（多個尾端空行可能是 code 內容）


def _is_fence(line: str) -> bool:
    """frontmatter 圍欄判定：須**無 leading 空白**（frontmatter 在第 0 欄），只容忍 trailing **ASCII 空格**
    （`rstrip(" ")`，非無參 rstrip——否則 NBSP/tab 等 Unicode 空白被當圍欄 cosmetic 而與真 `---` 同 hash，
    codex gate2）。`  ---`（縮排）也不是圍欄（codex gate）。"""
    return bool(line) and not line[0].isspace() and line.rstrip(" ") == _FENCE


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    """切出 frontmatter 文字與正文。回 (fm_text, body)；無合法 `---`…`---` 圍欄 → (None, 整檔)。

    圍欄須在檔首（首行為第 0 欄的 `---`），找下一條 `---` 行收尾。無收尾圍欄 → 視為無 frontmatter。"""
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = norm.split("\n")
    if not lines or not _is_fence(lines[0]):
        return None, norm
    for i in range(1, len(lines)):
        if _is_fence(lines[i]):
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None, norm  # 開了圍欄但沒收尾 → 不認，整檔當正文（fail-closed）


# 合法 frontmatter key 的**正向 allowlist**：拒 list marker `- `、`? ` explicit key、quoted key、含空白等
# 所有未支援 key 語法（否則 `- a: x` 會被當 key "- a"，反序 list 經 canon 排序後同 hash，codex r2-1）。
_KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")

# 身分 slug 判準（memory name 與 tombstone identity 共用）：用 **fullmatch**（非 `re.$`）——`$` 會匹配尾隨
# `\n` 前，令 `"fact\n"` 被當合法 slug → 漏配對 → 復活（codex P1d gate3 high）。`\Z` 不容尾隨換行。
_SLUG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def _is_slug(s: str | None) -> bool:
    """是否為 identity slug（letters/digits/`_`/`-`/`.`，無空白/換行/引號/其它）。tombstone identity 與檔案
    name 共用同一判準，非 slug 一律當「不可判」fail-closed。"""
    return bool(s and _SLUG_RE.fullmatch(s))

# unquoted 值以這些字元起首 → YAML 流式集合/指示符（含 `:`，codex r3）/引號/錨點/清單/註解 → fail-closed。
# 補齊 YAML c-indicator 全集，避免 `desc: : foo` 的值 ": foo" 被當純字串、與 quoted 壓同 hash。
_UNSAFE_LEAD = frozenset("[]{}&*!|>%@`\"'#,?-:")

# YAML 1.1 隱式型別解析 pattern：unquoted 命中任一 → 非字串純量（與同字面 quoted string 不同義）→ fail-closed。
# 用**完整 blocklist**而非弱 token 表——YAML 隱式型別（bool/null/int〔含 sexagesimal〕/float/timestamp）太多，
# 漏一種就把 `x: <typed>` 與 `x: "<typed>"` 壓成同一 hash（critical，codex r2-2）。
_YAML_NULL = re.compile(r"^(?:~|null|Null|NULL|none|None|NONE)$")
_YAML_BOOL = re.compile(
    r"^(?:y|Y|n|N|yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$"
)
_YAML_INT = re.compile(
    r"^[-+]?(?:0b[01_]+|0x[0-9a-fA-F_]+|0o?[0-7_]+|(?:0|[1-9][0-9_]*)|[1-9][0-9_]*(?::[0-5]?[0-9])+)$"
)
_YAML_FLOAT = re.compile(
    r"^[-+]?(?:\.(?:inf|Inf|INF)"
    r"|(?:[0-9][0-9_]*)?\.[0-9_]*(?:[eE][-+]?[0-9]+)?"
    r"|[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*)$"
    r"|^\.(?:nan|NaN|NAN)$"
)
_YAML_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}"
    r"(?:[Tt ][0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?"
    r"(?:[ ]*(?:Z|[-+][0-9]{1,2}(?::?[0-9]{2})?))?)?$"
)
_COMMENT_RE = re.compile(r"\s#")  # 值中 whitespace+# → YAML 註解起點 → 與 quoted 不同義（codex r2-2）
_COLON_RE = re.compile(r":(?:\s|$)")  # 值中 `: ` 或結尾 `:` → 非安全 plain scalar（mapping 分隔）→ fail-closed（codex gate）
# decode_bytes 對這些 BOM（UTF-16/32 LE/BE）會在文字保留**一個**前置 ﻿；utf-8-sig 已被 codec 消耗、不在此列。
_CODEC_LEFTOVER_BOM = (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff", b"\xff\xfe", b"\xfe\xff")


def _is_number_like(val: str) -> bool:
    """Python 能解析成 int/float 的數字形（補正規表達式漏掉的 e 記號/底線等）；寧可多判 → fail-closed。"""
    v = val.replace("_", "")
    if v in ("", "+", "-", ".", "+.", "-."):
        return False
    try:
        int(v, 0)
        return True
    except ValueError:
        pass
    try:
        float(v)
        return True
    except ValueError:
        return False


def _is_yaml_nonstring(val: str) -> bool:
    """val（unquoted）是否會被 YAML 解析成非字串純量（→ 與同字面 quoted string 不同義）。涵蓋
    null/bool/int〔含 sexagesimal/8進/16進〕/float〔含 .inf/.nan/sexagesimal〕/timestamp + 數字 backstop。"""
    return bool(
        _YAML_NULL.match(val) or _YAML_BOOL.match(val) or _YAML_TIMESTAMP.match(val)
        or _YAML_INT.match(val) or _YAML_FLOAT.match(val)
    ) or _is_number_like(val)


def _value(val: str) -> tuple[bool, str | None]:
    """把 frontmatter 純量值轉成 Python 字串；無法**忠實**表示者 → (False, None)，由上層退 raw hash。

    - 成對引號（' 或 "）：取殼內；內嵌同款引號 / 反斜線 escape → 不保證忠實解碼 → fail-closed。
    - unquoted：僅「明顯純字串」才接受。指示符/流式起首、值中註解（whitespace+#）、或命中 YAML 隱式型別
      （bool/null/number/timestamp）→ fail-closed。否則 `x: true` 與 `x: "true"`、`x: 2024-06-20` 與
      `x: "2024-06-20"` 等會被壓成同一 hash（critical，codex r1-1/r2-2）。
    """
    if not val:
        return True, ""  # 不會走到（空值在呼叫端當巢狀父）；防 val[0] IndexError
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        inner = val[1:-1]
        if val[0] in inner or "\\" in inner:
            return False, None
        return True, inner
    if (val[0] in _UNSAFE_LEAD or _COMMENT_RE.search(val) or _COLON_RE.search(val)
            or _is_yaml_nonstring(val)):
        return False, None
    return True, val


def _fm_ok_identity(frontmatter: dict | None) -> str | None:
    """fm_ok 檔的身分 = 頂層 `name`（slug 化）。**只由完整子集 parse 取得**（codex P1d gate4）：唯有掃完整段
    frontmatter 才能保證頂層 `name` **唯一**（重複鍵在 `_parse_frontmatter_block` 已 fail-closed）；任何只看開頭的
    lenient 掃描都可能漏掉後段第二個頂層 name → 漏配對/誤身分（不可靠）。非 fm_ok（出子集）檔身分一律 None，
    其跨檔身分留 P2（A14/A17.5）；content_hash 仍走 raw fallback、可按檔名同步。"""
    nm = frontmatter.get("name") if frontmatter else None
    return nm if isinstance(nm, str) and _is_slug(nm) else None


def _parse_frontmatter_block(fm_text: str) -> dict | None:
    """最小 YAML 子集解析：頂層 `key: value` 或 `key:`（空值→巢狀 mapping），其下**恰 2 空格**縮排的
    `subkey: value`（單層）。**任何超出子集者 → 回 None**，由上層退回整檔 raw hash（fail-closed）：
    tab 縮排、無冒號行、indent∉{0,2}、巢狀再開巢狀、清單/流式/型別值、**重複鍵**（codex r1-2）、深層巢狀
    壓平（codex r1-3）、**型別折疊 key**（true/1…，codex gate）、**空 `key:` 無 child**（null/空 map 歧義，
    codex gate）。"""
    result: dict[str, Any] = {}
    parent: str | None = None       # 當前巢狀 mapping 的頂層父鍵
    nested_seen: set[str] = set()    # 該巢狀層已見子鍵（查重）
    parent_count = 0                 # 當前 parent 已收的 child 數（空 `key:` 須 fail-closed，codex gate）
    for raw in fm_text.split("\n"):
        if raw.strip() == "":
            continue
        stripped = raw.lstrip(" \t")
        leading = raw[: len(raw) - len(stripped)]
        if "\t" in leading or ":" not in stripped:
            return None  # tab 縮排 / 非 key:value 行 → 子集外
        indent = len(leading)
        key, _, after = stripped.partition(":")
        if after and after[0] != " ":
            return None  # `key:value`（冒號後非空白）在 YAML block mapping **不是** key:value 而是 plain scalar →
            #            子集外、fail-closed（否則 `name:dup` 被當 {name:dup} 與 `name: dup` 同 hash＝conflation，codex gate4）
        key, val = key.strip(), after.strip()
        if not _KEY_RE.match(key) or _is_yaml_nonstring(key):
            return None  # 非 allowlist key（list marker/quoted/含空白）或 YAML 會型別折疊的 key（true/1…）→ 子集外（codex r2-1/gate）
        if indent == 0:
            if parent is not None and parent_count == 0:
                return None  # 前一個 `key:` 無任何 child（null/空 map 歧義）→ fail-closed（codex gate）
            if key in result:
                return None  # 重複頂層鍵 → 靜默覆蓋會丟欄位 → 退 raw（codex r1-2）
            if val == "":
                result[key] = {}
                parent, nested_seen, parent_count = key, set(), 0
            else:
                ok, scalar = _value(val)
                if not ok:
                    return None
                result[key] = scalar
                parent = None
        elif indent == 2:
            if parent is None or not isinstance(result.get(parent), dict) or val == "":
                return None  # 孤兒縮排 / 巢狀再開巢狀 → 子集外
            if key in nested_seen:
                return None  # 重複巢狀鍵
            ok, scalar = _value(val)
            if not ok:
                return None
            nested_seen.add(key)
            result[parent][key] = scalar
            parent_count += 1
        else:
            return None  # indent 1 或 >2 → 子集外（不 flatten 深層巢狀，codex r1-3）
    if parent is not None and parent_count == 0:
        return None  # 收尾：最後一個 `key:` 無 child → fail-closed（codex gate）
    return result or None  # 空 frontmatter（圍欄內無內容）→ None（無欄位可比，退 raw）


# ── 內容 hash / identity ──────────────────────────────────────────────────

def _strip_volatile(obj: Any) -> Any:
    """遞迴剔除 VOLATILE_FM_KEYS（任一巢狀層級）。"""
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in obj.items() if k not in VOLATILE_FM_KEYS}
    if isinstance(obj, list):
        return [_strip_volatile(x) for x in obj]
    return obj


def content_hash(doc: MemoryDoc) -> str | None:
    """正規化內容 hash（diff/identity 基準）。damaged → None。

    fm_ok：hash（穩定 frontmatter〔剔易變鍵〕 + 正規化正文）——cosmetic（鍵序/引號/換行/尾隨空白）
    差異不產生假衝突。否則（無/壞 frontmatter）：**fail-closed** 退回整檔正規化文字 hash——位元組相同
    仍判同，但不跳過未能解析的 frontmatter（不假裝欄位級相等）。"""
    if doc.state.is_damaged:
        return None
    if doc.fm_ok and doc.frontmatter is not None:
        return canon_hash({"fm": _strip_volatile(doc.frontmatter), "body": doc.body})
    return canon_hash({"raw": doc.body})


# ── 載入 / 列舉 ─────────────────────────────────────────────────────────────

def load_memory(path: str | os.PathLike) -> MemoryDoc:
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        return MemoryDoc(FileState.DECODE_ERROR, None, False, "", None, decode_error=str(e))
    return load_memory_bytes(raw)


def load_memory_bytes(raw: bytes) -> MemoryDoc:
    """bytes → MemoryDoc。三態 file-state 先判，再切 frontmatter/正文、解析 frontmatter（子集外退 raw）。"""
    if len(raw) == 0:
        return MemoryDoc(FileState.ZERO_BYTE, None, False, "", None)
    text, err = decode_bytes(raw)
    if err is not None:
        return MemoryDoc(FileState.DECODE_ERROR, None, False, "", None, decode_error=err)
    assert text is not None
    # decode_bytes 對 UTF-16/32 LE/BE 會在文字保留一個前置 ﻿（utf-8-sig 已被 codec 消耗）。僅當原始 bytes
    # 確以這些 BOM 起首時，剝掉那**一個** codec 殘留 BOM——須在 blank 判斷**前**（否則 BOM-only/BOM+空白被
    # 誤判 OK，codex r1-5），且只剝一個、不碰內容自帶的 ﻿（lstrip 會連內容 BOM 一起吃＝靜默丟，codex r2-3）。
    if raw.startswith(_CODEC_LEFTOVER_BOM) and text.startswith(BOM):
        text = text[1:]
    if text.strip() == "":
        return MemoryDoc(FileState.BLANK, None, False, "", None)
    fm_text, body_text = _split_frontmatter(text)
    if fm_text is not None:
        parsed = _parse_frontmatter_block(fm_text)
        if parsed is not None:
            # fm_ok：strict parse 懂結構、保證頂層 name 唯一 → 取其 slug 為身分（codex gate4）。
            return MemoryDoc(FileState.OK, parsed, True, _normalize_body(body_text), text,
                             identity=_fm_ok_identity(parsed))
    # 非 fm_ok（含無圍欄 fm_text=None）：身分不可判 → None（跨檔身分留 P2，A14/A17.5）；content_hash 走 raw fallback。
    return MemoryDoc(FileState.OK, None, False, _normalize_body(text), text, identity=None)


# Windows reparse-tag 精確分流（fresh gate ccdir-g1）：directory junction（MOUNT_POINT）是使用者刻意的同機共用
# → 跟隨；symlink / 其他 reparse（OneDrive·cloud 佔位、dedup、未知 tag）→ fail-closed 拒絕（非同機共用機制、
# 可寫到雲端/非預期目標）。「非 symlink reparse」不等於 junction，故只用 `S_ISLNK` 區分不夠、須看 reparse tag。
_IO_REPARSE_TAG_MOUNT_POINT = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def reparse_kind(p: str | os.PathLike, *, long_path: bool = False) -> str:
    """分類 p 的 reparse 狀態（跨 OS、**no-follow**〔os.lstat〕、**no-throw**）。回傳：
      `"none"`     普通檔/夾或不存在 → 可正常處理（junction 的 is_dir/iterdir/open 由 OS 透明跟隨）。
      `"symlink"`  POSIX symlink 或 Windows symlink reparse → 拒絕（非同機共用機制、可跨裝置逃逸）。
      `"junction"` Windows directory junction（reparse tag MOUNT_POINT）→ **跟隨**（使用者刻意同機共用，CLAUDE_CONFIG_DIR 模型）。
      `"other"`    其他/未知 reparse（OneDrive·cloud 佔位、dedup、WCIFS…）或 lstat 失敗 → **fail-closed**（拒絕）。
    註：① volume mount point 與 junction 共用 MOUNT_POINT tag、此處一併歸 `"junction"`（同屬使用者刻意的目錄重導；
    要再分需解析 reparse 目標，留待需要時）。② **dangling** junction（目標離線/被刪）仍回 `"junction"`（lstat 不跟隨
    末元件、讀得到 reparse tag）→ 呼叫端據此**不**當「空夾/刪除」（見 `list_memory_files`，fresh gate ccdir-g1 High）。"""
    try:
        # `long_path=True` **僅** memory-merge staging（深 >260 路徑，_claim_staging_dir）→ os_path 的 \\?\ 繞過
        # MAX_PATH（不改 lstat 的 no-follow 語意）。**預設 False＝plain（260-bound、與改動前逐位元組一致）**：非
        # staging 呼叫端（list_memory_files / apply._is_unfollowable_reparse）務必維持 fail-closed——>260 的真實
        # memory/ 夾 → os.lstat raise → "other" → UnsafeMemoryDir，**不可**因長路徑化令 lstat 過關、後續 plain
        # is_dir()/iterdir() 又 260-bound 失敗 → 誤當空夾 → 驅動 local-deleted 抑制真實 memory（codex longpath-r2 High）。
        st = os.lstat(atomicio.os_path(p) if long_path else p)
    except FileNotFoundError:
        return "none"
    except OSError:
        return "other"   # 不確定一律 fail-closed（含 os_path/abspath 罕見失敗）
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    if not (getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        return "none"
    return "junction" if getattr(st, "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT else "other"


def list_memory_files(memory_dir: str | os.PathLike) -> dict[str, Path]:
    """列該 `memory/` 夾下的 memory 檔，鍵=檔名（兩側以檔名配對，仿 session 的 sid）。

    **排除**：`MEMORY.md`（索引、非 memory，case-insensitive）、所有 dotfile/dotdir（含 `.merge/`，§7.1
    暫存區另在 memory 外）、子夾（只取頂層 `*.md`）、**子項 symlink**（防讀 memory 夾外內容）。
    **memory/ 根目錄本身是 symlink → raise `UnsafeMemoryDir`**（不跟隨、不當空夾，見該例外，codex P1d gate）。"""
    out: dict[str, Path] = {}
    d = Path(memory_dir)
    # **reparse 精確分流**（CLAUDE_CONFIG_DIR 模型 + fresh gate ccdir-g1 High/Medium，見 reparse_kind）：symlink 或
    # cloud/未知 reparse → raise（非回 {}）：回 {} 會把「指向空/錯夾或雲端」誤當「memory 全空」→ 看似刪除 → 驅動
    # local-deleted/suppress（單一 known 時 bulk guard 不觸發 → 抑制對側真實 memory，codex P1d gate）。**先於** is_dir
    # 檢查（is_dir 會跟隨 symlink→把目標內容當本專案 memory）。directory junction → 跟隨（使用者刻意同機共用、指向
    # 唯一真實副本）：is_dir() 透明跟隨到目標後正常列舉。
    kind = reparse_kind(d)
    if kind in ("symlink", "other"):
        raise UnsafeMemoryDir(f"memory/ 是 symlink 或不支援的 reparse point，拒絕跟隨：{d}")
    if not d.is_dir():
        # junction 但 is_dir()=False ＝ **dangling**（目標離線/被刪）→ **不可**當空夾：否則看似全刪 → 誤寫抑制
        # tombstone 蓋掉有效 memory（fresh gate ccdir-g1 High）。raise 讓 build_plan/reconcile 比照 symlink 略過。
        if kind == "junction":
            raise UnsafeMemoryDir(f"memory/ 是 junction 但目標不可用（dangling/offline），拒絕當作空夾：{d}")
        # >260 fail-closed（codex longpath-r2 High）：Windows 未開 LongPathsEnabled 時，超過 MAX_PATH 的**真實**
        # memory/ 夾令 plain `os.lstat`（reparse_kind → FileNotFoundError → "none"）與 `is_dir()` 皆 260-bound 失敗
        # → 看似「真的空」→ 誤驅動 local-deleted 抑制真實 memory。用 os_path 的 \\?\ **只探測是否為目錄**（不放寬
        # 260-bound 的 iterdir 列舉）：**是目錄**（含 >260 真實夾）→ 拒絕當空夾、fail-closed（比照 symlink/dangling
        # 略過）；非目錄（存在但是檔）或真的不存在 → isdir False → 才回真空（保留既有「存在但非目錄→真空」語意）。
        # 此處 os_path 是 **fail-closed 方向**（偵測到就 raise）、非放行列舉。
        if os.path.isdir(atomicio.os_path(d)):
            raise UnsafeMemoryDir(f"memory/ 路徑超過系統長度上限（MAX_PATH）、無法以標準 API 安全列舉：{d}")
        return out   # 普通夾：缺/存在但非目錄 → 真的空（合法邊界；iterdir 對非目錄會 raise，故先擋）
    # 存在但**讀不到**的夾（權限/陳舊網路掛載）：`iterdir()` 的 OSError **刻意不吞**、向上拋（fail-stop）。
    # 絕不 `except OSError: return {}`——把不可讀誤當「memory 全空」會看似大量刪除，下游可能寫抑制 tombstone
    # 去蓋掉對側真實 memory（codex P1d-r1）。**註**：session 側 `scan._session_files` 用 `glob`（對不可讀夾會
    # fail-open 回空、非 propagate），該 fail-stop 由 apply 在寫入前以 `scan._dir_scannable` 補（e2e gate9 finding2）。
    # iterdir + casefold 副檔名：`Path.glob("*.md")` 在 POSIX 是 case-sensitive（漏 `A.MD`）、Windows 又會
    # 匹配 → 跨 OS 掃描不一致；改逐項以 casefold 判副檔名與索引排除，兩端一致（codex r1-6）。
    for p in sorted(d.iterdir()):
        # 跳過 symlink（**先於** is_file，因 is_file 會跟隨）：memory/x.md -> /outside 會把 memory 夾外
        # 內容當 memory 同步（隱私外洩 + 跨 OS 不一致，codex gate）。
        if p.is_symlink() or not p.is_file() or p.name.startswith(".") or p.suffix.casefold() != ".md":
            continue
        if p.name.casefold() == INDEX_FILE.casefold():
            continue
        out[p.name] = p
    return out


def memory_dir(project_dir: str | os.PathLike) -> Path:
    """專案夾下的 memory 目錄（`<proj>/memory/`，DESIGN §0/§7）。local 與 hub 皆此結構。"""
    return Path(project_dir) / "memory"


# ── MEMORY.md 索引機械重建（Block 3c；DESIGN §7.4 + A14）──────────────────────────────────────
# 由**落地後**檔案集 frontmatter 機械重建索引（每則一行 `- [name](file.md) — description`），解決「純 union
# 後新檔不在索引 → Claude 看不到」的索引漂移（§7.4）。**最高鐵則＝永不靜默丟手寫內容**（A14）：
#
# 採「**工具自有 auto-block**」極性（使用者 2026-06-21 拍板，A14 字面的 `<!-- USER SECTION -->` 是「如」示意；
# 此極性更安全——把「不丟資料」做成**結構保證**而非啟發式猜測）：工具**只重寫自己 BEGIN/END 標記之間**的內容，
# 標記**外一律逐字保留**。故：
#   - 缺檔/空檔 → 建新（header + 空/實 auto-block）；無檔可索引且無現檔 → 不建（不留空索引檔）。
#   - 恰一對成對標記 → 只換框內條目，框前/框後逐字保留（連行終止符）。
#   - **無標記**（手寫/curated 索引，如現行多數真實檔）→ **絕不重建**；僅在偵測到「present 記憶檔未被索引引用」
#     （真實漂移）時回一句警告，否則靜默。改自動維護需手動加標記區（或新機/空檔由本函式自動建）。
#   - 標記不成對/順序異常（≠1 begin、≠1 end、end 在 begin 前）→ fail-closed 保留原檔 + 警告。
# 交易邊界：apply 在 **per-project memory 鎖內、reconcile 之後**呼叫，並以鎖內 re-glob 的磁碟現況為據（反映實際
# 落地狀態，非 stale plan）。MEMORY.md 本身**不**參與 memory diff（`list_memory_files` 已排除）。

INDEX_BEGIN = "<!-- BEGIN claude-session-sync auto memory index -->"
INDEX_END = "<!-- END claude-session-sync auto memory index -->"
_INDEX_HEADING = "# Memory Index"

# 從現有（手寫）索引文字抽出 markdown 連結目標：兼容 `](bare)`（無空白）與 `](<with spaces>)` 角括號形（後者
# 即 _index_link_target 對含空白檔名所產生／使用者手寫的形式，codex R1 Low）。只用於**漂移偵測**、不參與重建。
_LINK_TARGET_RE = re.compile(r"\]\(\s*(?:<([^>]*)>|([^()\s]+))\s*\)")
# 控制字元（含換行/CR/tab）：索引標題/描述一律剔除，避免破壞單行條目結構（codex R1 Low）。
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
# URL/外部連結 scheme（`http:`/`https:`/`mailto:`/`file:`/Windows `C:` …）：漂移偵測不可把 `https://x/a.md`
# 當本地 `a.md` 引用（會壓掉真實本地檔的漂移警告，codex 塊末 fresh gate Low）。只計相對本地目標。
_URL_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")
# CommonMark fenced code block 圍欄：≤3 空白縮排 + ≥3 個 ` 或 ~（開啟行可帶 info string；關閉行同字元、長度≥開啟、
# 無尾隨內容）。用於標記偵測忽略 code fence 內的標記行（codex 塊末 fresh gate r5 High）。
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


@dataclass(frozen=True)
class IndexResult:
    """索引重建結果。content=None → **不寫檔**（unchanged / 退讓保留手寫 / 損壞中止 / 無事可做）；非 None → 待
    原子寫入的完整新內容。note → 非 None 時由 apply 併入 report.warnings（漂移/退讓提示，不影響 exit code——索引是
    便利性、非安全性質；最壞只是索引過時，已另以警告提示）。status 為機器碼供測試/除錯。"""

    content: str | None
    status: str               # created | rebuilt | unchanged | kept-handwritten | kept-malformed | kept-unreadable | empty
    note: str | None = None


def _index_title_text(s: str) -> str:
    """索引標題文字：剔除控制字元（含換行/CR，防破行）→ 中和 surrogate（檔名含非 UTF-8 bytes 經 surrogateescape
    解碼會帶 lone surrogate，否則 `content.encode("utf-8")` 會 raise，codex 塊末 fresh gate Medium）→ 跳脫
    markdown `\\`/`[`/`]`。fm_ok name 是 slug（`[A-Za-z0-9_.-]`）天然不含這些；僅非 fm_ok 退回檔名 stem 才可能命中。"""
    s = _CTRL_RE.sub("", s).encode("utf-8", "replace").decode("utf-8")
    return s.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _index_link_target(filename: str) -> str:
    """連結目標 = percent-encode 檔名。用 `quote_from_bytes(os.fsencode(...))`（非 `quote(str)`）：POSIX 檔名可含
    非 UTF-8 bytes → iterdir 以 surrogateescape 解出 lone surrogate，`quote(str)` 會 raise UnicodeEncodeError、
    令整個 apply 在 memory 已寫後崩潰（codex 塊末 fresh gate Medium）。fsencode 還原原始 bytes 再逐 byte %XX。
    RFC3986 unreserved（`A-Za-z0-9-._~`，涵蓋 slug）不變 → 正常 `slug.md` 原樣；空白/`#`/`(`/`)`/`<`/`>`/控制
    字元/非 ASCII 一律 %XX，永不破壞 markdown 連結或行結構（markdown renderer 會 percent-decode 回實際檔名）。"""
    return quote_from_bytes(os.fsencode(filename), safe="")


def _index_entry(filename: str, doc: MemoryDoc) -> str:
    """單則索引行：`- [title](target) — description`。title = fm_ok 的 `name` slug，否則檔名 stem（去 .md）。
    description 取 fm_ok 頂層 `description` 字串（已剝引號、單行純量）；無/非 fm_ok → 省略 `— ...` 段。"""
    if doc.name:                       # fm_ok slug identity（安全字元集）
        title = doc.name
    else:                              # 非 fm_ok（可讀、未損、以 raw hash 正常同步）：退回檔名 stem（去單一尾 .md）
        title = filename[:-3] if filename.casefold().endswith(".md") else filename
    line = f"- [{_index_title_text(title)}]({_index_link_target(filename)})"
    if doc.fm_ok and doc.frontmatter is not None:
        desc = doc.frontmatter.get("description")
        if isinstance(desc, str) and desc.strip():
            line += f" — {_CTRL_RE.sub(' ', desc.strip())}"  # 單行純量理論上無控制字元，仍防禦剔除
    return line


def _render_index_entries(docs: dict[str, MemoryDoc]) -> list[str]:
    """由檔名→MemoryDoc 映射機械產生**已排序、決定性**的索引條目行。排序鍵 (casefold, exact)：跨機同檔案集 → 同
    索引（對稱 session_merge 的跨機決定性）。每檔一行（含跨檔同名的多檔——索引反映磁碟現況；同名衝突由 diff/classify
    另行處理，不在此判）。呼叫端須先濾掉 damaged doc（見 `plan_index_rebuild`）。"""
    return [_index_entry(fn, docs[fn]) for fn in sorted(docs, key=lambda s: (s.casefold(), s))]


def _code_fenced_lines(lines: list[str]) -> set[int]:
    """回 fenced code block 內（含圍欄行本身）的行索引集（近似 CommonMark：≤3 空白縮排 + ≥3 個 ` 或 ~；開啟可帶
    info string，關閉同字元、長度≥開啟、無尾隨）。供標記偵測與漂移偵測共用——code fence 內的標記/連結是**範例**，
    不可當真標記或真引用（codex 塊末 fresh gate r5/r6）。"""
    fenced: set[int] = set()
    fence: tuple[str, int] | None = None
    for i, raw in enumerate(lines):
        ln = raw.rstrip("\r\n")
        m = _FENCE_RE.match(ln)
        if m:
            ch, length = m.group(1)[0], len(m.group(1))
            rest = ln.lstrip(" ")[length:]
            fenced.add(i)                           # 圍欄行本身也算 code
            if fence is None:
                fence = (ch, length)
            elif ch == fence[0] and length >= fence[1] and rest.strip() == "":
                fence = None
            continue
        if fence is not None:
            fenced.add(i)
    return fenced


def _referenced_md_targets(text: str) -> set[str]:
    """現有索引文字中被引用的 `.md` 連結目標 basename（casefold）。供無標記手寫索引的漂移偵測。**略過 fenced/縮排
    code block**（範例連結不算真引用，否則只在 code 範例出現的檔會誤判已索引→壓掉漂移警告，codex 塊末 fresh gate
    r6 Medium）。percent-decode（與 `_index_link_target` 對稱）後取 basename，故 `](foo%20bar.md)`/`](<foo bar.md>)`
    都能對上 `foo bar.md`，不誤報（R1 Low）。"""
    out: set[str] = set()
    lines = text.splitlines()
    fenced = _code_fenced_lines(lines)
    for i, ln in enumerate(lines):
        if i in fenced or len(ln) - len(ln.lstrip(" ")) >= 4:
            continue  # fenced 或 ≥4 空白縮排（indented code block 近似）→ 範例、不算引用
        for m in _LINK_TARGET_RE.finditer(ln):
            raw = m.group(1) if m.group(1) is not None else m.group(2)
            # 外部連結/絕對 URL（`https://x/a.md`、`mailto:`、`//host/a.md`）不是本地 sibling 引用 → 跳過，否則
            # 其 basename `a.md` 會壓掉真實本地 `a.md` 的漂移警告（codex 塊末 fresh gate Low）。只計相對本地目標。
            if _URL_SCHEME_RE.match(raw) or raw.startswith("//"):
                continue
            # 先剝**未編碼**的 URI fragment/query（`#`/`?` 為分隔符）再 unquote：`](a.md#notes)` → `a.md`（否則
            # endswith(".md") 失敗 → 誤報該檔漂移，codex R2 Low）。檔名內字面 `#`/`?` 在連結中本應是 `%23`/`%3F`。
            raw = raw.split("#", 1)[0].split("?", 1)[0]
            if not raw:
                continue
            t = unquote(raw).replace("\\", "/")
            if t.startswith("./"):
                t = t[2:]
            # 只算 **memory/ 內的 sibling**（無目錄成分）：`](sub/a.md)`/`](../archive/a.md)`/`](/tmp/a.md)` 指向
            # 別處的 a.md，不可當本地 `memory/a.md` 引用（否則壓掉真實本地檔的漂移警告，codex r2 Low）。POSIX 檔名
            # 不含 `/`，故剔除含 `/` 者永不誤刪真實 sibling。
            if "/" in t:
                continue
            if t.casefold().endswith(".md"):
                out.add(t.casefold())
    return out


def _scan_markers(lines: list[str]) -> tuple[list[int], list[int]]:
    """掃出工具標記行索引 (begins, ends)，**忽略 fenced code block 內**的標記行。標記須精確（第 0 欄、無前後空白）；
    縮排標記＝indented code 因精確比對天然不認；``` / ~~~ fence 內的標記＝範例文件不認（codex 塊末 fresh gate
    r3/r5 High——防手寫索引展示標記用法時被誤當真標記而吃掉框內手寫內容）。"""
    fenced = _code_fenced_lines(lines)
    begins: list[int] = []
    ends: list[int] = []
    for i, raw in enumerate(lines):
        if i in fenced:
            continue
        ln = raw.rstrip("\r\n")
        if ln == INDEX_BEGIN:
            begins.append(i)
        elif ln == INDEX_END:
            ends.append(i)
    return begins, ends


def plan_index_rebuild(mem_dir: str | os.PathLike, current_text: str | None) -> IndexResult:
    """規劃 MEMORY.md 索引重建（純函式、易測）。`current_text`=現有索引內容（None=檔不存在）。

    可能 `raise UnsafeMemoryDir`（memory/ 根為 symlink），由呼叫端比照 reconcile 處理（警告、不崩）。"""
    files = list_memory_files(mem_dir)          # 鎖內現況；可能 raise UnsafeMemoryDir
    # symlink `.md` leaf 偵測（e2e gate6#2）：list_memory_files 略過 symlink → 若 mem_dir 有被略過的 symlink `.md`
    # leaf，檔案視圖對該 name 不完整/不可信 → **中止重建、保留現有索引**（否則把被略過的 name 從 auto-block 移除
    # ＝被略過 leaf 驅動 auto write，與 apply delete/copy 路徑的 fail-closed 立場不一致）。對稱上方 damaged 中止；
    # 待使用者移除該 symlink 或還原為一般檔後重跑 sync。root 已由 list_memory_files 驗過（symlink/dangling→raise）。
    symlinked = sorted(
        p.name for p in Path(mem_dir).iterdir()
        if p.is_symlink() and not p.name.startswith(".")
        and p.suffix.casefold() == ".md" and p.name.casefold() != INDEX_FILE.casefold())
    if symlinked:
        return IndexResult(None, "kept-symlink-leaf",
                           "MEMORY.md 索引重建中止：memory 有 symlink 檔（不可信、不跟隨）："
                           + ", ".join(symlinked) + " → 保留現有索引不動（移除該 symlink 或還原為一般檔後重跑 sync）。")
    docs = {fn: load_memory(files[fn]) for fn in files}
    # **任一 indexed 檔損壞 → 中止重建、保留現有索引**（fail-closed，codex R1 Medium）：load_memory 把讀錯（glob
    # 後消失/權限）也轉成 DECODE_ERROR，0-byte/空白/解碼錯亦 is_damaged → 身分不可判。把它們當有效條目列出＝fail
    # open；且若 transient 讀錯就略過該檔，會把索引「清空」成看似全刪。故只要有 damaged 就不寫、警告、保留現況。
    # 非 fm_ok（可讀、未損、以 raw content_hash 正常同步的真實 memory）**不算** damaged——排除它們才造成索引漂移
    # （身分讀不出時退回檔名 stem 條目，仍可被看見），與分類器「damaged→blocked、非 fm_ok→照常同步」一致。
    damaged = sorted(fn for fn, d in docs.items() if d.state.is_damaged)
    if damaged:
        return IndexResult(None, "kept-unreadable",
                           "MEMORY.md 索引重建中止：memory 檔讀不到/損壞（0-byte/空白/解碼錯/glob 後消失）："
                           + ", ".join(damaged) + " → 保留現有索引不動（修復後重跑 sync）。")
    entries = _render_index_entries(docs)
    present = {fn.casefold() for fn in files}

    # ① **缺檔（current_text is None）→ 建新**；無檔可索引且無現檔 → 不留空索引檔。**現存但全空白**不在此建新：
    #    空白檔是 markerless（使用者可能刻意清空以隱藏記憶）→ 往下走標記偵測 → 無標記 → kept-handwritten（+漂移
    #    警告），絕不自動覆寫（A14 極性：markerless 一律不重寫；codex 塊末 fresh gate r3 Medium——區分 None vs ""）。
    if current_text is None:
        if not entries:
            return IndexResult(None, "empty")
        content = "\n".join([_INDEX_HEADING, "", INDEX_BEGIN, *entries, INDEX_END, ""])
        return IndexResult(content, "created")

    # ② 定位工具標記（`_scan_markers`）。**精確比對**（只剝 \r\n、不剝前後空白）＋**忽略 fenced code block 內**：
    #    縮排標記＝indented code/示例不認（codex 塊末 fresh gate r3 High）；``` code fence 內的標記也不認（否則手寫
    #    索引用 fence 展示標記用法會被當真標記、吃掉框內手寫內容，codex 塊末 fresh gate r5 High）。工具自身恆在第
    #    0 欄、無前後空白、不在 fence 內輸出標記，故對自產檔仍冪等。
    lines = current_text.splitlines(keepends=True)
    begins, ends = _scan_markers(lines)

    # ③ 無任何標記 → 手寫/curated 索引：絕不重建（A14）。**雙向**漂移偵測（手寫檔永不重建 → 此警告是唯一訊號，
    # codex 塊末 fresh gate r2 Low）：present 檔未列入索引（Claude 看不到）＋ 索引列出但檔已不在（殘留死連結）。
    if not begins and not ends:
        referenced = _referenced_md_targets(current_text)
        missing = sorted(fn for fn in files if fn.casefold() not in referenced)       # 有檔、未列入
        stale = sorted(t for t in referenced - present if t != INDEX_FILE.casefold())  # 列了、檔已不在（排除自指）
        parts = []
        if missing:
            parts.append("未列入索引的記憶：" + ", ".join(missing))
        if stale:
            parts.append("索引列出但已不存在的記憶：" + ", ".join(stale))
        note = None
        if parts:
            note = ("MEMORY.md 為手寫索引（無工具標記），偵測到漂移（" + "；".join(parts)
                    + "）；未自動重建以免覆蓋手寫內容（待 P2 AI 合併；或在檔中加入 "
                    + INDEX_BEGIN + " / " + INDEX_END + " 標記區改為自動維護）。")
        return IndexResult(None, "kept-handwritten", note)

    # ④ 標記不成對/順序異常 → fail-closed 保留原檔。
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
        return IndexResult(None, "kept-malformed",
                           "MEMORY.md 索引標記不成對或順序異常（須恰一對 BEGIN…END），未自動重建（保留原檔，請人工修正）。")

    # ⑤ 恰一對標記 → 只換框內；框前/框後逐字保留（連行終止符）。沿用 BEGIN 行的行終止符避免混合換行。
    i, j = begins[0], ends[0]
    nl = "\r\n" if lines[i].endswith("\r\n") else "\n"
    before = "".join(lines[:i])
    after = "".join(lines[j + 1:])
    block = INDEX_BEGIN + nl + "".join(e + nl for e in entries) + INDEX_END
    new = before + block + nl + after if after else before + block + nl
    if new == current_text:
        return IndexResult(None, "unchanged")
    return IndexResult(new, "rebuilt")


# ── 檔級 diff + classify（唯讀；對稱 scan.classify_session，Block 2）─────────────────────────────

@dataclass
class MemoryPlan:
    """單一 memory 檔的同步動作（對稱 scan.SessionPlan）。name = 檔名（兩側配對鍵，仿 sid）。"""

    name: str
    action: str          # identical / conflict-content / conflict-cross-file-identity / copy-to-hub / copy-to-local /
                         # suppressed-deleted / conflict-delete-vs-update / local-deleted / blocked-*（unsupported-name /
                         # casefold-collision / tombstone-corrupt / tombstone-no-identity / damaged-source / unmapped /
                         # uninitialized / no-baseline / no-local-baseline / known-deleted / bulk-local-deletion）
    direction: str | None
    reason: str
    src_hash: str | None = None  # **單邊 copy** 來源的正規化 content_hash（僅 copy-to-hub/copy-to-local 非 None）。
    #   供 apply 綁定寫出 bytes 到分類所據——read raw 後須重算 == 此值才寫，否則 auth 後、寫入前來源被改名/改內容
    #   仍照寫＝跨檔衝突/復活（codex P1d 3b2-R1 #1）。對稱 session 的 verified-bytes/snapshot。


# duty (a) 升級為跨檔同名 conflict 的動作集 = 所有「會自動寫入/自動定案」者：copy（寫檔）、identical/
# conflict-content（兩側配對結論）、**local-deleted（自動寫 tombstone）**。**local-deleted 必須在內**（codex
# P1d 塊末 fresh gate high）：否則 rename（old.md→new.md 同 name）會讓 old.md 自動寫 identity tombstone、new.md
# 之後被當該 identity 復活而 suppress ⇒ 改名被誤解成「刪除＋抑制」、靜默丟掉改名的事實。在分裂群裡一律不自動
# 處理、整組交人挑檔名/合併。其餘 fail-closed/復活閘（blocked-*/suppressed-deleted/conflict-delete-vs-update/
# known-deleted）更具體、不蓋過。
_CROSS_FILE_UPGRADABLE = frozenset({
    "copy-to-hub", "copy-to-local", "identical", "conflict-content", "local-deleted",
})


def _suppress_or_conflict_memory(name: str, lf: Path | None, hf: Path | None, tomb) -> MemoryPlan:
    """memory tombstone 的**條件式**判定（A17.1，對稱 `scan._suppress_or_conflict`）。

    與 session 版**唯一差異 = 比對基準用正規化 `content_hash`**（非 raw bytes digest）：memory 全程以正規化
    內容 hash 做 diff，tombstone base 也須是正規化 hash（A17.1：「base frontmatter hash + 正文 canonical
    hash」即 content_hash），否則純編碼/鍵序/尾 newline 往返會把 suppress 誤轉 conflict、與 memory diff 的
    正規化語意不一致。→ **Block 3 寫 memory tombstone 時 base_hash 必須存 `content_hash`**（同一 hash 空間
    才可比；session 端用 raw bytes 是因 session 比對亦走 raw bytes，兩者各自自洽）。

    現存側內容（content_hash）**全部 == base** → suppress（尊重刪除、不復活）；否則 → conflict-delete-vs-update：
    交人、不復活也不丟更新（A3）。涵蓋 base=None（fail-closed）、一側 damaged（content_hash=None≠base）、刪除後
    又改、兩側不一。兩種結果都非自動套用 → apply 不寫（不復活仍成立）。"""
    base = tomb.base_hash
    present = [p for p in (lf, hf) if p is not None]
    digests = [content_hash(load_memory(p)) for p in present]
    if base is not None and digests and all(d == base for d in digests):
        return MemoryPlan(name, "suppressed-deleted", None,
                          "hub memory tombstone 且現存內容==base → 不復活（A3）")
    return MemoryPlan(name, "conflict-delete-vs-update", None,
                      "hub memory tombstone 但現存內容≠base（刪後又改/兩側不一/base 不明/損壞）→ 交人（A3）")


def _identity_resurrection(name: str, lf: Path | None, hf: Path | None,
                           tomb_identities: dict) -> MemoryPlan | None:
    """換檔名復活防護（Block 2b duty b，A14/§7.2.3，memory 專屬）。**任一現存側**的 frontmatter `name` 命中
    記在**別檔名**（`t.target != name`）的 memory tombstone identity → 即「已刪事實換檔名復活」（檔名鍵閘只
    攔同檔名、換檔名會繞過）→ 條件式 `_suppress_or_conflict_memory`（涵蓋單邊**與兩側**：全 ==base→suppress、
    任一≠base→conflict）。**必須在配對前呼叫**（對稱檔名鍵閘）：否則兩側都還在的換檔名檔會先被當 identical/
    conflict-content 而忽視刪除（codex P1d-r1 critical）。

    同一 identity 撞**多個**別檔名 tombstone（base 不一；name 設計上唯一、實屬多次換檔名刪除）→ fail-closed
    `conflict-delete-vs-update`，**不臆測**此檔復活的是哪一次刪除、不靜默 suppress（codex P1d-r1 high）。

    **檔案端身分不可判（codex P1d-r3 high）**：有帶 identity 的 memory tombstone 在場（`tomb_identities` 非空）
    時，若某現存側自己的 frontmatter 身分讀不出（`name=None`：out-of-subset／微壞 frontmatter）→ 無法比對它是否
    某已刪 identity 的（換檔名＋frontmatter 微壞）復活 → fail-closed `blocked-tombstone-no-identity`（對稱 tombstone
    端不可判）。`tomb_identities` 空 → 無已刪 identity 可撞 → None（照常配對/單邊，不誤擋一般 out-of-subset 檔）。"""
    if not tomb_identities:
        return None
    for side in (lf, hf):
        if side is None:
            continue
        dn = load_memory(side).name
        if not dn:
            return MemoryPlan(name, "blocked-tombstone-no-identity", None,
                              "現存側 memory frontmatter 身分不可判（name 讀不出），且專案有帶 identity 的 memory "
                              "tombstone → 無法排除其為已刪事實復活 → fail-closed")
        mts = [t for t in tomb_identities.get(dn, ()) if t.target != name]
        if not mts:
            continue
        if len(mts) > 1:
            return MemoryPlan(name, "conflict-delete-vs-update", None,
                              "frontmatter name 命中多個別檔名 memory tombstone（base 不一、換檔名復活）→ "
                              "fail-closed 交人（不臆測哪次刪除）")
        return _suppress_or_conflict_memory(name, lf, hf, mts[0])
    return None


def classify_memory(
    name: str, lf: Path | None, hf: Path | None, *,
    both: bool, coverage_initialized: bool, tombs: dict,
    is_collision: bool = False, corrupt: set | None = None, known: set | None = None,
    has_baseline: bool = True, local_known: set | None = None,
    bulk_local_deletion: bool = False, has_local_baseline: bool = True,
    tomb_identities: dict | None = None, memory_identity_undecidable: bool = False,
    local_unreadable: bool = False,
) -> MemoryPlan:
    """單一 memory 檔的分類（plan 與 apply-下重新分類共用 → 不漂移；**閘序與 `scan.classify_session` 完全一致**）。

    lf/hf = 該檔名在 local/hub 的路徑（None 表該側無）。both = 專案兩側皆綁定。
    known = 該專案 state 已知 **hub** memory 檔名集（hub baseline）；local_known = 已知 **local** memory 檔名集
      （local baseline，§7.2「曾見過、現消失 → 不復活」的判據）。has_baseline / has_local_baseline = 本機是否
      已有此專案的 hub / local memory 基線（pk ∈ known_memory / local_memory；**空集 ≠ 缺欄位**，後者是 migration）。
    local_unreadable = local 現存檔中是否有**身分不可判**（非 fm_ok→name None）者；True 時 local-deleted 改 fail-closed
      （該刪除可能是改名到那無法解析身分的檔，duty a 無從分組，codex gate5）。由 `plan_memory_pair` 算。

    memory 與 session 的**唯一分類差異**：memory 無 DAG、無 fast-forward——「兩側皆在」只有兩種結果：正規化
    content_hash 相同 → `identical`；不同 → `conflict-content`（同檔名不同內容 → 衝突，待 memory-merge，**不自動
    合併**，§7.3）。其餘（tombstone 閘先於配對、known/local_known 對稱刪除偵測、各 baseline / collision / corrupt /
    damaged 閘）逐條對稱 session。

    另一道 memory 專屬前置閘：檔名若**無法與 tombstone 檔名無損 round-trip**（含斜線/反斜線；`_mem_file`
    sanitize 有損）→ `blocked-unsupported-name`，**不複製、不寫 tombstone**（否則 write/read 不對稱會讓刪除標記
    落錯身分、真實檔復活，codex P1d gate）。真實 memory 檔名為 slug、不受影響。

    **Block 2b duty (b)：換檔名復活防護（identity 鍵 tombstone，A14/§7.2.3，memory 專屬）**——
    `tomb_identities` = {frontmatter name → list[memory `Tombstone`]}（由 `plan_memory_pair` 從 `tombs` 建）。
    `_identity_resurrection` 在**配對前**（對稱檔名鍵閘）攔換檔名復活——含**兩側都還在**的情形（codex P1d-r1
    critical：否則兩側換檔名檔被當 identical 而忽視刪除）；多個同 identity tombstone → fail-closed conflict。
    另：`memory_identity_undecidable`（本專案有 identity 不可判的 memory tombstone：合法但 identity=None，或 corrupt
    memory tombstone）→ would-copy 單邊檔**與兩側配對檔**皆無法排除是其換檔名復活 → `blocked-tombstone-no-identity`
    （fail-closed，codex P1d-r1 high/medium + r2 high〔兩側〕；正常 Block 3 恆寫 identity，此閘僅救援壞/缺 identity
    標記，且**不搶** local-deleted/known-deleted/baseline 等刪除/更具體閘）。`tomb_identities` 空且無不可判 tombstone
    （或檔無可判定 name）→ 退回 Block 2 純檔名行為。"""
    if not tombstone.is_tombstone_safe_name(name):
        return MemoryPlan(name, "blocked-unsupported-name", None,
                          "memory 檔名含路徑分隔字元（斜線/反斜線）→ tombstone 檔名 sanitize 不可逆、"
                          "無法安全追蹤刪除 → 阻擋（不複製、不寫 tombstone），待可逆檔名編碼")
    if is_collision:
        return MemoryPlan(name, "blocked-casefold-collision", None,
                          "casefold 撞名 memory 檔（同側或跨側 case-only，跨 OS 碰撞風險，A9）")
    # tombstone 閘**先於**配對分類（對稱 codex r14-1）：刪除標記不論成對/單邊都該抑制，否則 tombstoned 的
    # memory 若兩側都還在會被當 identical/conflict 處理＝忽視刪除。
    if ("memory", name) in tombs:
        return _suppress_or_conflict_memory(name, lf, hf, tombs[("memory", name)])
    if corrupt and ("memory", name) in corrupt:
        return MemoryPlan(name, "blocked-tombstone-corrupt", None,
                          "memory tombstone 損壞、無法確認是否已刪 → 阻擋（fail-closed，不復活）")
    # 換檔名復活防護（Block 2b duty b）：**配對前**（對稱檔名鍵閘），含兩側都還在的情形（codex P1d-r1 critical）。
    res = _identity_resurrection(name, lf, hf, tomb_identities or {})
    if res is not None:
        return res
    if lf and hf:
        hl, hh = content_hash(load_memory(lf)), content_hash(load_memory(hf))
        if hl is None or hh is None:
            return MemoryPlan(name, "blocked-damaged-source", None,
                              "兩側配對但一側 memory 損壞（0-byte/全空白/解碼錯）→ 不自動處理")
        # 兩側皆在也須過 undecidable 閘（codex P1d-r2 high）：identity 不可判的 memory tombstone 在場時，
        # 兩側 new.md 可能正是其換檔名復活 → 不可當 identical/conflict-content 報「in-sync」（同單邊 would-copy，
        # decidable 復活已由 `_identity_resurrection` 配對前攔下；此處擋「連刪了什麼都讀不出」的兩側情形）。
        if memory_identity_undecidable:
            return MemoryPlan(name, "blocked-tombstone-no-identity", None,
                              "本專案存在 identity 不可判的 memory tombstone（None/corrupt）→ 無法確認此兩側配對檔非換檔名復活 → fail-closed")
        if hl == hh:
            return MemoryPlan(name, "identical", None, "兩側內容相同（正規化後）")
        return MemoryPlan(name, "conflict-content", None,
                          "同檔名兩側內容不同 → 衝突（待 memory-merge，不自動合併）")
    # 單邊存在
    present = "local" if lf else "hub"
    if not both:
        return MemoryPlan(name, "blocked-unmapped", None,
                          "專案未對應到對側（需 --map / bootstrap），單邊 memory 不落地")
    if not coverage_initialized:
        return MemoryPlan(name, "blocked-uninitialized", None, "專案未 bootstrap，單邊 memory 不自動處理")
    if not has_baseline:
        return MemoryPlan(name, "blocked-no-baseline", None,
                          "本機未對此專案 bootstrap → 單邊 memory 不自動複製（避免復活刪除）")
    # present=hub 另需 **local memory 基線**：migration（舊 state 有 known_memory、無 local_memory）下無從分辨
    # 「新 hub memory」與「本機已刪」→ fail-closed，不 copy、不 tombstone，待重 bootstrap（對稱 codex r24-1）。
    if present == "hub" and not has_local_baseline:
        return MemoryPlan(name, "blocked-no-local-baseline", None,
                          "本機無此專案 local memory 基線（疑舊 state 遷移）→ 單邊 hub memory 不自動處理，請重 bootstrap")
    # 「已知 memory 單邊消失（無 tombstone）」的**對稱**偵測（對稱 session）：
    #  - hub 側消失（present=local，name∈known）：hub 永久歸檔不該無故掉檔 → 不信任 → 交人（blocked-known-deleted）。
    #  - local 側消失（present=hub，name∈local_known）：使用者刪自己的 local memory 是正常（常為敏感/過期資訊）
    #    → 信任 → local-deleted（apply 寫 hub tombstone 通知對側，不刪 hub，A3）；但**大量**消失 → 整批交人。
    if present == "local" and name in (known or set()):
        return MemoryPlan(name, "blocked-known-deleted", None,
                          "已知 memory 在 hub 消失且無 tombstone（疑刪除，非新檔）→ 交人決策")
    if present == "hub" and name in (local_known or set()):
        if bulk_local_deletion:
            return MemoryPlan(name, "blocked-bulk-local-deletion", None,
                              "本專案 local memory 大量消失（疑掛錯碟/被清空）→ 不自動寫 tombstone，整批交人")
        # 換檔名復活防護的**對稱缺口**（codex P1d gate5 high）：local-deleted 會寫帶 identity 的 hub tombstone；
        # 若 local 有**無法解析身分**（非 fm_ok → name None）的現存檔，它可能正是此事實**改名**到的目標（duty a
        # 因身分 None 無從分組偵測）→ 自動寫刪除 tombstone 會把「改名」誤記成「刪除」、日後抑制復活＝靜默丟改名
        # 的事實 → fail-closed 不自動寫（交人）。正常（local 全 fm_ok）不觸發。
        if local_unreadable:
            return MemoryPlan(name, "blocked-tombstone-no-identity", None,
                              "本機刪除疑似改名：local 有無法解析身分的現存檔（可能是此事實改名目標）→ 不自動寫刪除 tombstone，交人")
        return MemoryPlan(name, "local-deleted", None,
                          "已知 local memory 消失（本機刪除）→ 寫 hub tombstone 通知對側（不刪 hub 歸檔，A3）")
    # 單邊 copy 來源也要過損壞閘（對稱 codex r14-2）：damaged（0-byte/全空白/解碼錯）不複製散播。
    # 註：frontmatter 超出子集但檔身可解碼者 content_hash 走 raw fallback（非 None）→ 仍是合法可複製 memory。
    src_hash = content_hash(load_memory(lf or hf))   # 來源正規化 hash（供 apply 綁定寫出 bytes，src_hash）
    if src_hash is None:
        return MemoryPlan(name, "blocked-damaged-source", None,
                          "單邊來源 memory 損壞（0-byte/全空白/解碼錯），不複製")
    # 換檔名復活防護（Block 2b duty b / codex P1d-r1 high+medium）：本專案有 identity 不可判的 memory tombstone
    # （合法但 identity=None，或 corrupt memory tombstone）→ 無法排除此 would-copy 檔是其換檔名復活 → fail-closed
    # 不複製。decidable 的 identity 復活已在配對前 `_identity_resurrection` 攔下；此處擋「連刪了什麼都讀不出」。
    if memory_identity_undecidable:
        return MemoryPlan(name, "blocked-tombstone-no-identity", None,
                          "本專案存在 identity 不可判的 memory tombstone（None/corrupt）→ 無法確認此單邊檔非換檔名復活 → 不複製")
    action = "copy-to-hub" if present == "local" else "copy-to-local"
    return MemoryPlan(name, action, f"{present}->other", f"單邊新 memory（{present}）", src_hash=src_hash)


def plan_memory_pair(
    local_dir: Path | None,
    hub_dir: Path | None,
    *,
    coverage_initialized: bool,
    tombs: dict | None = None,
    corrupt: set | None = None,
    known: set | None = None,
    has_baseline: bool = True,
    local_known: set | None = None,
    has_local_baseline: bool = True,
) -> list[MemoryPlan]:
    """單一（已配對）專案的 memory 逐檔動作（對稱 `scan.plan_project_pair`）。

    `local_dir`/`hub_dir` = **專案夾**（非 memory 子夾）；本函式自取 `<proj>/memory/` 列舉（缺夾→空）。
    成對 classify content_hash；單邊查 tombstone/coverage/known/local_known。collision/bulk 由兩側檔名集算。

    **Block 2b：exact-frontmatter 跨檔身分（DESIGN §7.2.3 / A14，memory 專屬、session 無對應、只做 exact）。**
    檔名配對之上再加兩道以 frontmatter `name`（Block 1 的唯一 slug identity）為鍵的 pass：
      - **duty (a) 跨檔同名 conflict**：把兩側現存檔依 `name` 分組（`name` 無法判定者退回檔名身分、不入分組）；
        同一 `name` 落在 **≥2 個不同檔名** → 同一事實被拆成多檔（rename，或兩台各自新建同 slug）→ 不可無腦
        雙向 copy（製造重複事實）也不可當 identical 放著（索引兩條同 name）→ 升級成 `conflict-cross-file-identity`
        交人挑檔名/合併。**只升級會「寫入或視為一致」的動作**（`copy-*`/`identical`/`conflict-content`）；
        fail-closed / 刪除 / 復活閘（`blocked-*` / `suppressed-deleted` / `conflict-delete-vs-update` / `local-deleted`）
        更具體、不蓋過。
      - **duty (b) 換檔名復活防護**：見 `classify_memory` / `_identity_resurrection`——`tomb_identities`
        ({name → list[memory Tombstone]}) 在**配對前**把任一現存側（含兩側）的 `name` 對到記在**別檔名**的 memory
        tombstone identity → suppress/conflict（不復活）；多個同 identity → fail-closed conflict。另
        `memory_identity_undecidable`（identity=None 或 corrupt memory tombstone）→ would-copy 一律 fail-closed 阻擋。
    兩道**必須在 Block 3 wire apply 之前**落地：本模組仍唯讀只產 plan，但一旦 apply 消費 `copy-*` 即有換檔名
    復活/重複事實風險（codex P1d-r1 High）。模糊近似比對留 P2。"""
    tombs = tombs or {}
    both = local_dir is not None and hub_dir is not None
    local = list_memory_files(memory_dir(local_dir)) if local_dir is not None else {}
    hub = list_memory_files(memory_dir(hub_dir)) if hub_dir is not None else {}
    # NFC/NFD 折疊（e2e-r1 Finding 2）：memory 檔名可含 NFC/NFD 別名（跨平台撰寫），須以 name_key 折疊才認得撞名
    # （session 端 sid=UUID 無此問題、仍用預設 casefold）。判定端亦用 name_key（見 classify_memory 呼叫的 is_collision）。
    collisions = collision_casefolds(local.keys(), hub.keys(), keyfn=name_key)
    bulk = is_bulk_local_deletion(local_known, set(local.keys()))
    # duty (b)：{frontmatter name slug → list[memory Tombstone]}（識別「換檔名復活」）。只收 kind=memory 且 identity
    # 為 **slug 形**者（與檔案端 `_fm_ok_identity` 同判準 `_is_slug`；非 slug 的 tombstone identity〔如 "fact "
    # 帶空白、"fact\n" 帶換行〕視為**不可判**、不放進 decidable map，否則乾淨 name 的復活檔不匹配它 → 漏擋復活，codex gate2/3）。
    # 同一 identity 撞多個 tombstone 不壓平、保留全部 → `_identity_resurrection` 多者 fail-closed conflict
    # （不臆測哪次刪除，codex P1d-r1 high）。排序求決定性。
    tomb_identities: dict[str, list] = {}
    for (k, _t), tb in sorted(tombs.items()):
        if k == "memory" and _is_slug(tb.identity):
            tomb_identities.setdefault(tb.identity, []).append(tb)
    # 「identity 不可判」的 memory tombstone（identity 缺/非 slug，或 corrupt memory tombstone）存在 → would-copy
    # 檔無法排除是其換檔名復活 → fail-closed 阻擋（codex P1d-r1 high/medium + gate2/gate3）。正常 Block 3 恆寫 slug identity。
    memory_identity_undecidable = (
        any(k == "memory" and not _is_slug(tb.identity) for (k, _t), tb in tombs.items())
        or any(k == "memory" for k, _t in (corrupt or set()))
    )
    # duty (a)：{frontmatter name → 現存檔名集}（兩側）。每檔 load 一次取 name（小檔、plan-time 預覽，可接受；
    # apply 鎖內會重分類）。name=None（非 fm_ok）不入分組 → 退回純檔名身分。**並記 local 是否有 name=None 的
    # 現存檔**（`local_unreadable`）：供 local-deleted 對稱守住「改名到非 fm_ok 檔」的盲區（codex gate5）。
    name_to_files: dict[str, set[str]] = {}
    local_unreadable = False
    for side_files, is_local in ((local, True), (hub, False)):
        for fname, path in side_files.items():
            dn = load_memory(path).name
            if dn:
                name_to_files.setdefault(dn, set()).add(fname)
            elif is_local:
                local_unreadable = True
    split_files = {f for files in name_to_files.values() if len(files) >= 2 for f in files}
    plans: list[MemoryPlan] = []
    for name in sorted(set(local) | set(hub)):
        plans.append(classify_memory(
            name, local.get(name), hub.get(name), both=both,
            coverage_initialized=coverage_initialized, tombs=tombs, corrupt=corrupt, known=known,
            has_baseline=has_baseline, is_collision=name_key(name) in collisions,
            local_known=local_known, bulk_local_deletion=bulk, has_local_baseline=has_local_baseline,
            tomb_identities=tomb_identities, memory_identity_undecidable=memory_identity_undecidable,
            local_unreadable=local_unreadable,
        ))
    # duty (a) 後置升級：分組命中的檔，若動作仍是「會寫入/視為一致」者 → conflict-cross-file-identity。
    for pl in plans:
        if pl.name in split_files and pl.action in _CROSS_FILE_UPGRADABLE:
            pl.action = "conflict-cross-file-identity"
            pl.direction = None
            pl.reason = ("同 frontmatter name 出現在多個檔名（同一事實被拆成多檔／rename）→ 交人挑檔名或合併"
                         "（不自動雙向 copy 製造重複、不當 identical 漂移索引，A14/§7.2.3）")
    return plans
