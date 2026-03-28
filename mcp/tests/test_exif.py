"""
Unit-Tests für T-MKIT-027: EXIF/GPS/IPTC Metadaten aus Bildern extrahieren.

Tests prüfen:
- Kamera-Infos aus EXIF
- Aufnahme-Datum aus EXIF
- GPS-Koordinaten DMS → Dezimal-Konvertierung
- Negative GPS-Koordinaten (Süd/West)
- Bilder ohne EXIF → leeres dict
- meta.exif wird bei Bild-Konvertierung gesetzt
- GPS-Koordinaten landen im exif dict
- Nicht-Bilder (PDFs etc.) produzieren kein EXIF
"""

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from conftest import load_server_module, run_async, PNG_100x100, REAL_PIL_IMAGE


# =============================================================================
# Server-Modul laden — echtes PIL für EXIF-Tests
# =============================================================================

_server = load_server_module(use_real_pil=True)

extract_image_metadata = _server.extract_image_metadata
_dms_to_decimal = _server._dms_to_decimal
convert_auto = _server.convert_auto


# =============================================================================
# Hilfsfunktionen
# =============================================================================

def _make_exif_mock(tags: dict, gps_ifd: dict | None = None):
    """
    Erstellt einen Mock der img.getexif() Rückgabe.

    Args:
        tags: Dict mit EXIF-Tag-Nummer → Wert
        gps_ifd: Dict mit GPS-IFD-Einträgen (None = kein GPS)
    """
    exif_mock = MagicMock()
    exif_mock.get = MagicMock(side_effect=lambda key, *args: tags.get(key))
    if gps_ifd is not None:
        exif_mock.get_ifd = MagicMock(side_effect=lambda tag: gps_ifd if tag == 34853 else {})
    else:
        exif_mock.get_ifd = MagicMock(return_value={})
    return exif_mock


def _make_pil_image_mock(exif_mock, info: dict | None = None):
    """Erstellt einen Mock für PIL.Image.open() Rückgabe."""
    img_mock = MagicMock()
    img_mock.getexif = MagicMock(return_value=exif_mock)
    img_mock.info = info or {}
    return img_mock


# =============================================================================
# Unit-Tests: _dms_to_decimal
# =============================================================================

class TestDmsToDecimal:
    """Direkte Tests der DMS-zu-Dezimal-Konvertierungsfunktion."""

    def test_north_positive(self):
        """Nördliche Breite ist positiv."""
        result = _dms_to_decimal((51, 11, 23.64), "N")
        assert result > 0
        assert abs(result - 51.189900) < 0.01

    def test_east_positive(self):
        """Östliche Länge ist positiv."""
        result = _dms_to_decimal((6, 26, 29.76), "E")
        assert result > 0

    def test_south_negative(self):
        """Südliche Breite ist negativ."""
        result = _dms_to_decimal((33, 52, 4.0), "S")
        assert result < 0
        assert abs(result - (-33.867778)) < 0.01

    def test_west_negative(self):
        """Westliche Länge ist negativ."""
        result = _dms_to_decimal((70, 40, 0.0), "W")
        assert result < 0

    def test_zero_degrees(self):
        """Nullpunkt-Koordinate wird korrekt konvertiert."""
        result = _dms_to_decimal((0, 0, 0), "N")
        assert result == 0.0

    def test_result_precision(self):
        """Ergebnis wird auf 6 Dezimalstellen gerundet."""
        result = _dms_to_decimal((48, 51, 29.0), "N")
        # Check that it has at most 6 decimal places
        assert len(str(result).split(".")[-1]) <= 6


# =============================================================================
# Unit-Tests: extract_image_metadata — EXIF
# =============================================================================

class TestExtractExifCamera:
    """test_extract_exif_camera: Kamera-Make und -Model werden extrahiert."""

    def test_camera_make_extracted(self):
        """camera_make wird korrekt aus Tag 271 gelesen."""
        exif_mock = _make_exif_mock({271: "Canon", 272: "EOS 5D"})
        img_mock = _make_pil_image_mock(exif_mock)

        with patch.object(REAL_PIL_IMAGE, "open", return_value=img_mock):
            result = extract_image_metadata(PNG_100x100)

        assert "exif" in result
        assert result["exif"]["camera_make"] == "Canon"

    def test_camera_model_extracted(self):
        """camera_model wird korrekt aus Tag 272 gelesen."""
        exif_mock = _make_exif_mock({271: "Nikon", 272: "D850"})
        img_mock = _make_pil_image_mock(exif_mock)

        with patch.object(REAL_PIL_IMAGE, "open", return_value=img_mock):
            result = extract_image_metadata(PNG_100x100)

        assert "exif" in result
        assert result["exif"]["camera_model"] == "D850"

    def test_software_extracted(self):
        """software wird korrekt aus Tag 305 gelesen."""
        exif_mock = _make_exif_mock({305: "Lightroom 12.0"})
        img_mock = _make_pil_image_mock(exif_mock)

        with patch.object(REAL_PIL_IMAGE, "open", return_value=img_mock):
            result = extract_image_metadata(PNG_100x100)

        assert "exif" in result
        assert result["exif"]["software"] == "Lightroom 12.0"


