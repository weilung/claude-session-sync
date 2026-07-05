import os
import tempfile
import unittest
from pathlib import Path

from claude_session_sync import config as cfg


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_missing_file_is_empty_config(self):
        c = cfg.load(self.tmp / "nope.toml")
        self.assertIsNone(c.own_hub)
        self.assertEqual(c.remotes, {})
        self.assertFalse(c.force_unsafe_lock)

    def test_roundtrip_posix_paths(self):
        c = cfg.Config(
            own_hub="/media/will/HomeDrive/HomeJSONL",
            remotes={"office": "/media/will/HomeDrive/OfficeJSONL"},
            force_unsafe_lock=True,
        )
        p = self.tmp / "c.toml"
        cfg.save(c, p)
        back = cfg.load(p)
        self.assertEqual(back.own_hub, c.own_hub)
        self.assertEqual(back.remotes, c.remotes)
        self.assertTrue(back.force_unsafe_lock)

    def test_roundtrip_windows_backslash_path(self):
        # 決定 #4：手寫 writer 必須能原樣處理 Windows 反斜線路徑（literal string）
        c = cfg.Config(
            own_hub=r"C:\Users\admin\HomeJSONL",
            remotes={"office": r"E:\VsProject\OfficeJSONL"},
        )
        p = self.tmp / "win.toml"
        cfg.save(c, p)
        text = p.read_text(encoding="utf-8")
        self.assertIn(r"'C:\Users\admin\HomeJSONL'", text)  # 單引號 literal、未被轉義破壞
        back = cfg.load(p)
        self.assertEqual(back.own_hub, r"C:\Users\admin\HomeJSONL")
        self.assertEqual(back.remotes["office"], r"E:\VsProject\OfficeJSONL")

    def test_value_with_single_quote_falls_back_to_basic_string(self):
        c = cfg.Config(own_hub="/has/it's/quote")
        p = self.tmp / "q.toml"
        cfg.save(c, p)
        self.assertEqual(cfg.load(p).own_hub, "/has/it's/quote")

    def test_save_is_atomic_no_tmp_left(self):
        c = cfg.Config(own_hub="/x")
        p = self.tmp / "a.toml"
        cfg.save(c, p)
        leftovers = [x for x in self.tmp.iterdir() if x.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_force_unsafe_lock_string_rejected(self):
        p = self.tmp / "c.toml"
        p.write_text('force_unsafe_lock = "false"\n', encoding="utf-8")  # 字串而非布林
        with self.assertRaises(cfg.ConfigError):
            cfg.load(p)

    def test_own_hub_wrong_type_rejected(self):
        p = self.tmp / "c.toml"
        p.write_text('own_hub = ["/x"]\n', encoding="utf-8")
        with self.assertRaises(cfg.ConfigError):
            cfg.load(p)

    def test_remote_value_wrong_type_rejected(self):
        p = self.tmp / "c.toml"
        p.write_text("[remotes]\noffice = 123\n", encoding="utf-8")
        with self.assertRaises(cfg.ConfigError):
            cfg.load(p)

    def test_control_char_value_roundtrips(self):
        c = cfg.Config(own_hub="/x\twith\x01ctrl")
        p = self.tmp / "ctrl.toml"
        cfg.save(c, p)
        self.assertEqual(cfg.load(p).own_hub, "/x\twith\x01ctrl")

    def test_remote_key_with_dot_roundtrips(self):
        c = cfg.Config(remotes={"office.eu": "/srv/x"})
        p = self.tmp / "k.toml"
        cfg.save(c, p)
        self.assertEqual(cfg.load(p).remotes, {"office.eu": "/srv/x"})

    def test_default_path_per_os(self):
        # 不實際寫，只確認跨 OS 路徑邏輯不炸且含 app 名
        p = cfg.default_config_path()
        self.assertIn("claude-session-sync", str(p))
        self.assertTrue(str(p).endswith("config.toml"))


if __name__ == "__main__":
    unittest.main()
