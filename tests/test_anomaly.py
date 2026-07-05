import tempfile
import unicodedata
import unittest
from pathlib import Path

from claude_session_sync import anomaly
from claude_session_sync.pathsafe import name_key
from claude_session_sync.state import State
from tests import _caps


class TestAnomaly(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_mount_missing_halts(self):
        res = anomaly.check(None, self.tmp / "nope")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].code, "mount-missing")
        self.assertEqual(res[0].severity, "halt")

    def test_existing_no_state_ok(self):
        (self.tmp / "projA").mkdir()
        self.assertEqual(anomaly.check(None, self.tmp), [])

    def test_fingerprint_match_ok(self):
        (self.tmp / "projA").mkdir()
        s = State(hub_fingerprint=anomaly.hub_fingerprint(self.tmp))
        self.assertEqual(anomaly.check(s, self.tmp), [])

    def test_fingerprint_change_halts(self):
        (self.tmp / "projA").mkdir()
        s = State(hub_fingerprint="some-old-fingerprint")
        res = anomaly.check(s, self.tmp)
        self.assertTrue(any(a.code == "hub-fingerprint-changed" and a.severity == "halt" for a in res))

    # ── 已知 session 大量消失（codex r6 必補②）──────────────────────────────

    def _hub_sessions(self, proj: str, sids: list[str]):
        d = self.tmp / proj
        d.mkdir(exist_ok=True)
        for sid in sids:
            (d / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")

    def test_no_disappearance_when_all_present(self):
        sids = [f"s{i}" for i in range(10)]
        self._hub_sessions("projA", sids)
        s = State(known_sessions={"projA": set(sids)})
        self.assertIsNone(anomaly.detect_disappearance(s, self.tmp))
        self.assertEqual(anomaly.check(s, self.tmp), [])

    @_caps.needs_symlink
    def test_disappearance_skips_escaping_pk_dir(self):
        # e2e gate2 #4：pk 夾是逃逸 symlink（指向空夾）→ detect_disappearance 不 glob 界外、不誤判「全消失」halt
        #（若被讀成 8/8 消失會觸發 global halt；跳過逃逸 pk → None）。
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.tmp / "projA").symlink_to(outside, target_is_directory=True)
        s = State(known_sessions={"projA": {f"s{i}" for i in range(8)}})
        self.assertIsNone(anomaly.detect_disappearance(s, self.tmp))

    def test_mass_disappearance_halts(self):
        known = [f"s{i}" for i in range(10)]
        self._hub_sessions("projA", known[:2])  # 只剩 2/10
        s = State(known_sessions={"projA": set(known)})
        a = anomaly.detect_disappearance(s, self.tmp)
        self.assertIsNotNone(a)
        self.assertEqual(a.code, "known-sessions-vanished")
        self.assertEqual(a.severity, "halt")
        self.assertTrue(any(x.code == "known-sessions-vanished" for x in anomaly.check(s, self.tmp)))

    def test_small_sample_not_triggered(self):
        # 已知數低於門檻 → 不誤判（樣本太小）
        s = State(known_sessions={"projA": {"s1", "s2", "s3"}})  # 夾不存在 → 全消失但 <8
        self.assertIsNone(anomaly.detect_disappearance(s, self.tmp))

    def test_vanished_dir_counts_as_missing(self):
        known = [f"s{i}" for i in range(9)]
        # 不建任何 hub 夾 → projA 整夾消失
        s = State(known_sessions={"projA": set(known)})
        a = anomaly.detect_disappearance(s, self.tmp)
        self.assertIsNotNone(a)

    def test_disappearance_independent_of_fingerprint(self):
        # 夾名沒變（指紋相符）但內容被清空 → fingerprint 不觸發、disappearance 觸發
        known = [f"s{i}" for i in range(10)]
        self._hub_sessions("projA", known)
        fp = anomaly.hub_fingerprint(self.tmp)
        for sid in known:  # 清空內容、保留夾
            (self.tmp / "projA" / f"{sid}.jsonl").unlink()
        s = State(hub_fingerprint=fp, known_sessions={"projA": set(known)})
        codes = {a.code for a in anomaly.check(s, self.tmp)}
        self.assertIn("known-sessions-vanished", codes)
        self.assertNotIn("hub-fingerprint-changed", codes)

    def test_per_project_wipe_not_diluted(self):
        # 大專案還在不可稀釋掉小專案被整夾清空（codex r8 高）。
        self._hub_sessions("projB", [f"b{i}" for i in range(100)])
        s = State(known_sessions={
            "projA": {f"a{i}" for i in range(8)},   # projA 整夾未建 → 全消失
            "projB": {f"b{i}" for i in range(100)},
        })
        self.assertIsNotNone(anomaly.detect_disappearance(s, self.tmp))

    def test_exact_half_below_threshold(self):
        # 50% < 60%：精確分數比較不可誤觸（codex r8 中）。
        sids = [f"s{i}" for i in range(8)]
        self._hub_sessions("projA", sids[:4])
        s = State(known_sessions={"projA": set(sids)})
        self.assertIsNone(anomaly.detect_disappearance(s, self.tmp))

    def test_known_set_hash_sensitivity(self):
        h0 = anomaly.known_session_set_hash(State(known_sessions={"p": {"a"}}))
        h1 = anomaly.known_session_set_hash(State(known_sessions={"p": {"a", "b"}}))
        self.assertNotEqual(h0, h1)
        self.assertEqual(h0, anomaly.known_session_set_hash(State(known_sessions={"p": {"a"}})))


class TestCollisionCasefolds(unittest.TestCase):
    """e2e-r1 Finding 2：collision_casefolds 折疊鍵。session 預設 casefold（sid=UUID、無 NFC/NFD）；memory 傳
    pathsafe.name_key（NFC∘casefold∘NFC）才認得同一檔名的 NFC/NFD 兩拼法。"""

    def test_case_only_collision_caught_default(self):
        self.assertTrue(anomaly.collision_casefolds({"ABC"}, {"abc"}))          # 既有 case-only 撞名不回歸

    def test_default_casefold_misses_nfc_nfd(self):
        nfc, nfd = unicodedata.normalize("NFC", "café"), unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)                                            # 前提：位元組不同
        self.assertEqual(anomaly.collision_casefolds({nfc}, {nfd}), set())       # 預設 casefold 不折疊 → 漏（session 安全，sid 無此）

    def test_name_key_keyfn_catches_nfc_nfd(self):
        nfc, nfd = unicodedata.normalize("NFC", "Café"), unicodedata.normalize("NFD", "café")
        # 大小寫 + 正規化皆異，name_key 仍折疊到同鍵 → 判撞名（memory 端用此）。
        self.assertEqual(anomaly.collision_casefolds({nfc}, {nfd}, keyfn=name_key), {name_key(nfc)})

    def test_uuid_behavior_identical_both_keyfns(self):
        a, b = {"ABCD-0001"}, {"abcd-0001"}                                      # ASCII → name_key==casefold
        self.assertEqual(anomaly.collision_casefolds(a, b),
                         anomaly.collision_casefolds(a, b, keyfn=name_key))


if __name__ == "__main__":
    unittest.main()
