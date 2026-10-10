"""Daily CSV/JSONL files (spec §9).

Each day has its own directory. If a CSV header changes after an update, the
day's old file is kept beside the new one under a .schema-<time>.csv name.
"""

import csv
import datetime as dt
import json
import re
import shutil
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from . import classify

DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def now_text(t: Optional[dt.datetime] = None) -> str:
    t = t or dt.datetime.now()
    return t.strftime("%Y-%m-%d %H:%M:%S.") + f"{t.microsecond // 1000:03d}"


def fmt_rtt(v: Optional[float]) -> str:
    return "" if v is None else f"{v:.1f}"


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._checked = set()

    def day_dir(self, day: Optional[dt.date] = None) -> Path:
        d = self.root / (day or dt.date.today()).isoformat()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _ensure_header(self, path: Path, header: Sequence[str]) -> None:
        key = (path, tuple(header))
        if key in self._checked:
            return
        if path.exists() and path.stat().st_size:
            with path.open(encoding="utf-8", newline="") as fh:
                existing = next(csv.reader(fh), [])
            if existing != list(header):
                stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                path.rename(path.with_name(f"{path.stem}.schema-{stamp}{path.suffix}"))
        if not path.exists() or not path.stat().st_size:
            with path.open("w", encoding="utf-8", newline="") as fh:
                csv.writer(fh).writerow(header)
        self._checked.add(key)

    def append_csv(self, name: str, header: Sequence[str], rows: Iterable[Sequence],
                   path: Optional[Path] = None) -> None:
        path = path or self.day_dir() / name
        with self._lock:
            self._ensure_header(path, header)
            with path.open("a", encoding="utf-8", newline="") as fh:
                csv.writer(fh).writerows(rows)

    def append_jsonl(self, name: str, record: Dict) -> None:
        with self._lock:
            with (self.day_dir() / name).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def cleanup(self, retention_days: int) -> List[str]:
        """Removes dated day directories older than the retention window; never
        touches anything else under the root."""
        cutoff = dt.date.today() - dt.timedelta(days=retention_days)
        removed = []
        for child in self.root.iterdir():
            if child.is_dir() and DAY_DIR.match(child.name):
                try:
                    day = dt.date.fromisoformat(child.name)
                except ValueError:
                    continue
                if day < cutoff:
                    shutil.rmtree(child)
                    removed.append(child.name)
        return removed


# --- row layouts ---------------------------------------------------------------

LAYERS = (("LanGateway", "lan_gateway"), ("Modem", "modem"), ("FirstHop", "first_hop"))


def sample_header(public_names: Sequence[str]) -> List[str]:
    h = ["Timestamp", "Sequence", "Link"]
    for label, _ in LAYERS:
        h += [f"{label}Address", f"{label}OK", f"{label}RTT"]
    for name in public_names:
        h += [f"{name}OK", f"{name}RTT"]
    return h + ["Classification", "LateMs"]


def sample_row(s: classify.Sample) -> List[str]:
    row = [s.timestamp, str(s.sequence), "1" if s.link else "0"]
    for _, attr in LAYERS:
        p = getattr(s, attr)
        row += [p.address or "", "" if not p.probed else ("1" if p.ok else "0"), fmt_rtt(p.rtt)]
    for p in s.public.values():
        row += ["1" if p.ok else "0", fmt_rtt(p.rtt)]
    return row + [s.classification, str(s.late_ms)]


def event_header(public_names: Sequence[str]) -> List[str]:
    h = ["StartTime", "EndTime", "DurationSeconds", "Grade", "Classification", "FirstFailingLayer",
         "BadSamples", "ModemDownSamples", "LanGatewayDownSamples", "LinkLost",
         "LanGatewayAddress", "ModemAddress", "FirstHopAddress",
         "LanGatewayFailed", "ModemFailed", "FirstHopFailed"]
    h += [f"{n}Failed" for n in public_names]
    h += [f"{n}TCP443" for n in public_names]
    return h


def _parse_ts(text: str) -> dt.datetime:
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f")


def event_row(ev, public_names: Sequence[str]) -> List[str]:
    end = ev.end.timestamp if ev.end else ""
    duration = f"{(_parse_ts(end) - _parse_ts(ev.start.timestamp)).total_seconds():.1f}" if end else ""
    yn = lambda v: "" if v is None else ("1" if v else "0")
    row = [ev.start.timestamp, end, duration, ev.grade, ev.worst, ev.first_failing_layer,
           str(ev.bad_samples), str(ev.modem_down_samples), str(ev.lan_down_samples), yn(ev.link_lost),
           ev.start.lan_gateway.address or "", ev.start.modem.address or "", ev.start.first_hop.address or "",
           yn(ev.failed.get("lan_gateway")), yn(ev.failed.get("modem")), yn(ev.failed.get("isp_first_hop"))]
    row += [yn(ev.failed.get(n)) for n in public_names]
    row += [yn(ev.tcp.get(n)) for n in public_names]
    return row


class MinuteSummary:
    """Per-minute loss and RTT for each target."""

    def __init__(self, public_names: Sequence[str]):
        self.names = ["LanGateway", "Modem", "FirstHop"] + list(public_names)
        self.minute = None
        self._reset()

    def header(self) -> List[str]:
        h = ["Minute", "Samples", "LinkDown", "MaxLateMs"]
        for n in self.names:
            h += [f"{n}LossPct", f"{n}RttMin", f"{n}RttAvg", f"{n}RttMax"]
        return h

    def _reset(self):
        self.samples = 0
        self.link_down = 0
        self.max_late = 0
        self.stats = {n: [0, 0, []] for n in self.names}   # probed, lost, rtts

    def add(self, s: classify.Sample) -> Optional[List[str]]:
        """Adds a sample; returns the finished row when a new minute starts."""
        minute = s.timestamp[:16]
        row = None
        if self.minute is not None and minute != self.minute and self.samples:
            row = self.row()
            self._reset()
        self.minute = minute
        self.samples += 1
        self.link_down += not s.link
        self.max_late = max(self.max_late, s.late_ms)
        probes = [s.lan_gateway, s.modem, s.first_hop] + list(s.public.values())
        for n, p in zip(self.names, probes):
            if not p.probed:
                continue
            st = self.stats[n]
            st[0] += 1
            if p.ok:
                st[2].append(p.rtt)
            else:
                st[1] += 1
        return row

    def row(self) -> List[str]:
        out = [self.minute, str(self.samples), str(self.link_down), str(self.max_late)]
        for n in self.names:
            probed, lost, rtts = self.stats[n]
            if not probed:
                out += ["", "", "", ""]
                continue
            out.append(f"{100 * lost / probed:.1f}")
            out += [f"{min(rtts):.1f}", f"{sum(rtts) / len(rtts):.1f}", f"{max(rtts):.1f}"] if rtts else ["", "", ""]
        return out
