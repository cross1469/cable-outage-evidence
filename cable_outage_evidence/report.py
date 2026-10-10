"""Reports for an ISP (spec §10): summary, one event's evidence, DOCSIS log.

Reports state observations. The only text about a possible cause is the fixed
background note at the end of `report`, kept apart from the event data.
Personal identifiers are masked by default (redact=True).
"""

import collections
import datetime as dt
import ipaddress
import re
from pathlib import Path
from typing import Dict, List, Optional

from . import analysis, storage

BACKGROUND = """\
背景說明
本報告只列出觀察到的事實，不判定原因。上述事件期間，若數據機持續在線、下行頻道持續鎖定、
數據機日誌沒有紀錄，網路卻整段中斷，這樣的現象符合上行雜訊干擾（例如 burst ingress）的
特徵：數據機看不到 CMTS 端收到的上行訊號品質，上行封包遺失也不會在數據機日誌留下紀錄。
請協助檢查本用戶所在節點的上行品質（例如 CMTS 端的上行 SNR / RxMER 與未更正錯誤），
以及事件時間點前後的紀錄。"""

LAYER_ZH = {"link": "網路介面", "lan_gateway": "家用路由器", "modem": "數據機",
            "isp_first_hop": "ISP 第一跳", "public": "公共目標", "none": "-"}

CLASS_ZH = {
    "L": "本地端卡住（家用路由器或數據機的探測也失敗）",
    "A": "有 T4 / CM-STATUS 6 或觀察到頻道狀態變化",
    "CP-weak": "只有較弱的控制面紀錄（T3 或其他 CM-STATUS）",
    "B-verified": "沒有日誌紀錄、頻道狀態不變（每秒觀察確認）",
    "B-unverified": "沒有日誌紀錄、未見頻道變化（觀察密度不足以確認）",
}


# Factory management addresses of cable modems: not personal, and needed to read
# the report.
MODEM_DEFAULTS = {"192.168.0.1", "192.168.100.1"}


class Redactor:
    """Masks the user's own identifiers. ISP-side identifiers (the first hop,
    the CMTS MAC, the modem's CM gateway) stay: the ISP needs them."""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str) -> str:
        if not self.enabled:
            return text

        def ip(m):
            try:
                a = ipaddress.ip_address(m.group(0))
            except ValueError:
                return m.group(0)
            if str(a) in MODEM_DEFAULTS:
                return m.group(0)
            if a.is_private and not str(a).startswith(("10.", "100.")):
                # Home networks; ISP access networks often use 10/8 or 100.64/10.
                return "<home-network>"
            return m.group(0)
        text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", ip, text)
        # Any MAC except one labelled as the CMTS's.
        return re.sub(r"(?<!CMTS )(?<!CMTS-MAC=)\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b",
                      lambda m: "<mac>", text)


