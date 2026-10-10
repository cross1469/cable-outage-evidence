"""ucid-report (spec §10): one upstream channel's control-plane history.

For every CM-STATUS Event Type 6 naming the UCID, it chains the T4 before it,
the channel leaving and regaining OPERATE, and any outage nearby. Channel
changes are intervals between the last observation in the old state and the
first in the new, so their width is the resolution actually available.
"""

import datetime as dt
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import analysis, evidence

BEFORE_SEC = 60     # T4 searched this far before the CM-STATUS
SEARCH_SEC = 120    # channel and outage searched this far after the anchor


@dataclass
class Snapshot:
    t: dt.datetime
    # key -> state. "sc:<ChannelId>", "of:<index>", or from the 30 s aggregate
    # counts "agg:of" (every OFDMA channel operating, or not).
    channels: Dict[str, str]


def snapshots(root: Path, t0: dt.datetime, t1: dt.datetime) -> List[Snapshot]:
    out = []
    for o in analysis.modem_observations(root, t0, t1):
        if o.source == "us_scqam":
            out.append(Snapshot(o.t, {f"sc:{ch[1]}": ch[2] for ch in o.state}))
        elif o.source == "us_ofdma":
            out.append(Snapshot(o.t, {f"of:{ch[0]}": ch[2] for ch in o.state}))
        elif o.source == "aggregate" and o.state[3] not in ("", "0"):
            out.append(Snapshot(o.t, {"agg:of": "OPERATE" if o.state[2] == o.state[3] else "DEGRADED"}))
    return sorted(out, key=lambda s: s.t)


def _interval(a: dt.datetime, b: dt.datetime, anchor: dt.datetime) -> str:
    width = (b - a).total_seconds()
    return f"({(a - anchor).total_seconds():+.0f}, {(b - anchor).total_seconds():+.0f}] s，解析度 {width:.0f} s"


def channel_change(snaps: List[Snapshot], keys: List[str], after: dt.datetime, until: dt.datetime,
                   want_operate: bool) -> Optional[Tuple[dt.datetime, dt.datetime, str]]:
    """First change after `after` where one of `keys` leaves (want_operate=False)
    or regains (True) OPERATE: (last old-state observation, first new-state
    observation, key). Snapshots of SC-QAM and OFDMA are separate series."""
    for prefix in ("sc:", "of:", "agg:"):
        series = [s for s in snaps if any(k.startswith(prefix) for k in s.channels)]
        for a, b in zip(series, series[1:]):
            if b.t <= after or a.t > until:
                continue
            for k in keys:
                if not k.startswith(prefix) or k not in a.channels:
                    continue
                was = a.channels[k] == "OPERATE"
                now = b.channels.get(k, "") == "OPERATE"
                if was != now and now == want_operate:
                    return a.t, b.t, k
    return None


