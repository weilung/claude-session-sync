import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from claude_session_sync import sidecar as sc
from claude_session_sync.sidecar import MatchStatus
from tests import fixtures as fx

HAS_GIT = shutil.which("git") is not None


def _run(*args, cwd=None):
    subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)


def _make_repo(path: Path, remote_url: str):
    path.mkdir(parents=True, exist_ok=True)
    _run("git", "init", "-q", cwd=path)
    _run("git", "remote", "add", "origin", remote_url, cwd=path)
    (path / "f.txt").write_text("x", encoding="utf-8")
    _run("git", "add", "-A", cwd=path)
    _run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init", cwd=path)


class TestNormalizeRemote(unittest.TestCase):
    def test_forms_canonicalize_equal(self):
        canon = "github.com/weilung/repo"
        for url in [
            "git@github.com:weilung/repo.git",
            "https://github.com/weilung/repo.git",
            "https://github.com/weilung/repo",
            "ssh://git@github.com/weilung/repo.git",
            "git@GitHub.com:Weilung/Repo.git",
        ]:
            self.assertEqual(sc.normalize_remote(url), canon, url)

    def test_none_empty(self):
        self.assertIsNone(sc.normalize_remote(None))
        self.assertIsNone(sc.normalize_remote("  "))

    def test_non_default_port_kept_default_dropped(self):
        self.assertNotEqual(
            sc.normalize_remote("ssh://git@example.com:2222/org/repo"),
            sc.normalize_remote("ssh://git@example.com/org/repo"),
        )
        self.assertEqual(
            sc.normalize_remote("https://github.com:443/weilung/repo"),
            sc.normalize_remote("https://github.com/weilung/repo"),
        )

    def test_query_and_ipv6_do_not_crash(self):
        self.assertEqual(
            sc.normalize_remote("https://github.com/weilung/repo?ref=main"),
            "github.com/weilung/repo",
        )
        self.assertIsNotNone(sc.normalize_remote("ssh://git@[2001:db8::1]:2222/org/repo"))


@unittest.skipUnless(HAS_GIT, "git not available")
class TestFingerprintMatch(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_local_fingerprint(self):
        r = self.tmp / "a"
        _make_repo(r, "git@github.com:weilung/repo.git")
        fp = sc.local_fingerprint(r)
        self.assertTrue(fp.has_git)
        self.assertEqual(fp.remote_set, {"github.com/weilung/repo"})
        self.assertIsNotNone(fp.first_commit)

    def test_match(self):
        r = self.tmp / "a"
        _make_repo(r, "git@github.com:weilung/repo.git")
        fp = sc.local_fingerprint(r)
        side = sc.ProjectSidecar(git_remote="github.com/weilung/repo", first_commit=fp.first_commit)
        self.assertEqual(sc.match(fp, side).status, MatchStatus.MATCH)

    def test_no_match(self):
        r = self.tmp / "a"
        _make_repo(r, "git@github.com:weilung/repo.git")
        fp = sc.local_fingerprint(r)
        side = sc.ProjectSidecar(git_remote="other.com/x/y", first_commit="deadbeef")
        self.assertEqual(sc.match(fp, side).status, MatchStatus.NO_MATCH)

    def test_ambiguous_fork_same_first_commit_diff_remote(self):
        a = self.tmp / "a"
        _make_repo(a, "git@github.com:weilung/repo.git")
        b = self.tmp / "b"
        _run("git", "clone", "-q", str(a), str(b))                  # 同 root commit
        _run("git", "remote", "set-url", "origin", "git@github.com:fork/repo.git", cwd=b)
        fp_b = sc.local_fingerprint(b)
        side_a = sc.ProjectSidecar(git_remote="github.com/weilung/repo", first_commit=fp_b.first_commit)
        self.assertEqual(sc.match(fp_b, side_a).status, MatchStatus.AMBIGUOUS)

    def test_needs_map_when_no_git(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        fp = sc.local_fingerprint(plain)
        self.assertFalse(fp.has_git)
        side = sc.ProjectSidecar(git_remote="github.com/weilung/repo")
        self.assertEqual(sc.match(fp, side).status, MatchStatus.NEEDS_MAP)


class TestSessionMeta(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_meta_counts_and_determinism(self):
        p = fx.write_jsonl(fx.compact_system_root(), str(self.tmp / "s.jsonl"))
        m1 = sc.compute_session_meta(p)
        m2 = sc.compute_session_meta(p)
        self.assertIsNotNone(m1)
        self.assertEqual(m1, m2)  # 確定性
        # compact_system_root: 6 uuid 行 + 1 last-prompt(no-uuid)
        self.assertEqual(m1.uuid_count, 6)
        self.assertEqual(m1.non_uuid_count, 1)
        self.assertEqual(m1.line_count, 7)

    def test_meta_roundtrip_through_dict(self):
        p = fx.write_jsonl(fx.linear(), str(self.tmp / "l.jsonl"))
        m = sc.compute_session_meta(p)
        rt = self.tmp / "m.json"
        import json
        rt.write_text(json.dumps(m.to_dict()), encoding="utf-8")
        self.assertEqual(sc.read_session_meta(rt), m)

    def test_damaged_meta_is_none(self):
        z = self.tmp / "z.jsonl"
        z.write_bytes(b"")
        self.assertIsNone(sc.compute_session_meta(z))

    def test_meta_none_on_bad_line(self):
        p = self.tmp / "bad.jsonl"
        p.write_text('{"uuid":"u1","parentUuid":null,"type":"user"}\nNOT JSON\n', encoding="utf-8")
        self.assertIsNone(sc.compute_session_meta(p))  # meta 不得替壞檔背書

    def test_meta_none_on_same_uuid_diff(self):
        objs = [
            fx.umsg("u1", None, "user", 1),
            fx.umsg("u2", "u1", "assistant", 2, content="A"),
            fx.umsg("u2", "u1", "assistant", 2, content="B"),
        ]
        p = fx.write_jsonl(objs, str(self.tmp / "dup.jsonl"))
        self.assertIsNone(sc.compute_session_meta(p))

    def test_read_session_meta_rejects_invalid(self):
        import json
        bad = self.tmp / "m.json"
        bad.write_text(json.dumps({"content_hash": "xyz", "tail_hash": "xyz",
                                   "line_count": -1, "uuid_count": 0, "non_uuid_count": 0}),
                       encoding="utf-8")
        self.assertIsNone(sc.read_session_meta(bad))

    def test_read_project_sidecar_malformed_returns_none(self):
        import json
        d = self.tmp / "proj"
        d.mkdir()
        (d / "_project.json").write_text(
            json.dumps({"schema_version": "bad", "observed_cwds": "abc"}), encoding="utf-8"
        )
        self.assertIsNone(sc.read_project_sidecar(d))


if __name__ == "__main__":
    unittest.main()
