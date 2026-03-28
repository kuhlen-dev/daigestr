"""
Unit-Tests für T-MKIT-029: Email Transport Headers, Threading und Calendar ICS.

Tests prüfen:
- Received-Header-Chain wird als Liste extrahiert
- SPF/DKIM/DMARC aus Authentication-Results geparst
- Message-ID, In-Reply-To, References (Threading)
- X-Mailer/User-Agent extrahiert
- Nicht-EML-Daten (z.B. PDF) → None
- meta.email_routing wird über convert_auto gesetzt
- ICS Calendar Events werden geparst (wenn icalendar verfügbar)
- Kein ICS → leere calendar_events Liste, kein Crash
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from conftest import load_server_module, run_async


# =============================================================================
# Server-Modul laden
# =============================================================================

_server = load_server_module(use_real_pil=False)

extract_email_metadata = _server.extract_email_metadata
convert_auto = _server.convert_auto
ICALENDAR_AVAILABLE = _server.ICALENDAR_AVAILABLE


# =============================================================================
# Test-Fixtures
# =============================================================================

SAMPLE_EML = b"""From: sender@example.com
To: recipient@example.com
Subject: Test Invoice
Message-ID: <abc123@example.com>
In-Reply-To: <def456@example.com>
References: <def456@example.com> <ghi789@example.com>
Received: from mail.example.com (1.2.3.4) by mx.test.com; Mon, 25 Mar 2026 10:00:00 +0000
Authentication-Results: mx.test.com; spf=pass; dkim=pass; dmarc=pass
X-Mailer: Thunderbird 115.0
Content-Type: text/plain

This is a test email.
"""

SAMPLE_EML_WITH_ICS = b"""From: organizer@example.com
To: attendee@example.com
Subject: Meeting Invite
Message-ID: <meeting123@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="boundary123"

--boundary123
Content-Type: text/plain

Please find the meeting invitation attached.

--boundary123
Content-Type: text/calendar; charset=utf-8

BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Test//EN
BEGIN:VEVENT
UID:event-uid-001@example.com
SUMMARY:Team Meeting
DTSTART:20260325T100000Z
DTEND:20260325T110000Z
LOCATION:Conference Room A
ORGANIZER:mailto:organizer@example.com
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR

--boundary123--
"""

SAMPLE_EML_NO_AUTH = b"""From: simple@example.com
To: other@example.com
Subject: Simple Mail
Message-ID: <simple001@example.com>
Content-Type: text/plain

