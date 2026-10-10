import unittest

from cable_outage_evidence import events
from tests.test_classify import sample


def seq(states):
    """'.' healthy, 'x' public outage, 'm' outage with the modem down."""
    out = []
    for i, ch in enumerate(states):
        if ch == "x":
            s = sample(cf=None, gg=None, tw=None)
        elif ch == "m":
            s = sample(modem=None, cf=None, gg=None, tw=None)
        else:
            s = sample()
        s.sequence = i
        s.timestamp = f"2026-01-01 00:00:{i // 2:02d}.{(i % 2) * 500:03d}"
        out.append(s)
    return out


def tracker(pre=2, post=2):
    # 500 ms samples: pre/post seconds become twice as many samples.
    return events.Tracker(500, sustained_samples=4, recovery_rounds=2,
                          pre_event_seconds=pre, post_recovery_seconds=post)


class Lifecycle(unittest.TestCase):
    def run_all(self, t, samples):
        return [t.feed(s) for s in samples]

    def test_start_recovery_and_end_time(self):
        t = tracker()
        samples = seq(".....xxx...")
        ups = self.run_all(t, samples)
        started = [u.started for u in ups if u.started]
        recovered = [u.recovered for u in ups if u.recovered]
        self.assertEqual(len(started), 1)
        self.assertEqual(len(recovered), 1)
        ev = recovered[0]
        self.assertIs(ev.start, samples[5])
        # The end is the first recovered sample, not the one that confirmed it.
        self.assertIs(ev.end, samples[8])
        self.assertEqual(ev.bad_samples, 3)
        self.assertEqual(ev.grade, "P4")
        self.assertEqual(ev.worst, "ISP_UPSTREAM_OUTAGE")

    def test_one_healthy_sample_does_not_end_the_event(self):
        t = tracker()
        ups = self.run_all(t, seq("xx.xx..."))
        self.assertEqual(sum(1 for u in ups if u.started), 1)
        ev = [u.recovered for u in ups if u.recovered][0]
        self.assertEqual(ev.bad_samples, 4)
        self.assertEqual(ev.grade, "P3")

    def test_modem_down_throughout_is_p2(self):
        t = tracker()
        ups = self.run_all(t, seq("mmmm.."))
        ev = [u.recovered for u in ups if u.recovered][0]
        self.assertEqual(ev.grade, "P2")
        self.assertEqual(ev.first_failing_layer, "modem")
        self.assertEqual(ev.worst, "MODEM_UNREACHABLE")

    def test_grade_raised_once(self):
        t = tracker()
        ups = self.run_all(t, seq("xxxxxx.."))
        self.assertEqual([u.grade_raised.grade for u in ups if u.grade_raised], ["P3"])

    def test_tcp_failure_regrades_to_p1(self):
        t = tracker()
        t.feed(seq("x")[0])
        ev = t.event
        ev.tcp = {"Cloudflare": False, "Google": False, "TWNIC": True}
        self.assertTrue(t.regrade(ev))
        self.assertEqual(ev.grade, "P1")

    def test_incident_writes_cover_pre_and_post_window(self):
        t = tracker(pre=1, post=1)          # 2 samples either side
        samples = seq("...xx......")
        ups = self.run_all(t, samples)
        writes = [s for u in ups for (_, s) in u.writes]
        # 2 pre-event, 2 outage, 2 to confirm recovery, 2 post-recovery.
        self.assertEqual(writes, samples[1:9])
        self.assertEqual(sum(1 for u in ups if u.closed), 1)

    def test_new_outage_closes_post_window_early(self):
        t = tracker(pre=1, post=5)
        ups = self.run_all(t, seq("x..x.."))
        closed = [u for u in ups if u.closed]
        self.assertEqual(len(closed), 1)
        self.assertIsNotNone(closed[0].started)
        self.assertEqual(sum(1 for u in ups if u.started), 2)


if __name__ == "__main__":
    unittest.main()
