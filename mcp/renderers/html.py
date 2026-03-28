"""
HTML Renderer für Daigestr.

Konvertiert Markdown zu vollständigem HTML mit eingebettetem CSS,
Mermaid.js-Support und highlight.js Syntax-Highlighting.
"""

import os
import re
import markdown as md_lib

MERMAID_CDN_URL = os.getenv("MERMAID_CDN_URL", "https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js")
HIGHLIGHTJS_CDN_URL = os.getenv("HIGHLIGHTJS_CDN_URL", "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js")
HIGHLIGHTJS_CSS_URL = os.getenv("HIGHLIGHTJS_CSS_URL", "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css")


def markdown_to_html(markdown: str, title: str = "Document") -> str:
    """
    Konvertiert Markdown zu einer vollständigen HTML-Seite.

    Features:
    - Tabellen mit borders, padding, zebra-striping
    - Code-Blöcke mit monospace font und Hintergrundfarbe
    - Mermaid.js für Diagramme (```mermaid Blöcke)
    - highlight.js für Syntax-Highlighting
    - Responsive max-width Layout
    - UTF-8 Meta-Tag

    Args:
        markdown: Markdown-Inhalt
        title: Seitentitel für <title> Tag

    Returns:
        Vollständige HTML-Seite als String
    """
    # Mermaid-Blöcke VOR markdown-Konvertierung extrahieren und durch Platzhalter ersetzen
    # damit die markdown Library sie nicht als Code-Block rendert
    mermaid_blocks: list[str] = []

    def extract_mermaid(m: re.Match) -> str:
        content = m.group(1)
        idx = len(mermaid_blocks)
        mermaid_blocks.append(content)
        return f"MERMAID_PLACEHOLDER_{idx}"

    markdown_processed = re.sub(
        r"```mermaid\n(.*?)```",
        extract_mermaid,
        markdown,
        flags=re.DOTALL
    )

    # Markdown → HTML mit Extensions
    html_body = md_lib.markdown(
        markdown_processed,
        extensions=["tables", "fenced_code", "codehilite"],
        extension_configs={
            "codehilite": {
                "use_pygments": False,  # highlight.js übernimmt das
                "css_class": "highlight",
            }
        }
    )

    # Mermaid-Platzhalter durch <pre class="mermaid"> ersetzen
    for idx, content in enumerate(mermaid_blocks):
        placeholder = f"MERMAID_PLACEHOLDER_{idx}"
        mermaid_html = f'<pre class="mermaid">{content}</pre>'
        html_body = html_body.replace(placeholder, mermaid_html)

    css = """
        * {
            box-sizing: border-box;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 960px;
            margin: 0 auto;
            padding: 2rem;
            background: #fff;
        }
        h1, h2, h3, h4, h5, h6 {
            font-weight: 600;
            line-height: 1.25;
            margin-top: 1.5em;
            margin-bottom: 0.5em;
        }
        h1 { font-size: 2em; border-bottom: 2px solid #eee; padding-bottom: 0.3em; }
        h2 { font-size: 1.5em; border-bottom: 1px solid #eee; padding-bottom: 0.3em; }
        h3 { font-size: 1.25em; }
        p {
            margin: 0.8em 0;
        }
        a {
            color: #0366d6;
            text-decoration: none;
        }
        a:hover {
            text-decoration: underline;
        }
        /* Tabellen */
        table {
            border-collapse: collapse;
            width: 100%;
            margin: 1em 0;
            font-size: 0.95em;
        }
        th, td {
            border: 1px solid #ddd;
            padding: 8px 12px;
            text-align: left;
        }
        th {
            background-color: #f4f4f4;
            font-weight: 600;
        }
        /* Zebra-Striping */
        tr:nth-child(even) {
            background-color: #f9f9f9;
        }
        tr:hover {
            background-color: #f0f0f0;
        }
        /* Code-Blöcke */
        code {
            font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
            font-size: 0.9em;
            background: #f6f8fa;
            padding: 0.2em 0.4em;
            border-radius: 3px;
        }
        pre {
            background: #f6f8fa;
            border: 1px solid #e1e4e8;
            border-radius: 6px;
            padding: 1em;
            overflow-x: auto;
            margin: 1em 0;
        }
        pre code {
            background: none;
            padding: 0;
            font-size: 0.875em;
        }
        /* Mermaid Diagramme */
        pre.mermaid {
            background: #fff;
            border: 1px solid #e1e4e8;
            border-radius: 6px;
            padding: 1em;
            text-align: center;
        }
        blockquote {
            margin: 0;
            padding: 0 1em;
            border-left: 4px solid #dfe2e5;
            color: #6a737d;
        }
        img {
            max-width: 100%;
        }
        ul, ol {
            padding-left: 2em;
            margin: 0.8em 0;
        }
        li {
            margin: 0.3em 0;
        }
        hr {
            border: none;
            border-top: 1px solid #eee;
            margin: 1.5em 0;
        }
    """

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
{css}
    </style>
    <!-- highlight.js für Syntax-Highlighting -->
    <link rel="stylesheet" href="{HIGHLIGHTJS_CSS_URL}">
    <script src="{HIGHLIGHTJS_CDN_URL}"></script>
</head>
<body>
{html_body}
<!-- Mermaid.js für Diagramme -->
<script src="{MERMAID_CDN_URL}"></script>
<script>
    mermaid.initialize({{startOnLoad: true}});
    document.addEventListener('DOMContentLoaded', function() {{
        hljs.highlightAll();
    }});
</script>
</body>
</html>"""

    return html
