"""
Tests für T-DAI-008: Prompts und Scoring-Gewichtungen in SQLite-DB.

Alle Tests laufen ohne Docker-Container und ohne echte API-Calls.
DB wird in tmp_path angelegt und mit seed.sql befüllt.
"""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import load_server_module


# ---------------------------------------------------------------------------
# Server laden und DB-Hilfsfunktionen extrahieren
# ---------------------------------------------------------------------------

_server = load_server_module(use_real_pil=False)


# ---------------------------------------------------------------------------
# Fixture: Temporäre DB mit vollständiger Initialisierung (inkl. Seed)
# ---------------------------------------------------------------------------

@pytest.fixture
def initialized_db(tmp_path):
    """Legt eine temporäre DB an, führt init_templates_db() aus (inkl. seed.sql-Seeding)."""
    db_path = tmp_path / "test_prompts.db"
    with patch.object(_server, "TEMPLATES_DB_PATH", db_path):
        _server.init_templates_db()
        yield db_path


# ---------------------------------------------------------------------------
# Hilfsfunktion: get_prompt und get_scoring_weight direkt aus server-Namespace
# ---------------------------------------------------------------------------

def _get_prompt(db_path, category, name, language="de"):
    """Ruft get_prompt mit gepatchtem DB-Pfad auf."""
    with patch.object(_server, "TEMPLATES_DB_PATH", db_path):
        return _server.get_prompt(category, name, language)


def _get_scoring_weight(db_path, weight_name):
    """Ruft get_scoring_weight mit gepatchtem DB-Pfad auf."""
    with patch.object(_server, "TEMPLATES_DB_PATH", db_path):
        return _server.get_scoring_weight(weight_name)


def _upsert_prompt(db_path, prompt_id, category, name, content_de=None, content_en=None):
    """Ruft upsert_prompt mit gepatchtem DB-Pfad auf."""
    with patch.object(_server, "TEMPLATES_DB_PATH", db_path):
        return _server.upsert_prompt(prompt_id, category, name, content_de, content_en)


# ---------------------------------------------------------------------------
# Tests: Tabellen-Initialisierung
# ---------------------------------------------------------------------------

class TestInitPromptsTables:
    """Tests dass die neuen Tabellen korrekt erstellt werden."""

    def test_prompts_table_created(self, initialized_db):
        """prompt-Tabelle wird von init_templates_db erstellt."""
        conn = sqlite3.connect(str(initialized_db))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='prompt'"
        )
        result = cursor.fetchone()
        conn.close()
        assert result is not None, "prompt-Tabelle sollte existieren"

    def test_scoring_weights_table_created(self, initialized_db):
        """scoring_weight-Tabelle wird von init_templates_db erstellt."""
        conn = sqlite3.connect(str(initialized_db))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scoring_weight'"
        )
        result = cursor.fetchone()
        conn.close()
        assert result is not None, "scoring_weight-Tabelle sollte existieren"

    def test_prompts_table_has_required_columns(self, initialized_db):
        """prompt-Tabelle hat alle benötigten Spalten."""
        conn = sqlite3.connect(str(initialized_db))
        cursor = conn.execute("PRAGMA table_info(prompt)")
        cols = {row[1] for row in cursor.fetchall()}
        conn.close()
        required = {"id", "category", "name", "content_de", "content_en", "version", "created_at", "updated_at"}
        assert required.issubset(cols), f"Fehlende Spalten: {required - cols}"

    def test_scoring_weights_table_has_required_columns(self, initialized_db):
        """scoring_weight-Tabelle hat alle benötigten Spalten."""
        conn = sqlite3.connect(str(initialized_db))
        cursor = conn.execute("PRAGMA table_info(scoring_weight)")
        cols = {row[1] for row in cursor.fetchall()}
        conn.close()
        required = {"id", "name", "value", "description", "created_at"}
        assert required.issubset(cols), f"Fehlende Spalten: {required - cols}"


# ---------------------------------------------------------------------------
# Tests: Seed-Daten (alle Prompts aus seed.sql geladen)
# ---------------------------------------------------------------------------

