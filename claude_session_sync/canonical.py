"""行解析、canonical hash、編碼吸收（畢業自 spikes/canonical.py，加三態 file-state）。

設計依據：DESIGN 附錄 A2/A8 + 附錄 B（B1 跨 OS hash 穩定、非 UTF-8 整檔判 damaged 不 crash）。

三態（PLAN v0.3 / codex H1）：
  - ZERO_BYTE：0-byte 檔 → damaged。
  - BLANK：可解碼但只有空白 → damaged（Claude JSONL 不該只有空白）。
  - DECODE_ERROR：任何已知編碼都解不開 → damaged（不 raise）。
  - OK：可解碼且有非空白內容（個別行仍可能是壞 JSON，由 Line.ok 標記）。
純標準庫。
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any

BOM = "﻿"

# 依 BOM 前綴選 codec（記事本「另存為」會產生這些）。4-byte 的 UTF-32 要排在 UTF-16 前。
_BOM_CODECS: list[tuple[bytes, str]] = [
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
]


class FileState(str, Enum):
    OK = "ok"
    ZERO_BYTE = "zero_byte"
    BLANK = "blank_only"
    DECODE_ERROR = "decode_error"

    @property
    def is_damaged(self) -> bool:
        return self is not FileState.OK


def _nfc(obj: Any) -> Any:
    """遞迴對字串**值**做 Unicode NFC（解 macOS NFD vs Linux NFC 差異）。

    **不正規化 dict 鍵**（codex r21）：NFC 折疊不同的 key（如 "e\\u0301" 與 "\\u00e9"）會在
    dict 推導裡互蓋、**丟掉一個 key/value** → canon_dumps 寫出殘缺行、canon_hash 讓不同行雷同
    （誤去重）。JSON 物件鍵是檔案**內容**（非檔名），OS 不會對它做 NFD/NFC 正規化，故跨 OS 穩定
    性不需要正規化 key；值才需要（內嵌路徑/檔名在 macOS 可能是 NFD）。"""
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    if isinstance(obj, list):
        return [_nfc(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _nfc(v) for k, v in obj.items()}
    return obj


def canon_dumps(obj: Any) -> str:
    """穩定鍵序 + NFC + 緊湊分隔的 canonical JSON 文字（不含換行）。

    與 `canon_hash` 共用同一正規化：故 `canon_hash(json.loads(canon_dumps(obj))) == canon_hash(obj)`
    （idempotent）。session_merge 用它輸出 union 行 → 同一 line-identity 在任何機器都序列化成
    相同 bytes（跨機 union 收斂、再讀回分類一致）。
    """
    return json.dumps(_nfc(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def canon_hash(obj: Any) -> str:
    """穩定鍵序 + NFC + 緊湊分隔 → sha256。吸收行內空白/換行/鍵序差異，但不壓掉語意差異。"""
    return hashlib.sha256(canon_dumps(obj).encode("utf-8")).hexdigest()


def decode_bytes(raw: bytes) -> tuple[str | None, str | None]:
    """bytes → (text, err)；err!=None 表整檔無法解碼。依 BOM 偵測編碼，預設 UTF-8。"""
    for bom, enc in _BOM_CODECS:
        if raw.startswith(bom):
            try:
                return raw.decode(enc), None
            except Exception as e:  # noqa: BLE001 - 任何解碼失敗都回 err，不 raise
                return None, f"{enc} decode failed: {e}"
    try:
        return raw.decode("utf-8"), None
    except Exception as e:  # noqa: BLE001
        return None, f"utf-8 decode failed: {e}"


def parse_line(raw: str) -> tuple[bool | None, dict | None]:
    """單行 → (ok, obj)。ok=None=空白行；ok=False=壞 JSON；ok=True=正常。去前置 BOM。"""
    s = raw.rstrip("\r\n").lstrip(BOM)
    if s.strip() == "":
        return None, None
    try:
        obj = json.loads(s)
    except Exception:  # noqa: BLE001
        return False, None
    if not isinstance(obj, dict):
        return False, None
    return True, obj


@dataclass(frozen=True)
class Line:
    """一行的行身分。ok=False 代表壞 JSON 行（damaged 候選）。"""

    index: int
    ok: bool
    obj: dict | None
    uuid: str | None
    parent: str | None
    ts: str | None
    type: str | None
    is_sidechain: bool
    canon_hash: str | None

    @property
    def identity(self) -> tuple[str | None, str | None]:
        """行身分：有 uuid 用 (uuid, hash)；無 uuid 用 (None, content-hash)。"""
        return (self.uuid, self.canon_hash) if self.uuid else (None, self.canon_hash)

    @property
    def is_tool_fanout(self) -> bool:
        """平行工具 fan-out 行：type=user 且帶 toolUseResult（非真 tip）。"""
        return self.type == "user" and isinstance(self.obj, dict) and "toolUseResult" in self.obj


@dataclass
class LoadResult:
    state: FileState
    lines: list[Line]
    decode_error: str | None = None

    @property
    def has_bad(self) -> bool:
        return any(not ln.ok for ln in self.lines)

    @property
    def ok_lines(self) -> list[Line]:
        return [ln for ln in self.lines if ln.ok]


def _line_from_obj(index: int, obj: dict) -> Line:
    return Line(
        index=index,
        ok=True,
        obj=obj,
        uuid=obj.get("uuid"),
        parent=obj.get("parentUuid"),
        ts=obj.get("timestamp"),
        type=obj.get("type"),
        is_sidechain=bool(obj.get("isSidechain")),
        canon_hash=canon_hash(obj),
    )


def load(path: str) -> LoadResult:
    """讀 jsonl → LoadResult。先判 file-state 三態，再逐行解析。"""
    with open(path, "rb") as f:
        raw = f.read()
    return load_bytes(raw)


def load_bytes(raw: bytes) -> LoadResult:
    """同 load 但吃 **bytes**（供「讀一次來源 bytes → 分類同一份 bytes → 寫同一份」綁定，transfer）。"""
    if len(raw) == 0:
        return LoadResult(FileState.ZERO_BYTE, [])
    text, err = decode_bytes(raw)
    if err is not None:
        return LoadResult(FileState.DECODE_ERROR, [], err)
    assert text is not None
    if text.strip() == "":
        return LoadResult(FileState.BLANK, [])

    lines: list[Line] = []
    norm = text.replace("\r\n", "\n").replace("\r", "\n")
    for i, raw_ln in enumerate(norm.split("\n")):
        ok, obj = parse_line(raw_ln)
        if ok is None:
            continue
        if ok and obj is not None:
            lines.append(_line_from_obj(i, obj))
        else:
            lines.append(Line(i, False, None, None, None, None, None, False, None))
    return LoadResult(FileState.OK, lines)
