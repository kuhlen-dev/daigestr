"""
Daigestr — Image Converter Utilities

Enthält alle Hilfsfunktionen für Bild-Verarbeitung:
- Resize
- EXIF/GPS/IPTC Metadata Extraction
- Embedded Image Extraction (DOCX, PPTX)
- Image Classification via Vision API
- Diagram → Mermaid Conversion
- Chart Data Extraction
- Embedded Image Description
- Image Placeholder Insertion
- PDF First Page Rendering
"""

import io
import re
import zipfile
from pathlib import Path
from typing import Optional

import structlog
from PIL import Image

from settings import (
    IMAGE_MAX_WIDTH,
    IMAGE_EXTENSIONS,
    MIN_IMAGE_SIZE_PX,
    PDF_RENDER_DPI,
)
from mistral_client import analyze_with_mistral_vision
from utils import detect_mimetype_from_bytes, strip_llm_artifacts, _get, _LOADED_BY_SERVER  # noqa: F401

try:
    from pdf2image import convert_from_path
    PDF2IMAGE_AVAILABLE = True
except ImportError:
    PDF2IMAGE_AVAILABLE = False

log = structlog.get_logger()


def resize_image_if_needed(image_data: bytes, max_width: int = IMAGE_MAX_WIDTH) -> tuple[bytes, dict]:
    """
    Verkleinert ein Bild falls es zu groß ist.

    Returns:
        tuple: (image_bytes, resize_meta)
    """
    resize_meta = {
        "resized": False,
        "original_width": None,
        "original_height": None,
    }

    try:
        img = Image.open(io.BytesIO(image_data))
        resize_meta["original_width"] = img.width
        resize_meta["original_height"] = img.height

        if img.width > max_width:
            ratio = max_width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
            resize_meta["resized"] = True
            resize_meta["width"] = max_width
            resize_meta["height"] = new_height

            output = io.BytesIO()
            img_format = "JPEG" if img.mode == "RGB" else "PNG"
            if img.mode == "RGBA" and img_format == "JPEG":
                img = img.convert("RGB")
            img.save(output, format=img_format, quality=85)
            return output.getvalue(), resize_meta
        else:
            resize_meta["width"] = img.width
            resize_meta["height"] = img.height
            return image_data, resize_meta

    except Exception as e:
        log.warning("image_resize_failed", error=str(e))
        return image_data, resize_meta


def _dms_to_decimal(dms_tuple, ref: str) -> float:
    """Convert GPS DMS (degrees, minutes, seconds) to decimal degrees."""
    degrees, minutes, seconds = dms_tuple
    decimal = float(degrees) + float(minutes) / 60 + float(seconds) / 3600
    if ref in ("S", "W"):
        decimal = -decimal
    return round(decimal, 6)


