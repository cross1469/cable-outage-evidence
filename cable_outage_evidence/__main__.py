"""Command line: python -m cable_outage_evidence <command>."""

import argparse
import logging
import signal
import sys
from pathlib import Path

from . import __version__, analysis, config, hitron, monitor, platform_net, report, storage, ucid


def cmd_run(cfg, args):
    m = monitor.Monitor(cfg)
    signal.signal(signal.SIGINT, lambda *_: m.stop())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: m.stop())
    m.run()


def cmd_once(cfg, args):
    m = monitor.Monitor(cfg)
    m.path.discover()
    s = m.take_sample()
    from . import classify
    s.classification = classify.classify_sample(s)
    width = max(len(n) for n in ["LanGateway", "Modem", "FirstHop"] + m.names)
    for label, p in [("LanGateway", s.lan_gateway), ("Modem", s.modem), ("FirstHop", s.first_hop)] + list(s.public.items()):
        state = "not probed" if not p.probed else (f"{p.rtt:.1f} ms" if p.ok else "FAILED")
        print(f"{label:<{width}}  {p.address or '-':<16}  {state}")
    print(f"classification: {s.classification}  (ping via {m.pinger.method})")


def cmd_discover_path(cfg, args):
    pinger = platform_net.Pinger()
    gw = platform_net.default_gateway()
    print(f"default gateway: {gw or 'none'}")
    hops = platform_net.traceroute(cfg.path_discovery_target)
    for i, h in enumerate(hops, 1):
        print(f"  hop {i}: {h or '*'}")
    print(f"ISP first hop: {platform_net.first_isp_hop(hops, [gw, cfg.modem_address]) or 'unknown'}")


def cmd_init(cfg, args):
    import getpass
    path = Path(args.config) if args.config else config.default_config_path()
    if path.exists() and not args.force:
        print(f"{path} already exists (use --force to overwrite)")
        return 1
    if sys.stdin.isatty():
        url = input(f"modem address [{cfg.modem_url}] (none to skip modem reading): ").strip()
        if url.lower() == "none":
            cfg.modem_type = "none"
        else:
            cfg.modem_url = url or cfg.modem_url
            cfg.modem_username = input(f"modem username [{cfg.modem_username}]: ").strip() or cfg.modem_username
            cfg.modem_password = getpass.getpass("modem password (not shown; Enter keeps the factory default): ") or cfg.modem_password
    print(f"wrote {config.save(cfg, path)}")


def cmd_modem_check(cfg, args):
    """Logs in once and prints what the modem reports; never prints credentials."""
    if cfg.modem_type == "none":
        print("modem reading is off (modem_type = none)")
        return 1
    client = hitron.HitronClient(cfg.modem_url, cfg.modem_username, cfg.modem_password)
    try:
        client.login()
        data = {ep: client.get(ep)[0] for ep in ("SysInfo", "UsInfo", "UsOfdm", "DsInfo", "DsOfdm")}
        c = hitron.counts(data["UsInfo"], data["UsOfdm"], data["DsInfo"], data["DsOfdm"])
        print(f"network access: {hitron.clean(data['SysInfo'].get('ntAccess'))}")
        print(f"upstream SC-QAM {c['UsScQamOperate']}/{c['UsScQamCount']} OPERATE, "
              f"OFDMA {c['UsOfdmaOperate']}/{c['UsOfdmaCount']} OPERATE")
        print(f"downstream SC-QAM {c['DsScQamLocked']}/{c['DsScQamCount']} locked, "
              f"OFDM {c['DsOfdmLocked']}/{c['DsOfdmCount']} locked")
        entries = hitron.log_entries(client.get("Log")[0])
        print(f"event log: {len(entries)} entries")
    except hitron.ModemError as e:
        from .modem_worker import status_message, UNREACHABLE, AUTH_FAILED, BAD_RESPONSE
        kind = (AUTH_FAILED if isinstance(e, hitron.ModemAuthError) else
                UNREACHABLE if isinstance(e, hitron.ModemUnreachable) else BAD_RESPONSE)
        print(status_message(kind, str(e), cfg, monitoring=False))
        return 1
    finally:
        client.logout()


