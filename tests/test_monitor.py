import unittest

from cable_outage_evidence import config, monitor


class FakePinger:
    method = "fake"

    def __init__(self, answering):
        self.answering = set(answering)

    def ping(self, address, timeout):
        return 1.0 if address in self.answering else None


class NearTargets(unittest.TestCase):
    def path(self, answering, modem_url="http://192.168.0.1"):
        cfg = config.Config(modem_url=modem_url, ping_timeout_sec=0.1)
        p = monitor.PathState.__new__(monitor.PathState)
        monitor.PathState.__init__(p, cfg, FakePinger(answering))
        p.gateway = "192.168.1.1"
        return p

    def test_only_answering_targets_are_probed(self):
        p = self.path({"192.168.1.1"})
        p.check_near_targets()
        self.assertEqual(p.near_targets(), ("192.168.1.1", None))

    def test_router_that_never_answers_is_not_probed(self):
        p = self.path({"192.168.0.1", "1.1.1.1"})
        p.check_near_targets()
        self.assertEqual(p.near_targets(), (None, "192.168.0.1"))

    def test_silence_while_online_drops_the_target(self):
        p = self.path({"192.168.1.1", "192.168.0.1", "1.1.1.1"})
        p.check_near_targets()
        p.pinger.answering = {"192.168.0.1", "1.1.1.1"}
        p.check_near_targets()
        self.assertEqual(p.near_targets(), (None, "192.168.0.1"))

    def test_silence_during_an_outage_keeps_the_target(self):
        p = self.path({"192.168.1.1", "192.168.0.1", "1.1.1.1"})
        p.check_near_targets()
        p.pinger.answering = set()          # everything down: this is evidence
        p.check_near_targets()
        self.assertEqual(p.near_targets(), ("192.168.1.1", "192.168.0.1"))

    def test_modem_disabled_is_not_probed(self):
        p = self.path({"192.168.1.1", "192.168.0.1"}, modem_url="")
        p.check_near_targets()
        self.assertEqual(p.near_targets(), ("192.168.1.1", None))

    def test_modem_address_equal_to_router_is_probed_once(self):
        p = self.path({"192.168.1.1"}, modem_url="http://192.168.1.1")
        p.check_near_targets()
        self.assertEqual(p.near_targets(), ("192.168.1.1", None))


if __name__ == "__main__":
    unittest.main()
