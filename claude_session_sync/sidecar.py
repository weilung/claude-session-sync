"""Sidecar：專案同一性（git 指紋優先）與 session meta（A4 hashes）。

依據 DESIGN §8.2/§8.3 + 附錄 A4 + PLAN v0.4 §2.5：
  - 跨機同一性**優先 git remote / repo fingerprint**；cwd 字串永不單獨自動落地（決定 #7）。
  - 「無法判斷」與「同一 repo」嚴格分開：判不出一律 NEEDS_MAP，不猜。
  - 降級：no-git → NEEDS_MAP；同 first-commit 不同 remote（fork/rename）→ AMBIGUOUS（要人確認）。

P1a 只做**讀 + 比對 + 計算**（不寫 sidecar；寫入是 P1b）。純標準庫（git 走 subprocess）。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .lineset import analyze

SCHEMA_VERSION = 1
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_PORTS = {"ssh": 22, "git": 9418, "http": 80, "https": 443}


# ── git remote 正規化 ────────────────────────────────────────────────────

def normalize_remote(url: str | None) -> str | None:
    """把各種 remote URL 形式正規化成 `host[:port]/path`（小寫、去 .git、去認證/scheme）。
    **保留非預設 port**（同 host/path 不同 port = 不同伺服器，不可當同專案）。IPv6/query 走 urllib。"""
    u = (url or "").strip()
    if not u:
        return None
    u = re.sub(r"\.git/?$", "", u)
    if "://" not in u:
        # scp-like: user@host:owner/repo（無 scheme）
        m = re.match(r"^[\w.+-]+@([^:/]+):(.+)$", u)
        if m:
            return f"{m.group(1).lower()}/{m.group(2).strip('/').lower()}"
        return u.lower()  # 本地路徑/未知形式
    try:
        parsed = urllib.parse.urlparse(u)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return u.lower()
    if not host:
        return u.lower()
    if port is not None and port != _DEFAULT_PORTS.get(parsed.scheme):
        host = f"{host}:{port}"
    path = parsed.path.strip("/").lower()
    return f"{host}/{path}" if path else host


def _git(cwd: str | Path, *args: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:  # noqa: BLE001 - git 不存在/逾時都當無 git
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


# ── 專案指紋 ─────────────────────────────────────────────────────────────

@dataclass
class ProjectFingerprint:
    cwd: str
    repo_root: str | None
    remotes: dict[str, str]          # name -> normalized remote
    first_commit: str | None
    has_git: bool

    @property
    def remote_set(self) -> set[str]:
        return set(self.remotes.values())


def local_fingerprint(cwd: str | Path) -> ProjectFingerprint:
    root = _git(cwd, "rev-parse", "--show-toplevel")
    remotes: dict[str, str] = {}
    first: str | None = None
    if root:
        for name in (_git(cwd, "remote") or "").split():
            norm = normalize_remote(_git(cwd, "remote", "get-url", name))
            if norm:
                remotes[name] = norm
        roots = _git(cwd, "rev-list", "--max-parents=0", "--all") or ""
        first = sorted(roots.split())[0] if roots.split() else None
    return ProjectFingerprint(str(cwd), root, remotes, first, has_git=bool(root))


# ── _project.json（hub 端專案 sidecar）────────────────────────────────────

@dataclass
class ProjectSidecar:
    git_remote: str | None = None        # 正規化（primary）
    first_commit: str | None = None
    repo_root: str | None = None
    observed_cwds: list[str] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "git_remote": self.git_remote,
            "first_commit": self.first_commit,
            "repo_root": self.repo_root,
            "observed_cwds": self.observed_cwds,
        }


def read_project_sidecar(project_dir: str | Path) -> ProjectSidecar | None:
    p = Path(project_dir) / "_project.json"
    if not p.exists() or p.is_symlink():   # leaf 防線：symlink _project.json → 不跟隨讀界外身分（treat as absent，e2e）
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(d, dict):
        return None
    gr, fc, rr = d.get("git_remote"), d.get("first_commit"), d.get("repo_root")
    if not all(x is None or isinstance(x, str) for x in (gr, fc, rr)):
        return None  # 壞 sidecar → blocked，不可扭曲身分
    cwds = d.get("observed_cwds", [])
    if not isinstance(cwds, list) or not all(isinstance(x, str) for x in cwds):
        return None
    sv = d.get("schema_version", SCHEMA_VERSION)
    if not isinstance(sv, int) or isinstance(sv, bool):
        return None
    return ProjectSidecar(git_remote=gr, first_commit=fc, repo_root=rr,
                          observed_cwds=list(cwds), schema_version=sv)


# ── 同一性比對 ───────────────────────────────────────────────────────────

class MatchStatus(str, Enum):
    MATCH = "match"
    NO_MATCH = "no-match"
    AMBIGUOUS = "ambiguous"       # 同 lineage 不同 remote（fork/rename）→ 要人確認
    NEEDS_MAP = "needs-map"       # 缺 git 身分 → 不可自動，要 --map


@dataclass
class MatchResult:
    status: MatchStatus
    reason: str


def match(local: ProjectFingerprint, sc: ProjectSidecar) -> MatchResult:
    """本機專案指紋 vs hub _project.json。git 指紋優先；判不出一律 NEEDS_MAP，不靠 cwd 猜。"""
    if not local.has_git or not sc.git_remote:
        return MatchResult(MatchStatus.NEEDS_MAP, "缺 git remote 身分，需 --map 人工對應")
    if sc.git_remote in local.remote_set:
        return MatchResult(MatchStatus.MATCH, f"git remote 相符：{sc.git_remote}")
    if sc.first_commit and local.first_commit and sc.first_commit == local.first_commit:
        return MatchResult(MatchStatus.AMBIGUOUS, "first-commit 相同但 remote 不同（fork/rename）→ 要確認")
    return MatchResult(MatchStatus.NO_MATCH, "remote 與 first-commit 皆不符")


# ── session meta（A4 hashes；P1a 計算 + 讀）──────────────────────────────

@dataclass
class SessionMeta:
    content_hash: str            # 全檔 ordered canonical content digest
    tail_hash: str               # 末段 digest（快速偵測截斷/延伸）
    line_count: int
    uuid_count: int
    non_uuid_count: int
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
            "tail_hash": self.tail_hash,
            "line_count": self.line_count,
            "uuid_count": self.uuid_count,
            "non_uuid_count": self.non_uuid_count,
        }


def _sha(parts: list[str]) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def compute_session_meta(path: str | Path, tail: int = 20) -> SessionMeta | None:
    """從一個 jsonl 算 A4 meta。**任何 damaged 都回 None**：檔級(zero/blank/decode)、壞 JSON 行、
    或同檔同 uuid 異 hash —— meta 不得替 damaged 檔背書（codex r5 critical）。"""
    shape = analyze(str(path))
    if shape.is_damaged:
        return None
    ok = shape.lines
    hashes = [ln.canon_hash or "" for ln in ok]
    uuid_count = sum(1 for ln in ok if ln.uuid)
    return SessionMeta(
        content_hash=_sha(hashes),
        tail_hash=_sha(hashes[-tail:]),
        line_count=len(ok),
        uuid_count=uuid_count,
        non_uuid_count=len(ok) - uuid_count,
    )


def read_session_meta(meta_path: str | Path) -> SessionMeta | None:
    p = Path(meta_path)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(d, dict):
        return None
    ch, th = d.get("content_hash"), d.get("tail_hash")
    if not (isinstance(ch, str) and _HEX64.match(ch) and isinstance(th, str) and _HEX64.match(th)):
        return None
    counts: dict[str, int] = {}
    for key in ("line_count", "uuid_count", "non_uuid_count"):
        v = d.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:  # bool 是 int 子類，須排除
            return None
        counts[key] = v
    sv = d.get("schema_version", SCHEMA_VERSION)
    if not isinstance(sv, int) or isinstance(sv, bool):
        return None
    return SessionMeta(content_hash=ch, tail_hash=th, schema_version=sv, **counts)