Just a simple message.
"""

PDF_BYTES = b"%PDF-1.4 fake pdf content"


# =============================================================================
# Tests: Received-Chain
# =============================================================================

class TestExtractReceivedChain:
    """test_extract_received_chain: Received-Header als Liste extrahiert."""

    def test_received_chain_is_list(self):
        """Received-Header wird als Liste zurückgegeben."""
        result = extract_email_metadata(SAMPLE_EML)
        assert result is not None
        assert isinstance(result["routing"]["received_chain"], list)

    def test_received_chain_not_empty(self):
        """Mindestens ein Received-Header wird gefunden."""
        result = extract_email_metadata(SAMPLE_EML)
        assert result is not None
        assert len(result["routing"]["received_chain"]) >= 1

    def test_received_chain_contains_sender_info(self):
        """Received-Header enthält Absender-Informationen."""
        result = extract_email_metadata(SAMPLE_EML)
        assert result is not None
        chain = result["routing"]["received_chain"]
        assert any("mail.example.com" in str(r) for r in chain)

    def test_received_chain_empty_when_no_received(self):
        """Kein Received-Header → leere Liste, kein Crash."""
        result = extract_email_metadata(SAMPLE_EML_NO_AUTH)
        assert result is not None
        assert isinstance(result["routing"]["received_chain"], list)


# =============================================================================
# Tests: SPF/DKIM/DMARC
# =============================================================================

class TestExtractSpfDkimDmarc:
    """test_extract_spf_dkim_dmarc: Authentication-Ergebnisse aus Authentication-Results."""

    def test_spf_pass_extracted(self):
        """SPF=pass wird korrekt extrahiert."""
        result = extract_email_metadata(SAMPLE_EML)
        assert result is not None
        assert result["routing"]["spf"] == "pass"

    def test_dkim_pass_extracted(self):
        """DKIM=pass wird korrekt extrahiert."""
        result = extract_email_metadata(SAMPLE_EML)
        assert result is not None
        assert result["routing"]["dkim"] == "pass"

    def test_dmarc_pass_extracted(self):
        """DMARC=pass wird korrekt extrahiert."""
        result = extract_email_metadata(SAMPLE_EML)
        assert result is not None
        assert result["routing"]["dmarc"] == "pass"

    def test_spf_fail_extracted(self):
        """SPF=fail wird korrekt extrahiert."""
        eml = b"From: x@example.com\nAuthentication-Results: mx.test.com; spf=fail\n\nBody\n"
        result = extract_email_metadata(eml)
        assert result is not None
        assert result["routing"]["spf"] == "fail"

    def test_spf_none_when_no_auth_results(self):
        """Kein Authentication-Results Header → spf/dkim/dmarc sind None."""
        result = extract_email_metadata(SAMPLE_EML_NO_AUTH)
        assert result is not None
        assert result["routing"]["spf"] is None
        assert result["routing"]["dkim"] is None
        assert result["routing"]["dmarc"] is None


# =============================================================================
# Tests: Threading
# =============================================================================

class TestExtractThreading:
    """test_extract_threading: Message-ID, In-Reply-To, References."""

    def test_message_id_extracted(self):
        """Message-ID wird extrahiert."""
        result = extract_email_metadata(SAMPLE_EML)
        assert result is not None
        assert result["thread"]["message_id"] is not None
        assert "abc123" in str(result["thread"]["message_id"])

    def test_in_reply_to_extracted(self):
        """In-Reply-To wird extrahiert."""
        result = extract_email_metadata(SAMPLE_EML)
        assert result is not None
        assert result["thread"]["in_reply_to"] is not None
        assert "def456" in str(result["thread"]["in_reply_to"])

    def test_references_is_list(self):
        """References wird als Liste zurückgegeben."""
        result = extract_email_metadata(SAMPLE_EML)
        assert result is not None
        assert isinstance(result["thread"]["references"], list)

    def test_references_contains_multiple_ids(self):
        """References enthält beide Message-IDs."""
        result = extract_email_metadata(SAMPLE_EML)
        assert result is not None
        refs = result["thread"]["references"]
        assert len(refs) >= 2

    def test_in_reply_to_none_when_new_thread(self):
        """In-Reply-To ist None wenn kein Reply."""
        result = extract_email_metadata(SAMPLE_EML_NO_AUTH)
        assert result is not None
        assert result["thread"]["in_reply_to"] is None

    def test_references_empty_list_when_no_references(self):
        """Kein References-Header → leere Liste."""
        result = extract_email_metadata(SAMPLE_EML_NO_AUTH)
        assert result is not None
        assert result["thread"]["references"] == []


# =============================================================================
# Tests: Mailer
# =============================================================================

class TestExtractMailer:
    """test_extract_mailer: X-Mailer/User-Agent wird extrahiert."""

    def test_mailer_extracted(self):
        """X-Mailer wird als mailer zurückgegeben."""
        result = extract_email_metadata(SAMPLE_EML)
        assert result is not None
        assert result["routing"]["mailer"] is not None
        assert "Thunderbird" in str(result["routing"]["mailer"])

    def test_mailer_none_when_absent(self):
        """Kein X-Mailer/User-Agent Header → None."""
        result = extract_email_metadata(SAMPLE_EML_NO_AUTH)
        assert result is not None
        assert result["routing"]["mailer"] is None


# =============================================================================
# Tests: Nicht-EML → None
# =============================================================================

class TestNonEmailReturnsNone:
    """test_non_email_returns_none: PDF und andere Formate → None."""

    def test_pdf_returns_none(self):
        """PDF-Bytes werden nicht als E-Mail erkannt."""
        result = extract_email_metadata(PDF_BYTES)
        assert result is None

    def test_random_bytes_returns_none(self):
        """Zufällige Bytes ohne E-Mail-Header → None."""
        result = extract_email_metadata(b"\x00\x01\x02\x03 not an email")
        assert result is None

    def test_empty_bytes_returns_none(self):
        """Leere Bytes → None."""
        result = extract_email_metadata(b"")
        assert result is None

    def test_plain_text_without_email_headers_returns_none(self):
        """Plaintext ohne E-Mail-Header → None."""
        result = extract_email_metadata(b"Hello World\nThis is just text\n")
        assert result is None


# =============================================================================
# Tests: meta.email_routing über convert_auto
# =============================================================================

class TestEmailRoutingInMeta:
    """test_email_routing_in_meta: meta.email_routing wird gesetzt."""

    def test_email_routing_set_in_meta(self):
        """convert_auto setzt meta.email_routing für EML-Dateien."""
        fake_email_metadata = {
            "routing": {"spf": "pass", "dkim": "pass", "dmarc": "pass",
                        "received_chain": ["from mail.example.com"],
                        "originating_ip": None, "mailer": "Thunderbird"},
            "thread": {"message_id": "<test@example.com>", "in_reply_to": None,
                       "references": [], "thread_index": None},
            "calendar_events": [],
        }

        with patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch.object(_server, "convert_with_markitdown", return_value={
                 "success": True,
                 "markdown": "# Test Email",
                 "title": None,
                 "zugferd": None,
                 "xmp_metadata": None,
                 "embedded_files": None,
                 "document_properties": None,
                 "email_metadata": fake_email_metadata,
             }):
            response = run_async(convert_auto(
                file_data=SAMPLE_EML,
                filename="test.eml",
                source="test",
                source_type="base64",
                input_meta={},
            ))

        assert response.success is True
        assert response.meta.email_routing is not None
        assert response.meta.email_routing["spf"] == "pass"

    def test_email_thread_set_in_meta(self):
        """convert_auto setzt meta.email_thread für EML-Dateien."""
        fake_email_metadata = {
            "routing": {"spf": None, "dkim": None, "dmarc": None,
                        "received_chain": [],
                        "originating_ip": None, "mailer": None},
            "thread": {"message_id": "<abc@example.com>", "in_reply_to": "<def@example.com>",
                       "references": ["<def@example.com>"], "thread_index": None},
            "calendar_events": [],
        }

        with patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch.object(_server, "convert_with_markitdown", return_value={
                 "success": True,
                 "markdown": "# Thread Test",
                 "title": None,
                 "zugferd": None,
                 "xmp_metadata": None,
                 "embedded_files": None,
                 "document_properties": None,
                 "email_metadata": fake_email_metadata,
             }):
            response = run_async(convert_auto(
                file_data=SAMPLE_EML,
                filename="thread.eml",
                source="test",
                source_type="base64",
                input_meta={},
            ))

        assert response.success is True
        assert response.meta.email_thread is not None
        assert response.meta.email_thread["message_id"] == "<abc@example.com>"

    def test_email_routing_none_for_non_eml(self):
        """meta.email_routing ist None für Nicht-EML-Dateien."""
        with patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch.object(_server, "convert_with_markitdown", return_value={
                 "success": True,
                 "markdown": "# PDF Content",
                 "title": None,
                 "zugferd": None,
                 "xmp_metadata": None,
                 "embedded_files": None,
                 "document_properties": None,
                 "email_metadata": None,
             }):
            response = run_async(convert_auto(
                file_data=b"%PDF fake",
                filename="document.pdf",
                source="test",
                source_type="base64",
                input_meta={},
            ))

        assert response.success is True
        assert response.meta.email_routing is None


# =============================================================================
# Tests: Calendar Event Extraction
# =============================================================================

class TestCalendarEventExtraction:
    """test_calendar_event_extraction: ICS Events werden geparst."""

    @pytest.mark.skipif(not ICALENDAR_AVAILABLE, reason="icalendar not installed")
    def test_calendar_event_parsed(self):
        """ICS-Anhang wird als calendar_event extrahiert."""
        result = extract_email_metadata(SAMPLE_EML_WITH_ICS)
        assert result is not None
        events = result["calendar_events"]
        assert len(events) >= 1

    @pytest.mark.skipif(not ICALENDAR_AVAILABLE, reason="icalendar not installed")
    def test_calendar_event_summary(self):
        """Event-Summary wird korrekt extrahiert."""
        result = extract_email_metadata(SAMPLE_EML_WITH_ICS)
        assert result is not None
        events = result["calendar_events"]
        summaries = [e["summary"] for e in events]
        assert any("Meeting" in s for s in summaries)

    @pytest.mark.skipif(not ICALENDAR_AVAILABLE, reason="icalendar not installed")
    def test_calendar_event_uid(self):
        """Event-UID wird extrahiert."""
        result = extract_email_metadata(SAMPLE_EML_WITH_ICS)
        assert result is not None
        events = result["calendar_events"]
        assert len(events) >= 1
        assert "event-uid-001" in events[0]["uid"]

    @pytest.mark.skipif(not ICALENDAR_AVAILABLE, reason="icalendar not installed")
    def test_calendar_event_start_end(self):
        """DTSTART und DTEND werden extrahiert."""
        result = extract_email_metadata(SAMPLE_EML_WITH_ICS)
        assert result is not None
        events = result["calendar_events"]
        assert len(events) >= 1
        assert events[0]["start"] is not None
        assert events[0]["end"] is not None

    @pytest.mark.skipif(not ICALENDAR_AVAILABLE, reason="icalendar not installed")
    def test_calendar_event_location(self):
        """Location wird extrahiert."""
        result = extract_email_metadata(SAMPLE_EML_WITH_ICS)
        assert result is not None
        events = result["calendar_events"]
        assert len(events) >= 1
        assert "Conference Room" in events[0]["location"]


# =============================================================================
# Tests: Kein ICS → leere Liste
# =============================================================================

class TestNoCalendarGraceful:
    """test_no_calendar_graceful: Kein ICS-Anhang → leere calendar_events Liste."""

    def test_no_ics_returns_empty_list(self):
        """E-Mail ohne ICS-Anhang → calendar_events ist leere Liste."""
        result = extract_email_metadata(SAMPLE_EML)
        assert result is not None
        assert isinstance(result["calendar_events"], list)
        assert len(result["calendar_events"]) == 0

    def test_no_ics_simple_email(self):
        """Einfache E-Mail ohne Anhänge → calendar_events ist leere Liste."""
        result = extract_email_metadata(SAMPLE_EML_NO_AUTH)
        assert result is not None
        assert result["calendar_events"] == []

    def test_result_structure_always_present(self):
        """Ergebnis-Dict hat immer routing, thread, calendar_events Keys."""
        result = extract_email_metadata(SAMPLE_EML)
        assert result is not None
        assert "routing" in result
        assert "thread" in result
        assert "calendar_events" in result
