"""
Daigestr — Email Metadata Extractor

Enthält Funktionen für E-Mail-Metadaten-Extraktion:
- extract_email_metadata: EML-Metadaten (Routing, Threading, Calendar Events)
"""

import email as _email_stdlib
import email.policy as _email_policy
from typing import Any, Optional

import structlog

try:
    from icalendar import Calendar as ICalendar
    ICALENDAR_AVAILABLE = True
except ImportError:
    ICALENDAR_AVAILABLE = False

log = structlog.get_logger()


def extract_email_metadata(file_data: bytes) -> Optional[dict[str, Any]]:
    """
    Extrahiert E-Mail-Metadaten aus rohen EML-Bytes.

    Prüft ob die Daten eine E-Mail sind (typische Header-Zeilen) und parst
    mit der Python-Stdlib email.message_from_bytes().

    Extrahiert:
    - Routing: Received-Chain, SPF/DKIM/DMARC aus Authentication-Results,
               X-Originating-IP, X-Mailer/User-Agent
    - Threading: Message-ID, In-Reply-To, References, Thread-Index
    - Calendar Events: ICS-Anhänge (text/calendar Parts), parsed via icalendar

    Args:
        file_data: Rohe Datei-Bytes (muss eine gültige E-Mail sein)

    Returns:
        Dict mit 'routing', 'thread', 'calendar_events' oder None wenn
        die Datei keine E-Mail ist.
    """
    if not file_data:
        return None

    # Schnelle Heuristik: E-Mails beginnen typischerweise mit bekannten Headern
    try:
        head = file_data[:2048].decode("utf-8", errors="replace")
    except Exception:
        return None

    first_line = head.split("\n")[0].strip()
    is_email = (
        first_line.startswith("From:")
        or first_line.startswith("Received:")
        or first_line.startswith("Return-Path:")
        or first_line.startswith("MIME-Version:")
        or first_line.startswith("Date:")
        or first_line.startswith("Message-ID:")
    )
    if not is_email:
        return None

    try:
        msg = _email_stdlib.message_from_bytes(file_data, policy=_email_policy.default)
    except Exception as e:
        log.warning("email_parse_failed", error=str(e))
        return None

    # --- Routing ---
    auth_results = str(msg.get("Authentication-Results", ""))
    spf = None
    dkim = None
    dmarc = None
    if "spf=pass" in auth_results:
        spf = "pass"
    elif "spf=fail" in auth_results:
        spf = "fail"
    elif "spf=softfail" in auth_results:
        spf = "softfail"
    elif "spf=neutral" in auth_results:
        spf = "neutral"

    if "dkim=pass" in auth_results:
        dkim = "pass"
    elif "dkim=fail" in auth_results:
        dkim = "fail"
    elif "dkim=none" in auth_results:
        dkim = "none"

    if "dmarc=pass" in auth_results:
        dmarc = "pass"
    elif "dmarc=fail" in auth_results:
        dmarc = "fail"
    elif "dmarc=none" in auth_results:
        dmarc = "none"

    routing = {
        "received_chain": msg.get_all("Received") or [],
        "spf": spf,
        "dkim": dkim,
        "dmarc": dmarc,
        "originating_ip": msg.get("X-Originating-IP"),
        "mailer": msg.get("X-Mailer") or msg.get("User-Agent"),
    }

    # --- Threading ---
    references_raw = str(msg.get("References") or "")
    thread = {
        "message_id": msg.get("Message-ID"),
        "in_reply_to": msg.get("In-Reply-To"),
        "references": references_raw.split() if references_raw.strip() else [],
        "thread_index": msg.get("Thread-Index"),
    }

    # --- Calendar Events (ICS) ---
    calendar_events: list[dict[str, Any]] = []
    if ICALENDAR_AVAILABLE:
        try:
            for part in msg.walk():
                if part.get_content_type() == "text/calendar":
                    try:
                        ics_bytes = part.get_payload(decode=True)
                        if not ics_bytes:
                            continue
                        cal = ICalendar.from_ical(ics_bytes)
                        for component in cal.walk("VEVENT"):
                            dtstart = component.get("dtstart")
                            dtend = component.get("dtend")
                            calendar_events.append({
                                "uid": str(component.get("uid", "")),
                                "summary": str(component.get("summary", "")),
                                "start": str(dtstart.dt) if dtstart else None,
                                "end": str(dtend.dt) if dtend else None,
                                "location": str(component.get("location", "")),
                                "organizer": str(component.get("organizer", "")),
                                "status": str(component.get("status", "")),
                            })
                    except Exception as ics_err:
                        log.debug("ics_parse_failed", error=str(ics_err))
        except Exception as walk_err:
            log.debug("email_walk_failed", error=str(walk_err))

    return {
        "routing": routing,
        "thread": thread,
        "calendar_events": calendar_events,
    }
