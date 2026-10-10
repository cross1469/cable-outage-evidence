import datetime as dt
import re
import tempfile
import unittest
from pathlib import Path

from cable_outage_evidence import analysis, hitron, modem_worker, report, storage

START = dt.datetime(2026, 1, 1, 20, 0, 0)
NAMES = ["Cloudflare", "Google", "TWNIC"]
CAUSE_WORDS = re.compile(r"\b(RF|ingress|noise|scheduler|RPD)\b|雜訊|干擾", re.IGNORECASE)


def ts(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S.") + f"{t.microsecond // 1000:03d}"


class Dataset:
    """Builds one day's files around a 10 s outage starting at START."""

    def __init__(self, root: Path, duration=10, modem_rtt=1.0):
        self.root = root
        self.store = storage.Store(root)
        self.day = self.store.day_dir(START.date())
        self.end = START + dt.timedelta(seconds=duration)
        header = storage.sample_header(NAMES)
        rows = []
        t = START - dt.timedelta(seconds=120)
        seq = 0
        while t <= self.end + dt.timedelta(seconds=120):
            out = START <= t < self.end
            seq += 1
            pub = ["0", ""] * 3 if out else ["1", "10.0"] * 3
            rows.append([ts(t), str(seq), "1", "192.168.1.1", "1", "1.0", "192.168.0.1", "1", f"{modem_rtt if out else 1.0:.1f}",
                         "10.20.0.1", "0" if out else "1", "" if out else "8.0"] + pub
                        + ["ISP_ACCESS_OUTAGE" if out else "OK", "0"])
            t += dt.timedelta(milliseconds=500)
        inc = self.day / "incidents"
        inc.mkdir(exist_ok=True)
        self.store.append_csv("", header, rows, path=inc / f"incident-{START:%Y%m%d-%H%M%S}.csv")
        ev = {h: "" for h in storage.event_header(NAMES)}
        ev.update(StartTime=ts(START), EndTime=ts(self.end), DurationSeconds=f"{duration:.1f}", Grade="P1",
                  Classification="ISP_ACCESS_OUTAGE", FirstFailingLayer="isp_first_hop", BadSamples=str(duration * 2),
                  LanGatewayAddress="192.168.1.1", ModemAddress="192.168.0.1", FirstHopAddress="10.20.0.1",
                  CloudflareTCP443="0", GoogleTCP443="0", TWNICTCP443="1")
        self.store.append_csv("events.csv", storage.event_header(NAMES), [[ev[h] for h in storage.event_header(NAMES)]])

    def skew(self, seconds=2):
        row = hitron.failure_row(ts(START - dt.timedelta(seconds=30)), "Periodic", "") + [str(seconds)]
        row[2] = "1"
        self.store.append_csv("modem-signals.csv", modem_worker.SIGNALS_HEADER, [row], path=self.day / "modem-signals.csv")

    def log(self, event_type, message, offset_sec=0, skew=2):
        # The modem clock runs `skew` seconds behind the computer.
        mt = START + dt.timedelta(seconds=offset_sec - skew)
        self.store.append_csv("docsis-log.csv", hitron.LOG_HEADER,
                              [[ts(START + dt.timedelta(minutes=5)), mt.strftime(analysis.MODEM_LOG_FORMAT), "3",
                                event_type, message, "02:00:00:00:00:fe", "02:00:00:00:00:01"]],
                              path=self.day / "docsis-log.csv")

    def channels(self, t, ofdma_states=("OPERATE", "OPERATE"), reason="Fast"):
        rows = [[ts(t), reason, "us_scqam", str(i), str(i + 1), "OPERATE", "34600000", "6400kHz", "64QAM",
                 "40", "", "", "", "", "", "", "", "", "", "", ""] for i in (1, 2, 3)]
        rows += [[ts(t), reason, "us_ofdma", str(i), "", st, "", w, "", "50", "", "2K", "", "", "36", "", "", "", "", "", ""]
                 for i, (st, w) in enumerate(zip(ofdma_states, ("70.8000", "18.8000")))]
        self.store.append_csv("modem-channels.csv", hitron.CHANNEL_HEADER, rows, path=self.day / "modem-channels.csv")

    def fast_ticks(self, t0, t1):
        rows = []
        t = t0
        while t <= t1:
            rows.append([ts(t), "IncidentStart", "1", "0", "1", "100", "", "2", "3", "3", "2", "2", "2", "2", "", ""])
            t += dt.timedelta(seconds=1)
        self.store.append_csv("fast-poll.csv", modem_worker.FAST_HEADER, rows, path=self.day / "fast-poll.csv")


class Classes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def classify(self, d):
        ev = analysis.find_event(self.root)
        return analysis.classify_and_save(self.root, ev)

    def test_t3_only_is_cp_weak(self):
        d = Dataset(self.root); d.skew()
        d.log("82000200", "No Ranging Response received - T3 time-out", 3)
        c = self.classify(d)
        self.assertEqual(c.cls, "CP-weak")
        self.assertTrue(c.flags["WeakControlPlane"])

    def test_cm_status_16_only_is_cp_weak(self):
        d = Dataset(self.root); d.skew()
        d.log("74010100", "CM-STATUS message sent. Event Type Code: 16; Chan ID: 7; ", -20)
        self.assertEqual(self.classify(d).cls, "CP-weak")

    def test_t4_only_is_a(self):
        d = Dataset(self.root); d.skew()
        d.log("82000400", "No Unicast Maintenance opportunities received - T4 time out", 1)
        c = self.classify(d)
        self.assertEqual(c.cls, "A")
        self.assertEqual(len(c.strong), 1)
        # The modem time was converted with the recorded skew.
        self.assertEqual(c.strong[0].t, START + dt.timedelta(seconds=1))

    def test_cm_status_6_only_is_a(self):
        d = Dataset(self.root); d.skew()
        d.log("74010100", "CM-STATUS message sent. Event Type Code: 6; Chan ID: 7; ", 30)
        self.assertEqual(self.classify(d).cls, "A")

    def test_entry_outside_window_is_ignored(self):
        d = Dataset(self.root); d.skew()
        d.log("82000400", "T4 time out", 200)
        self.assertEqual(self.classify(d).cls, "B-unverified")

    def test_channel_transition_only_is_a(self):
        d = Dataset(self.root); d.skew()
        d.channels(START - dt.timedelta(seconds=5), reason="PrevObserved")
        d.channels(START + dt.timedelta(seconds=2), ("OPERATE", "NOT_READY"), reason="StateChange")
        c = self.classify(d)
        self.assertEqual(c.cls, "A")
        self.assertTrue(c.flags["ChannelTransition"])
        self.assertFalse(c.flags["StrongControlPlane"])

    def test_nothing_with_one_second_coverage_is_b_verified(self):
        d = Dataset(self.root); d.skew()
        d.channels(START - dt.timedelta(seconds=4), reason="PrevObserved")
        d.fast_ticks(START + dt.timedelta(seconds=1), d.end + dt.timedelta(seconds=5))
        c = self.classify(d)
        self.assertEqual(c.cls, "B-verified", c.reasons)
        self.assertTrue(c.flags["ChannelStateVerified"])

    def test_nothing_with_sparse_coverage_is_b_unverified(self):
        d = Dataset(self.root); d.skew()
        d.channels(START - dt.timedelta(seconds=20), reason="Periodic")
        d.channels(START + dt.timedelta(seconds=10), reason="Periodic")
        c = self.classify(d)
        self.assertEqual(c.cls, "B-unverified")
        self.assertTrue(any("觀察頻道狀態 0 次" in r for r in c.reasons))

    def test_local_stall_with_t4_keeps_both_flags(self):
        d = Dataset(self.root, modem_rtt=283.0); d.skew()
        d.log("82000400", "T4 time out", 0)
        c = self.classify(d)
        self.assertEqual(c.cls, "L")
        self.assertTrue(c.flags["LocalStall"] and c.flags["StrongControlPlane"])
        self.assertTrue(any("數據機探測 283 ms" in r for r in c.reasons))
        self.assertTrue(any(r.startswith("T4") for r in c.reasons))

    def test_no_skew_is_stated(self):
        d = Dataset(self.root)
        c = self.classify(d)
        self.assertIsNone(c.skew)
        self.assertTrue(any("沒有時差紀錄" in r for r in c.reasons))

    def test_reclassify_replaces_row_and_writes_timeline(self):
        d = Dataset(self.root); d.skew()
        self.classify(d)
        c = self.classify(d)
        rows = (d.day / "event-classes.csv").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(rows), 2)
        text = c.timeline_path.read_text(encoding="utf-8")
        self.assertIn("EVENT  斷線開始", text)
        self.assertIn("PROBE", text)
        self.assertIsNone(CAUSE_WORDS.search(text))


class Reports(unittest.TestCase):
    def test_reports_mask_home_addresses_and_name_no_cause(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = Dataset(root)
            d.skew()
            d.log("82000400", "T4 time out", 1)
            # The report covers recent days; move "today" by using a long window.
            days = (dt.date.today() - START.date()).days + 1
            text = report.summary(root, days)
            self.assertIn("P1 1", text)
            self.assertIn("T4", text)
            self.assertNotIn("192.168.1.1", text)
            body, background = text.split("背景說明")
            self.assertIsNone(CAUSE_WORDS.search(body), CAUSE_WORDS.search(body))
            self.assertIn("burst ingress", background)
            ev = report.evidence(root)
            self.assertIn("觀察分類 A", ev)
            self.assertNotIn("192.168.1.1", ev)
            self.assertIn("192.168.1.1", report.evidence(root, redact=False))
            self.assertIn("T4", report.docsis_log(root, days + 1))
            # The ISP identifies the subscriber by the modem MAC; it is not masked.
            self.assertIn("數據機 MAC（數據機日誌回報）：02:00:00:00:00:01", text)
            self.assertIn("CMTS MAC（數據機日誌回報）：02:00:00:00:00:fe", text)
            self.assertIn("02:00:00:00:00:01", ev)

    def test_redactor(self):
        r = report.Redactor(True)
        self.assertEqual(r("home 192.168.8.1 modem 192.168.100.1 isp 10.27.0.1 dns 1.1.1.1"),
                         "home <home-network> modem 192.168.100.1 isp 10.27.0.1 dns 1.1.1.1")
        self.assertEqual(r("CMTS 02:00:00:00:00:fe cm 02:00:00:00:00:01"), "CMTS 02:00:00:00:00:fe cm 02:00:00:00:00:01")


if __name__ == "__main__":
    unittest.main()
