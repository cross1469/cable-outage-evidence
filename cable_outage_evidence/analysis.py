"""Observational event classes and timelines (spec §7).

Everything here reads the daily files back, so an event can be classified
again at any time, and nothing depends on the monitor still running.
Labels describe observations only; no label or reason names a cause.
"""

import csv
import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import evidence, storage

TS_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
MODEM_LOG_FORMAT = "%m/%d/%Y %H:%M:%S"
WINDOW_SEC = 60            # control-plane evidence window either side (spec §7.2)
TIMELINE_SEC = 120
LOCAL_STALL_MS = 100
MAX_GAP_SEC = 2.0
MAX_FIRST_GAP_SEC = 6.0    # the last 5 s observation before the first fast tick


def parse_ts(text: str) -> Optional[dt.datetime]:
    text = (text or "").strip()
    for fmt in (TS_FORMAT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def fmt(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


# --- reading the daily files ------------------------------------------------------

def day_dirs(root: Path, first: dt.date, last: dt.date) -> List[Path]:
    out = []
    d = first
    while d <= last:
        p = root / d.isoformat()
        if p.is_dir():
            out.append(p)
        d += dt.timedelta(days=1)
    return out


def read_rows(root: Path, name: str, t0: dt.datetime, t1: dt.datetime,
              ts_field: str = "Timestamp") -> List[Dict[str, str]]:
    """Rows of every day file `name` whose ts_field falls in [t0, t1]. A
    window can cross midnight, so neighbouring days are read too."""
    rows = []
    for d in day_dirs(root, (t0 - dt.timedelta(days=1)).date(), (t1 + dt.timedelta(days=1)).date()):
        path = d / name
        if not path.exists():
            continue
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                t = parse_ts(row.get(ts_field, ""))
                if t is not None and t0 <= t <= t1:
                    row["_t"] = t
                    rows.append(row)
    rows.sort(key=lambda r: r["_t"])
    return rows


def read_jsonl(root: Path, name: str, first: dt.date, last: dt.date) -> List[Dict]:
    out = []
    for d in day_dirs(root, first, last):
        path = d / name
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
    return out


def all_events(root: Path, first: dt.date = dt.date(2000, 1, 1),
               last: dt.date = dt.date(2999, 1, 1)) -> List[Dict[str, str]]:
    rows = []
    if not root.exists():
        return rows
    for d in sorted(p for p in root.iterdir() if p.is_dir() and storage.DAY_DIR.match(p.name)):
        day = dt.date.fromisoformat(d.name)
        if not first <= day <= last or not (d / "events.csv").exists():
            continue
        with (d / "events.csv").open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                row["_start"] = parse_ts(row["StartTime"])
                row["_end"] = parse_ts(row["EndTime"]) or row["_start"]
                if row["_start"]:
                    rows.append(row)
    rows.sort(key=lambda r: r["_start"])
    return rows


def find_event(root: Path, match: str = "") -> Optional[Dict[str, str]]:
    """The latest event, or the latest whose start begins with `match`
    (a partial time such as '2026-09-03 23:26')."""
    events = all_events(root)
    if match:
        events = [e for e in events if e["StartTime"].startswith(match.strip())]
    return events[-1] if events else None


def incident_samples(root: Path, event: Dict[str, str]) -> List[Dict[str, str]]:
    start = event["_start"]
    stamp = start.strftime("%Y%m%d-%H%M%S")
    path = root / start.date().isoformat() / "incidents" / f"incident-{stamp}.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["_t"] = parse_ts(r["Timestamp"])
    return [r for r in rows if r["_t"]]


# --- modem observations ------------------------------------------------------------

@dataclass
class Observation:
    t: dt.datetime
    source: str                 # "us_scqam" / "us_ofdma" (per-channel rows) or "fast" (a 1 s tick)
    state: Tuple                # comparable state; counts for fast ticks
    text: str                   # human-readable summary


def _channel_observations(rows: List[Dict[str, str]]) -> List[Observation]:
    """One observation per channel kind per read.

    Kinds are kept apart because a read can carry one kind without the other
    (only UsOfdm answered), which must not look like the other kind's channels
    disappearing. Rows are merged by channel index, because two reads within
    one second can share a timestamp.
    """
    groups: Dict[Tuple, Dict[str, Dict[str, str]]] = {}
    for r in rows:
        # An empty State is a read that returned no value, not a state.
        if r["Kind"] not in ("us_scqam", "us_ofdma") or not r["State"]:
            continue
        groups.setdefault((r["Timestamp"], r["Reason"], r["Kind"]), {})[r["Index"]] = r
    out = []
    for (ts, reason, kind), by_index in groups.items():
        rs = [by_index[i] for i in sorted(by_index)]
        state = tuple((r["Index"], r["ChannelId"], r["State"], r["FrequencyHz"], r["Width"], r["Modulation"])
                      for r in rs)
        if kind == "us_scqam":
            text = f"SC {sum(r['State'] == 'OPERATE' for r in rs)}/{len(rs)} OPERATE"
        else:
            text = "OFDMA " + " ".join(f"idx{r['Index']} {r['State']}" for r in rs)
        out.append(Observation(rs[0]["_t"], kind, state, text))
    return out


def _fast_observations(rows: List[Dict[str, str]]) -> List[Observation]:
    out = []
    for r in rows:
        if r.get("CoreOK") != "1":
            continue
        key = tuple(r.get(k, "") for k in ("UsScQamOperate", "UsScQamCount", "UsOfdmaOperate",
                                             "UsOfdmaCount", "DsOfdmLocked", "DsOfdmCount"))
        text = (f"SC {key[0]}/{key[1]} OPERATE | OFDMA {key[2]}/{key[3]} OPERATE | OFDM {key[4]}/{key[5]} locked"
                + (f" | DS {r['DsScQamLocked']}/{r['DsScQamCount']}" if r.get("DsScQamCount") else ""))
        out.append(Observation(r["_t"], "fast", key, text))
    return out


# Aggregate rows from full reads only: the rows written at fast-mode entry,
# recovery and exit summarise cached responses and are not observations.
FRESH_AGGREGATE_REASONS = {"Periodic", "Startup"}
AGGREGATE_KEYS = ("UsScQamOperateCount", "UsScQamCount", "UsOfdmaOperateCount", "UsOfdmaCount",
                  "DsOfdmLockedCount", "DsOfdmCount", "DsLockedCount", "DsChannelCount")


def _aggregate_observations(rows: List[Dict[str, str]]) -> List[Observation]:
    """Channel counts from the 30 s full reads in modem-signals.csv. Coarse,
    but the only channel record before per-channel rows existed."""
    out = []
    for r in rows:
        if r.get("ReadOK") != "1" or r.get("Reason") not in FRESH_AGGREGATE_REASONS:
            continue
        key = tuple(r.get(k, "") for k in AGGREGATE_KEYS)
        text = (f"SC {key[0]}/{key[1]} OPERATE | OFDMA {key[2]}/{key[3]} OPERATE | OFDM {key[4]}/{key[5]} locked"
                f" | DS {key[6]}/{key[7]} locked（30 秒讀取）")
        out.append(Observation(r["_t"], "aggregate", key, text))
    return out


def modem_observations(root: Path, t0: dt.datetime, t1: dt.datetime) -> List[Observation]:
    obs = (_channel_observations(read_rows(root, "modem-channels.csv", t0, t1))
           + _fast_observations(read_rows(root, "fast-poll.csv", t0, t1))
           + _aggregate_observations(read_rows(root, "modem-signals.csv", t0, t1)))
    return sorted(obs, key=lambda o: o.t)


def transitions(obs: List[Observation]) -> List[Tuple[Observation, Observation]]:
    """Consecutive observations of the same kind whose state differs.

    A change usually shows in both the per-channel rows and the one-second
    counts; the per-channel pair says more, so a count change within 2 s of
    one is left out. Downstream OFDM lock changes only appear in the counts.
    """
    def pairs(source):
        seq = [o for o in obs if o.source == source]
        return [(a, b) for a, b in zip(seq, seq[1:]) if a.state != b.state]

    chan = pairs("us_scqam") + pairs("us_ofdma")
    fast = [p for p in pairs("fast")
            if not any(abs((p[1].t - c[1].t).total_seconds()) <= 2 for c in chan)]
    finer = chan + fast
    # A 30 s aggregate change is kept only when nothing finer saw it.
    agg = [p for p in pairs("aggregate")
           if not any(p[0].t - dt.timedelta(seconds=2) <= c[1].t <= p[1].t + dt.timedelta(seconds=2) for c in finer)]
    return sorted(finer + agg, key=lambda p: p[1].t)


def coverage(obs: List[Observation], start: dt.datetime, end: dt.datetime) -> Tuple[bool, float, int]:
    """Whether channel state was observed across [start, end] densely enough
    to call the absence of a transition verified (spec §7.2). Returns
    (verified, max gap in seconds, observations inside the outage).

    Upstream state and the one-second ticks are checked separately: only the
    ticks carry the downstream OFDM lock, so a tick whose reads did not all
    complete leaves downstream unobserved even when the upstream rows exist.
    """
    # The 30 s aggregates are too coarse to count towards coverage.
    obs = [o for o in obs if o.source != "aggregate"]
    ticks = [o for o in obs if o.source == "fast"]
    up_ok, up_gap, inside = _covered(obs, start, end)
    if not ticks:
        return False, up_gap, inside
    # Fast mode starts with the outage, so no tick precedes it; the span before
    # the first tick is anchored on the last observation of any kind.
    anchor = [o.t for o in obs if o.t <= start]
    tick_ok, tick_gap, _ = _covered(ticks, start, end, anchor[-1:])
    return up_ok and tick_ok, max(up_gap, tick_gap), inside


def _covered(obs: List[Observation], start: dt.datetime, end: dt.datetime,
             anchor: List[dt.datetime] = ()) -> Tuple[bool, float, int]:
    times = sorted({o.t for o in obs} | set(anchor))
    before = [t for t in times if t <= start]
    after = [t for t in times if t >= end]
    inside = [t for t in times if start < t < end]
    if not before or not after:
        return False, float("inf"), len(inside)
    span = [before[-1]] + inside + [after[0]]
    gaps = [(b - a).total_seconds() for a, b in zip(span, span[1:])]
    first, rest = gaps[0], gaps[1:]
    verified = first <= MAX_FIRST_GAP_SEC and all(g <= MAX_GAP_SEC for g in rest)
    return verified, max(gaps), len(inside)


# --- DOCSIS log --------------------------------------------------------------------------

def skew_near(root: Path, t: dt.datetime) -> Optional[int]:
    """Median (computer - modem) clock difference recorded within 10 minutes."""
    w = dt.timedelta(minutes=10)
    values = []
    for name in ("fast-poll.csv", "modem-signals.csv"):
        for r in read_rows(root, name, t - w, t + w):
            try:
                values.append(int(r["SkewSec"]))
            except (KeyError, ValueError):
                pass
    if not values:
        return None
    values.sort()
    return values[len(values) // 2]


@dataclass
class LogEntry:
    modem_time: str
    t: dt.datetime              # on the computer's clock
    event_type: str
    message: str
    cmts_mac: str
    kind: str
    label: str
    cm_mac: str = ""


def log_entries(root: Path, t0: dt.datetime, t1: dt.datetime, skew: int) -> List[LogEntry]:
    """Entries whose corrected time falls in [t0, t1]. Entries are copied out
    of the modem's ring up to days late, so RecordedAt is not the event time."""
    out = []
    seen = set()
    rows = read_rows(root, "docsis-log.csv", t0 - dt.timedelta(days=1), t1 + dt.timedelta(days=5), "RecordedAt")
    for r in rows:
        try:
            mt = dt.datetime.strptime(r["LogTime"], MODEM_LOG_FORMAT)
        except ValueError:
            continue
        t = mt + dt.timedelta(seconds=skew)
        key = (r["LogTime"], r["EventType"], r["Message"])
        if t0 <= t <= t1 and key not in seen:
            seen.add(key)
            out.append(LogEntry(r["LogTime"], t, r["EventType"], r["Message"], r.get("CmtsMac", ""),
                                evidence.evidence_kind(r["EventType"], r["Message"]),
                                evidence.evidence_label(r["EventType"], r["Message"]), r.get("CmMac") or ""))
    return sorted(out, key=lambda e: e.t)


# --- classification ------------------------------------------------------------------------

CLASS_HEADER = ["StartTime", "EndTime", "DurationSeconds", "Grade", "Classification", "Class", "ClassReason",
                "LocalStall", "StrongControlPlane", "ChannelTransition", "WeakControlPlane",
                "ChannelStateVerified", "ChannelObservations", "MaxStateGapSec", "SkewSec",
                "FirstFailingLayer", "TimelineFile"]


@dataclass
class Classified:
    event: Dict[str, str]
    cls: str
    reasons: List[str]
    flags: Dict[str, bool]
    strong: List[LogEntry]
    weak: List[LogEntry]
    other_log: List[LogEntry]
    changes: List[Tuple[Observation, Observation]]
    verified: bool
    max_gap: float
    inside: int
    skew: Optional[int]
    samples: List[Dict[str, str]]
    observations: List[Observation]
    timeline_path: Optional[Path] = None


def _local_stall(samples: List[Dict[str, str]], start: dt.datetime, end: dt.datetime) -> List[str]:
    found = []
    for s in samples:
        if not start <= s["_t"] < end:
            continue
        at = s["_t"].strftime("%H:%M:%S.%f")[:-3]
        for layer, label in (("LanGateway", "家用路由器"), ("Modem", "數據機")):
            if s.get(f"{layer}OK") == "0":
                found.append(f"{at} {label}探測失敗")
            else:
                try:
                    rtt = float(s.get(f"{layer}RTT") or "")
                except ValueError:
                    continue
                if rtt > LOCAL_STALL_MS:
                    found.append(f"{at} {label}探測 {rtt:.0f} ms（超過 {LOCAL_STALL_MS} ms）")
    return found


def decide(flags: Dict[str, bool]) -> str:
    if flags["LocalStall"]:
        return "L"
    if flags["StrongControlPlane"] or flags["ChannelTransition"]:
        return "A"
    if flags["WeakControlPlane"]:
        return "CP-weak"
    return "B-verified" if flags["ChannelStateVerified"] else "B-unverified"


def classify(root: Path, event: Dict[str, str]) -> Classified:
    start, end = event["_start"], event["_end"]
    w0, w1 = start - dt.timedelta(seconds=WINDOW_SEC), end + dt.timedelta(seconds=WINDOW_SEC)
    t0, t1 = start - dt.timedelta(seconds=TIMELINE_SEC), end + dt.timedelta(seconds=TIMELINE_SEC)

    samples = incident_samples(root, event)
    obs = modem_observations(root, t0, t1)
    skew = skew_near(root, start)
    entries = log_entries(root, w0, w1, skew or 0)
    strong = [e for e in entries if e.kind == "strong"]
    weak = [e for e in entries if e.kind == "weak"]
    other = [e for e in entries if e.kind == "none"]
    changes = [(a, b) for a, b in transitions(obs) if w0 <= b.t <= w1]
    verified, max_gap, inside = coverage(obs, start, end)
    stall = _local_stall(samples, start, end)

    flags = {
        "LocalStall": bool(stall),
        "StrongControlPlane": bool(strong),
        "ChannelTransition": bool(changes),
        "WeakControlPlane": bool(weak),
        # Verified absence needs no entry of any strength and no transition.
        "ChannelStateVerified": verified and not strong and not weak and not changes,
    }
    reasons = []
    reasons += stall[:3] + ([f"另有 {len(stall) - 3} 次本地探測異常"] if len(stall) > 3 else [])
    reasons += [f"{e.label}（數據機時間 {e.modem_time}）" for e in strong]
    reasons += [f"頻道狀態 {a.text} → {b.text}（{a.t:%H:%M:%S} 到 {b.t:%H:%M:%S} 之間）"
                for a, b in changes]
    reasons += [f"{e.label}（數據機時間 {e.modem_time}，較弱的關聯）" for e in weak]
    if not (strong or weak or changes):
        reasons.append("前後 60 秒內沒有 DOCSIS 日誌，也沒有頻道狀態變化")
        reasons.append(f"斷線期間觀察頻道狀態 {inside} 次"
                       + (f"，最大間隔 {max_gap:.1f} 秒" if max_gap != float("inf") else "，斷線前後沒有觀察可對照"))
    if skew is None:
        reasons.append("附近沒有時差紀錄，數據機時間未換算")
    c = Classified(event, decide(flags), reasons, flags, strong, weak, other, changes, verified,
                   max_gap, inside, skew, samples, obs)
    return c


def class_row(c: Classified) -> List[str]:
    e = c.event
    yn = lambda v: "yes" if v else "no"
    return [e["StartTime"], e["EndTime"], e["DurationSeconds"], e["Grade"], e["Classification"], c.cls,
            "；".join(c.reasons), yn(c.flags["LocalStall"]), yn(c.flags["StrongControlPlane"]),
            yn(c.flags["ChannelTransition"]), yn(c.flags["WeakControlPlane"]), yn(c.flags["ChannelStateVerified"]),
            str(c.inside), "" if c.max_gap == float("inf") else f"{c.max_gap:.1f}",
            "" if c.skew is None else str(c.skew), e.get("FirstFailingLayer", ""),
            str(c.timeline_path.name) if c.timeline_path else ""]


# --- timeline ---------------------------------------------------------------------------------

def timeline_text(root: Path, c: Classified) -> str:
    start, end = c.event["_start"], c.event["_end"]
    t0, t1 = start - dt.timedelta(seconds=TIMELINE_SEC), end + dt.timedelta(seconds=TIMELINE_SEC)
    lines: List[Tuple[dt.datetime, int, str, str]] = []

    def add(t, order, source, text):
        if t0 <= t <= t1:
            lines.append((t, order, source, text))

    add(start, 0, "EVENT", f"斷線開始（{c.event['Classification']}，最先失敗 "
                           f"{c.event.get('FirstFailingLayer', '')}）")
    add(end, 0, "EVENT", f"恢復，歷時 {c.event['DurationSeconds']} 秒（等級 {c.event['Grade']}）")
    targets = None
    for s in c.samples:
        if targets is None:
            targets = [k[:-2] for k in s if k.endswith("OK") and not k.startswith("_")]
        parts = []
        for name in targets:
            okv = s.get(f"{name}OK", "")
            if okv == "":
                continue
            parts.append(f"{name} {s.get(f'{name}RTT') or '-'}" if okv == "1" else f"{name} FAIL")
        add(s["_t"], 2, "PROBE", " ".join(parts) + (f"  [{s['Classification']}]" if s["Classification"] != "OK" else ""))
    # Per source: a change is always shown (marked *), otherwise every 10 s.
    last: Dict[str, Tuple[str, dt.datetime]] = {}
    for o in c.observations:
        prev = last.get(o.source)
        changed = prev is not None and o.text != prev[0]
        if prev is None or changed or (o.t - prev[1]).total_seconds() >= 10:
            add(o.t, 1, "MODEM", ("* " if changed else "") + o.text)
            last[o.source] = (o.text, o.t)
    skew = c.skew or 0
    for e in log_entries(root, t0, t1, skew):
        add(e.t, 1, "LOG", f"{e.label} [{e.kind}] 數據機時間 {e.modem_time}：{e.message}")
    for rec in read_jsonl(root, "events-journal.jsonl", t0.date(), t1.date()):
        if rec.get("type") == "modem_status":
            t = parse_ts(rec.get("time", ""))
            if t:
                add(t, 1, "MODEM", f"數據機讀取狀態 {rec.get('previous')} → {rec.get('status')}")

    head = [
        f"事件 {fmt(start)} ~ {fmt(end)}（{c.event['DurationSeconds']} 秒），等級 {c.event['Grade']}，"
        f"{c.event['Classification']}",
        f"分類 {c.cls}：" + "；".join(c.reasons),
        "旗標：" + "，".join(f"{k}={'yes' if v else 'no'}" for k, v in c.flags.items()),
        f"時差（電腦 - 數據機）：{c.skew if c.skew is not None else '無紀錄，以 0 計'} 秒",
        "第一欄是相對斷線開始的秒數。分類只描述觀察，不代表原因。",
        "",
    ]
    body = []
    for t, _, source, text in sorted(lines, key=lambda x: (x[0], x[1])):
        off = (t - start).total_seconds()
        body.append(f"{off:+8.1f}  {t.strftime('%H:%M:%S.%f')[:-3]}  {source:<5}  {text}")
    return "\n".join(head + body) + "\n"


def classify_and_save(root: Path, event: Dict[str, str]) -> Classified:
    """Classifies one event, writes its timeline, and replaces its row in
    that day's event-classes.csv."""
    c = classify(root, event)
    day = root / event["_start"].date().isoformat()
    tl_dir = day / "timelines"
    tl_dir.mkdir(parents=True, exist_ok=True)
    c.timeline_path = tl_dir / f"timeline-{event['_start']:%Y%m%d-%H%M%S}.txt"
    c.timeline_path.write_text(timeline_text(root, c), encoding="utf-8")
    path = day / "event-classes.csv"
    rows = []
    if path.exists():
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if header == CLASS_HEADER:
                rows = [r for r in reader if r and r[0] != event["StartTime"]]
    rows.append(class_row(c))
    rows.sort(key=lambda r: r[0])
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CLASS_HEADER)
        w.writerows(rows)
    return c


def load_classes(root: Path) -> Dict[str, Dict[str, str]]:
    out = {}
    if not root.exists():
        return out
    for d in root.iterdir():
        p = d / "event-classes.csv"
        if d.is_dir() and p.exists():
            with p.open(encoding="utf-8", newline="") as fh:
                for r in csv.DictReader(fh):
                    out[r["StartTime"]] = r
    return out
