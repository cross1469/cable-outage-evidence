"""Exercises the real platform commands. Runs only with COE_LIVE=1 (set in CI),
because it depends on the machine: ping, the routing table, traceroute."""

import os
import sys
import unittest

from cable_outage_evidence import platform_net as pn

LIVE = os.environ.get("COE_LIVE") == "1"


@unittest.skipUnless(LIVE, "set COE_LIVE=1 to run against this machine")
class LivePlatform(unittest.TestCase):
    def test_ping_command_parses_loopback(self):
        p = pn.Pinger()
        p.method = "command"
        rtt = p.ping("127.0.0.1", 1.0)
        self.assertIsNotNone(rtt, "the system ping output was not parsed")
        self.assertLess(rtt, 100)

    def test_ping_socket_where_available(self):
        p = pn.Pinger()
        if p.method != "socket":
            self.skipTest(f"no unprivileged ICMP socket on {sys.platform}")
        self.assertIsNotNone(p.ping("127.0.0.1", 1.0))

    def test_unanswered_ping_fails_in_time(self):
        p = pn.Pinger()
        p.method = "command"
        # TEST-NET-1 is never routed; the attempt must fail, not hang.
        self.assertIsNone(p.ping("192.0.2.1", 1.0))

    def test_default_gateway_reads(self):
        gw = pn.default_gateway()
        print(f"\n  {sys.platform}: default gateway {gw}")
        if gw is not None:
            self.assertRegex(gw, r"^\d+\.\d+\.\d+\.\d+$")

    def test_traceroute_does_not_crash(self):
        hops = pn.traceroute("1.1.1.1", max_hops=2, wait_sec=1)
        print(f"\n  {sys.platform}: traceroute hops {hops}")
        self.assertIsInstance(hops, list)

    def test_tcp_connect(self):
        self.assertFalse(pn.tcp_connect("127.0.0.1", 9, 1.0))


if __name__ == "__main__":
    unittest.main()