def _coverage(root: Path, first: dt.date, last: dt.date) -> (int, int):
    """(minutes with samples, minutes in the period) from minute-summary.csv.
    Sleep, shutdown and the monitor not running all show up as missing minutes."""
    seen = 0
    for d in analysis.day_dirs(root, first, last):
        p = d / "minute-summary.csv"
        if p.exists():
            with p.open(encoding="utf-8") as fh:
                seen += max(0, sum(1 for _ in fh) - 1)
    now = dt.datetime.now()
    period_end = min(now, dt.datetime.combine(last, dt.time.max))
    total = int((period_end - dt.datetime.combine(first, dt.time.min)).total_seconds() // 60)
    return seen, max(total, 1)


def _modem_gaps(root: Path, first: dt.date, last: dt.date) -> List[str]:
    out = []
    for rec in analysis.read_jsonl(root, "events-journal.jsonl", first, last):
        if rec.get("type") == "modem_status" and rec.get("previous") != "unknown":
            out.append(f"  {rec.get('time', '')[:19]}  數據機讀取 {rec.get('previous')} -> {rec.get('status')}")
    return out


def _ensure_class(root: Path, event, classes) -> Dict[str, str]:
    c = classes.get(event["StartTime"])
    if c is None:
        cl = analysis.classify_and_save(root, event)
        c = dict(zip(analysis.CLASS_HEADER, analysis.class_row(cl)))
    return c


def summary(root: Path, days: int = 30, redact: bool = True) -> str:
    r = Redactor(redact)
    last = dt.date.today()
    first = last - dt.timedelta(days=days - 1)
    events = analysis.all_events(root, first, last)
    classes = analysis.load_classes(root)
    seen, total = _coverage(root, first, last)
    out = [f"Cable 斷線紀錄報告  {first} ~ {last}（{days} 天）", "=" * 40, ""]
    out.append(f"監控涵蓋：{seen} 分鐘 / {total} 分鐘（{100 * seen / total:.1f}%）。"
               "電腦睡眠、關機或程式未執行的時間沒有資料，不代表沒有斷線。")
    out.append("方法：每 0.5 秒同時探測家用路由器、數據機、ISP 第一跳與三個公共目標（1.1.1.1、8.8.8.8、"
               "101.101.101.101）；三個公共目標中至少兩個失敗才算斷線。斷線時另測 TCP 443，並每秒讀取數據機"
               "的上下行頻道狀態，定期複製數據機的 DOCSIS 日誌。")
    out.append("")
    grades = collections.Counter(e["Grade"] for e in events)
    out.append("事件數（依等級）：" + "，".join(f"{g} {grades.get(g, 0)}" for g in ("P1", "P2", "P3", "P4")))
    out.append("  P1 確認中斷（TCP 443 也失敗或網路介面中斷）；P2 可能中斷（數據機整段無回應）；"
               "P3 持續 ICMP 遺失但 TCP 443 仍可連；P4 未達持續門檻（約 2 秒）且 TCP 443 仍可連，只計數。")
    serious = [e for e in events if e["Grade"] in ("P1", "P2", "P3")]
    if serious:
        cls = collections.Counter(_ensure_class(root, e, classes)["Class"] for e in serious)
        out.append("P1–P3 依觀察分類：" + "，".join(f"{k} {v}" for k, v in sorted(cls.items())))
        hours = collections.Counter(e["_start"].hour for e in serious)
        out.append("P1–P3 時段分布：" + " ".join(f"{h:02d}時:{hours[h]}" for h in sorted(hours)))
        durations = sorted(float(e["DurationSeconds"] or 0) for e in serious)
        out.append(f"P1–P3 持續時間：最短 {durations[0]:.1f} s，中位 {durations[len(durations) // 2]:.1f} s，"
                   f"最長 {durations[-1]:.1f} s")
    gaps = _modem_gaps(root, first, last)
    if gaps:
        out += ["", "數據機讀取狀態變化（這些期間可能缺少數據機資料）："] + gaps
    out += ["", "P1–P3 事件明細", "-" * 40]
    if not serious:
        out.append("（無）")
    for e in serious:
        c = _ensure_class(root, e, classes)
        out.append(f"{e['StartTime'][:19]}  {e['DurationSeconds']} s  {e['Grade']}  "
                   f"最先失敗：{LAYER_ZH.get(e.get('FirstFailingLayer', ''), e.get('FirstFailingLayer', ''))}  "
                   f"分類 {c['Class']}（{CLASS_ZH.get(c['Class'], '')}）")
        out.append(f"    觀察：{c['ClassReason']}")
        cl = analysis.classify(root, e)
        for entry in cl.strong + cl.weak + cl.other_log:
            out.append(f"    日誌 {entry.modem_time}（數據機時間）{entry.event_type} {entry.message}"
                       + (f"  CMTS {entry.cmts_mac}" if entry.cmts_mac else ""))
    out += ["", f"P4（未達持續門檻）：{grades.get('P4', 0)} 次，未逐筆列出。", "", BACKGROUND, ""]
    return r("\n".join(out))


def evidence(root: Path, match: str = "", redact: bool = True) -> str:
    r = Redactor(redact)
    e = analysis.find_event(root, match)
    if e is None:
        return "找不到事件 / no matching event\n"
    c = analysis.classify_and_save(root, e)
    start, end = e["_start"], e["_end"]
    out = [f"事件證據 {e['StartTime'][:19]} ~ {e['EndTime'][:19]}（{e['DurationSeconds']} s）", "=" * 40]
    out.append(f"等級 {e['Grade']}，取樣分類 {e['Classification']}，最先失敗："
               f"{LAYER_ZH.get(e.get('FirstFailingLayer', ''), '')}")
    out.append(f"探測目標：家用路由器 {e.get('LanGatewayAddress') or '-'}，數據機 {e.get('ModemAddress') or '-'}，"
               f"ISP 第一跳 {e.get('FirstHopAddress') or '未知'}")
    out.append(f"觀察分類 {c.cls}（{CLASS_ZH.get(c.cls, '')}）")
    for reason in c.reasons:
        out.append(f"  - {reason}")
    failed = [k[:-6] for k, v in e.items() if k.endswith("Failed") and v == "1"]
    out.append("失敗過的層：" + ("，".join(failed) or "（無）"))
    tcp = {k[:-6]: v for k, v in e.items() if k.endswith("TCP443")}
    out.append("TCP 443：" + "，".join(f"{k} {'成功' if v == '1' else '失敗' if v == '0' else '未測'}"
                                      for k, v in tcp.items()))
    out += ["", "事件前 10 秒到恢復後 10 秒的取樣："]
    for s in c.samples:
        if start - dt.timedelta(seconds=10) <= s["_t"] <= end + dt.timedelta(seconds=10):
            cols = [f"{k[:-2]} {s[k[:-2] + 'RTT'] or '-'}" if s[k] == "1" else f"{k[:-2]} FAIL"
                    for k in s if k.endswith("OK") and s[k] in ("0", "1") and not k.startswith("_")]
            out.append(f"  {s['_t']:%H:%M:%S.%f}"[:-3] + "  " + " ".join(cols) + f"  [{s['Classification']}]")
    out += ["", "事件前後半小時的 DOCSIS 日誌（時間已換算為電腦時間）："]
    entries = analysis.log_entries(root, start - dt.timedelta(minutes=30), end + dt.timedelta(minutes=30), c.skew or 0)
    out += [f"  {x.t:%H:%M:%S}（數據機 {x.modem_time}）[{x.kind}] {x.event_type} {x.message}" for x in entries] or ["  （無）"]
    out += ["", "訊號值（事件前最後一次與事件後第一次的完整讀取）："]
    before = analysis.read_rows(root, "modem-signals.csv", start - dt.timedelta(minutes=10), start)
    after = analysis.read_rows(root, "modem-signals.csv", end, end + dt.timedelta(minutes=10))
    before = [x for x in before if x.get("ReadOK") == "1"]
    after = [x for x in after if x.get("ReadOK") == "1"]
    if before and after:
        b, a = before[-1], after[0]
        for k in ("DsPowerMin", "DsPowerMax", "DsSnrMin", "UsPowerMin", "UsPowerMax",
                  "UsOfdmaPower1_6Min", "UsOfdmaPower1_6Max", "DsLockedCount", "UsScQamOperateCount",
                  "UsOfdmaOperateCount"):
            out.append(f"  {k:<22} {b.get(k, ''):>8} -> {a.get(k, ''):>8}")
    else:
        out.append("  （事件前後沒有完整讀取）")
    out += ["", f"時間軸：{c.timeline_path}", ""]
    return r("\n".join(out))


def docsis_log(root: Path, days: int = 7, redact: bool = True) -> str:
    r = Redactor(redact)
    t1 = dt.datetime.now()
    t0 = t1 - dt.timedelta(days=days)
    entries = analysis.log_entries(root, t0, t1, 0)
    out = [f"DOCSIS 日誌（{days} 天，數據機時間）", "=" * 40]
    counts = collections.Counter(e.label if e.label != "profile change" else "profile change" for e in entries)
    out.append("依類型：" + "，".join(f"{k} {v}" for k, v in counts.most_common()) if counts else "（無）")
    out.append("")
    out += [f"{e.modem_time}  [{e.kind:<6}] {e.event_type}  {e.message}" for e in entries]
    return r("\n".join(out) + "\n")


def timeline(root: Path, match: str = "", redact: bool = True) -> str:
    e = analysis.find_event(root, match)
    if e is None:
        return "找不到事件 / no matching event\n"
    c = analysis.classify_and_save(root, e)
    return Redactor(redact)(c.timeline_path.read_text(encoding="utf-8"))
