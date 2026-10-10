import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cable_outage_evidence import __main__ as cli
from cable_outage_evidence import config, setup
from tests.fake_hitron import FakeHitron


def scripted(answers):
    """An input() replacement that returns the scripted answers in order."""
    it = iter(answers)
    return lambda prompt="": next(it)


def closed_port_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


class Dialogue(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHitron(password="right")
        self.out = []

    def tearDown(self):
        self.fake.close()

    def run_dialogue(self, answers, secrets, cfg=None):
        cfg = cfg or config.Config()
        status = setup.interactive(cfg, scripted(answers), scripted(secrets), self.out.append)
        return cfg, status

    def test_correct_login_first_time(self):
        cfg, status = self.run_dialogue([self.fake.url, ""], ["right"])
        self.assertEqual(status, setup.OK)
        self.assertEqual((cfg.modem_username, cfg.modem_password), ("admin", "right"))
        self.assertIn("數據機連線成功 / modem OK", self.out)

    def test_wrong_password_is_asked_again(self):
        cfg, status = self.run_dialogue([self.fake.url, "", "1", "", ""], ["wrong", "right"])
        self.assertEqual(status, setup.OK)
        self.assertEqual(cfg.modem_password, "right")
        self.assertTrue(any("帳密錯誤" in line for line in self.out))
        self.assertFalse(any("wrong" in line or "right" in line for line in self.out))

    def test_enter_at_the_menu_means_retry(self):
        cfg, status = self.run_dialogue([self.fake.url, "", "", "", ""], ["wrong", "right"])
        self.assertEqual(status, setup.OK)

    def test_choose_no_modem(self):
        cfg, status = self.run_dialogue([self.fake.url, "", "2"], ["wrong"])
        self.assertEqual(status, setup.OFF)
        self.assertEqual(cfg.modem_type, "none")
        self.assertEqual(cfg.modem_password, "password")   # the unverified one is not kept

    def test_keep_while_unreachable(self):
        url = closed_port_url()
        cfg, status = self.run_dialogue([url, "", "3"], ["right"])
        self.assertEqual(status, setup.KEPT)
        self.assertEqual(cfg.modem_url, url)

    def test_unsupported_modem_defaults_to_not_reading(self):
        foreign = FakeHitron(foreign_title="CBN CH8568")
        try:
            cfg, status = self.run_dialogue([foreign.url, "", ""], ["password"])
        finally:
            foreign.close()
        self.assertEqual(status, setup.OFF)
        self.assertEqual(cfg.modem_type, "none")
        self.assertEqual(cfg.modem_url, foreign.url)     # still pinged as a layer
        text = "\n".join(self.out)
        self.assertIn("不是 Hitron CODA", text)
        self.assertNotIn("帳密錯誤", text)

    def test_dialog_wording_does_not_send_to_init(self):
        self.run_dialogue([self.fake.url, "", "2"], ["wrong"])
        text = "\n".join(self.out)
        self.assertIn("請重新輸入帳密", text)
        self.assertNotIn("請執行 init", text)

    def test_none_turns_modem_reading_off(self):
        cfg, status = self.run_dialogue(["none"], [])
        self.assertEqual((status, cfg.modem_type), (setup.OFF, "none"))
        self.assertEqual(cfg.modem_url, "")              # no modem at all: nothing pinged

    def test_address_without_scheme(self):
        cfg = config.Config()
        setup.ask_settings(cfg, scripted(["192.168.100.1", ""]), scripted([""]))
        self.assertEqual(cfg.modem_url, "http://192.168.100.1")
        self.assertEqual(cfg.modem_password, "password")    # Enter keeps the factory default


class StartCommand(unittest.TestCase):
    """The double-click path: what gets saved, and when."""

    def setUp(self):
        self.fake = FakeHitron(password="right")
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.ini"

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def start(self, answers, secrets):
        args = mock.Mock(config=str(self.path))
        cfg = config.load(self.path)
        with mock.patch("sys.stdin") as stdin, \
                mock.patch("builtins.input", scripted(answers)), \
                mock.patch("getpass.getpass", scripted(secrets)), \
                mock.patch.object(cli, "cmd_run", lambda cfg, args: None), \
                mock.patch("builtins.print"):
            stdin.isatty.return_value = True
            cli.cmd_start(cfg, args)

    def test_first_run_saves_only_the_verified_password(self):
        self.start([self.fake.url, "", "1", "", ""], ["wrong", "right"])
        cfg = config.load(self.path)
        self.assertEqual(cfg.modem_password, "right")

    def test_later_wrong_password_is_offered_for_reentry(self):
        config.save(config.Config(modem_url=self.fake.url, modem_password="old"), self.path)
        self.start(["", "", ""], ["right"])     # Y (default), keep address, keep username
        self.assertEqual(config.load(self.path).modem_password, "right")

    def test_declining_reentry_leaves_the_file_alone(self):
        config.save(config.Config(modem_url=self.fake.url, modem_password="old"), self.path)
        self.start(["n"], [])
        self.assertEqual(config.load(self.path).modem_password, "old")

    def test_reentry_still_wrong_is_not_saved_unless_kept(self):
        config.save(config.Config(modem_url=self.fake.url, modem_password="old"), self.path)
        self.start(["", "", "", "2"], ["still-wrong"])
        cfg = config.load(self.path)
        # The user turned modem reading off; the wrong password was not written.
        self.assertEqual(cfg.modem_type, "none")
        self.assertEqual(cfg.modem_password, "old")


if __name__ == "__main__":
    unittest.main()


class StartWithUnsupportedModem(unittest.TestCase):
    def test_offers_to_stop_reading_and_keeps_pinging(self):
        foreign = FakeHitron(foreign_title="CBN CH8568")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.ini"
            config.save(config.Config(modem_url=foreign.url), path)
            args = mock.Mock(config=str(path))
            with mock.patch("sys.stdin") as stdin, \
                    mock.patch("builtins.input", scripted([""])), \
                    mock.patch.object(cli, "cmd_run", lambda cfg, args: None), \
                    mock.patch("builtins.print"):
                stdin.isatty.return_value = True
                cli.cmd_start(config.load(path), args)
            cfg = config.load(path)
            self.assertEqual(cfg.modem_type, "none")
            self.assertEqual(cfg.modem_url, foreign.url)
        foreign.close()
