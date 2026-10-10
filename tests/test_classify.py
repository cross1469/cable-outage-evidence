import unittest

from cable_outage_evidence import classify as c

P = c.Probe


def sample(link=True, lan=1.0, modem=2.0, hop=9.0, cf=10.0, gg=11.0, tw=12.0, hop_addr="198.51.100.1"):
    return c.Sample(
        timestamp="2026-01-01 00:00:00.000", sequence=1, link=link,
        lan_gateway=P("192.168.1.1", lan), modem=P("192.168.0.1", modem), first_hop=P(hop_addr, hop),
        public={"Cloudflare": P("1.1.1.1", cf), "Google": P("8.8.8.8", gg), "TWNIC": P("101.101.101.101", tw)})


class SampleClassification(unittest.TestCase):
    def check(self, expected, **kw):
        self.assertEqual(c.classify_sample(sample(**kw)), expected)

    def test_ok(self):
        self.check("OK")

    def test_link_down(self):
        self.check("LINK_DOWN", link=False)

    def test_lan_gateway_unreachable(self):
        self.check("LAN_GATEWAY_UNREACHABLE", lan=None, cf=None, gg=None)

    def test_modem_unreachable(self):
        self.check("MODEM_UNREACHABLE", modem=None, cf=None, gg=None, tw=None)

    def test_isp_access(self):
        self.check("ISP_ACCESS_OUTAGE", hop=None, gg=None, tw=None)

    def test_isp_upstream(self):
        self.check("ISP_UPSTREAM_OUTAGE", cf=None, tw=None)

    def test_isp_upstream_without_hop(self):
        self.check("ISP_UPSTREAM_OUTAGE_NO_HOP", hop_addr=None, hop=None, cf=None, gg=None)

    def test_one_public_failure_is_not_an_outage(self):
        self.check("GOOGLE_PATH_ONLY", gg=None)
        self.assertFalse(c.is_outage("GOOGLE_PATH_ONLY"))

    def test_icmp_only_diagnostics(self):
        self.check("LAN_GATEWAY_ICMP_ONLY", lan=None)
        self.check("MODEM_ICMP_ONLY", modem=None)
        self.check("FIRST_HOP_ICMP_ONLY", hop=None)

    def test_unprobed_hop_is_not_a_failure(self):
        self.check("OK", hop_addr=None, hop=None)

    def test_first_failing_layer(self):
        self.assertEqual(c.first_failing_layer(sample(modem=None, cf=None, gg=None)), "modem")
        self.assertEqual(c.first_failing_layer(sample(cf=None, gg=None)), "public")
        self.assertEqual(c.first_failing_layer(sample(link=False)), "link")


class Grading(unittest.TestCase):
    def test_link_lost_is_p1(self):
        self.assertEqual(c.event_grade(True, 0, 1, 0, 4), "P1")

    def test_tcp_failing_is_p1(self):
        self.assertEqual(c.event_grade(False, 2, 1, 0, 4), "P1")

    def test_sustained_with_modem_down_throughout_is_p2(self):
        self.assertEqual(c.event_grade(False, 0, 5, 5, 4), "P2")

    def test_sustained_with_tcp_ok_is_p3(self):
        self.assertEqual(c.event_grade(False, 1, 5, 2, 4), "P3")

    def test_short_with_tcp_ok_is_p4(self):
        self.assertEqual(c.event_grade(False, 0, 3, 3, 4), "P4")


if __name__ == "__main__":
    unittest.main()
