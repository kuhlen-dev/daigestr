"""
Text Renderer für Daigestr.

Konvertiert Markdown zu Plain Text ohne Markup-Syntax.
Nutzt ausschließlich regex, keine externen Libraries.
"""

import re


def markdown_to_text(markdown: str) -> str:
    """
    Entfernt Markdown-Syntax und konvertiert zu Plain Text.

    Konvertierungen:
    - ```mermaid Blöcke → [Diagram]
    - ```code``` Blöcke → eingerückt mit 4 Spaces
    - # Headings → GROSSBUCHSTABEN + Unterstrich
    - **bold** und __bold__ → text
    - *italic* und _italic_ → text
    - | Tabellen | → Tab-separiert
    - [links](url) → text (url)
    - Bilder ![alt](src) → [Image: alt]

    Args:
        markdown: Markdown-Inhalt

    Returns:
        Plain Text ohne Markdown-Syntax
    """
    text = markdown

    # 1. Mermaid-Blöcke → [Diagram]
    text = re.sub(
        r"```mermaid\n.*?```",
        "[Diagram]",
        text,
        flags=re.DOTALL
    )

    # 2. Fenced Code-Blöcke → eingerückt mit 4 Spaces (Sprach-Tag entfernen)
    def indent_code_block(m: re.Match) -> str:
        code_content = m.group(1)
        lines = code_content.split("\n")
        indented = "\n".join("    " + line for line in lines)
        return indented

    text = re.sub(
        r"```(?:\w+)?\n(.*?)```",
        indent_code_block,
        text,
        flags=re.DOTALL
    )

    # 3. Bilder ![alt](src) → [Image: alt]  (VOR Links verarbeiten!)
    text = re.sub(
        r"!\[([^\]]*)\]\([^)]*\)",
        lambda m: f"[Image: {m.group(1)}]" if m.group(1) else "[Image]",
        text
    )

    # 4. Links [text](url) → text (url)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r"\1 (\2)",
        text
    )

    # 5. Inline-Code `code` → code (Backticks entfernen)
    text = re.sub(r"`([^`]+)`", r"\1", text)

    # 6. Bold **text** oder __text__ → text
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)

    # 7. Italic *text* oder _text_ → text
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"_([^_]+)_", r"\1", text)

    # 8. Headings # → GROSSBUCHSTABEN + Unterstrich
    def heading_to_upper(m: re.Match) -> str:
        level = len(m.group(1))
        heading_text = m.group(2).strip()
        upper = heading_text.upper()
        # H1/H2: Unterstrich mit = oder -, H3+: nur Großbuchstaben
        if level == 1:
            underline = "=" * len(upper)
            return f"{upper}\n{underline}"
        elif level == 2:
            underline = "-" * len(upper)
            return f"{upper}\n{underline}"
        else:
            return upper

    text = re.sub(
        r"^(#{1,6})\s+(.+)$",
        heading_to_upper,
        text,
        flags=re.MULTILINE
    )

    # 9. Tabellen → Tab-separiert
    def table_to_tabs(m: re.Match) -> str:
        row = m.group(0).strip()
        # Separator-Zeilen (|---|---|) überspringen
        if re.match(r"^\|[\s\-:|]+\|$", row):
            return ""
        # Zellen extrahieren
        cells = [cell.strip() for cell in row.split("|") if cell.strip()]
        return "\t".join(cells)

    # Tabellenzeilen erkennen und konvertieren
    text = re.sub(
        r"^\|.+\|$",
        table_to_tabs,
        text,
        flags=re.MULTILINE
    )

    # 10. Blockquotes > → text ohne >
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)

    # 11. Horizontale Linien --- / *** / ___ → leere Zeile
    text = re.sub(r"^[-*_]{3,}$", "", text, flags=re.MULTILINE)

    # 12. Mehrfache Leerzeilen auf max 2 reduzieren
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()