class TestExtractExifDatetime:
    """test_extract_exif_datetime: Aufnahme-Datum wird extrahiert."""

    def test_datetime_original_extracted(self):
        """datetime_original wird korrekt aus Tag 36867 gelesen."""
        exif_mock = _make_exif_mock({36867: "2023:08:15 14:30:00"})
        img_mock = _make_pil_image_mock(exif_mock)

        with patch.object(REAL_PIL_IMAGE, "open", return_value=img_mock):
            result = extract_image_metadata(PNG_100x100)

        assert "exif" in result
        assert result["exif"]["datetime_original"] == "2023:08:15 14:30:00"

    def test_datetime_digitized_extracted(self):
        """datetime_digitized wird korrekt aus Tag 36868 gelesen."""
        exif_mock = _make_exif_mock({36868: "2023:08:15 14:30:05"})
        img_mock = _make_pil_image_mock(exif_mock)

        with patch.object(REAL_PIL_IMAGE, "open", return_value=img_mock):
            result = extract_image_metadata(PNG_100x100)

        assert "exif" in result
        assert result["exif"]["datetime_digitized"] == "2023:08:15 14:30:05"

    def test_no_datetime_if_not_in_exif(self):
        """datetime_original nicht gesetzt wenn Tag fehlt."""
        exif_mock = _make_exif_mock({271: "Sony"})
        img_mock = _make_pil_image_mock(exif_mock)

        with patch.object(REAL_PIL_IMAGE, "open", return_value=img_mock):
            result = extract_image_metadata(PNG_100x100)

        assert "exif" in result
        assert "datetime_original" not in result["exif"]


# =============================================================================
# Unit-Tests: extract_image_metadata — GPS
# =============================================================================

class TestExtractGpsCoordinates:
    """test_extract_gps_coordinates: GPS DMS → Dezimal-Konvertierung."""

    def test_gps_latitude_set(self):
        """GPS-Breitengrad wird als Dezimalzahl gesetzt."""
        gps_ifd = {
            1: "N",
            2: (51, 11, 23.64),
            3: "E",
            4: (6, 26, 29.76),
        }
        exif_mock = _make_exif_mock({}, gps_ifd=gps_ifd)
        img_mock = _make_pil_image_mock(exif_mock)

        with patch.object(REAL_PIL_IMAGE, "open", return_value=img_mock):
            result = extract_image_metadata(PNG_100x100)

        assert "exif" in result
        assert "latitude" in result["exif"]
        lat = result["exif"]["latitude"]
        assert isinstance(lat, float)
        assert lat > 0  # Nördlich

    def test_gps_longitude_set(self):
        """GPS-Längengrad wird als Dezimalzahl gesetzt."""
        gps_ifd = {
            1: "N",
            2: (51, 11, 23.64),
            3: "E",
            4: (6, 26, 29.76),
        }
        exif_mock = _make_exif_mock({}, gps_ifd=gps_ifd)
        img_mock = _make_pil_image_mock(exif_mock)

        with patch.object(REAL_PIL_IMAGE, "open", return_value=img_mock):
            result = extract_image_metadata(PNG_100x100)

        assert "exif" in result
        assert "longitude" in result["exif"]
        lon = result["exif"]["longitude"]
        assert isinstance(lon, float)
        assert lon > 0  # Östlich

    def test_gps_altitude_set(self):
        """GPS-Höhe wird als float gesetzt."""
        gps_ifd = {
            1: "N",
            2: (48, 51, 29.0),
            3: "E",
            4: (2, 21, 5.0),
            5: 0,    # Above sea level
            6: 35.0, # 35 meters
        }
        exif_mock = _make_exif_mock({}, gps_ifd=gps_ifd)
        img_mock = _make_pil_image_mock(exif_mock)

        with patch.object(REAL_PIL_IMAGE, "open", return_value=img_mock):
            result = extract_image_metadata(PNG_100x100)

        assert "exif" in result
        assert "altitude" in result["exif"]
        assert result["exif"]["altitude"] == 35.0


