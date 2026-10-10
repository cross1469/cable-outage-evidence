"""Per-sample classification (spec §4) and event grading (spec §6).

Pure functions: no I/O, so every branch can be tested directly.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Probe:
    address: Optional[str]          # None: not probed (unknown or disabled)
    rtt: Optional[float] = None     # None with an address: no reply

    @property
    def probed(self) -> bool:
        return self.address is not None

    @property
    def ok(self) -> bool:
        return self.rtt is not None

    @property
    def failed(self) -> bool:
        return self.probed and not self.ok


@dataclass
class Sample:
    timestamp: str
    sequence: int
    link: bool                      # a default route exists
    lan_gateway: Probe
    modem: Probe
    first_hop: Probe
    public: Dict[str, Probe] = field(default_factory=dict)
    late_ms: int = 0
    classification: str = ""

    @property
    def public_failures(self) -> int:
        return sum(1 for p in self.public.values() if not p.ok)


def classify_sample(s: Sample) -> str:
    if not s.link:
        return "LINK_DOWN"
    if s.public_failures >= 2:
        if s.lan_gateway.failed:
            return "LAN_GATEWAY_UNREACHABLE"
        if s.modem.failed:
            return "MODEM_UNREACHABLE"
        if not s.first_hop.probed:
            return "ISP_UPSTREAM_OUTAGE_NO_HOP"
        if s.first_hop.failed:
            return "ISP_ACCESS_OUTAGE"
        return "ISP_UPSTREAM_OUTAGE"
    if s.lan_gateway.failed:
        return "LAN_GATEWAY_ICMP_ONLY"
    if s.modem.failed:
        return "MODEM_ICMP_ONLY"
    if s.first_hop.failed:
        return "FIRST_HOP_ICMP_ONLY"
    for name, probe in s.public.items():
        if not probe.ok:
            return f"{name.upper()}_PATH_ONLY"
    return "OK"


OUTAGE_CLASSES = {
    "LINK_DOWN", "LAN_GATEWAY_UNREACHABLE", "MODEM_UNREACHABLE",
    "ISP_ACCESS_OUTAGE", "ISP_UPSTREAM_OUTAGE", "ISP_UPSTREAM_OUTAGE_NO_HOP",
}

# Worst first; an event is labelled with the worst class any of its samples had.
SEVERITY = {
    "LINK_DOWN": 100, "LAN_GATEWAY_UNREACHABLE": 60, "MODEM_UNREACHABLE": 55,
    "ISP_ACCESS_OUTAGE": 40, "ISP_UPSTREAM_OUTAGE": 30, "ISP_UPSTREAM_OUTAGE_NO_HOP": 30,
}


def is_outage(classification: str) -> bool:
    return classification in OUTAGE_CLASSES


def first_failing_layer(s: Sample) -> str:
    """The failing layer nearest the computer (spec §7.2)."""
    if not s.link:
        return "link"
    for name, probe in (("lan_gateway", s.lan_gateway), ("modem", s.modem), ("isp_first_hop", s.first_hop)):
        if probe.failed:
            return name
    return "public" if s.public_failures else "none"


def event_grade(link_lost: bool, tcp_failures: int, bad_samples: int,
                modem_down_samples: int, sustained_samples: int) -> str:
    """Grades whether the network really broke, from the evidence (spec §6).

    TCP/443 completing is positive evidence that the data path worked, so it
    lowers the grade rather than merely failing to raise it. Losing the modem in
    the same sample as the public targets usually means our own probing stalled;
    only loss of it throughout a sustained event counts as a modem problem.
    """
    if link_lost or tcp_failures >= 2:
        return "P1"
    if bad_samples >= sustained_samples:
        return "P2" if modem_down_samples >= bad_samples else "P3"
    return "P4"


GRADE_RANK = {"P1": 4, "P2": 3, "P3": 2, "P4": 1}

GRADE_SUMMARY = {
    "P1": "Confirmed outage",
    "P2": "Probable outage (modem unreachable throughout)",
    "P3": "Sustained ICMP loss; TCP/443 still completed",
    "P4": "Below noise floor; TCP/443 completed",
}


def grade_notifies_immediately(grade: str) -> bool:
    return grade in ("P1", "P2")