def _emit(text, args):
    if getattr(args, "out", None):
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)


def cmd_report(cfg, args):
    _emit(report.summary(cfg.data_path, args.days, not args.no_redact), args)


def cmd_evidence(cfg, args):
    _emit(report.evidence(cfg.data_path, args.time, not args.no_redact), args)


def cmd_timeline(cfg, args):
    _emit(report.timeline(cfg.data_path, args.time, not args.no_redact), args)


def cmd_docsis_log(cfg, args):
    _emit(report.docsis_log(cfg.data_path, args.days, not args.no_redact), args)


def cmd_ucid_report(cfg, args):
    _emit(report.Redactor(not args.no_redact)(ucid.report(cfg.data_path, args.ucid, args.days)), args)


def cmd_classify(cfg, args):
    events = analysis.all_events(cfg.data_path)
    if args.time:
        events = [e for e in events if e["StartTime"].startswith(args.time)]
    for e in events:
        c = analysis.classify_and_save(cfg.data_path, e)
        print(f"{e['StartTime'][:19]}  {e['Grade']}  {c.cls:<12}  {'; '.join(c.reasons)[:120]}")
    if not events:
        print("no matching event")


def cmd_paths(cfg, args):
    print(f"config: {Path(args.config) if args.config else config.default_config_path()}")
    print(f"data:   {cfg.data_path}")


COMMANDS = {
    "run": (cmd_run, "monitor until stopped (Ctrl+C)"),
    "once": (cmd_once, "take one sample and print it"),
    "discover-path": (cmd_discover_path, "show the default gateway and ISP first hop"),
    "modem-check": (cmd_modem_check, "log in to the modem once and show its channel state"),
    "report": (cmd_report, "summary report for the ISP"),
    "evidence": (cmd_evidence, "full evidence for one event (latest, or TIME)"),
    "timeline": (cmd_timeline, "timeline of one event (latest, or TIME)"),
    "docsis-log": (cmd_docsis_log, "the collected DOCSIS event log"),
    "ucid-report": (cmd_ucid_report, "one upstream channel's T4 / CM-STATUS / channel history"),
    "classify": (cmd_classify, "classify events again (all, or those starting with TIME)"),
    "init": (cmd_init, "write a config file with every default"),
    "paths": (cmd_paths, "print the config and data locations"),
}


def main(argv=None):
    p = argparse.ArgumentParser(prog="cable-outage-evidence",
                                description="Records cable outages with evidence for ISP reports.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", help="config file (default: per-user location)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    for name, (_, text) in COMMANDS.items():
        sp = sub.add_parser(name, help=text)
        if name == "init":
            sp.add_argument("--force", action="store_true")
        if name in ("evidence", "timeline", "classify"):
            sp.add_argument("time", nargs="?", default="", help="start time prefix, e.g. '2026-09-03 23:26'")
        if name == "ucid-report":
            sp.add_argument("ucid", help="upstream channel ID as the CM-STATUS entries name it, e.g. 7")
        if name in ("report", "docsis-log", "ucid-report"):
            sp.add_argument("--days", type=int, default={"report": 30, "docsis-log": 7}.get(name, 14))
        if name in ("report", "evidence", "timeline", "docsis-log", "ucid-report"):
            sp.add_argument("--no-redact", action="store_true", help="do not mask home addresses and MACs")
            sp.add_argument("--out", help="write to this file (UTF-8) instead of the console")
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        # Reports are Chinese; a legacy console code page must not crash them.
        sys.stdout.reconfigure(errors="replace")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = config.load(Path(args.config) if args.config else None)
    return COMMANDS[args.command][0](cfg, args) or 0


if __name__ == "__main__":
    sys.exit(main())
