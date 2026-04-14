"""
Daigestr — Utility Functions

Enthält allgemeine Hilfsfunktionen:
- strip_llm_artifacts: LLM-Output bereinigen
- detect_mimetype_from_bytes: MIME-Type aus Magic Bytes
- detect_code_language: Programmiersprache via Regex
- detect_and_fence_code_blocks: Code-Blöcke in Markdown fencen
- get_mimetype: MIME-Type einer Datei ermitteln
- get_file_extension: Dateiendung extrahieren
- resolve_path: Pfad auflösen
- is_image_file, is_markitdown_file, is_audio_file, is_video_file, should_skip_file
"""

import re
import sys
from pathlib import Path
from typing import Optional

import magic
import structlog


# ---------------------------------------------------------------------------
# Server-Module Lookup (shared across all extracted modules)
# ---------------------------------------------------------------------------

_LOADED_BY_SERVER: object = sys.modules.get("server")


def _get(name: str, default):
    """Look up *name* in the server module that loaded this sub-module.

    Uses _LOADED_BY_SERVER (the server module at import time) directly so that
    test patches on a specific server instance propagate even when sys.modules['server']
    has been replaced by another test file's load_server_module() call.
    """
    m = _LOADED_BY_SERVER
    if m is not None and hasattr(m, name):
        return getattr(m, name)
    return default

from settings import (
    DATA_DIR,
    ALLOWED_PATH_ROOTS,
    ALLOW_SYMLINK_PATHS,
    IMAGE_EXTENSIONS,
    MARKITDOWN_EXTENSIONS,
    AUDIO_EXTENSIONS,
    VIDEO_EXTENSIONS,
    SKIP_FILES,
)

log = structlog.get_logger()


# =============================================================================
# LLM-Artifact Patterns
# =============================================================================

_PREAMBLE_PATTERNS = [
    # German patterns
    re.compile(
        r'^(?:hier\s+ist\s+(?:der|die|das)\s+[^\n]{0,120}\n+)',
        re.IGNORECASE,
    ),
    re.compile(
        r'^(?:im\s+folgenden\s+[^\n]{0,120}\n+)',
        re.IGNORECASE,
    ),
    re.compile(
        r'^(?:nachfolgend\s+[^\n]{0,120}\n+)',
        re.IGNORECASE,
    ),
    re.compile(
        r'^(?:gerne[,!.]?\s+[^\n]{0,120}\n+)',
        re.IGNORECASE,
    ),
    # English patterns
    re.compile(
        r'^(?:here\s+is\s+(?:the\s+)?[^\n]{0,120}\n+)',
        re.IGNORECASE,
    ),
    re.compile(
        r'^(?:below\s+is\s+(?:the\s+)?[^\n]{0,120}\n+)',
        re.IGNORECASE,
    ),
    re.compile(
        r'^(?:the\s+following\s+[^\n]{0,120}\n+)',
        re.IGNORECASE,
    ),
    re.compile(
        r'^(?:certainly[!,.]?\s*[^\n]{0,120}\n+)',
        re.IGNORECASE,
    ),
    re.compile(
        r'^(?:sure[!,.]?\s*[^\n]{0,120}\n+)',
        re.IGNORECASE,
    ),
]

# Pattern for full-output code block wrapping (```markdown ... ``` or ``` ... ```)
_FULL_CODEBLOCK_PATTERN = re.compile(
    r'^```(?:markdown|md)?\s*\n([\s\S]*?)\n```\s*$',
    re.IGNORECASE,
)

# =============================================================================
# Code Language Detection Patterns
# =============================================================================

_LANGUAGE_PATTERNS: list[tuple[str, list[str]]] = [
    ("python",     [r"\bdef ", r"\bimport ", r"\bclass ", r"if __name__", r"\bprint\(", r"\bself\."]),
    ("javascript", [r"\bfunction ", r"\bconst ", r"\blet ", r"=> ", r"\bconsole\.log"]),
    ("java",       [r"\bpublic class\b", r"\bprivate ", r"\bSystem\.out\b", r"\bvoid "]),
    ("sql",        [r"\bSELECT\b", r"\bFROM\b", r"\bWHERE\b", r"\bINSERT INTO\b"]),
    ("html",       [r"<html", r"<div", r"<body", r"<!DOCTYPE"]),
    ("css",        [r"\{color:", r"\bmargin:", r"\bpadding:", r"\bdisplay:"]),
    ("bash",       [r"#!/bin/", r"\becho ", r"if \[", r"\bfi\b", r"\bdone\b"]),
    ("go",         [r"\bfunc ", r"\bpackage ", r"\bimport \(", r"\bfmt\."]),
    ("rust",       [r"\bfn ", r"\blet mut\b", r"\bimpl ", r"\bpub fn\b"]),
    ("cpp",        [r"#include", r"\bint main\b", r"\bprintf\(", r"\bstd::"]),
]

# Minimum score (number of pattern matches) to identify a language
_MIN_LANG_SCORE = 2


# =============================================================================
# LLM Artifact Removal
# =============================================================================

