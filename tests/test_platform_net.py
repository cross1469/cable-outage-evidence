import unittest

from cable_outage_evidence import platform_net as pn


class PingParsing(unittest.TestCase):
    def test_windows_english(self):
        out = "Pinging 1.1.1.1 with 32 bytes of data:\nReply from 1.1.1.1: bytes=32 time=4ms TTL=57\n"
        self.assertEqual(pn.parse_ping(out), 4.0)

    def test_windows_chinese(self):
        out = "Ping 1.1.1.1 (使用 32 位元組的資料):\n回覆自 1.1.1.1: 位元組=32 時間=12ms TTL=57\n"
        self.assertEqual(pn.parse_ping(out), 12.0)

    def test_windows_below_one_ms(self):
        self.assertEqual(pn.parse_ping("Reply from 192.168.0.1: bytes=32 time<1ms TTL=64"), 0.5)

    def test_windows_unreachable_is_not_a_reply(self):
        out = "Reply from 192.168.8.1: Destination host unreachable.\n"
        self.assertIsNone(pn.parse_ping(out))

    def test_linux_and_macos(self):
        out = "64 bytes from 8.8.8.8: icmp_seq=1 ttl=117 time=5.12 ms\n"
        self.assertEqual(pn.parse_ping(out), 5.12)

    def test_timeout(self):
        self.assertIsNone(pn.parse_ping("Request timed out.\n"))


class GatewayParsing(unittest.TestCase):
    def test_route_print_lowest_metric(self):
        out = ("Network Destination        Netmask          Gateway       Interface  Metric\n"
               "          0.0.0.0          0.0.0.0      192.168.8.1    192.168.8.193     20\n"
               "          0.0.0.0          0.0.0.0      10.0.0.1       10.0.0.5          50\n")
        self.assertEqual(pn.parse_route_print(out), "192.168.8.1")

    def test_route_print_none(self):
        self.assertIsNone(pn.parse_route_print("===========\nPersistent Routes:\n  None\n"))

    def test_proc_net_route(self):
        out = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
               "eth0\t00000000\t0108A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
               "eth0\t0008A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n")
        self.assertEqual(pn.parse_proc_net_route(out), "192.168.8.1")

    def test_route_get(self):
        out = "   route to: default\ndestination: default\n    gateway: 192.168.1.1\n  interface: en0\n"
        self.assertEqual(pn.parse_route_get(out), "192.168.1.1")


class Traceroute(unittest.TestCase):
    def test_tracert(self):
        out = ("Tracing route to 1.1.1.1 over a maximum of 3 hops\n\n"
               "  1    <1 ms    <1 ms    <1 ms  192.168.8.1 \n"
               "  2     *        *        *     Request timed out.\n"
               "  3     9 ms     8 ms     9 ms  10.27.0.1 \n\nTrace complete.\n")
        self.assertEqual(pn.parse_traceroute(out), ["192.168.8.1", None, "10.27.0.1"])

    def test_unix_traceroute(self):
        out = ("traceroute to 1.1.1.1 (1.1.1.1), 3 hops max, 60 byte packets\n"
               " 1  192.168.1.1  0.512 ms\n 2  *\n 3  198.51.100.1  9.800 ms\n")
        self.assertEqual(pn.parse_traceroute(out), ["192.168.1.1", None, "198.51.100.1"])

    def test_first_isp_hop_skips_router_and_modem_but_not_private(self):
        hops = ["192.168.8.1", "192.168.0.1", None, "10.27.0.1", "198.51.100.1"]
        self.assertEqual(pn.first_isp_hop(hops, ["192.168.8.1", "192.168.0.1"]), "10.27.0.1")

    def test_first_isp_hop_none(self):
        self.assertIsNone(pn.first_isp_hop([None, None], ["192.168.8.1"]))


if __name__ == "__main__":
    unittest.main()