def extract_image_metadata(image_data: bytes) -> dict:
    """
    Extrahiert EXIF-, GPS- und IPTC-Metadaten aus Bild-Bytes via Pillow.

    Args:
        image_data: Rohe Bild-Bytes (JPEG, PNG, etc.)

    Returns:
        Dict mit optionalen Schlüsseln 'exif', 'gps', 'iptc'.
        Leeres Dict wenn keine Metadaten vorhanden oder Fehler auftritt.
    """
    result: dict = {}
    try:
        img = Image.open(io.BytesIO(image_data))
        raw_exif = img.getexif()

        # --- EXIF-Basisdaten ---
        exif_data: dict = {}

        camera_make = raw_exif.get(271)   # Make
        if camera_make:
            exif_data["camera_make"] = str(camera_make).strip()

        camera_model = raw_exif.get(272)  # Model
        if camera_model:
            exif_data["camera_model"] = str(camera_model).strip()

        software = raw_exif.get(305)      # Software
        if software:
            exif_data["software"] = str(software).strip()

        datetime_original = raw_exif.get(36867)   # DateTimeOriginal
        if datetime_original:
            exif_data["datetime_original"] = str(datetime_original).strip()

        datetime_digitized = raw_exif.get(36868)  # DateTimeDigitized
        if datetime_digitized:
            exif_data["datetime_digitized"] = str(datetime_digitized).strip()

        # --- GPS ---
        try:
            gps_ifd = raw_exif.get_ifd(34853)  # GPSInfo IFD tag
            if gps_ifd:
                gps_data: dict = {}

                lat_dms = gps_ifd.get(2)   # GPSLatitude
                lat_ref = gps_ifd.get(1)   # GPSLatitudeRef
                lon_dms = gps_ifd.get(4)   # GPSLongitude
                lon_ref = gps_ifd.get(3)   # GPSLongitudeRef
                alt_val = gps_ifd.get(6)   # GPSAltitude
                alt_ref = gps_ifd.get(5)   # GPSAltitudeRef (0=above, 1=below sea level)

                if lat_dms and lat_ref:
                    gps_data["latitude"] = _dms_to_decimal(lat_dms, lat_ref)
                if lon_dms and lon_ref:
                    gps_data["longitude"] = _dms_to_decimal(lon_dms, lon_ref)
                if alt_val is not None:
                    try:
                        alt_decimal = float(alt_val)
                        if alt_ref == 1:
                            alt_decimal = -alt_decimal
                        gps_data["altitude"] = round(alt_decimal, 2)
                    except (TypeError, ValueError):
                        pass

                if gps_data:
                    exif_data.update(gps_data)
        except Exception:
            pass  # GPS gracefully skipped

        if exif_data:
            result["exif"] = exif_data

        # --- IPTC (via APP13 / photoshop block) ---
        try:
            photoshop_data = img.info.get("photoshop") or img.info.get("APP13")
            if photoshop_data and isinstance(photoshop_data, (bytes, bytearray)):
                iptc_data: dict = {}
                data = bytes(photoshop_data)
                i = 0
                while i < len(data) - 4:
                    # IPTC marker: 0x1C followed by record number and dataset number
                    if data[i] == 0x1C and data[i + 1] == 0x02:
                        dataset = data[i + 2]
                        length = (data[i + 3] << 8) | data[i + 4]
                        value_bytes = data[i + 5: i + 5 + length]
                        try:
                            value = value_bytes.decode("utf-8", errors="replace").strip()
                        except Exception:
                            value = ""
                        if dataset == 120 and value:   # Caption/Abstract
                            iptc_data["caption"] = value
                        elif dataset == 25 and value:  # Keywords (can repeat)
                            iptc_data.setdefault("keywords", []).append(value)
                        elif dataset == 116 and value: # Copyright Notice
                            iptc_data["copyright"] = value
                        elif dataset == 90 and value:  # City
                            iptc_data["city"] = value
                        elif dataset == 101 and value: # Country
                            iptc_data["country"] = value
                        i += 5 + length
                    else:
                        i += 1
                if iptc_data:
                    result["iptc"] = iptc_data
        except Exception:
            pass  # IPTC gracefully skipped

    except Exception as e:
        log.debug("extract_image_metadata_failed", error=str(e))

    return result


def extract_images_from_docx(file_path: Path) -> list[dict]:
    """
    Extrahiert eingebettete Bilder aus einer DOCX-Datei.

    DOCX ist intern ein ZIP-Archiv. Bilder liegen in word/media/*.

    Args:
        file_path: Pfad zur DOCX-Datei

    Returns:
        Liste von Dicts mit 'name', 'data' (bytes), 'position_hint' (Index)
    """
    images: list[dict] = []
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            media_files = [
                name for name in zf.namelist()
                if name.startswith("word/media/") and not name.endswith("/")
            ]
            for idx, media_name in enumerate(sorted(media_files)):
                try:
                    data = zf.read(media_name)
                    img = Image.open(io.BytesIO(data))
                    width, height = img.size
                    if width < MIN_IMAGE_SIZE_PX or height < MIN_IMAGE_SIZE_PX:
                        log.debug(
                            "skip_small_image_docx",
                            name=media_name,
                            width=width,
                            height=height,
                        )
                        continue
                    images.append({
                        "name": Path(media_name).name,
                        "data": data,
                        "position_hint": idx,
                    })
                except Exception as e:
                    log.warning("docx_image_read_error", name=media_name, error=str(e))
    except Exception as e:
        log.error("docx_open_error", file=str(file_path), error=str(e))
    log.info("docx_images_extracted", file=str(file_path), count=len(images))
    return images


