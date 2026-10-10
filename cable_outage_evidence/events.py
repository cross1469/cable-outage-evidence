"""Event lifecycle (spec §5): start, recovery, and the window kept around it.

The tracker is pure: it takes samples and returns what happened, and the
monitor does the I/O (TCP checks, files, the modem's fast mode).
"""

import collections
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from . import classify


@dataclass(eq=False)
class Event:
    start: classify.Sample
    end: Optional[classify.Sample] = None     # the first recovered sample
    worst: str = ""
    bad_samples: int = 0
    modem_down_samples: int = 0
    lan_down_samples: int = 0
    link_lost: bool = False
    failed: Dict[str, bool] = field(default_factory=dict)   # layer/target -> ever failed
    tcp: Dict[str, Optional[bool]] = field(default_factory=dict)
    first_failing_layer: str = ""
    grade: str = "P4"

    @property
    def tcp_failures(self) -> int:
        return sum(1 for ok in self.tcp.values() if ok is False)


@dataclass
class Update:
    started: Optional[Event] = None
    grade_raised: Optional[Event] = None
    recovered: Optional[Event] = None
    # Rows for incident files, in order; the event names the file.
    writes: List[Tuple[Event, classify.Sample]] = field(default_factory=list)
    # The incident whose post-recovery window just closed (after its writes).
    closed: Optional[Event] = None


class Tracker:
    def __init__(self, interval_ms: int, sustained_samples: int, recovery_rounds: int,
                 pre_event_seconds: int, post_recovery_seconds: int):
        self.sustained = sustained_samples
        self.recovery_rounds = recovery_rounds
        per_sec = max(1, round(1000 / interval_ms))
        self.ring: Deque[classify.Sample] = collections.deque(maxlen=max(1, pre_event_seconds * per_sec))
        self.post_samples = post_recovery_seconds * per_sec
        self.event: Optional[Event] = None
        self.healthy_run = 0
        self.first_recovered: Optional[classify.Sample] = None
        self.closing: Optional[Event] = None
        self.post_left = 0

    def regrade(self, ev: Event) -> bool:
        """Recomputes the grade; returns True if it rose."""
        grade = classify.event_grade(ev.link_lost, ev.tcp_failures, ev.bad_samples,
                                     ev.modem_down_samples, self.sustained)
        if classify.GRADE_RANK[grade] > classify.GRADE_RANK[ev.grade]:
            ev.grade = grade
            return True
        return False

    def feed(self, s: classify.Sample) -> Update:
        up = Update()
        s.classification = classify.classify_sample(s)
        outage = classify.is_outage(s.classification)

        if self.event is None:
            if self.closing is not None:
                if outage:
                    # A new outage ends the previous post-recovery window early.
                    up.closed, self.closing = self.closing, None
                else:
                    up.writes.append((self.closing, s))
                    self.post_left -= 1
                    if self.post_left <= 0:
                        up.closed, self.closing = self.closing, None
            if outage:
                ev = Event(start=s, first_failing_layer=classify.first_failing_layer(s))
                self.event = ev
                self.healthy_run = 0
                self.first_recovered = None
                up.started = ev
                up.writes.extend((ev, old) for old in self.ring)

        ev = self.event
        if ev is not None:
            up.writes.append((ev, s))
            if outage:
                self.healthy_run = 0
                self.first_recovered = None
                ev.bad_samples += 1
                ev.link_lost |= not s.link
                ev.modem_down_samples += s.modem.failed
                ev.lan_down_samples += s.lan_gateway.failed
                if classify.SEVERITY.get(s.classification, 0) > classify.SEVERITY.get(ev.worst, 0):
                    ev.worst = s.classification
                for name, probe in self._probes(s).items():
                    ev.failed[name] = ev.failed.get(name, False) or probe.failed
                if self.regrade(ev) and up.started is None:
                    up.grade_raised = ev
            else:
                self.healthy_run += 1
                if self.healthy_run == 1:
                    self.first_recovered = s
                if self.healthy_run >= self.recovery_rounds:
                    ev.end = self.first_recovered
                    up.recovered = ev
                    self.event = None
                    self.closing = ev
                    self.post_left = self.post_samples
                    if self.post_left <= 0:
                        up.closed, self.closing = ev, None

        self.ring.append(s)
        return up

    @staticmethod
    def _probes(s: classify.Sample) -> Dict[str, classify.Probe]:
        probes = {"lan_gateway": s.lan_gateway, "modem": s.modem, "isp_first_hop": s.first_hop}
        probes.update(s.public)
        return probes
