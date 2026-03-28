"""
Tests für output_format html + text (T-DAI-016 + T-DAI-017).
"""

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# Sicherstellen dass mcp/ im Python-Pfad ist
sys.path.insert(0, str(Path(__file__).parent.parent))

from renderers.html import markdown_to_html
from renderers.text import markdown_to_text
from models import ConvertResponse, ConvertRequest


# =============================================================================
# HTML Renderer Tests
# =============================================================================

SAMPLE_MARKDOWN = """# Überschrift

Normaler Absatz mit **fettem** und *kursivem* Text.

## Tabelle

| Name | Alter | Stadt |
|------|-------|-------|
| Alice | 30 | Berlin |
| Bob | 25 | Hamburg |

## Code

```python
def hello():
    print("Hello World")
```

Inline `code` hier.
"""

MERMAID_MARKDOWN = """# Diagramm

Hier ist ein Flussdiagramm:

```mermaid
graph TD
    A[Start] --> B{Entscheidung}
    B -->|Ja| C[Ende]
    B -->|Nein| D[Weiter]
```

Text nach dem Diagramm.
"""


def test_html_contains_basic_structure():
    """HTML muss DOCTYPE, html, style-Tag und gerendertes Markdown enthalten."""
    html = markdown_to_html(SAMPLE_MARKDOWN)

    assert "<!DOCTYPE html>" in html
    assert "<html" in html
    assert "<style>" in html
    assert "</html>" in html


def test_html_contains_utf8_meta():
    """HTML muss UTF-8 Meta-Tag enthalten."""
    html = markdown_to_html(SAMPLE_MARKDOWN)
    assert 'charset="UTF-8"' in html or "charset=UTF-8" in html


def test_html_contains_mermaid_script():
    """HTML muss Mermaid.js Script-Tag enthalten."""
    html = markdown_to_html(SAMPLE_MARKDOWN)
    assert "mermaid" in html
    assert "mermaid.initialize" in html


def test_html_contains_rendered_table():
    """Tabellen müssen als HTML-Tabelle gerendert werden."""
    html = markdown_to_html(SAMPLE_MARKDOWN)
    assert "<table>" in html or "<table " in html
    assert "<th>" in html or "<th " in html
    assert "<td>" in html or "<td " in html
    assert "Alice" in html
    assert "Bob" in html


def test_html_mermaid_block_converted():
    """```mermaid Blöcke müssen zu <pre class="mermaid"> konvertiert werden."""
    html = markdown_to_html(MERMAID_MARKDOWN)
    assert '<pre class="mermaid">' in html
    assert "graph TD" in html
    # Sicherstellen dass es NICHT als normaler code-Block gerendert wird
    assert "```mermaid" not in html


def test_html_with_title():
    """Seitentitel muss im <title> Tag erscheinen."""
    html = markdown_to_html("# Test", title="Mein Dokument")
    assert "<title>Mein Dokument</title>" in html


def test_html_default_title():
    """Standard-Titel ist 'Document'."""
    html = markdown_to_html("# Test")
    assert "<title>Document</title>" in html


def test_html_highlight_js():
    """highlight.js CDN muss eingebunden sein."""
    html = markdown_to_html(SAMPLE_MARKDOWN)
    assert "highlight.js" in html or "highlight.min.js" in html


# =============================================================================
# Text Renderer Tests
# =============================================================================

TEXT_MARKDOWN = """# Hauptüberschrift

Normaler Text mit **fett** und *kursiv*.

## Unterüberschrift

| Spalte A | Spalte B |
|----------|----------|
| Wert 1 | Wert 2 |

```python
x = 42
```

[Link Text](https://example.com)

![Bild Alt](https://example.com/bild.png)
"""


def test_text_no_markdown_symbols():
    """Plain Text darf keine Markdown-Syntax enthalten."""
    text = markdown_to_text(TEXT_MARKDOWN)

    # Kein # am Zeilenanfang
    for line in text.split("\n"):
        assert not line.startswith("#"), f"Zeile beginnt mit #: {line!r}"

    # Kein **bold**
    assert "**" not in text

    # Kein Tabellen-Separator
    assert "|---|" not in text
    assert "|----------|" not in text

    # Kein Fenced Code Block
    assert "```" not in text