def extract_images_from_pptx(file_path: Path) -> list[dict]:
    """
    Extrahiert eingebettete Bilder aus einer PPTX-Datei.

    PPTX ist intern ein ZIP-Archiv. Bilder liegen in ppt/media/*.

    Args:
        file_path: Pfad zur PPTX-Datei

    Returns:
        Liste von Dicts mit 'name', 'data' (bytes), 'slide_number' (Index)
    """
    images: list[dict] = []
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            media_files = [
                name for name in zf.namelist()
                if name.startswith("ppt/media/") and not name.endswith("/")
            ]
            for idx, media_name in enumerate(sorted(media_files)):
                try:
                    data = zf.read(media_name)
                    img = Image.open(io.BytesIO(data))
                    width, height = img.size
                    if width < MIN_IMAGE_SIZE_PX or height < MIN_IMAGE_SIZE_PX:
                        log.debug(
                            "skip_small_image_pptx",
                            name=media_name,
                            width=width,
                            height=height,
                        )
                        continue
                    images.append({
                        "name": Path(media_name).name,
                        "data": data,
                        "slide_number": idx + 1,
                    })
                except Exception as e:
                    log.warning("pptx_image_read_error", name=media_name, error=str(e))
    except Exception as e:
        log.error("pptx_open_error", file=str(file_path), error=str(e))
    log.info("pptx_images_extracted", file=str(file_path), count=len(images))
    return images


async def classify_image_type(image_data: bytes, mimetype: str) -> str:
    """
    Klassifiziert ein Bild via Mistral Vision in eine von fünf Kategorien.

    Schickt das Bild an die Vision-API mit einem präzisen Klassifizierungs-Prompt
    und gibt genau eine Kategorie zurück.

    Args:
        image_data: Rohe Bildbytes.
        mimetype: MIME-Typ des Bildes (z.B. 'image/png').
        analyze_with_mistral_vision: Callable aus server.py (injiziert als Parameter).

    Returns:
        Einer der Werte: 'photo', 'chart', 'diagram', 'text_scan', 'decorative'.
        Fallback: 'photo' bei Fehler oder unbekannter Antwort.
    """
    try:
        from templates_db import get_prompt as _get_prompt  # noqa: PLC0415
        prompt = _get("get_prompt", _get_prompt)("image", "classify", language="en")
    except Exception:
        prompt = (
            "Classify this image into EXACTLY one category. Reply with ONE word only:\n\n"
            "photo      = photograph of real-world objects, people, places\n"
            "chart      = bar chart, line chart, pie chart, data visualization with axes/values\n"
            "diagram    = flowchart, org chart, mind map, network diagram, UML, architecture diagram\n"
            "text_scan  = image of a document, form, invoice, letter, or any image where text is the primary content\n"
            "decorative = logo, icon, background image, decorative graphic with no information value\n\n"
            "Reply with exactly one of: photo, chart, diagram, text_scan, decorative"
        )
    valid_types = {"photo", "chart", "diagram", "text_scan", "decorative"}

    log.info("classify_image_type_start", size=len(image_data), mimetype=mimetype)

    result = await _get("analyze_with_mistral_vision", analyze_with_mistral_vision)(image_data, mimetype, prompt, language="en")
    if not result.get("success"):
        log.warning(
            "classify_image_type_failed",
            error=result.get("error"),
            fallback="photo",
        )
        return "photo"

    raw: str = result.get("markdown", "").strip().lower()
    # Extrahiere erstes Wort aus der Antwort (falls Modell mehr zurückgibt)
    first_word = raw.split()[0] if raw.split() else ""
    image_type = first_word if first_word in valid_types else "photo"

    log.info("classify_image_type_result", raw_response=raw, image_type=image_type)
    return image_type


