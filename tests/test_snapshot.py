import tempfile
import unittest
from pathlib import Path

from claude_session_sync import tombstone
from claude_session_sync.config import Config
from claude_session_sync.snapshot import compute_decision_snapshot
from claude_session_sync.state import State
from tests import fixtures as fx


class TestDecisionSnapshot(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.ld = self.tmp / "local" / "projA"
        self.hd = self.tmp / "hub" / "projA"
        self.ld.mkdir(parents=True)
        self.hd.mkdir(parents=True)
        self.local = self.ld / "s1.jsonl"
        self.hub = self.hd / "s1.jsonl"
        fx.write_jsonl(fx.linear(), str(self.local))
        fx.write_jsonl(fx.linear(), str(self.hub))
        self.cfg = Config(own_hub=str(self.tmp / "hub"), remotes={"office": "/x"})
        self.state = State(known_sessions={"projA": set()}, bindings={})

    def tearDown(self):
        self._td.cleanup()

    def _snap(self):
        return compute_decision_snapshot(
            session_id="s1", local_project_dir=self.ld, hub_project_dir=self.hd,
            config=self.cfg, state=self.state, project_key="projA", cwd="/home/me/projA",
        )

    def test_stable_when_unchanged(self):
        self.assertEqual(self._snap(), self._snap())

    def test_local_data_change_detected(self):
        before = self._snap()
        fx.write_jsonl(fx.fast_forward_of_linear(), str(self.local))
        self.assertNotEqual(before, self._snap())

    def test_hub_data_change_detected(self):
        before = self._snap()
        fx.write_jsonl(fx.fast_forward_of_linear(), str(self.hub))
        self.assertNotEqual(before, self._snap())

    def test_project_sidecar_change_detected(self):
        before = self._snap()
        (self.hd / "_project.json").write_text('{"git_remote": "x"}', encoding="utf-8")
        self.assertNotEqual(before, self._snap())

    def test_tombstone_dir_change_detected(self):
        before = self._snap()
        tombstone.write_coverage(self.hd, epoch=1)  # 寫進 .tombstones/ → digest 變
        self.assertNotEqual(before, self._snap())

    def test_coverage_epoch_change_detected(self):
        tombstone.write_coverage(self.hd, epoch=1)
        before = self._snap()
        tombstone.write_coverage(self.hd, epoch=2)
        self.assertNotEqual(before, self._snap())

    def test_config_change_detected(self):
        before = self._snap()
        self.cfg.own_hub = "/somewhere/else"
        self.assertNotEqual(before, self._snap())

    def test_meta_change_detected(self):
        before = self._snap()
        (self.hd / "s1.meta.json").write_text('{"content_hash": "x"}', encoding="utf-8")
        self.assertNotEqual(before, self._snap())

    def test_state_entry_stable_under_other_session_commit(self):
        # 對「其他」session 的 commit（known_sessions 加別的 sid）不可使本 session 快照過期。
        before = self._snap()
        self.state.known_sessions["projA"].add("OTHER-SID")
        self.assertEqual(before, self._snap())

    def test_state_entry_changes_when_this_session_known(self):
        before = self._snap()
        self.state.known_sessions["projA"].add("s1")  # 本 session 被標記為已知 → 條目變
        self.assertNotEqual(before, self._snap())

    def test_state_entry_changes_on_binding(self):
        before = self._snap()
        self.state.bindings["/home/me/projA"] = "projA"
        self.assertNotEqual(before, self._snap())

    def test_state_entry_changes_on_dir_binding(self):
        # 空夾身分靠 local_dir_bindings 解析 → 其變動（並發 remap）須令快照失效（與 cwd-binding 對稱，codex r26-2）。
        before = self._snap()
        self.state.local_dir_bindings[self.ld.name] = "encH"
        self.assertNotEqual(before, self._snap())

    def test_state_entry_changes_on_dir_assertion(self):
        # 斷言夾（--map 斷言整夾，2026-07-14）的身分解析繞過 cwd 檢查 → 斷言的授予/撤銷須令快照失效（對稱 dir_binding）。
        before = self._snap()
        self.state.asserted_dirs.add(self.ld.name)
        self.assertNotEqual(before, self._snap())

    def test_missing_side_is_absent_token(self):
        snap = compute_decision_snapshot(
            session_id="ghost", local_project_dir=None, hub_project_dir=self.hd,
            config=self.cfg, state=self.state, project_key="projA", cwd=None,
        )
        self.assertEqual(snap.local_data, "absent")  # 未綁定 local 側 → 恆 absent
        self.assertEqual(snap.hub_data, "absent")    # hub 夾內無 ghost.jsonl

    def test_file_appearing_where_absent_is_detected(self):
        # codex r8 critical①：plan 時某側無檔，apply 前被建出來 → 重算快照必須改變（否則覆蓋新檔）。
        kw = dict(session_id="newsid", local_project_dir=self.ld, hub_project_dir=self.hd,
                  config=self.cfg, state=self.state, project_key="projA", cwd="/c")
        before = compute_decision_snapshot(**kw)
        self.assertEqual(before.local_data, "absent")
        fx.write_jsonl(fx.linear(), str(self.ld / "newsid.jsonl"))
        after = compute_decision_snapshot(**kw)
        self.assertNotEqual(before, after)
        self.assertTrue(after.local_data.startswith("sha:"))

    def test_absent_vs_nonregular_vs_present_distinct(self):
        # codex r8 critical②：不存在 / 非一般檔 / 一般檔 三態必須可辨，不可都當「沒檔」。
        from claude_session_sync.snapshot import _file_digest
        self.assertEqual(_file_digest(self.ld / "nope.jsonl"), "absent")
        d = self.ld / "adir.jsonl"
        d.mkdir()
        self.assertTrue(_file_digest(d).startswith("nonreg:"))
        self.assertTrue(_file_digest(self.local).startswith("sha:"))


if __name__ == "__main__":
    unittest.main()
