"""
Tests für T-DAI-009: Alle ENV-Variablen aus .env.example müssen in settings.py konfigurierbar sein.

Strategie:
1. Parse .env.example — extrahiere alle Variablennamen (KEY=value Zeilen)
2. Importiere settings-Modul
3. Prüfe für jede Variable ob sie als Attribut in settings existiert

Ausnahmen (Variablen die bewusst NICHT in settings.py landen):
- MCP_TRANSPORT und BIND_HOST sind in settings.py vorhanden
- Keine Ausnahmen nötig
"""

import importlib
import re
import sys
from pathlib import Path

import pytest

# Pfad zum Repo-Root (zwei Ebenen über tests/)
REPO_ROOT = Path(__file__).parent.parent.parent
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"
SETTINGS_MODULE_PATH = REPO_ROOT / "mcp" / "settings.py"

# Variablen aus .env.example die ABSICHTLICH nicht in settings.py als Attribut
# auftauchen (z.B. weil sie nur von Docker / Compose genutzt werden):
KNOWN_EXCLUSIONS: set[str] = set()


def parse_env_example(path: Path) -> list[str]:
    """Lese .env.example und gib alle Variablennamen zurück (ohne Kommentare/Leerzeilen)."""
    variables: list[str] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            # Überspringe Kommentare und Leerzeilen
            if not line or line.startswith("#"):
                continue
            # KEY=value oder KEY= Format
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
            if match:
                variables.append(match.group(1))
    return variables


def load_settings():
    """Lade settings.py als Modul und gib es zurück."""
    spec = importlib.util.spec_from_file_location("settings", SETTINGS_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- Fixtures ---

@pytest.fixture(scope="module")
def env_vars() -> list[str]:
    assert ENV_EXAMPLE_PATH.exists(), f".env.example nicht gefunden: {ENV_EXAMPLE_PATH}"
    return parse_env_example(ENV_EXAMPLE_PATH)


@pytest.fixture(scope="module")
def settings():
    assert SETTINGS_MODULE_PATH.exists(), f"settings.py nicht gefunden: {SETTINGS_MODULE_PATH}"
    return load_settings()


# --- Tests ---

class TestEnvExampleCompleteness:
    """Alle Variablen aus .env.example müssen in settings.py vorhanden sein."""

    def test_env_example_exists(self):
        assert ENV_EXAMPLE_PATH.exists(), ".env.example fehlt"

    def test_settings_py_exists(self):
        assert SETTINGS_MODULE_PATH.exists(), "mcp/settings.py fehlt"

    def test_env_example_not_empty(self, env_vars):
        assert len(env_vars) > 0, ".env.example enthält keine Variablen"

    def test_all_env_vars_in_settings(self, env_vars, settings):
        """Jede Variable aus .env.example muss als Attribut in settings.py existieren."""
        missing = []
        for var in env_vars:
            if var in KNOWN_EXCLUSIONS:
                continue
            if not hasattr(settings, var):
                missing.append(var)

        if missing:
            pytest.fail(
                f"Folgende Variablen aus .env.example fehlen in settings.py:\n"
                + "\n".join(f"  - {v}" for v in sorted(missing))
            )

    @pytest.mark.parametrize("var_name", parse_env_example(ENV_EXAMPLE_PATH) if ENV_EXAMPLE_PATH.exists() else [])
    def test_single_var_in_settings(self, var_name, settings):
        """Parametrisierter Test: eine Variable pro Test-Case für bessere Übersicht."""
        if var_name in KNOWN_EXCLUSIONS:
            pytest.skip(f"{var_name} ist bewusst ausgenommen")
        assert hasattr(settings, var_name), (
            f"Variable '{var_name}' aus .env.example fehlt als Attribut in settings.py"
        )


class TestSettingsDefaults:
    """Stichproben: Neu hinzugefügte Variablen haben die richtigen Defaults."""

    def test_classify_cache_ttl_default(self, settings):
        assert settings.CLASSIFY_CACHE_TTL == 300
        assert isinstance(settings.CLASSIFY_CACHE_TTL, int)

    def test_bind_host_default(self, settings):
        assert settings.BIND_HOST == "0.0.0.0"
        assert isinstance(settings.BIND_HOST, str)

    def test_mcp_transport_default(self, settings):
        assert settings.MCP_TRANSPORT == "sse"
        assert isinstance(settings.MCP_TRANSPORT, str)

    def test_templates_db_path_default(self, settings):
        # Muss ein Path-Objekt sein das auf /data/templates.db zeigt
        from pathlib import Path
        assert isinstance(settings.TEMPLATES_DB_PATH, Path)
        assert str(settings.TEMPLATES_DB_PATH).endswith("templates.db")

    def test_classify_categories_default(self, settings):
        assert isinstance(settings.DEFAULT_CLASSIFY_CATEGORIES, list)
        assert len(settings.DEFAULT_CLASSIFY_CATEGORIES) > 0
        assert "invoice" in settings.DEFAULT_CLASSIFY_CATEGORIES
        assert "other" in settings.DEFAULT_CLASSIFY_CATEGORIES

    def test_backward_compat_alias(self, settings):
        """_CLASSIFY_CACHE_TTL Alias muss gleichen Wert wie CLASSIFY_CACHE_TTL haben."""
        assert settings._CLASSIFY_CACHE_TTL == settings.CLASSIFY_CACHE_TTL

    def test_quality_retry_defaults(self, settings):
        assert settings.QUALITY_RETRY_ENABLED is False
        assert settings.QUALITY_RETRY_THRESHOLD == 0.75
        assert settings.QUALITY_RETRY_MODE == "full"