async def convert_diagram_to_mermaid(
    image_data: bytes,
    mimetype: str,
) -> str:
    """
    Konvertiert ein Diagramm/Flowchart-Bild in Mermaid-Syntax via Vision-API.

    Nutzt einen spezialisierten Prompt um Flowcharts und andere Diagramme
    als valide Mermaid-Syntax zu extrahieren.

    Args:
        image_data: Rohe Bildbytes des Diagramms.
        mimetype: MIME-Typ des Bildes (z.B. 'image/png').
        analyze_with_mistral_vision: Callable aus server.py (injiziert als Parameter).
        strip_llm_artifacts: Callable aus server.py (injiziert als Parameter).

    Returns:
        Mermaid-Code-Block (```mermaid ... ```) oder Fallback-Text bei Fehler.
    """
    try:
        from templates_db import get_prompt as _get_prompt  # noqa: PLC0415
        prompt = _get("get_prompt", _get_prompt)("image", "mermaid", language="en")
    except Exception:
        prompt = (
            "Convert this diagram image into valid Mermaid syntax.\n\n"
            "Choose the appropriate diagram type:\n"
            "- Flowchart/decision tree → graph TD\n"
            "- Sequence diagram → sequenceDiagram\n"
            "- Class diagram → classDiagram\n"
            "- Org chart → graph TD with descriptive node labels\n\n"
            "Rules:\n"
            "- Output ONLY the Mermaid code inside ```mermaid ... ``` fences\n"
            "- Use exact labels from the image — do not invent labels\n"
            "- If the image cannot be represented as Mermaid: output ```mermaid\\ngraph TD\\n    A[Nicht darstellbar]\\n```\n"
            "- No explanations, no text outside the code block"
        )

    log.info("convert_diagram_to_mermaid_start", size=len(image_data), mimetype=mimetype)

    result = await _get("analyze_with_mistral_vision", analyze_with_mistral_vision)(image_data, mimetype, prompt, language="en")
    if not result.get("success"):
        log.warning(
            "convert_diagram_to_mermaid_failed",
            error=result.get("error"),
        )
        return "[Diagramm-Konvertierung nicht verfügbar]"

    mermaid_output: str = strip_llm_artifacts(result.get("markdown", "").strip())
    log.info("convert_diagram_to_mermaid_success", output_length=len(mermaid_output))
    return mermaid_output


async def extract_chart_data(
    image_data: bytes,
    mimetype: str,
) -> str:
    """
    Extrahiert Daten aus einem Chart/Diagramm-Bild als Markdown-Tabelle.

    Nutzt einen spezialisierten Prompt um Achsenbeschriftungen, Datenpunkte
    und Legenden aus Balken-, Linien- und Kreisdiagrammen zu extrahieren.

    Args:
        image_data: Rohe Bildbytes des Charts.
        mimetype: MIME-Typ des Bildes (z.B. 'image/png').
        analyze_with_mistral_vision: Callable aus server.py (injiziert als Parameter).
        strip_llm_artifacts: Callable aus server.py (injiziert als Parameter).

    Returns:
        Markdown-Tabelle mit den extrahierten Daten oder Fallback-Text bei Fehler.
    """
    try:
        from templates_db import get_prompt as _get_prompt  # noqa: PLC0415
        prompt = _get("get_prompt", _get_prompt)("image", "chart", language="en")
    except Exception:
        prompt = (
            "Extract all data from this chart as a Markdown table.\n\n"
            "Instructions:\n"
            "- For bar/line charts: columns = X-axis label + one column per data series; rows = data points\n"
            "- For pie/donut charts: columns = Category | Value | Percentage\n"
            "- Use exact axis labels and legend entries as column headers\n"
            "- If exact values are not readable, use estimates with ~ prefix (e.g. ~42)\n"
            "- If no chart data found: output only [No chart data found]\n\n"
            "Output ONLY the Markdown table. No explanations, no introductions."
        )

    log.info("extract_chart_data_start", size=len(image_data), mimetype=mimetype)

    result = await _get("analyze_with_mistral_vision", analyze_with_mistral_vision)(image_data, mimetype, prompt, language="en")
    if not result.get("success"):
        log.warning(
            "extract_chart_data_failed",
            error=result.get("error"),
        )
        return "[Daten-Extraktion nicht verfügbar]"

    table_output: str = strip_llm_artifacts(result.get("markdown", "").strip())
    log.info("extract_chart_data_success", output_length=len(table_output))
    return table_output


