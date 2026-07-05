import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_session_sync import atomicio, resolve, scan, state as state_mod, tombstone
from claude_session_sync.lineset import analyze
from claude_session_sync.resolve import Choice, Decision
from tests import _caps, fixtures as fx


def _name_match(local_dir, hub_dirs):
    for hd in hub_dirs:
        if hd.name == local_dir.name:
            return ("match", hd)
    return ("needs-map", None)


def _no_ts(o):
    o = dict(o)
    o.pop("timestamp", None)
    return o


class TestResolve(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.local, self.hub = self.tmp / "local", self.tmp / "hub"
        self.lA, self.hA = self.local / "projA", self.hub / "projA"
        self.lA.mkdir(parents=True)
        self.hA.mkdir(parents=True)
        self.state = self.tmp / "state.json"
        self.st = state_mod.State(known_sessions={"projA": {"s1"}})
        state_mod.save(self.st, self.state)

    def tearDown(self):
        self._td.cleanup()

    def _w(self, path, objs):
        fx.write_jsonl(objs, str(path))

    def _fork(self):
        # local 與 hub 共享 u1,u2；local tip=u4、hub tip=u3 → fork。
        self._w(self.lA / "s1.jsonl", fx.fork_of_linear())   # u1,u2,u4
        self._w(self.hA / "s1.jsonl", fx.linear())           # u1,u2,u3
        tombstone.write_coverage(self.hA)

    def _plan(self):
        return scan.build_plan(self.local, self.hub, self.st, identity_fn=_name_match)

    def _resolve(self, decider, **kw):
        return resolve.resolve_plan(
            self._plan(), hub_root=self.hub, state=self.st, state_path=str(self.state),
            decider=decider, machine="testhost", **kw)

    def _local_files(self):
        return {p.name for p in self.lA.glob("*.jsonl")}

    def _only(self, report):
        self.assertEqual(len(report.outcomes), 1)
        return report.outcomes[0]

    @_caps.needs_symlink
    def test_resolve_skips_escaping_project_dir(self):
        # e2e gate2 #1：plan 時 projA 安全（fork s1），resolve 前 local projA 換成逃逸 symlink → resolve_plan 重驗
        # → skipped，不讀/寫界外（互動 apply 是**獨立**寫入路徑，須自守，非只靠 apply_plan）。
        self._fork()
        plan = self._plan()   # projA 有 fork s1
        import shutil
        shutil.rmtree(self.lA)
        outside = self.tmp / "outside"
        outside.mkdir()
        self._w(outside / "s1.jsonl", fx.fork_of_linear())
        self.lA.symlink_to(outside, target_is_directory=True)
        report = resolve.resolve_plan(
            plan, hub_root=self.hub, state=self.st, state_path=str(self.state),
            decider=lambda ctx: Decision(Choice.UNION), machine="testhost")
        o = self._only(report)
        self.assertEqual(o.result, "skipped")
        self.assertIn("逃逸", o.detail)
        self.assertEqual({p.name for p in outside.glob("*.jsonl")}, {"s1.jsonl"})  # 無新 keep-both 寫入界外

    # ── UNION ────────────────────────────────────────────────────────────────
    def test_union_creates_merged_local_file_originals_untouched(self):
        self._fork()
        before_local = (self.lA / "s1.jsonl").read_bytes()
        before_hub = (self.hA / "s1.jsonl").read_bytes()
        r = self._resolve(lambda ctx: Decision(Choice.UNION))
        o = self._only(r)
        self.assertEqual(o.result, "union-merged")
        # C3：兩個原檔一字未動
        self.assertEqual((self.lA / "s1.jsonl").read_bytes(), before_local)
        self.assertEqual((self.hA / "s1.jsonl").read_bytes(), before_hub)
        # 新增一個 keep-both 檔，內容為 union（u1..u4 + tip=u4）
        new = self._local_files() - {"s1.jsonl"}
        self.assertEqual(len(new), 1)
        shp = analyze(str(self.lA / next(iter(new))))
        self.assertEqual(shp.uuids, {"u1", "u2", "u3", "u4"})
        self.assertEqual(shp.active_tip, "u4")
        self.assertEqual(o.path, str(self.lA / next(iter(new))))

    def test_union_ambiguous_tip_requires_choice(self):
        # 兩枝 tip 皆無 timestamp → merge 自動選不出 → 需 chosen_tip。
        self._w(self.lA / "s1.jsonl", [fx.umsg("u1", None, "user", 1),
                fx.umsg("u2", "u1", "assistant", 2), _no_ts(fx.umsg("u4", "u2", "user", 4)),
                fx.lastprompt("u4")])
        self._w(self.hA / "s1.jsonl", [fx.umsg("u1", None, "user", 1),
                fx.umsg("u2", "u1", "assistant", 2), _no_ts(fx.umsg("u3", "u2", "user", 3)),
                fx.lastprompt("u3")])
        tombstone.write_coverage(self.hA)
        # 無 tip → union-unavailable
        self.assertEqual(self._only(self._resolve(lambda ctx: Decision(Choice.UNION))).result,
                         "union-unavailable")
        # 指定 tip → union-merged
        o = self._only(self._resolve(lambda ctx: Decision(Choice.UNION, chosen_tip="u4")))
        self.assertEqual(o.result, "union-merged")

    # ── KEEP_BOTH ──────────────────────────────────────────────────────────────
    def test_keep_both_brings_hub_branch_as_new_local_file(self):
        self._fork()
        hub_bytes = (self.hA / "s1.jsonl").read_bytes()
        local_before = (self.lA / "s1.jsonl").read_bytes()
        o = self._only(self._resolve(lambda ctx: Decision(Choice.KEEP_BOTH)))
        self.assertEqual(o.result, "kept-both")
        self.assertEqual((self.lA / "s1.jsonl").read_bytes(), local_before)  # 原檔不動（C3）
        new = self._local_files() - {"s1.jsonl"}
        self.assertEqual(len(new), 1)
        self.assertEqual((self.lA / next(iter(new))).read_bytes(), hub_bytes)  # 帶進的是 hub 分枝

    # ── SKIP ─────────────────────────────────────────────────────────────────
    def test_skip_does_nothing(self):
        self._fork()
        o = self._only(self._resolve(lambda ctx: Decision(Choice.SKIP)))
        self.assertEqual(o.result, "skipped")
        self.assertEqual(self._local_files(), {"s1.jsonl"})  # 無新檔

    # ── 安全：鎖內重新分類 / 鎖 / context ───────────────────────────────────────
    def test_tombstone_appeared_reclassifies_and_skips(self):
        # plan 時 s1=fork；plan 後才出現 tombstone（內容≠base）→ 鎖內重分類已非 fork → 不 union。
        self._fork()
        plan = self._plan()
        tombstone.write_session_tombstone(self.hA, "s1", base_hash="nope")  # plan 後出現
        r = resolve.resolve_plan(plan, hub_root=self.hub, state=self.st, state_path=str(self.state),
                                 decider=lambda ctx: Decision(Choice.UNION), machine="testhost")
        o = self._only(r)
        self.assertEqual(o.result, "skipped-changed")
        self.assertEqual(self._local_files(), {"s1.jsonl"})  # 未寫任何 union 檔

    def test_locked_session_skipped(self):
        self._fork()
        held = atomicio.FileLock(self.hA / "s1.jsonl").acquire()
        try:
            o = self._only(self._resolve(lambda ctx: Decision(Choice.UNION), lock_timeout_s=0.2))
        finally:
            held.release()
        self.assertEqual(o.result, "skipped-locked")

    def test_decider_sees_union_outcome_and_leaves(self):
        self._fork()
        seen = {}

        def decider(ctx):
            seen["action"] = ctx.action
            seen["outcome"] = ctx.union_outcome
            seen["leaves"] = {l.uuid for l in ctx.leaves}
            return Decision(Choice.SKIP)

        self._resolve(decider)
        self.assertEqual(seen["action"], "fork")
        self.assertEqual(seen["outcome"].value, "merged")
        self.assertEqual(seen["leaves"], {"u3", "u4"})

    # ── decider 契約防呆（codex r23）────────────────────────────────────────────
    def test_invalid_choice_does_not_write(self):
        self._fork()
        o = self._only(self._resolve(lambda ctx: Decision("bogus")))  # 非 Choice
        self.assertEqual(o.result, "skipped")
        self.assertEqual(self._local_files(), {"s1.jsonl"})  # 未落到 UNION 寫入

    def test_invalid_chosen_tip_type_does_not_crash(self):
        self._fork()
        o = self._only(self._resolve(lambda ctx: Decision(Choice.UNION, chosen_tip=[])))  # 須 str|None
        self.assertEqual(o.result, "skipped")
        self.assertEqual(self._local_files(), {"s1.jsonl"})

    # ── 鎖內重讀 coverage（codex r23）────────────────────────────────────────────
    def test_coverage_removed_during_decider_skips(self):
        self._fork()

        def decider(ctx):  # decider 暫停期間 coverage 被移除
            (tombstone.tombstones_dir(self.hA) / tombstone.COVERAGE_FILE).unlink()
            return Decision(Choice.UNION)

        o = self._only(self._resolve(decider))
        self.assertEqual(o.result, "skipped-changed")
        self.assertEqual(self._local_files(), {"s1.jsonl"})  # 信任邊界沒了 → 不寫

    # ── 寫入錯誤 → had_error（codex r23 → CLI 非零）────────────────────────────────
    def test_write_error_sets_had_error(self):
        self._fork()
        with mock.patch.object(resolve.atomicio, "write_keep_both",
                               side_effect=atomicio.AtomicWriteError("disk full")):
            r = self._resolve(lambda ctx: Decision(Choice.UNION))
        self.assertEqual(self._only(r).result, "error")
        self.assertTrue(r.had_error)

    # ── Phase A 讀取 race → 不 crash（codex r23 Low）────────────────────────────────
    def test_phase_a_read_race_skips(self):
        self._fork()
        with mock.patch.object(resolve, "analyze", side_effect=OSError("gone")):
            o = self._only(self._resolve(lambda ctx: Decision(Choice.UNION)))
        self.assertEqual(o.result, "skipped-changed")

    def test_phase_b_union_reread_race_skips(self):
        # Phase A 兩次 _safe_analyze 成功（讓 decider 看到 preview），Phase B union 重讀（第 3/4 次）回 None
        # → skipped-changed、不寫（classify_session 走的是 analyze 非 _safe_analyze，故 reclassify 不受影響）。
        self._fork()
        real = resolve._safe_analyze
        n = {"c": 0}

        def flaky(path):
            n["c"] += 1
            return real(path) if n["c"] <= 2 else None

        with mock.patch.object(resolve, "_safe_analyze", flaky):
            o = self._only(self._resolve(lambda ctx: Decision(Choice.UNION)))
        self.assertEqual(o.result, "skipped-changed")
        self.assertEqual(self._local_files(), {"s1.jsonl"})  # 未寫 union

    def test_non_fork_sessions_not_touched(self):
        # identical session 不在 RESOLVABLE → resolve 完全不碰、無 outcome。
        self._w(self.lA / "s1.jsonl", fx.linear())
        self._w(self.hA / "s1.jsonl", fx.linear())
        tombstone.write_coverage(self.hA)
        r = self._resolve(lambda ctx: Decision(Choice.UNION))
        self.assertEqual(r.outcomes, [])


if __name__ == "__main__":
    unittest.main()
