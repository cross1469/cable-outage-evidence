"""Command line: python -m cable_outage_evidence <command>."""

import argparse
import logging
import signal
import sys
from pathlib import Path

from . import __version__, config, monitor, platform_net, storage


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
    path = Path(args.config) if args.config else config.default_config_path()
    if path.exists() and not args.force:
        print(f"{path} already exists (use --force to overwrite)")
        return 1
    print(f"wrote {config.save(cfg, path)}")


def cmd_paths(cfg, args):
    print(f"config: {Path(args.config) if args.config else config.default_config_path()}")
    print(f"data:   {cfg.data_path}")


COMMANDS = {
    "run": (cmd_run, "monitor until stopped (Ctrl+C)"),
    "once": (cmd_once, "take one sample and print it"),
    "discover-path": (cmd_discover_path, "show the default gateway and ISP first hop"),
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
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = config.load(Path(args.config) if args.config else None)
    return COMMANDS[args.command][0](cfg, args) or 0


if __name__ == "__main__":
    sys.exit(main())