# Alle erwarteten Prompt-IDs (aus seed.sql)
_EXPECTED_PROMPT_IDS = [
    "vision.system",
    "vision.default",
    "vision.scanned_pdf",
    "image.classify",
    "image.mermaid",
    "image.chart",
    "image.photo_de",
    "image.photo_en",
    "image.text_scan_de",
    "image.text_scan_en",
    "classify.system_de",
    "classify.system_en",
    "classify.user_de",
    "classify.user_en",
    "ocr_correct.system_de",
    "ocr_correct.system_en",
    "ocr_correct.user_de",
    "ocr_correct.user_en",
    "extract.system_de",
    "extract.system_en",
    "extract.user_de",
    "extract.user_en",
    "validate.dual_pass",
]

# Alle erwarteten Scoring-Weights aus seed.sql
_EXPECTED_SCORING_WEIGHT_IDS = [
    "density_low_threshold",
    "density_mid_threshold",
    "density_high_threshold",
    "density_optimal",
    "word_quality_max",
    "word_min_chars_threshold",
    "word_short_text_penalty",
    "structure_element_score",
    "structure_max",
    "vision_efficiency_high",
    "vision_efficiency_mid",
    "vision_efficiency_low",
    "vision_score_high",
    "vision_score_mid",
    "vision_score_low",
    "vision_score_min",
    "vision_baseline",
    "grade_poor",
    "grade_fair",
    "grade_good",
]