async def describe_embedded_images(
    images: list[dict],
    language: str = "de",
) -> list[dict]:
    """
    Beschreibt eine Liste extrahierter Bilder via Mistral Pixtral Vision.

    Bilder werden zuerst klassifiziert (AC-004-1):
    - 'diagram' → Mermaid-Syntax-Konvertierung (AC-004-2)
    - 'chart'   → Datentabellen-Extraktion (AC-004-3)
    - 'photo'   → generische Vision-Beschreibung
    - 'text_scan' → generische Vision-Beschreibung (Text-Extraktion)
    - 'decorative' → wird übersprungen

    Args:
        images: Liste von Dicts mit 'name' und 'data' (bytes) aus extract_images_from_*()
        language: Antwortsprache ('de' oder 'en')

    Returns:
        Liste von Dicts mit 'name', 'description', 'tokens', 'image_type'
    """
    results: list[dict] = []
    for image in images:
        name = image["name"]
        data = image["data"]
        mimetype = _get("detect_mimetype_from_bytes", detect_mimetype_from_bytes)(data) or "image/png"
        log.info("describing_embedded_image", name=name, size=len(data))

        # AC-004-1: Bild klassifizieren
        image_type = await _get("classify_image_type", classify_image_type)(data, mimetype)
        log.info("image_classified", name=name, image_type=image_type)

        if image_type == "decorative":
            # Dekorative Bilder überspringen
            log.info("skip_decorative_image", name=name)
            continue

        if image_type == "diagram":
            # AC-004-2: Flowcharts/Organigramme → Mermaid
            description = await _get("convert_diagram_to_mermaid", convert_diagram_to_mermaid)(data, mimetype)
            results.append({
                "name": name,
                "description": description,
                "tokens": 0,
                "image_type": image_type,
            })
            continue

        if image_type == "chart":
            # AC-004-3: Balken-/Linien-/Kreisdiagramme → Datentabelle
            description = await _get("extract_chart_data", extract_chart_data)(data, mimetype)
            results.append({
                "name": name,
                "description": description,
                "tokens": 0,
                "image_type": image_type,
            })
            continue

        # 'photo' und 'text_scan': differenzierte Vision-Beschreibung
        try:
            from templates_db import get_prompt as _get_prompt  # noqa: PLC0415
        except Exception:
            _get_prompt = None  # type: ignore[assignment]

        if image_type == "text_scan":
            _prompt_name = "text_scan_de" if language == "de" else "text_scan_en"
            if _get_prompt:
                try:
                    generic_prompt = _get("get_prompt", _get_prompt)("image", _prompt_name, language=language)
                except Exception:
                    generic_prompt = (
                        "Extrahiere den gesamten sichtbaren Text aus diesem Bild. "
                        "Gib ihn strukturiert als Markdown wieder. Übersetze nicht."
                        if language == "de"
                        else "Extract all visible text from this image. "
                             "Return it as structured Markdown. Do not translate."
                    )
            else:
                generic_prompt = (
                    "Extrahiere den gesamten sichtbaren Text aus diesem Bild. "
                    "Gib ihn strukturiert als Markdown wieder. Übersetze nicht."
                    if language == "de"
                    else "Extract all visible text from this image. "
                         "Return it as structured Markdown. Do not translate."
                )
        else:  # photo oder unbekannt
            _prompt_name = "photo_de" if language == "de" else "photo_en"
            if _get_prompt:
                try:
                    generic_prompt = _get("get_prompt", _get_prompt)("image", _prompt_name, language=language)
                except Exception:
                    generic_prompt = (
                        "Beschreibe dieses Bild präzise: was ist zu sehen, relevante Beschriftungen, "
                        "erkennbare Objekte und der Gesamtkontext. Format: kurzer Absatz."
                        if language == "de"
                        else "Describe this image precisely: what is shown, relevant labels, "
                             "recognizable objects and overall context. Format: short paragraph."
                    )
            else:
                generic_prompt = (
                    "Beschreibe dieses Bild präzise: was ist zu sehen, relevante Beschriftungen, "
                    "erkennbare Objekte und der Gesamtkontext. Format: kurzer Absatz."
                    if language == "de"
                    else "Describe this image precisely: what is shown, relevant labels, "
                         "recognizable objects and overall context. Format: short paragraph."
                )

        result = await _get("analyze_with_mistral_vision", analyze_with_mistral_vision)(data, mimetype, generic_prompt, language)
        if result["success"]:
            results.append({
                "name": name,
                "description": result["markdown"],
                "tokens": result.get("tokens_total", 0),
                "image_type": image_type,
            })
        else:
            log.warning(
                "embedded_image_description_failed",
                name=name,
                error=result.get("error"),
            )
            results.append({
                "name": name,
                "description": f"[Bildbeschreibung nicht verfügbar: {result.get('error', 'Unbekannter Fehler')}]",
                "tokens": 0,
                "image_type": image_type,
            })
    return results


