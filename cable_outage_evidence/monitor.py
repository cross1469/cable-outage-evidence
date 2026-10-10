"""The probe loop (spec §3, §5): one sample every interval_ms, events, files."""

import concurrent.futures as cf
import datetime as dt
import logging
import queue
import threading
import time
from typing import Dict, Optional

from . import classify, config, events, platform_net, storage

log = logging.getLogger("cable-outage-evidence")


class PathState:
    """Default gateway and ISP first hop, refreshed off the probe loop.

    Reading the routing table means a subprocess on Windows and macOS, too slow
    for every 500 ms sample, so the gateway is checked every few seconds and the
    first hop on the discovery interval or when the gateway changes.
    """

    GATEWAY_CHECK_SEC = 5

    def __init__(self, cfg: config.Config, pinger: platform_net.Pinger):
        self.cfg = cfg
        self.pinger = pinger
        self.lock = threading.Lock()
        self.gateway: Optional[str] = platform_net.default_gateway()
        self.first_hop: Optional[str] = None
        self._discover_now = threading.Event()
        self._discover_now.set()
        self._stop = threading.Event()

    def snapshot(self):
        with self.lock:
            return self.gateway, self.first_hop

    def start(self):
        threading.Thread(target=self._gateway_loop, name="gateway", daemon=True).start()
        threading.Thread(target=self._discovery_loop, name="discovery", daemon=True).start()

    def stop(self):
        self._stop.set()
        self._discover_now.set()

    def _gateway_loop(self):
        while not self._stop.wait(self.GATEWAY_CHECK_SEC):
            gw = platform_net.default_gateway()
            with self.lock:
                changed = gw != self.gateway
                self.gateway = gw
                if changed:
                    # Never report the old topology against a new gateway.
                    self.first_hop = None
            if changed:
                log.info("default gateway changed to %s", gw or "none")
                self._discover_now.set()

    def _discovery_loop(self):
        while not self._stop.is_set():
            self._discover_now.wait(self.cfg.path_discovery_interval_sec)
            self._discover_now.clear()
            if self._stop.is_set():
                return
            self.discover()

    def discover(self) -> Optional[str]:
        gw, _ = self.snapshot()
        if gw is None:
            return None
        hops = platform_net.traceroute(self.cfg.path_discovery_target)
        skip = [gw]
        if self.cfg.modem_type != "none" or self.cfg.modem_url:
            skip.append(self.cfg.modem_address)
        hop = platform_net.first_isp_hop(hops, skip)
        # Adopted only if it answers ping; some ISP routers answer only with
        # TTL-exceeded, and probing those would cost every sample a timeout.
        if hop and platform_net.ping_with_retries(self.pinger, hop, 2, self.cfg.ping_timeout_sec) is None:
            log.info("first hop %s does not answer ping; not probed", hop)
            hop = None
        with self.lock:
            if hop or not hops:
                # A failed traceroute keeps the last valid address.
                self.first_hop = hop if hop else self.first_hop
            else:
                self.first_hop = None
        log.info("ISP first hop: %s", hop or "unknown")
        return hop


