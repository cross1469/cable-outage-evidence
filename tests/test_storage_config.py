import csv
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from cable_outage_evidence import config, events, storage
from tests.test_events import seq, tracker


class StoreFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = storage.Store(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_header_change_keeps_old_file(self):
        self.store.append_csv("x.csv", ["A", "B"], [["1", "2"]])
        store2 = storage.Store(self.root)
        store2.append_csv("x.csv", ["A", "B", "C"], [["1", "2", "3"]])
        day = self.store.day_dir()
        archived = list(day.glob("x.schema-*.csv"))
        self.assertEqual(len(archived), 1)
        with (day / "x.csv").open(encoding="utf-8") as fh:
            self.assertEqual(list(csv.reader(fh)), [["A", "B", "C"], ["1", "2", "3"]])

    def test_cleanup_removes_only_old_dated_dirs(self):
        old = self.root / (dt.date.today() - dt.timedelta(days=200)).isoformat()
        old.mkdir()
        keep = self.root / "notes"
        keep.mkdir()
        recent = self.store.day_dir()
        self.assertEqual(self.store.cleanup(180), [old.name])
        self.assertTrue(keep.exists() and recent.exists())

    def test_event_and_sample_rows_match_headers(self):
        names = ["Cloudflare", "Google", "TWNIC"]
        t = tracker()
        ev = None
        for s in seq("xx.."):
            up = t.feed(s)
            ev = up.recovered or ev
            self.assertEqual(len(storage.sample_row(s)), len(storage.sample_header(names)))
        self.assertEqual(len(storage.event_row(ev, names)), len(storage.event_header(names)))
        self.assertEqual(storage.event_row(ev, names)[2], "1.0")

    def test_minute_summary(self):
        ms = storage.MinuteSummary(["Cloudflare", "Google", "TWNIC"])
        samples = seq("x.")
        for s in samples:
            self.assertIsNone(ms.add(s))
        nxt = seq(".")[0]
        nxt.timestamp = "2026-01-01 00:01:00.000"
        row = ms.add(nxt)
        self.assertEqual(len(row), len(ms.header()))
        header = ms.header()
        self.assertEqual(row[header.index("CloudflareLossPct")], "50.0")
        self.assertEqual(row[header.index("ModemLossPct")], "0.0")


class Config(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.ini"
            cfg = config.Config(interval_ms=1000, modem_url="http://192.168.100.1",
                                public_targets=[("A", "1.1.1.1"), ("B", "8.8.8.8")])
            config.save(cfg, path)
            back = config.load(path)
            self.assertEqual(back.interval_ms, 1000)
            self.assertEqual(back.public_targets, [("A", "1.1.1.1"), ("B", "8.8.8.8")])
            self.assertEqual(back.modem_address, "192.168.100.1")
            self.assertFalse(back.telegram_enabled)

    def test_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            config.validate(config.Config(interval_ms=50))
        with self.assertRaises(ValueError):
            config._parse_targets("1.1.1.1")

    def test_defaults_include_twnic(self):
        self.assertIn(("TWNIC", "101.101.101.101"), config.Config().public_targets)


if __name__ == "__main__":
    unittest.main()