def insert_image_descriptions(markdown: str, descriptions: list[dict]) -> str:
    """
    Ersetzt Bild-Platzhalter im Markdown durch Pixtral-Beschreibungen.

    Markitdown erzeugt Platzhalter wie ![image](image1.png) für eingebettete Bilder.
    Diese Funktion ersetzt sie durch einen beschreibenden Blockquote.

    Args:
        markdown: Konvertierter Markdown-Text mit Bild-Platzhaltern
        descriptions: Liste von Dicts mit 'name' und 'description'

    Returns:
        Markdown mit eingefügten Bildbeschreibungen
    """
    # Baue Lookup: Dateiname → Beschreibung
    desc_map: dict[str, str] = {d["name"]: d["description"] for d in descriptions}

    def replace_placeholder(match: re.Match) -> str:
        alt_text = match.group(1)
        img_ref = match.group(2)
        # img_ref kann ein Dateiname oder Pfad sein
        img_name = Path(img_ref).name
        if img_name in desc_map:
            description = desc_map[img_name]
            return f"> **[Bild: {img_name}]** {description}"
        # Kein passender Eintrag → Original behalten
        return match.group(0)

    # Muster: ![alt](ref) — erfasst Bild-Platzhalter
    pattern = r"!\[([^\]]*)\]\(([^)]+)\)"
    return re.sub(pattern, replace_placeholder, markdown)


def render_first_page_as_image(file_path: Path) -> Optional[tuple[bytes, str]]:
    """Rendert die erste Seite eines PDFs als Bild für Dual-Pass Validierung.

    Args:
        file_path: Pfad zur PDF-Datei.

    Returns:
        Tuple (image_bytes, mimetype) oder None wenn nicht verfügbar/fehlgeschlagen.
    """
    try:
        if not _get("PDF2IMAGE_AVAILABLE", PDF2IMAGE_AVAILABLE):
            return None
        images = _get("convert_from_path", convert_from_path)(str(file_path), dpi=PDF_RENDER_DPI, first_page=1, last_page=1)
        if not images:
            return None
        first_page = images[0]
        if first_page.width > IMAGE_MAX_WIDTH:
            ratio = IMAGE_MAX_WIDTH / first_page.width
            new_height = int(first_page.height * ratio)
            first_page = first_page.resize(
                (IMAGE_MAX_WIDTH, new_height), Image.Resampling.LANCZOS
            )
        buf = io.BytesIO()
        first_page.save(buf, format="PNG")
        return buf.getvalue(), "image/png"
    except Exception as e:
        log.warning("render_first_page_failed", error=str(e))
        return None