def strip_llm_artifacts(text: str) -> str:
    """
    Entfernt typische LLM-Artefakte aus dem Output.

    Entfernt:
    - Einleitende Preamble-Sätze ("Hier ist...", "Here is...", "Im Folgenden...", etc.)
    - ```markdown ... ``` Wrapping wenn der gesamte Output darin eingeschlossen ist
    - ``` ... ``` Wrapping wenn der gesamte Output darin eingeschlossen ist

    Codeblöcke INNERHALB des Textes werden NICHT entfernt.

    Args:
        text: LLM-Output-Text der bereinigt werden soll.

    Returns:
        Bereinigter Text.
    """
    if not text:
        return text

    result = text.strip()

    # Mermaid-Codeblöcke NICHT strippen — sie sind gewollter Output
    if result.startswith("```mermaid"):
        return result

    # 1. Outer code block wrapping entfernen (```markdown ... ``` oder ``` ... ```)
    codeblock_match = _FULL_CODEBLOCK_PATTERN.match(result)
    if codeblock_match:
        result = codeblock_match.group(1).strip()

    # 2. Preamble-Zeilen am Anfang entfernen
    for pattern in _PREAMBLE_PATTERNS:
        result = pattern.sub('', result)
        result = result.strip()

    return result


# =============================================================================
# MIME-Type Detection
# =============================================================================

def detect_mimetype_from_bytes(data: bytes) -> Optional[str]:
    """Erkennt MIME-Type aus Magic Bytes."""
    try:
        return magic.from_buffer(data, mime=True)
    except Exception:
        return None


def get_mimetype(path: Path) -> str:
    """Ermittelt den MIME-Type einer Datei."""
    suffix = path.suffix.lower()
    mime_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".pdf": "application/pdf",
    }
    return mime_map.get(suffix, "application/octet-stream")


# =============================================================================
# Code Language Detection
# =============================================================================

def detect_code_language(text: str) -> str:
    """
    Erkennt die Programmiersprache eines Code-Blocks via Regex-Heuristik.

    Args:
        text: Zu analysierender Text-Block.

    Returns:
        Sprachname in Kleinbuchstaben (z.B. "python", "javascript") oder ""
        wenn die Sprache nicht mit ausreichender Sicherheit erkannt werden kann.
    """
    best_lang = ""
    best_score = 0

    for lang, patterns in _LANGUAGE_PATTERNS:
        score = sum(1 for pat in patterns if re.search(pat, text))
        if score > best_score:
            best_score = score
            best_lang = lang

    return best_lang if best_score >= _MIN_LANG_SCORE else ""


def detect_and_fence_code_blocks(markdown: str) -> str:
    """
    Sucht in Markdown-Text nach nicht-gefenctem Code und wrapp ihn in
    ```language ... ``` Fences.

    Erkennungs-Kriterien:
    1. Indentierte Blöcke (4+ Leerzeichen am Zeilenanfang), mindestens 3 Zeilen.
    2. Blöcke mit mindestens 3 Code-Indikatoren (Klammern, Semikolons, Zuweisungen).

    Bereits vorhandene Fences (``` ... ```) werden nicht erneut gewrappt.

    Args:
        markdown: Markdown-Text nach der Konvertierung.

    Returns:
        Markdown-Text mit gefencten Code-Blöcken.
    """
    if not markdown:
        return markdown

    # Schritt 1: Vorhandene Fences aus dem Text ausblenden, damit wir sie nicht
    # versehentlich als Kandidaten erkennen.
    # Wir ersetzen sie durch Platzhalter und stellen sie am Ende wieder her.
    fenced_blocks: list[str] = []
    fence_pattern = re.compile(r"```[\s\S]*?```", re.MULTILINE)

    def _stash_fence(m: re.Match) -> str:
        idx = len(fenced_blocks)
        fenced_blocks.append(m.group(0))
        return f"\x00FENCE{idx}\x00"

    working = fence_pattern.sub(_stash_fence, markdown)

    # Schritt 2: Kandidaten-Blöcke identifizieren.
    # Ein Block ist eine zusammenhängende Gruppe von Zeilen mit 4+ Spaces Einrückung
    # ODER eine Gruppe von Zeilen mit Code-Indikatoren.
    lines = working.split("\n")
    result_lines: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Sammle zusammenhängende Zeilen mit 4+ Spaces
        if re.match(r"^    ", line) and line.strip():
            block_lines: list[str] = []
            j = i
            while j < len(lines) and (re.match(r"^    ", lines[j]) or lines[j].strip() == ""):
                block_lines.append(lines[j])
                j += 1

            # Trailing leere Zeilen aus dem Block entfernen
            while block_lines and not block_lines[-1].strip():
                block_lines.pop()

            # Mindestens 3 nicht-leere Zeilen
            non_empty = [l for l in block_lines if l.strip()]  # noqa: E741

            if len(non_empty) >= 3:
                block_text = "\n".join(block_lines)
                lang = detect_code_language(block_text)
                fence_open = f"```{lang}" if lang else "```"
                result_lines.append(fence_open)
                result_lines.extend(block_lines)
                result_lines.append("```")
                log.debug(
                    "code_block_fenced",
                    lines=len(non_empty),
                    language=lang or "unknown",
                )
            else:
                result_lines.extend(block_lines)

            i = j
            continue

        result_lines.append(line)
        i += 1

    working = "\n".join(result_lines)

    # Schritt 3: Platzhalter durch originale Fences ersetzen
    for idx, original in enumerate(fenced_blocks):
        working = working.replace(f"\x00FENCE{idx}\x00", original)

    return working