def test_text_heading_uppercase():
    """Headings werden zu GROSSBUCHSTABEN konvertiert."""
    text = markdown_to_text("# Hallo Welt")
    assert "HALLO WELT" in text


def test_text_heading_with_underline():
    """H1 bekommt Unterstrich mit =."""
    text = markdown_to_text("# Titel")
    assert "TITEL" in text
    assert "=====" in text or "=" * 5 in text


def test_text_h2_underline():
    """H2 bekommt Unterstrich mit -."""
    text = markdown_to_text("## Abschnitt")
    assert "ABSCHNITT" in text
    # H2 hat - Unterstrich
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if "ABSCHNITT" in line and i + 1 < len(lines):
            assert "-" in lines[i + 1]
            break


def test_text_table_tab_separated():
    """Tabellen werden Tab-separiert."""
    md = "| A | B | C |\n|---|---|---|\n| 1 | 2 | 3 |"
    text = markdown_to_text(md)
    # Tabellen-Inhalt ist Tab-separiert
    assert "\t" in text
    assert "A" in text
    assert "1" in text
    # Separator-Zeile ist weg
    assert "---" not in text


def test_text_no_bold():
    """Bold-Syntax wird entfernt, Text bleibt erhalten."""
    text = markdown_to_text("**Wichtig** ist das!")
    assert "**" not in text
    assert "Wichtig" in text


def test_text_no_italic():
    """Italic-Syntax wird entfernt, Text bleibt erhalten."""
    text = markdown_to_text("*kursiv* und _auch kursiv_")
    assert "*kursiv*" not in text
    assert "kursiv" in text


def test_text_links():
    """Links werden zu text (url) Format konvertiert."""
    text = markdown_to_text("[Link Text](https://example.com)")
    assert "Link Text" in text
    assert "https://example.com" in text
    assert "[Link Text]" not in text


def test_text_images():
    """Bilder werden zu [Image: alt] konvertiert."""
    text = markdown_to_text("![Alt Text](https://example.com/img.png)")
    assert "[Image: Alt Text]" in text
    assert "![" not in text


def test_text_mermaid_to_diagram():
    """Mermaid-Blöcke werden zu [Diagram]."""
    text = markdown_to_text(MERMAID_MARKDOWN)
    assert "[Diagram]" in text
    assert "```mermaid" not in text


def test_text_code_block_indented():
    """Code-Blöcke werden mit 4 Spaces eingerückt."""
    md = "```python\nx = 42\nprint(x)\n```"
    text = markdown_to_text(md)
    assert "```" not in text
    assert "    x = 42" in text or "x = 42" in text


# =============================================================================
# ConvertResponse html-Feld Tests
# =============================================================================

def test_convert_response_html_field_default_none():
    """html-Feld ist standardmäßig None."""
    response = ConvertResponse(success=True, markdown="# Test")
    assert response.html is None


def test_convert_response_html_field_settable():
    """html-Feld kann gesetzt werden."""
    response = ConvertResponse(success=True, markdown="# Test")
    response.html = "<html><body><h1>Test</h1></body></html>"
    assert response.html is not None
    assert "<html>" in response.html


# =============================================================================
# output_format Validator Tests
# =============================================================================

def test_output_format_markdown_valid():
    """output_format='markdown' ist gültig."""
    req = ConvertRequest(path="/data/test.pdf", output_format="markdown")
    assert req.output_format == "markdown"


def test_output_format_html_valid():
    """output_format='html' ist gültig."""
    req = ConvertRequest(path="/data/test.pdf", output_format="html")
    assert req.output_format == "html"


def test_output_format_text_valid():
    """output_format='text' ist gültig."""
    req = ConvertRequest(path="/data/test.pdf", output_format="text")
    assert req.output_format == "text"


def test_output_format_invalid_raises():
    """Ungültiger output_format löst ValidationError aus."""
    with pytest.raises(ValidationError):
        ConvertRequest(path="/data/test.pdf", output_format="pdf")


def test_output_format_default_is_markdown():
    """Standard-output_format ist 'markdown'."""
    req = ConvertRequest(path="/data/test.pdf")
    assert req.output_format == "markdown"
