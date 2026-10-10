"""Platform-specific network facts: the default gateway, ping and traceroute.

Every parser works on numbers and ASCII markers only, because Windows prints
ping and tracert output in the user's language (for example 時間=4ms).
"""

import os
import re
import socket
import struct
import subprocess
import sys
import time
from typing import List, Optional, Tuple

IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
RTT = re.compile(r"[=<]\s*(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(args: List[str], timeout: float) -> Tuple[int, str]:
    """Runs a command and returns (exit code, output); output is decoded
    leniently since only its ASCII parts are parsed."""
    try:
        proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


# --- default gateway -------------------------------------------------------

def parse_proc_net_route(text: str) -> Optional[str]:
    best = None
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 8 or cols[1] != "00000000" or cols[7] != "00000000":
            continue
        flags, metric = int(cols[3], 16), int(cols[6])
        if not flags & 0x2:  # RTF_GATEWAY
            continue
        gw = socket.inet_ntoa(struct.pack("<I", int(cols[2], 16)))
        if best is None or metric < best[0]:
            best = (metric, gw)
    return best[1] if best else None


def parse_route_print(text: str) -> Optional[str]:
    """Windows `route print -4 0.0.0.0`: destination, mask, gateway, interface,
    metric. The lowest metric wins."""
    best = None
    for line in text.splitlines():
        cols = line.split()
        if len(cols) >= 5 and cols[0] == "0.0.0.0" and cols[1] == "0.0.0.0" and IPV4.fullmatch(cols[2]):
            try:
                metric = int(cols[4])
            except ValueError:
                continue
            if best is None or metric < best[0]:
                best = (metric, cols[2])
    return best[1] if best else None


def parse_route_get(text: str) -> Optional[str]:
    """macOS `route -n get default`."""
    m = re.search(r"gateway:\s*(\S+)", text)
    return m.group(1) if m and IPV4.fullmatch(m.group(1)) else None


def default_gateway() -> Optional[str]:
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/net/route") as fh:
                return parse_proc_net_route(fh.read())
        except OSError:
            return None
    if sys.platform == "win32":
        return parse_route_print(run(["route", "print", "-4", "0.0.0.0"], 5)[1])
    if sys.platform == "darwin":
        return parse_route_get(run(["route", "-n", "get", "default"], 5)[1])
    return None


# --- ping --------------------------------------------------------------------

def parse_ping(text: str) -> Optional[float]:
    """RTT in ms of the first reply, or None. Only a line carrying a TTL is a
    reply: Windows also exits 0 for "Destination host unreachable"."""
    for line in text.splitlines():
        if "ttl=" not in line.lower():
            continue
        m = RTT.search(line)
        if m:
            # Windows prints time<1ms; count it as half a millisecond.
            return 0.5 if line[m.start()] == "<" else float(m.group(1))
    return None


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


class Pinger:
    """ICMP echo. Uses an unprivileged ICMP datagram socket where the system
    allows one (macOS; Linux when net.ipv4.ping_group_range includes the user),
    otherwise the system ping command."""

    def __init__(self):
        self._seq = 0
        self.method = "socket" if self._socket_works() else "command"

    @staticmethod
    def _socket_works() -> bool:
        if sys.platform not in ("linux", "darwin"):
            return False
        try:
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP).close()
            return True
        except OSError:
            return False

    def ping(self, address: str, timeout: float) -> Optional[float]:
        if self.method == "socket":
            return self._ping_socket(address, timeout)
        return self._ping_command(address, timeout)

    def _ping_socket(self, address: str, timeout: float) -> Optional[float]:
        self._seq = (self._seq + 1) & 0xFFFF
        seq = self._seq
        payload = struct.pack("!d", time.monotonic()) + b"cable-outage-evidence"
        header = struct.pack("!BBHHH", 8, 0, 0, os.getpid() & 0xFFFF, seq)
        packet = struct.pack("!BBHHH", 8, 0, _checksum(header + payload), os.getpid() & 0xFFFF, seq) + payload
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP) as s:
                start = time.monotonic()
                s.settimeout(timeout)
                s.sendto(packet, (address, 0))
                while True:
                    remaining = timeout - (time.monotonic() - start)
                    if remaining <= 0:
                        return None
                    s.settimeout(remaining)
                    data, _ = s.recvfrom(2048)
                    # macOS hands back the IP header too; Linux does not.
                    if data and data[0] >> 4 == 4:
                        data = data[(data[0] & 0x0F) * 4:]
                    if len(data) >= 8 and data[0] == 0 and struct.unpack("!H", data[6:8])[0] == seq:
                        return (time.monotonic() - start) * 1000
        except (OSError, socket.timeout):
            return None

    def _ping_command(self, address: str, timeout: float) -> Optional[float]:
        if sys.platform == "win32":
            args = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), address]
        elif sys.platform == "darwin":
            args = ["ping", "-n", "-c", "1", "-W", str(int(timeout * 1000)), address]
        else:
            args = ["ping", "-n", "-c", "1", "-W", str(max(1, int(round(timeout)))), address]
        code, out = run(args, timeout + 2)
        if code != 0:
            return None
        return parse_ping(out)


def ping_with_retries(pinger: Pinger, address: str, retries: int, timeout: float) -> Optional[float]:
    """Fails only when every attempt goes unanswered (spec §3.2)."""
    for _ in range(retries):
        rtt = pinger.ping(address, timeout)
        if rtt is not None:
            return rtt
    return None


def tcp_connect(address: str, port: int = 443, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


# --- traceroute ------------------------------------------------------------------

def parse_traceroute(text: str) -> List[Optional[str]]:
    """Hop addresses in order (None for a hop with no answer). Works for
    Windows tracert and BSD/Linux traceroute -n output."""
    hops = {}
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)\s+(.*)", line)
        if not m:
            continue
        rest = m.group(2)
        # The header line "traceroute to 1.1.1.1 (1.1.1.1), 3 hops max" does
        # not start with a hop number, so it is skipped by the match above.
        addresses = IPV4.findall(rest)
        hops[int(m.group(1))] = addresses[0] if addresses else None
    return [hops.get(i) for i in range(1, max(hops) + 1)] if hops else []


def traceroute(target: str, max_hops: int = 4, wait_sec: float = 1.0) -> List[Optional[str]]:
    if sys.platform == "win32":
        args = ["tracert", "-d", "-h", str(max_hops), "-w", str(int(wait_sec * 1000)), target]
    else:
        args = ["traceroute", "-n", "-m", str(max_hops), "-q", "1", "-w", str(max(1, int(wait_sec))), target]
    _, out = run(args, max_hops * (wait_sec * 3 + 1) + 5)
    return parse_traceroute(out)


def first_isp_hop(hops: List[Optional[str]], skip: List[str]) -> Optional[str]:
    """First answering hop that is not the home router or the modem (spec §3.1).
    Private addresses are not skipped: some ISPs number the CMTS in 10.0.0.0/8."""
    for hop in hops:
        if hop and hop not in skip:
            return hop
    return None