class TestExtractGpsSouthWest:
    """test_extract_gps_south_west: Negative Koordinaten für S/W."""

    def test_southern_latitude_is_negative(self):
        """Südliche Breite (S) wird als negative Zahl gesetzt."""
        gps_ifd = {
            1: "S",
            2: (33, 52, 4.0),
            3: "E",
            4: (151, 12, 26.0),
        }
        exif_mock = _make_exif_mock({}, gps_ifd=gps_ifd)
        img_mock = _make_pil_image_mock(exif_mock)

        with patch.object(REAL_PIL_IMAGE, "open", return_value=img_mock):
            result = extract_image_metadata(PNG_100x100)

        assert "exif" in result
        assert result["exif"]["latitude"] < 0

    def test_western_longitude_is_negative(self):
        """Westliche Länge (W) wird als negative Zahl gesetzt."""
        gps_ifd = {
            1: "N",
            2: (40, 42, 46.0),
            3: "W",
            4: (74, 0, 21.0),
        }
        exif_mock = _make_exif_mock({}, gps_ifd=gps_ifd)
        img_mock = _make_pil_image_mock(exif_mock)

        with patch.object(REAL_PIL_IMAGE, "open", return_value=img_mock):
            result = extract_image_metadata(PNG_100x100)

        assert "exif" in result
        assert result["exif"]["longitude"] < 0

    def test_below_sea_level_altitude_is_negative(self):
        """Altitude unter Meeresspiegel (alt_ref=1) ist negativ."""
        gps_ifd = {
            1: "N",
            2: (31, 46, 0.0),
            3: "E",
            4: (35, 30, 0.0),
            5: 1,     # Below sea level
            6: 430.5, # 430.5 m below
        }
        exif_mock = _make_exif_mock({}, gps_ifd=gps_ifd)
        img_mock = _make_pil_image_mock(exif_mock)

        with patch.object(REAL_PIL_IMAGE, "open", return_value=img_mock):
            result = extract_image_metadata(PNG_100x100)

        assert "exif" in result
        assert result["exif"]["altitude"] < 0


# =============================================================================
# Unit-Tests: extract_image_metadata — kein EXIF
# =============================================================================

class TestExtractNoExif:
    """test_extract_no_exif: Bild ohne EXIF liefert leeres dict."""

    def test_no_exif_returns_empty_dict(self):
        """Wenn kein EXIF vorhanden, wird leeres dict zurückgegeben."""
        exif_mock = _make_exif_mock({})  # Keine Tags
        img_mock = _make_pil_image_mock(exif_mock)

        with patch.object(REAL_PIL_IMAGE, "open", return_value=img_mock):
            result = extract_image_metadata(PNG_100x100)

        assert result == {}

    def test_broken_image_returns_empty_dict(self):
        """Bei kaputten Bilddaten wird leeres dict zurückgegeben (keine Exception)."""
        result = extract_image_metadata(b"not-an-image")
        assert isinstance(result, dict)

    def test_empty_bytes_returns_empty_dict(self):
        """Bei leeren Bytes wird leeres dict zurückgegeben (keine Exception)."""
        result = extract_image_metadata(b"")
        assert isinstance(result, dict)


# =============================================================================
# Integration-Tests: convert_auto setzt meta.exif
# =============================================================================

class TestExifInMeta:
    """test_exif_in_meta: meta.exif wird bei Bild-Konvertierung gesetzt."""

    def test_meta_exif_set_when_exif_present(self):
        """meta.exif wird gesetzt wenn EXIF-Daten vorhanden sind."""
        exif_mock = _make_exif_mock({271: "TestCam", 272: "Model X"})
        img_mock = _make_pil_image_mock(exif_mock)

        vision_result = {
            "success": True,
            "markdown": "# Test Image",
            "vision_model": "pixtral-12b",
            "tokens_prompt": 10,
            "tokens_completion": 20,
            "tokens_total": 30,
        }

        with patch.object(_server, "analyze_with_mistral_vision", return_value=vision_result), \
             patch.object(_server, "resize_image_if_needed", return_value=(PNG_100x100, {"width": 100, "height": 100})), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch.object(_server, "calculate_quality_score", return_value={}), \
             patch.object(REAL_PIL_IMAGE, "open", return_value=img_mock):
            response = run_async(convert_auto(
                file_data=PNG_100x100,
                filename="photo.jpg",
                source="test",
                source_type="base64",
                input_meta={},
            ))

        assert response.success is True
        assert response.meta.exif is not None
        assert response.meta.exif.get("camera_make") == "TestCam"
        assert response.meta.exif.get("camera_model") == "Model X"

    def test_meta_exif_none_when_no_exif(self):
        """meta.exif bleibt None wenn keine EXIF-Daten vorhanden."""
        exif_mock = _make_exif_mock({})
        img_mock = _make_pil_image_mock(exif_mock)

        vision_result = {
            "success": True,
            "markdown": "# Plain Image",
            "vision_model": "pixtral-12b",
            "tokens_prompt": 5,
            "tokens_completion": 10,
            "tokens_total": 15,
        }

        with patch.object(_server, "analyze_with_mistral_vision", return_value=vision_result), \
             patch.object(_server, "resize_image_if_needed", return_value=(PNG_100x100, {"width": 100, "height": 100})), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch.object(_server, "calculate_quality_score", return_value={}), \
             patch.object(REAL_PIL_IMAGE, "open", return_value=img_mock):
            response = run_async(convert_auto(
                file_data=PNG_100x100,
                filename="plain.png",
                source="test",
                source_type="base64",
                input_meta={},
            ))

        assert response.success is True
        assert response.meta.exif is None


