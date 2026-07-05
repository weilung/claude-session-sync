"""設定檔（決定 #1/#3/#4）：`tomllib` 讀 + 手寫極簡 writer，零第三方相依、跨 OS。

config.toml（DESIGN §8.4）：
    own_hub = '/media/will/HomeDrive/HomeJSONL'
    force_unsafe_lock = false          # 決定 #8：不可靠 FS 預設 best-effort+偵測升級 abort；
                                       # 設 true 等於明確 --force-unsafe-lock 永久版
    [remotes]
    office = '/media/will/HomeDrive/OfficeJSONL'

跨路徑 local↔hub 綁定（A17.4）放 state.json，不在這裡。
寫入用 **TOML literal string（單引號）** 容納 Windows 反斜線路徑，免轉義（決定 #4 須測）。
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

APP = "claude-session-sync"
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


class ConfigError(Exception):
    """config.toml 型別/結構不符。寧可明確報錯，也不靜默吃進危險值（如把 "false" 當 True）。"""


def default_config_path() -> Path:
    """跨 OS 設定路徑：POSIX 走 XDG_CONFIG_HOME/~/.config；Windows 走 %APPDATA%。"""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP / "config.toml"


@dataclass
class Config:
    own_hub: str | None = None
    remotes: dict[str, str] = field(default_factory=dict)
    force_unsafe_lock: bool = False
    path: str | None = None  # 載入來源（save 預設寫回這裡）


def _toml_str(v: str) -> str:
    """序列化字串值。優先 literal string（單引號）原樣容納反斜線；含單引號/換行/控制字元才退
    basic string 並完整轉義（含 \\uXXXX 控制字元），避免寫出讀不回的 TOML。"""
    has_ctrl = any(ord(c) < 0x20 for c in v)
    if "'" not in v and not has_ctrl and "\n" not in v and "\r" not in v:
        return f"'{v}'"
    out = []
    for c in v:
        if c == "\\":
            out.append("\\\\")
        elif c == '"':
            out.append('\\"')
        elif c == "\n":
            out.append("\\n")
        elif c == "\r":
            out.append("\\r")
        elif c == "\t":
            out.append("\\t")
        elif ord(c) < 0x20:
            out.append(f"\\u{ord(c):04X}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def _toml_key(name: str) -> str:
    """bare key（[A-Za-z0-9_-]）直接用；否則用 quoted key，避免含點/空白破壞結構。"""
    return name if _BARE_KEY.match(name) else _toml_str(name)


def to_toml(c: Config) -> str:
    lines: list[str] = []
    if c.own_hub is not None:
        lines.append(f"own_hub = {_toml_str(c.own_hub)}")
    lines.append(f"force_unsafe_lock = {'true' if c.force_unsafe_lock else 'false'}")
    if c.remotes:
        lines.append("")
        lines.append("[remotes]")
        for name in sorted(c.remotes):
            lines.append(f"{_toml_key(name)} = {_toml_str(c.remotes[name])}")
    return "\n".join(lines) + "\n"


def load(path: str | os.PathLike | None = None) -> Config:
    """讀設定。檔不存在 → 回空 Config（不報錯；首次 config set 才會建檔）。"""
    p = Path(path) if path is not None else default_config_path()
    if not p.exists():
        return Config(path=str(p))
    with open(p, "rb") as f:
        try:
            data = tomllib.load(f)
        except Exception as e:  # noqa: BLE001
            raise ConfigError(f"config.toml 無法解析：{e}") from e

    own_hub = data.get("own_hub")
    if own_hub is not None and not isinstance(own_hub, str):
        raise ConfigError("own_hub 必須是字串")

    ful = data.get("force_unsafe_lock", False)
    if not isinstance(ful, bool):  # 注意：TOML 的 "false"(字串) 不是 bool → 擋下，不靜默變 True
        raise ConfigError('force_unsafe_lock 必須是布林 true/false（不可加引號）')

    remotes_raw = data.get("remotes", {})
    if not isinstance(remotes_raw, dict):
        raise ConfigError("remotes 必須是表(table)")
    remotes: dict[str, str] = {}
    for k, v in remotes_raw.items():
        if not isinstance(v, str):
            raise ConfigError(f"remote '{k}' 的值必須是字串路徑")
        remotes[str(k)] = v

    return Config(own_hub=own_hub, remotes=remotes, force_unsafe_lock=ful, path=str(p))


def save(c: Config, path: str | os.PathLike | None = None) -> str:
    """原子寫（同目錄 temp + os.replace）。回寫出的路徑。"""
    p = Path(path) if path is not None else Path(c.path or default_config_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(to_toml(c), encoding="utf-8")
    os.replace(tmp, p)  # 同目錄 rename：POSIX 原子；Windows os.replace 可覆蓋
    c.path = str(p)
    return str(p)
