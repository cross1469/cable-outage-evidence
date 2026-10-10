"""Evidence strength of a modem DOCSIS log entry (spec §7.1).

Shared by classification and every report so the rule cannot drift between
them. Strong: T4, CM-STATUS Event Type Code 6. Weak: T3 and any other
CM-STATUS. Everything else (profile changes, DHCP notices) is not evidence.
"""

import re

T3 = "82000200"
T4 = "82000400"
CM_STATUS = "74010100"
PROFILE_CHANGE = ("67061600", "67061601")


def evidence_kind(event_type: str, message: str) -> str:
    if event_type == T4:
        return "strong"
    if event_type == CM_STATUS and "Event Type Code: 6;" in message:
        return "strong"
    if event_type in (CM_STATUS, T3):
        return "weak"
    return "none"


def _field(message: str, name: str) -> str:
    m = re.search(re.escape(name) + r":\s*([^;]*)", message)
    return m.group(1).strip() if m else ""


def evidence_label(event_type: str, message: str) -> str:
    if event_type == T4:
        return "T4"
    if event_type == T3:
        return "T3"
    if event_type == CM_STATUS:
        return f"CM-STATUS type {_field(message, 'Event Type Code')} Chan {_field(message, 'Chan ID')}"
    if event_type in PROFILE_CHANGE:
        return "profile change"
    return f"type {event_type}"


def cm_status_channel(message: str) -> str:
    return _field(message, "Chan ID")