class Monitor:
    def __init__(self, cfg: config.Config):
        self.cfg = cfg
        self.names = [n for n, _ in cfg.public_targets]
        self.store = storage.Store(cfg.data_path)
        self.pinger = platform_net.Pinger()
        self.path = PathState(cfg, self.pinger)
        self.tracker = events.Tracker(cfg.interval_ms, cfg.sustained_samples, cfg.recovery_rounds,
                                      cfg.pre_event_seconds, cfg.post_recovery_seconds)
        self.minutes = storage.MinuteSummary(self.names)
        # A failing ping takes retries x timeout, so during an outage several
        # samples are in flight at once; this many keeps the 500 ms cadence.
        self.max_in_flight = max(2, int(cfg.ping_retries * cfg.ping_timeout_sec * 1000 / cfg.interval_ms) + 2)
        self.sampler = cf.ThreadPoolExecutor(max_workers=self.max_in_flight, thread_name_prefix="sample")
        per_sample = 3 + len(self.names)
        self.pool = cf.ThreadPoolExecutor(max_workers=self.max_in_flight * per_sample + len(self.names),
                                          thread_name_prefix="probe")
        self.tcp_results: "queue.Queue" = queue.Queue()
        self.incident_paths: Dict[int, object] = {}
        self.awaiting_tcp = set()
        self.sequence = 0
        self._stop = threading.Event()
        self.on_event_start = None      # hook for the modem module's fast mode
        self.on_event_recovered = None

    # --- probing -------------------------------------------------------------

    def _ping(self, address: Optional[str]) -> classify.Probe:
        if not address:
            return classify.Probe(None)
        rtt = platform_net.ping_with_retries(self.pinger, address, self.cfg.ping_retries,
                                             self.cfg.ping_timeout_sec)
        return classify.Probe(address, rtt)

    def take_sample(self, sequence: int = 0, timestamp: str = "", late_ms: int = 0) -> classify.Sample:
        """Probes every layer at once. The timestamp is when the sample started:
        during an outage the pings take retries x timeout to give up."""
        timestamp = timestamp or storage.now_text()
        gw, hop = self.path.snapshot()
        modem = self.cfg.modem_address if self.cfg.modem_url else None
        if modem == gw:
            modem = None    # the "modem" address is the home router itself
        futures = {
            "lan": self.pool.submit(self._ping, gw),
            "modem": self.pool.submit(self._ping, modem),
            "hop": self.pool.submit(self._ping, hop),
        }
        for name, address in self.cfg.public_targets:
            futures[name] = self.pool.submit(self._ping, address)
        return classify.Sample(
            timestamp=timestamp, sequence=sequence, link=gw is not None,
            lan_gateway=futures["lan"].result(), modem=futures["modem"].result(),
            first_hop=futures["hop"].result(),
            public={n: futures[n].result() for n in self.names}, late_ms=late_ms)

    def _tcp_checks(self, ev: events.Event):
        results = {}
        checks = {n: self.pool.submit(platform_net.tcp_connect, a, 443, self.cfg.tcp_timeout_sec)
                  for n, a in self.cfg.public_targets}
        for n, fut in checks.items():
            results[n] = fut.result()
        self.tcp_results.put((ev, results))

    def _apply_tcp(self, ev: events.Event, results):
        ev.tcp = results
        if self.tracker.regrade(ev):
            log.warning("outage grade now %s (TCP/443)", ev.grade)
        if id(ev) in self.awaiting_tcp:
            self.awaiting_tcp.discard(id(ev))
            self._finish(ev)

    def _finish(self, ev: events.Event):
        self.store.append_csv("events.csv", storage.event_header(self.names),
                              [storage.event_row(ev, self.names)])
        self.store.append_jsonl("events-journal.jsonl", {
            "type": "recovery", "start": ev.start.timestamp, "end": ev.end.timestamp,
            "grade": ev.grade, "classification": ev.worst})
        log.warning("outage recovered %s, grade %s (%s), %d bad samples",
                    ev.end.timestamp, ev.grade, ev.worst, ev.bad_samples)

    # --- files -----------------------------------------------------------------

    def _incident_path(self, ev: events.Event):
        key = id(ev)
        if key not in self.incident_paths:
            d = self.store.day_dir(dt.date.fromisoformat(ev.start.timestamp[:10])) / "incidents"
            d.mkdir(exist_ok=True)
            stamp = ev.start.timestamp[:19].replace("-", "").replace(":", "").replace(" ", "-")
            self.incident_paths[key] = d / f"incident-{stamp}.csv"
        return self.incident_paths[key]

    def handle(self, up: events.Update):
        header = storage.sample_header(self.names)
        by_event = {}
        for ev, s in up.writes:
            by_event.setdefault(id(ev), (ev, []))[1].append(storage.sample_row(s))
        for ev, rows in by_event.values():
            self.store.append_csv("", header, rows, path=self._incident_path(ev))
        if up.started:
            ev = up.started
            log.warning("outage started %s: %s", ev.start.timestamp, ev.start.classification)
            self.store.append_jsonl("events-journal.jsonl", {
                "type": "start", "time": ev.start.timestamp, "classification": ev.start.classification,
                "first_failing_layer": ev.first_failing_layer})
            threading.Thread(target=self._tcp_checks, args=(ev,), daemon=True).start()
            if self.on_event_start:
                self.on_event_start(ev)
        if up.grade_raised:
            log.warning("outage grade now %s", up.grade_raised.grade)
        if up.recovered:
            ev = up.recovered
            if ev.tcp:
                self._finish(ev)
            else:
                # The TCP/443 checks can outlast a short outage; the row waits for them.
                self.awaiting_tcp.add(id(ev))
            if self.on_event_recovered:
                self.on_event_recovered(ev)
        if up.closed:
            self.incident_paths.pop(id(up.closed), None)

    # --- loop ---------------------------------------------------------------------

    def stop(self):
        self._stop.set()

    def _process(self, sample: classify.Sample):
        while not self.tcp_results.empty():
            self._apply_tcp(*self.tcp_results.get())
        self.handle(self.tracker.feed(sample))
        row = self.minutes.add(sample)
        if row:
            self.store.append_csv("minute-summary.csv", self.minutes.header(), [row])

    def run(self):
        self.path.start()
        self.store.append_jsonl("sessions.jsonl", {"type": "start", "time": storage.now_text(),
                                                   "ping_method": self.pinger.method})
        log.info("monitoring every %d ms; ping via %s; data in %s",
                 self.cfg.interval_ms, self.pinger.method, self.cfg.data_path)
        interval = self.cfg.interval_ms / 1000
        next_start = time.monotonic()
        last_cleanup = None
        pending: Dict[int, cf.Future] = {}
        next_feed = 1
        skipped = 0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_start:
                    if now - next_start > 5:
                        # After sleep or a long stall, restart the schedule instead
                        # of firing a burst of catch-up samples.
                        next_start = now
                    if len(pending) < self.max_in_flight:
                        self.sequence += 1
                        # Lateness is recorded, never absorbed (spec 3.2); a tick
                        # skipped because too many samples were in flight counts too.
                        late_ms = int((now - next_start) * 1000) + int(skipped * interval * 1000)
                        skipped = 0
                        pending[self.sequence] = self.sampler.submit(
                            self.take_sample, self.sequence, storage.now_text(), late_ms)
                    else:
                        skipped += 1
                    next_start += interval
                # Samples finish out of order during an outage; events need them in order.
                while next_feed in pending and pending[next_feed].done():
                    self._process(pending.pop(next_feed).result())
                    next_feed += 1
                today = dt.date.today()
                if last_cleanup != today:
                    self.store.cleanup(self.cfg.retention_days)
                    last_cleanup = today
                self._stop.wait(min(0.02, max(0.0, next_start - time.monotonic())))
            for seq in sorted(pending):
                self._process(pending[seq].result())
            if self.minutes.samples:
                self.store.append_csv("minute-summary.csv", self.minutes.header(), [self.minutes.row()])
        finally:
            self.path.stop()
            self.sampler.shutdown(wait=False)
            self.pool.shutdown(wait=False)
            self.store.append_jsonl("sessions.jsonl", {"type": "stop", "time": storage.now_text(),
                                                       "open_event": self.tracker.event.start.timestamp
                                                       if self.tracker.event else None})