def report(root: Path, ucid: str, days: int = 14) -> str:
    t1 = dt.datetime.now()
    t0 = t1 - dt.timedelta(days=days)
    entries = analysis.log_entries(root, t0, t1, 0)
    # Each entry is placed with the skew recorded near it.
    for e in entries:
        skew = analysis.skew_near(root, e.t)
        if skew:
            e.t = e.t + dt.timedelta(seconds=skew)
    t4s = [e for e in entries if e.event_type == evidence.T4]
    status = [e for e in entries if e.event_type == evidence.CM_STATUS
              and evidence.cm_status_channel(e.message) == ucid]
    type6 = [e for e in status if "Event Type Code: 6;" in e.message]
    others = [e for e in status if e not in type6]
    events = analysis.all_events(root, t0.date(), t1.date())
    snaps = snapshots(root, t0 - dt.timedelta(minutes=5), t1)
    sc_key = f"sc:{ucid}"
    has_sc = any(sc_key in s.channels for s in snaps)

    out = [f"上行頻道 UCID {ucid} 報告（{days} 天）", "=" * 40,
           f"列出每一筆指名 UCID {ucid} 的 CM-STATUS type 6，與它之前 {BEFORE_SEC} 秒內的 T4、頻道離開與恢復 OPERATE、"
           f"以及附近的斷線。秒數相對於 T4（沒有 T4 時相對於 CM-STATUS）。頻道變化以區間表示：最後一次舊狀態的觀察到"
           f"第一次新狀態的觀察之間。",
           "這個 UCID " + ("是一個 SC-QAM 上行頻道。" if has_sc else
                          "不在 SC-QAM 頻道裡，所以追蹤 OFDMA 頻道；數據機的 OFDMA 資料沒有 UCID，"
                          "下面的「掉的 OFDMA index」會累積 UCID 與 index 的對應。"), ""]
    lags_status, lags_outage, removal_widths = [], [], []
    used_t4 = set()
    keys_all = [sc_key] if has_sc else sorted({k for s in snaps for k in s.channels if k.startswith("of:")}) + ["agg:of"]
    for cs in type6:
        before = [t for t in t4s if cs.t - dt.timedelta(seconds=BEFORE_SEC) <= t.t <= cs.t]
        t4 = before[-1] if before else None
        if t4:
            used_t4.add(id(t4))
        anchor = t4.t if t4 else cs.t
        out.append(f"CM-STATUS type 6（數據機時間 {cs.modem_time}，電腦時間 {cs.t:%Y-%m-%d %H:%M:%S}）")
        if t4:
            lag = (cs.t - t4.t).total_seconds()
            lags_status.append(lag)
            out.append(f"  T4 → CM-STATUS：{lag:+.0f} s（T4 數據機時間 {t4.modem_time}）")
        else:
            out.append(f"  前 {BEFORE_SEC} 秒內沒有 T4")
        removal = channel_change(snaps, keys_all, anchor - dt.timedelta(seconds=BEFORE_SEC),
                                 anchor + dt.timedelta(seconds=SEARCH_SEC), want_operate=False)
        if removal:
            a, b, key = removal
            removal_widths.append((b - a).total_seconds())
            out.append(f"  頻道離開 OPERATE：{_interval(a, b, anchor)}"
                       + (f"；掉的 OFDMA index {key[3:]}" if key.startswith("of:") else "")
                       + ("（30 秒讀取的頻道數）" if key.startswith("agg:") else ""))
            restore = channel_change(snaps, [key], b - dt.timedelta(seconds=1), b + dt.timedelta(minutes=30),
                                     want_operate=True)
            out.append(f"  頻道恢復 OPERATE：{_interval(restore[0], restore[1], anchor)}" if restore
                       else "  30 分鐘內沒有觀察到恢復")
        else:
            out.append("  前後沒有觀察到頻道離開 OPERATE")
        near = [e for e in events
                if anchor - dt.timedelta(seconds=BEFORE_SEC) <= e["_start"] <= anchor + dt.timedelta(seconds=SEARCH_SEC)]
        for ev in near:
            lag = (ev["_start"] - anchor).total_seconds()
            lags_outage.append(lag)
            out.append(f"  斷線：開始 {lag:+.0f} s，歷時 {float(ev['DurationSeconds'] or 0):.1f} s，等級 {ev['Grade']}")
        if not near:
            out.append("  附近沒有斷線")
        out.append("")
    if not type6:
        out += ["（期間內沒有指名這個 UCID 的 CM-STATUS type 6）", ""]

    def stats(name, values):
        if values:
            out.append(f"  {name}：最小 {min(values):+.0f} s，中位 {statistics.median(values):+.0f} s，"
                       f"最大 {max(values):+.0f} s（{len(values)} 筆）")
    out.append("統計")
    stats("T4 → CM-STATUS", lags_status)
    stats("T4 → 斷線開始", lags_outage)
    fine = [w for w in removal_widths if w <= 5]
    out.append(f"  頻道離開的區間：{len(fine)} 筆解析度 ≤ 5 s，{len(removal_widths) - len(fine)} 筆只能當作範圍")
    out.append("")
    out.append(f"同一 UCID 的其他 CM-STATUS（{len(others)} 筆）")
    out += [f"  {e.modem_time}  {e.label}" for e in others] or ["  （無）"]
    lone_t4 = [t for t in t4s if id(t) not in used_t4]
    out.append(f"沒有接著 UCID {ucid} CM-STATUS type 6 的 T4（{len(lone_t4)} 筆）")
    out += [f"  {e.modem_time}" for e in lone_t4] or ["  （無）"]
    drops = []
    cursor = t0
    while True:
        ch = channel_change(snaps, keys_all, cursor, t1, want_operate=False)
        if not ch:
            break
        first_new = ch[1]
        if not any(first_new - dt.timedelta(seconds=BEFORE_SEC) <= t.t <= first_new for t in t4s):
            drops.append(ch)
        cursor = first_new
    out.append(f"前 {BEFORE_SEC} 秒內沒有 T4 的頻道掉線（{len(drops)} 筆）")
    out += [f"  {a:%Y-%m-%d %H:%M:%S} ~ {b:%H:%M:%S}  {key}" for a, b, key in drops] or ["  （無）"]
    return "\n".join(out) + "\n"