class TestGpsInExif:
    """test_gps_in_exif: GPS-Koordinaten landen im exif dict."""

    def test_gps_coordinates_in_meta_exif(self):
        """meta.exif enthält latitude und longitude wenn GPS vorhanden."""
        gps_ifd = {
            1: "N",
            2: (51, 11, 0.0),
            3: "E",
            4: (6, 26, 0.0),
        }
        exif_mock = _make_exif_mock({271: "Sony"}, gps_ifd=gps_ifd)
        img_mock = _make_pil_image_mock(exif_mock)

        vision_result = {
            "success": True,
            "markdown": "# GPS Image",
            "vision_model": "pixtral-12b",
            "tokens_prompt": 5,
            "tokens_completion": 10,
            "tokens_total": 15,
        }

        with patch.object(_server, "analyze_with_mistral_vision", return_value=vision_result), \
             patch.object(_server, "resize_image_if_needed", return_value=(PNG_100x100, {"width": 100, "height": 100})), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch.object(_server, "calculate_quality_score", return_value={}), \
             patch.object(REAL_PIL_IMAGE, "open", return_value=img_mock):
            response = run_async(convert_auto(
                file_data=PNG_100x100,
                filename="gps_photo.jpg",
                source="test",
                source_type="base64",
                input_meta={},
            ))

        assert response.success is True
        assert response.meta.exif is not None
        assert "latitude" in response.meta.exif
        assert "longitude" in response.meta.exif
        assert response.meta.exif["latitude"] > 0   # North
        assert response.meta.exif["longitude"] > 0  # East


# =============================================================================
# Unit-Tests: Nicht-Bilder produzieren kein EXIF via extract_image_metadata
# =============================================================================

class TestNonImageNoExif:
    """test_non_image_no_exif: Nicht-Bild-Bytes liefern leeres dict."""

    def test_pdf_bytes_return_empty_exif(self):
        """PDF-Bytes produzieren keine EXIF-Metadaten."""
        fake_pdf = b"%PDF-1.4 fake content"
        result = extract_image_metadata(fake_pdf)
        # PDF is not a valid image — PIL.Image.open will fail → graceful empty dict
        assert isinstance(result, dict)
        assert "exif" not in result

    def test_text_bytes_return_empty_exif(self):
        """Textdatei-Bytes produzieren keine EXIF-Metadaten."""
        fake_txt = b"Hello, this is a text document."
        result = extract_image_metadata(fake_txt)
        assert isinstance(result, dict)
        assert "exif" not in result

    def test_random_bytes_return_empty_exif(self):
        """Beliebige Bytes die kein Bild sind liefern leeres dict (keine Exception)."""
        import os
        random_data = os.urandom(256)
        result = extract_image_metadata(random_data)
        assert isinstance(result, dict)

    def test_extract_only_called_for_images_in_convert_auto(self):
        """extract_image_metadata wird nur für Bilder aufgerufen — nicht für PDFs."""
        # We verify this indirectly: for PDF input, convert_auto should not call
        # extract_image_metadata (since it's in the image branch only).
        # We mock extract_image_metadata and confirm it is NOT called for PDF.
        fake_pdf = b"%PDF-1.4 fake"

        markitdown_result = MagicMock()
        markitdown_result.text_content = "# PDF"

        with patch.object(_server, "extract_image_metadata") as mock_extract, \
             patch.object(_server, "detect_mimetype_from_bytes", return_value=None), \
             patch.object(_server, "md") as mock_md:
            mock_md.convert_stream.return_value = markitdown_result
            run_async(convert_auto(
                file_data=fake_pdf,
                filename="document.pdf",
                source="test",
                source_type="base64",
                input_meta={},
            ))

        # extract_image_metadata must NOT have been called for PDF
        mock_extract.assert_not_called()