# =============================================================================
# PDF Page Selection
# =============================================================================

def parse_pages(spec: str, total_pages: int) -> list[int]:
    """
    Parse a page selection spec string into a sorted list of 0-based page indices.

    Syntax:
    - "N"      → single page N (1-based)
    - "N-M"    → pages N through M (inclusive, 1-based)
    - "!N"     → exclude page N (1-based)
    - Comma-separated combinations: "1-3,7,!2"

    Args:
        spec:        Page selection string, e.g. "1-3", "7,14,22", "10-20,!15".
        total_pages: Total number of pages in the document (1-based max).

    Returns:
        Sorted list of 0-based page indices.

    Raises:
        ValueError: If the resulting page set is empty.
    """
    if not spec or not spec.strip():
        raise ValueError("Empty page spec")

    included: set[int] = set()
    excluded: set[int] = set()

    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue

        if part.startswith("!"):
            # Exclusion
            try:
                n = int(part[1:])
            except ValueError:
                continue
            if 1 <= n <= total_pages:
                excluded.add(n - 1)
        elif "-" in part:
            # Range N-M
            dash_idx = part.index("-")
            try:
                n = int(part[:dash_idx])
                m = int(part[dash_idx + 1:])
            except ValueError:
                continue
            for p in range(n, m + 1):
                if 1 <= p <= total_pages:
                    included.add(p - 1)
        else:
            # Single page
            try:
                n = int(part)
            except ValueError:
                continue
            if 1 <= n <= total_pages:
                included.add(n - 1)

    result = sorted(included - excluded)
    if not result:
        raise ValueError(f"Page spec '{spec}' results in no valid pages (total: {total_pages})")
    return result


# =============================================================================
# Path / File Helpers
# =============================================================================

class PathPolicyError(ValueError):
    """Raised when a requested filesystem path violates the configured storage policy."""

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


def _path_has_symlink_component(path: Path) -> bool:
    """Return True when any existing component in *path* is a symlink."""
    current = Path(path.anchor) if path.is_absolute() else Path()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current = current / part
        if current.exists() and current.is_symlink():
            return True
    return False


def resolve_path(path: str) -> Path:
    """Resolve a user path against the configured storage policy."""
    if not path or not str(path).strip():
        raise PathPolicyError("Pfad darf nicht leer sein", reason="empty_path")

    requested = Path(path)
    candidate = requested if requested.is_absolute() else (DATA_DIR / requested)
    normalized = candidate.resolve(strict=False)

    if not ALLOW_SYMLINK_PATHS and _path_has_symlink_component(candidate):
        raise PathPolicyError(
            f"Symlink-Pfade sind nicht erlaubt: {candidate}",
            reason="symlink_not_allowed",
        )

    if not any(normalized == root or root in normalized.parents for root in ALLOWED_PATH_ROOTS):
        allowed = ", ".join(str(root) for root in ALLOWED_PATH_ROOTS)
        raise PathPolicyError(
            f"Pfad liegt außerhalb der erlaubten Roots: {normalized} (erlaubt: {allowed})",
            reason="path_outside_allowed_roots",
        )

    return normalized


def get_file_extension(filename: str) -> str:
    """Extrahiert die Dateiendung."""
    return Path(filename).suffix.lower()


def is_image_file(path: Path) -> bool:
    """Prüft ob eine Datei ein unterstütztes Bild ist."""
    return path.suffix.lower() in IMAGE_EXTENSIONS


def is_markitdown_file(path: Path) -> bool:
    """Prüft ob eine Datei von MarkItDown verarbeitet werden kann."""
    return path.suffix.lower() in MARKITDOWN_EXTENSIONS


def is_audio_file(path: Path) -> bool:
    """Prüft ob eine Datei eine unterstützte Audio-Datei ist (FR-MKIT-006).

    Args:
        path: Pfad zur Datei.

    Returns:
        True wenn die Dateiendung in AUDIO_EXTENSIONS enthalten ist.
    """
    return path.suffix.lower() in AUDIO_EXTENSIONS


def is_video_file(path: Path) -> bool:
    """Prüft ob eine Datei eine unterstützte Video-Datei ist (FR-MKIT-006).

    Args:
        path: Pfad zur Datei.

    Returns:
        True wenn die Dateiendung in VIDEO_EXTENSIONS enthalten ist.
    """
    return path.suffix.lower() in VIDEO_EXTENSIONS


def should_skip_file(filename: str) -> bool:
    """Prüft ob eine Datei übersprungen werden soll."""
    return filename in SKIP_FILES or filename.startswith(".")
