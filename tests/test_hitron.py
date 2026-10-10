import csv
import tempfile
import time
import unittest
from pathlib import Path

from cable_outage_evidence import config, evidence, hitron, modem_worker, storage
from tests.fake_hitron import FakeHitron, load


class Parsing(unittest.TestCase):
    def test_channel_rows(self):
        rows = hitron.channel_rows("t", "Periodic", us=load("UsInfo"), of=load("UsOfdm"),
                                   ds=load("DsInfo"), dof=load("DsOfdm"))
        kinds = [r[2] for r in rows]
        self.assertEqual((kinds.count("us_scqam"), kinds.count("us_ofdma"),
                          kinds.count("ds_scqam"), kinds.count("ds_ofdm")), (3, 2, 32, 2))
        self.assertTrue(all(len(r) == len(hitron.CHANNEL_HEADER) for r in rows))
        ofdma = [r for r in rows if r[2] == "us_ofdma"]
        state = hitron.CHANNEL_HEADER.index("State")
        self.assertEqual(ofdma[0][state], "OPERATE")         # padding removed
        self.assertNotIn(" ", "".join(ofdma[0]))

    def test_upstream_state_ignores_power_but_sees_state(self):
        us, of = load("UsInfo"), load("UsOfdm")
        base = hitron.upstream_state(us, of)
        of2 = load("UsOfdm")
        of2["Freq_List"][0]["repPower"] = "   99.0000"
        self.assertEqual(hitron.upstream_state(us, of2), base)
        of2["Freq_List"][1]["state"] = "   NOT_READY"
        self.assertNotEqual(hitron.upstream_state(us, of2), base)

    def test_counts_and_summary(self):
        data = {ep: load(ep) for ep in ("SysInfo", "DsInfo", "UsInfo", "UsOfdm", "DsOfdm")}
        c = hitron.counts(data["UsInfo"], data["UsOfdm"], data["DsInfo"], data["DsOfdm"])
        self.assertEqual(c["UsOfdmaCount"], 2)
        self.assertEqual(c["DsScQamCount"], 32)
        row = hitron.summary_row("t", "Periodic", data)
        self.assertEqual(len(row), len(hitron.SUMMARY_HEADER))
        self.assertEqual(row[hitron.SUMMARY_HEADER.index("NetworkAccess")], "Permitted")

    def test_log_entries_split_tags(self):
        entries = hitron.log_entries(load("Log"))
        self.assertEqual(len(entries), 4)
        t, prio, typ, msg, cmts = entries[2]
        self.assertEqual(typ, "74010100")
        self.assertNotIn("CM-MAC", msg)
        self.assertEqual(cmts, "02:00:00:00:00:fe")
        self.assertEqual(evidence.evidence_label(typ, msg), "CM-STATUS type 6 Chan 7")

    def test_dedupe_ignores_index_shift(self):
        with tempfile.TemporaryDirectory() as d:
            dd = hitron.LogDeduper(Path(d) / "seen")
            log = load("Log")
            self.assertEqual(len(dd.new(hitron.log_entries(log))), 4)
            for i, e in enumerate(log["Log_List"]):
                e["index"] = i + 10
            self.assertEqual(dd.new(hitron.log_entries(log)), [])
            # A fresh process reads the same seen file.
            self.assertEqual(hitron.LogDeduper(Path(d) / "seen").new(hitron.log_entries(log)), [])


class Evidence(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(evidence.evidence_kind("82000400", ""), "strong")
        self.assertEqual(evidence.evidence_kind("74010100", "Event Type Code: 6; Chan ID: 7;"), "strong")
        self.assertEqual(evidence.evidence_kind("74010100", "Event Type Code: 16; Chan ID: 7;"), "weak")
        self.assertEqual(evidence.evidence_kind("82000200", ""), "weak")
        self.assertEqual(evidence.evidence_kind("67061601", ""), "none")


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


class WorkerAgainstFakeModem(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = None

    def tearDown(self):
        if self.fake:
            self.fake.close()
        self.tmp.cleanup()

    def worker(self, **fake_kw):
        self.fake = FakeHitron(**fake_kw)
        cfg = config.Config(modem_url=self.fake.url, modem_username="admin", modem_password="secret",
                            data_dir=self.tmp.name, modem_state_poll_interval_sec=1,
                            modem_poll_interval_sec=60, modem_fast_statechange_sec=2)
        store = storage.Store(cfg.data_path)
        return modem_worker.ModemWorker(cfg, store), store

    def test_periodic_read_writes_signals_channels_and_log(self):
        w, store = self.worker(clock_offset=3)
        w.start()
        time.sleep(2.5)
        w.stop()
        day = store.day_dir()
        signals = read_csv(day / "modem-signals.csv")
        self.assertEqual(signals[0]["Reason"], "Periodic")
        self.assertEqual(signals[0]["ReadOK"], "1")
        self.assertIn(signals[0]["SkewSec"], ("3", "4"))   # Date has 1 s resolution
        self.assertEqual(len(read_csv(day / "docsis-log.csv")), 4)
        kinds = {r["Kind"] for r in read_csv(day / "modem-channels.csv")}
        self.assertEqual(kinds, {"us_scqam", "us_ofdma", "ds_scqam", "ds_ofdm"})
        self.assertNotIn("secret", (day / "modem-signals.csv").read_text(encoding="utf-8"))

    def test_state_change_writes_both_observations_and_enters_fast_mode(self):
        w, store = self.worker(drop_ofdma_after=2)
        w.start()
        time.sleep(5.5)
        w.stop()
        day = store.day_dir()
        rows = read_csv(day / "modem-channels.csv")
        reasons = [r["Reason"] for r in rows if r["Kind"] == "us_ofdma"]
        self.assertIn("PrevObserved", reasons)
        self.assertIn("StateChange", reasons)
        prev = [r for r in rows if r["Reason"] == "PrevObserved" and r["Kind"] == "us_ofdma"]
        original = [hitron.clean(c["state"]) for c in load("UsOfdm")["Freq_List"]]
        self.assertEqual([r["State"] for r in prev], original)
        fast = read_csv(day / "fast-poll.csv")
        self.assertGreaterEqual(len(fast), 1)
        self.assertEqual(fast[0]["Trigger"], "StateChange")
        # Channel 1 left its original state, so it is not OPERATE now.
        self.assertEqual(fast[-1]["UsOfdmaOperate"], str(original.count("OPERATE") - (original[1] == "OPERATE")))

    def test_wrong_password_records_failure_without_secret(self):
        w, store = self.worker()
        w.client = type(w.client)(self.fake.url, "admin", "wrong-password")
        w.start()
        time.sleep(1.5)
        w.stop()
        text = (store.day_dir() / "modem-signals.csv").read_text(encoding="utf-8")
        self.assertIn("login rejected", text)
        self.assertNotIn("wrong-password", text)


if __name__ == "__main__":
    unittest.main()