class TestSeedDataLoaded:
    """Testet dass alle Prompts aus seed.sql korrekt geladen wurden."""

    def test_all_prompts_seeded(self, initialized_db):
        """Alle erwarteten Prompt-IDs sind in der DB vorhanden."""
        conn = sqlite3.connect(str(initialized_db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id FROM prompt").fetchall()
        conn.close()
        seeded_ids = {r["id"] for r in rows}
        for expected_id in _EXPECTED_PROMPT_IDS:
            assert expected_id in seeded_ids, f"Prompt fehlt: '{expected_id}'"

    def test_all_scoring_weights_seeded(self, initialized_db):
        """Alle erwarteten Scoring-Weights sind in der DB vorhanden."""
        conn = sqlite3.connect(str(initialized_db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id FROM scoring_weight").fetchall()
        conn.close()
        seeded_ids = {r["id"] for r in rows}
        for expected_id in _EXPECTED_SCORING_WEIGHT_IDS:
            assert expected_id in seeded_ids, f"Scoring-Weight fehlt: '{expected_id}'"

    def test_prompts_count_minimum(self, initialized_db):
        """Mindestens die erwartete Anzahl Prompts sind vorhanden."""
        conn = sqlite3.connect(str(initialized_db))
        count = conn.execute("SELECT COUNT(*) FROM prompt").fetchone()[0]
        conn.close()
        assert count >= len(_EXPECTED_PROMPT_IDS), (
            f"Zu wenig Prompts: {count} < {len(_EXPECTED_PROMPT_IDS)}"
        )

    def test_scoring_weights_count_minimum(self, initialized_db):
        """Mindestens die erwartete Anzahl Scoring-Weights sind vorhanden."""
        conn = sqlite3.connect(str(initialized_db))
        count = conn.execute("SELECT COUNT(*) FROM scoring_weight").fetchone()[0]
        conn.close()
        assert count >= len(_EXPECTED_SCORING_WEIGHT_IDS), (
            f"Zu wenig Scoring-Weights: {count} < {len(_EXPECTED_SCORING_WEIGHT_IDS)}"
        )


# ---------------------------------------------------------------------------
# Tests: get_prompt() — gültige IDs
# ---------------------------------------------------------------------------

class TestGetPromptValid:
    """Testet get_prompt() mit gültigen IDs."""

    def test_get_vision_system_de(self, initialized_db):
        """vision.system DE-Prompt ist nicht leer und enthält Schlüsselwörter."""
        result = _get_prompt(initialized_db, "vision", "system", language="de")
        assert isinstance(result, str)
        assert len(result) > 10
        assert "Assistent" in result or "präzis" in result

    def test_get_vision_system_en(self, initialized_db):
        """vision.system EN-Prompt ist nicht leer und enthält Schlüsselwörter."""
        result = _get_prompt(initialized_db, "vision", "system", language="en")
        assert isinstance(result, str)
        assert len(result) > 10
        assert "assistant" in result.lower() or "precise" in result.lower()

    def test_get_image_classify_en(self, initialized_db):
        """image.classify EN-Prompt enthält die Klassifizierungs-Kategorien."""
        result = _get_prompt(initialized_db, "image", "classify", language="en")
        assert "photo" in result
        assert "chart" in result
        assert "diagram" in result
        assert "text_scan" in result
        assert "decorative" in result

    def test_get_image_mermaid_en(self, initialized_db):
        """image.mermaid EN-Prompt enthält Mermaid-Syntax-Anweisungen."""
        result = _get_prompt(initialized_db, "image", "mermaid", language="en")
        assert "mermaid" in result.lower() or "Mermaid" in result
        assert "graph TD" in result

    def test_get_classify_system_de(self, initialized_db):
        """classify.system_de enthält JSON-Anweisung."""
        result = _get_prompt(initialized_db, "classify", "system_de", language="de")
        assert "JSON" in result
        assert "Dokumentenklassifizierung" in result

    def test_get_ocr_correct_user_de(self, initialized_db):
        """ocr_correct.user_de enthält Korrektur-Anweisungen."""
        result = _get_prompt(initialized_db, "ocr_correct", "user_de", language="de")
        assert "OCR" in result
        assert "CORRECTIONS" in result
        assert "{text}" in result

    def test_get_extract_system_en(self, initialized_db):
        """extract.system_en enthält JSON-Anweisung."""
        result = _get_prompt(initialized_db, "extract", "system_en", language="en")
        assert "JSON" in result
        assert "expert" in result.lower()

    def test_get_validate_dual_pass(self, initialized_db):
        """validate.dual_pass enthält OCR-Vergleichs-Anweisung."""
        result = _get_prompt(initialized_db, "validate", "dual_pass", language="de")
        assert "OCR" in result
        assert "{markdown}" in result

    def test_get_vision_scanned_pdf(self, initialized_db):
        """vision.scanned_pdf enthält PDF-Scan-Anweisungen."""
        result = _get_prompt(initialized_db, "vision", "scanned_pdf", language="de")
        assert "Markdown" in result
        assert "UNLESERLICH" in result or "unleserlich" in result

    def test_get_vision_default(self, initialized_db):
        """vision.default enthält Bild-Analyse-Anweisungen."""
        result = _get_prompt(initialized_db, "vision", "default", language="de")
        assert "Markdown" in result
        assert "Bild" in result or "Text" in result

    def test_language_fallback_de_to_en(self, initialized_db):
        """Wenn DE-Inhalt fehlt, fällt get_prompt() auf EN zurück."""
        # image.classify hat nur content_en, kein content_de → DE-Anfrage gibt EN zurück
        result = _get_prompt(initialized_db, "image", "classify", language="de")
        assert isinstance(result, str)
        assert len(result) > 0

    @pytest.mark.parametrize("prompt_id,category,name,lang", [
        ("vision.system", "vision", "system", "de"),
        ("vision.system", "vision", "system", "en"),
        ("image.classify", "image", "classify", "en"),
        ("image.mermaid", "image", "mermaid", "en"),
        ("image.chart", "image", "chart", "en"),
        ("image.photo_de", "image", "photo_de", "de"),
        ("image.photo_en", "image", "photo_en", "en"),
        ("image.text_scan_de", "image", "text_scan_de", "de"),
        ("image.text_scan_en", "image", "text_scan_en", "en"),
        ("classify.system_de", "classify", "system_de", "de"),
        ("classify.system_en", "classify", "system_en", "en"),
        ("classify.user_de", "classify", "user_de", "de"),
        ("classify.user_en", "classify", "user_en", "en"),
        ("ocr_correct.system_de", "ocr_correct", "system_de", "de"),
        ("ocr_correct.system_en", "ocr_correct", "system_en", "en"),
        ("ocr_correct.user_de", "ocr_correct", "user_de", "de"),
        ("ocr_correct.user_en", "ocr_correct", "user_en", "en"),
        ("extract.system_de", "extract", "system_de", "de"),
        ("extract.system_en", "extract", "system_en", "en"),
        ("extract.user_de", "extract", "user_de", "de"),
        ("extract.user_en", "extract", "user_en", "en"),
        ("validate.dual_pass", "validate", "dual_pass", "de"),
    ])
    def test_all_expected_prompts_loadable(self, initialized_db, prompt_id, category, name, lang):
        """Alle Prompts aus seed.sql können per get_prompt() geladen werden."""
        result = _get_prompt(initialized_db, category, name, language=lang)
        assert isinstance(result, str), f"Prompt {prompt_id} sollte String sein"
        assert len(result) > 0, f"Prompt {prompt_id} sollte nicht leer sein"


# ---------------------------------------------------------------------------
# Tests: get_prompt() — ungültige ID → Error (KEIN Fallback!)
# ---------------------------------------------------------------------------

class TestGetPromptInvalid:
    """Testet dass get_prompt() bei unbekannter ID ValueError wirft — KEIN Fallback."""

    def test_unknown_category_raises_value_error(self, initialized_db):
        """Unbekannte Kategorie → ValueError."""
        with patch.object(_server, "TEMPLATES_DB_PATH", initialized_db):
            with pytest.raises(ValueError, match="Prompt nicht gefunden"):
                _server.get_prompt("nonexistent_category", "nonexistent_name")

    def test_unknown_name_raises_value_error(self, initialized_db):
        """Bekannte Kategorie, unbekannter Name → ValueError."""
        with patch.object(_server, "TEMPLATES_DB_PATH", initialized_db):
            with pytest.raises(ValueError, match="Prompt nicht gefunden"):
                _server.get_prompt("vision", "nonexistent_name")

    def test_empty_category_raises_value_error(self, initialized_db):
        """Leere Kategorie → ValueError."""
        with patch.object(_server, "TEMPLATES_DB_PATH", initialized_db):
            with pytest.raises(ValueError):
                _server.get_prompt("", "")

    def test_no_silent_fallback_no_default_returned(self, initialized_db):
        """get_prompt() gibt NIEMALS einen Default-String zurück wenn Prompt fehlt."""
        with patch.object(_server, "TEMPLATES_DB_PATH", initialized_db):
            raised = False
            try:
                result = _server.get_prompt("definitely_nonexistent", "also_nonexistent")
                # Wenn wir hier ankommen, ist kein Fehler geworfen worden
                # Das ist ein Test-Fehler
                assert False, f"Erwartete ValueError, aber bekam: '{result}'"
            except ValueError:
                raised = True
            assert raised, "get_prompt() muss ValueError werfen wenn Prompt nicht gefunden"


# ---------------------------------------------------------------------------
# Tests: get_scoring_weight() — gültige IDs
# ---------------------------------------------------------------------------

class TestGetScoringWeightValid:
    """Testet get_scoring_weight() mit gültigen IDs."""

    def test_grade_poor_threshold(self, initialized_db):
        """grade_poor hat den erwarteten Wert 0.3."""
        result = _get_scoring_weight(initialized_db, "grade_poor")
        assert isinstance(result, float)
        assert abs(result - 0.3) < 1e-9

    def test_grade_fair_threshold(self, initialized_db):
        """grade_fair hat den erwarteten Wert 0.6."""
        result = _get_scoring_weight(initialized_db, "grade_fair")
        assert abs(result - 0.6) < 1e-9

    def test_grade_good_threshold(self, initialized_db):
        """grade_good hat den erwarteten Wert 0.8."""
        result = _get_scoring_weight(initialized_db, "grade_good")
        assert abs(result - 0.8) < 1e-9

    def test_density_low_threshold(self, initialized_db):
        """density_low_threshold hat den erwarteten Wert 0.1."""
        result = _get_scoring_weight(initialized_db, "density_low_threshold")
        assert abs(result - 0.1) < 1e-9

    def test_density_optimal(self, initialized_db):
        """density_optimal hat den erwarteten Wert 0.3."""
        result = _get_scoring_weight(initialized_db, "density_optimal")
        assert abs(result - 0.3) < 1e-9

    def test_vision_score_high(self, initialized_db):
        """vision_score_high hat den erwarteten Wert 0.2."""
        result = _get_scoring_weight(initialized_db, "vision_score_high")
        assert abs(result - 0.2) < 1e-9

    def test_word_quality_max(self, initialized_db):
        """word_quality_max hat den erwarteten Wert 0.3."""
        result = _get_scoring_weight(initialized_db, "word_quality_max")
        assert abs(result - 0.3) < 1e-9

    @pytest.mark.parametrize("weight_id,expected_value", [
        ("density_low_threshold", 0.1),
        ("density_mid_threshold", 0.2),
        ("density_high_threshold", 0.8),
        ("density_optimal", 0.3),
        ("word_quality_max", 0.3),
        ("word_short_text_penalty", 0.5),
        ("structure_element_score", 0.05),
        ("structure_max", 0.2),
        ("vision_efficiency_high", 0.5),
        ("vision_efficiency_mid", 0.2),
        ("vision_efficiency_low", 0.05),
        ("vision_score_high", 0.2),
        ("vision_score_mid", 0.15),
        ("vision_score_low", 0.1),
        ("vision_score_min", 0.05),
        ("vision_baseline", 0.2),
        ("grade_poor", 0.3),
        ("grade_fair", 0.6),
        ("grade_good", 0.8),
    ])
    def test_scoring_weight_value(self, initialized_db, weight_id, expected_value):
        """Alle Scoring-Weights haben die erwarteten Werte."""
        result = _get_scoring_weight(initialized_db, weight_id)
        assert isinstance(result, float), f"{weight_id} sollte float sein"
        assert abs(result - expected_value) < 1e-9, (
            f"{weight_id}: erwartet {expected_value}, bekam {result}"
        )


# ---------------------------------------------------------------------------
# Tests: get_scoring_weight() — ungültige ID → Error (KEIN Fallback!)
# ---------------------------------------------------------------------------

class TestGetScoringWeightInvalid:
    """Testet dass get_scoring_weight() bei unbekannter ID ValueError wirft."""

    def test_unknown_weight_raises_value_error(self, initialized_db):
        """Unbekannte Gewichtung → ValueError."""
        with patch.object(_server, "TEMPLATES_DB_PATH", initialized_db):
            with pytest.raises(ValueError, match="Scoring-Gewichtung nicht gefunden"):
                _server.get_scoring_weight("nonexistent_weight")

    def test_no_silent_fallback(self, initialized_db):
        """get_scoring_weight() gibt NIEMALS 0.0 oder None zurück wenn Gewichtung fehlt."""
        with patch.object(_server, "TEMPLATES_DB_PATH", initialized_db):
            raised = False
            try:
                result = _server.get_scoring_weight("definitely_nonexistent_weight")
                assert False, f"Erwartete ValueError, aber bekam: {result}"
            except ValueError:
                raised = True
            assert raised


# ---------------------------------------------------------------------------
# Tests: upsert_prompt() — Erstellen und Versionierung
# ---------------------------------------------------------------------------

class TestUpsertPrompt:
    """Testet upsert_prompt() — Erstellen und Versionierung."""

    def test_upsert_creates_new_prompt(self, initialized_db):
        """upsert_prompt() erstellt einen neuen Prompt wenn ID nicht existiert."""
        result = _upsert_prompt(
            initialized_db,
            "test.new_prompt",
            "test",
            "new_prompt",
            content_de="Test-Inhalt DE",
            content_en="Test content EN",
        )
        assert result["id"] == "test.new_prompt"
        assert result["version"] == 1

        # Verify in DB
        content = _get_prompt(initialized_db, "test", "new_prompt", language="de")
        assert content == "Test-Inhalt DE"

    def test_upsert_updates_existing_prompt(self, initialized_db):
        """upsert_prompt() aktualisiert bestehenden Prompt."""
        # Erst erstellen
        _upsert_prompt(
            initialized_db,
            "test.update_me",
            "test",
            "update_me",
            content_de="Original DE",
        )
        # Dann aktualisieren
        result = _upsert_prompt(
            initialized_db,
            "test.update_me",
            "test",
            "update_me",
            content_de="Updated DE",
        )
        assert result["version"] == 2

        # Verify updated content
        content = _get_prompt(initialized_db, "test", "update_me", language="de")
        assert content == "Updated DE"

    def test_upsert_increments_version_on_each_update(self, initialized_db):
        """Version wird bei jedem Update inkrementiert."""
        for i in range(3):
            result = _upsert_prompt(
                initialized_db,
                "test.versioned",
                "test",
                "versioned",
                content_de=f"Version {i + 1}",
            )
        assert result["version"] == 3

    def test_upsert_returns_correct_metadata(self, initialized_db):
        """upsert_prompt() gibt korrekte Metadaten zurück."""
        result = _upsert_prompt(
            initialized_db,
            "test.metadata",
            "test",
            "metadata",
            content_de="DE content",
            content_en="EN content",
        )
        assert result["id"] == "test.metadata"
        assert result["category"] == "test"
        assert result["name"] == "metadata"
        assert result["version"] == 1

    def test_upsert_en_content_loadable(self, initialized_db):
        """Upsert-Prompt mit EN-Inhalt ist per get_prompt(..., language='en') ladbar."""
        _upsert_prompt(
            initialized_db,
            "test.bilingual",
            "test",
            "bilingual",
            content_de="Deutsch",
            content_en="English",
        )
        result_de = _get_prompt(initialized_db, "test", "bilingual", language="de")
        result_en = _get_prompt(initialized_db, "test", "bilingual", language="en")
        assert result_de == "Deutsch"
        assert result_en == "English"


# ---------------------------------------------------------------------------
# Tests: get_prompt_by_id()
# ---------------------------------------------------------------------------

class TestGetPromptById:
    """Testet get_prompt_by_id()."""

    def test_get_existing_prompt_by_id(self, initialized_db):
        """Bekannter Prompt-ID wird korrekt geladen."""
        with patch.object(_server, "TEMPLATES_DB_PATH", initialized_db):
            result = _server.get_prompt_by_id("vision.system")
        assert result["id"] == "vision.system"
        assert result["category"] == "vision"
        assert result["name"] == "system"
        assert "version" in result

    def test_get_nonexistent_prompt_by_id_raises(self, initialized_db):
        """Unbekannte ID → ValueError."""
        with patch.object(_server, "TEMPLATES_DB_PATH", initialized_db):
            with pytest.raises(ValueError, match="Prompt nicht gefunden"):
                _server.get_prompt_by_id("definitely.nonexistent")


# ---------------------------------------------------------------------------
# Tests: list_prompts()
# ---------------------------------------------------------------------------

class TestListPrompts:
    """Testet list_prompts()."""

    def test_list_all_prompts(self, initialized_db):
        """list_prompts() ohne Filter gibt alle Prompts zurück."""
        with patch.object(_server, "TEMPLATES_DB_PATH", initialized_db):
            result = _server.list_prompts()
        assert isinstance(result, list)
        assert len(result) >= len(_EXPECTED_PROMPT_IDS)

    def test_list_prompts_by_category(self, initialized_db):
        """list_prompts(category='vision') gibt nur Vision-Prompts zurück."""
        with patch.object(_server, "TEMPLATES_DB_PATH", initialized_db):
            result = _server.list_prompts(category="vision")
        assert len(result) > 0
        for item in result:
            assert item["category"] == "vision"

    def test_list_prompts_unknown_category_returns_empty(self, initialized_db):
        """list_prompts() mit unbekannter Kategorie gibt leere Liste zurück."""
        with patch.object(_server, "TEMPLATES_DB_PATH", initialized_db):
            result = _server.list_prompts(category="nonexistent_category")
        assert result == []

    def test_list_prompts_has_required_keys(self, initialized_db):
        """Jeder Eintrag in list_prompts() hat id, category, name, version."""
        with patch.object(_server, "TEMPLATES_DB_PATH", initialized_db):
            result = _server.list_prompts(category="classify")
        for item in result:
            assert "id" in item
            assert "category" in item
            assert "name" in item
            assert "version" in item
