import concurrent.futures
import tempfile
import time
import unittest

from cable_outage_evidence import cbn, config, evidence, hitron, modem, modem_worker, setup, storage
from tests.fake_cbn import FakeCbn


class CbnTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCbn()
        self.cfg = config.Config(modem_url=self.fake.url)

    def tearDown(self):
        self.fake.close()

    def client(self):
        client = modem.create_client(self.cfg)
        client.login()
        self.addCleanup(client.logout)
        return client

    def test_codec_uri_unicode_and_reserved_characters(self):
        value = {"fun": 7, "Password": "測試 +%&/=!()'"}
        self.assertEqual(cbn.decode_payload(cbn.encode_payload(value).encode()), value)
        for raw in (b"HTML", b"W10=", b"%%%"):
            with self.assertRaises(hitron.ModemResponseError):
                cbn.decode_payload(raw)

    def test_auto_and_legacy_configuration(self):
        for kind in ("auto", "hitron-coda", "cbn"):
            self.cfg.modem_type = kind
            config.validate(self.cfg)
            status, lines = setup.check(self.cfg)
            self.assertEqual(status, "ok", lines)
            self.assertIn("  網路存取 network access: Permitted", lines)
            self.assertTrue(any("SC-QAM 1/1 OPERATE，OFDMA 1/2 OPERATE" in s for s in lines))
            self.assertTrue(any("OFDM 1/2 locked" in s for s in lines))
            self.assertFalse(self.fake.logged_in)

    def test_rejected_credentials_are_not_unsupported_or_leaked(self):
        self.cfg.modem_password = "private-secret"
        status, lines = setup.check(self.cfg)
        self.assertEqual(status, "auth_failed")
        self.assertNotIn("private-secret", str(lines))
        self.assertEqual(self.fake.logins, 1)

    def test_concurrent_channel_requests_share_snapshot_and_rotate_token(self):
        client = self.client()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            data = list(pool.map(client.get, hitron.FAST_ENDPOINTS))
        self.assertEqual(self.fake.reads[108], 1)
        self.assertEqual(hitron.counts(data[0][0], data[1][0], dof=data[2][0])["UsOfdmaOperate"], 1)
        client.get("Log")
        client.get("SysInfo")
        self.assertTrue(client.logged_in)
        self.assertGreater(self.fake.sequence, 3)

    def test_channel_mapping_summary_and_logs(self):
        client = self.client()
        data = {ep: client.get(ep)[0] for ep in ("SysInfo", "UsInfo", "UsOfdm", "DsInfo", "DsOfdm")}
        summary = dict(zip(hitron.SUMMARY_HEADER, hitron.summary_row("t", "Periodic", data)))
        self.assertEqual(summary["DsUncorrectableTotal"], "3")
        self.assertEqual(summary["DsPowerMin"], "7.199")
        channels = hitron.channel_rows("t", "Periodic", us=data["UsInfo"], of=data["UsOfdm"], ds=data["DsInfo"], dof=data["DsOfdm"])
        channels = [dict(zip(hitron.CHANNEL_HEADER, r)) for r in channels]
        self.assertEqual(channels[0]["State"], "OPERATE")
        self.assertEqual(channels[1]["Modulation"], "2048QAM")
        self.assertEqual(channels[3]["Uncorrectable"], "3")
        entry = hitron.log_entries(client.get("Log")[0])[0]
        self.assertEqual(entry[2], evidence.T4)
        self.assertEqual(evidence.evidence_kind(entry[2], entry[3]), "strong")
        self.assertEqual(entry[4], "00:00:00:00:00:02")
        self.assertEqual(entry[5], "00:00:00:00:00:01")

    def test_expired_session_is_reauthenticated_by_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.cfg.data_dir = tmp
            worker = modem_worker.ModemWorker(self.cfg, storage.Store(self.cfg.data_path))
            self.addCleanup(worker.client.logout)
            self.addCleanup(worker.core.shutdown)
            self.addCleanup(worker.slow.shutdown)
            worker._get("UsInfo")
            self.fake.logged_in = False
            with self.assertRaises(hitron.ModemResponseError):
                worker._get("Log")
            worker._get("UsInfo")
            self.assertEqual(self.fake.logins, 2)
            self.assertEqual(worker.status, "ok")

    def test_malformed_read_invalidates_session(self):
        client = self.client()
        self.fake.bad_response = True
        with self.assertRaises(hitron.ModemResponseError):
            client.get("Log")
        self.assertFalse(client.logged_in)

    def test_missing_channels_do_not_become_successful_empty_read(self):
        client = self.client()
        del self.fake.docsis["Upstream"]
        with self.assertRaises(hitron.ModemResponseError):
            client.get("UsInfo")

    def test_queued_request_has_deadline(self):
        client = cbn.CbnClient(self.fake.url, "admin", "password")
        client._lock.acquire()
        try:
            start = time.monotonic()
            with self.assertRaises(hitron.ModemUnreachable):
                client.get("UsInfo", timeout=0.05)
            self.assertLess(time.monotonic() - start, 0.5)
        finally:
            client._lock.release()

    def test_malformed_device_information_is_a_read_error(self):
        client = self.client()
        self.fake.docsis["DeviceInfo"] = []
        with self.assertRaises(hitron.ModemResponseError):
            client.get("UsInfo")
        self.assertFalse(client.logged_in)

    def test_description_evidence_mapping_is_conservative(self):
        self.assertEqual(cbn.event_type("No Ranging Response received - T3 time-out"), evidence.T3)
        self.assertEqual(cbn.event_type("CM-STATUS message sent. Event Type Code: 6; Chan ID: 11;"), evidence.CM_STATUS)
        self.assertEqual(cbn.event_type("US profile assignment change"), "")
        self.assertEqual(cbn.event_type("DHCP RENEW notice"), "")
