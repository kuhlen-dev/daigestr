"""
Tests for E8/W8.1/T8.1.1: DB-required startup bootstrap.
"""

from unittest.mock import MagicMock, patch

import pytest

from conftest import load_server_module


_server = load_server_module(use_real_pil=False)


class TestInitializePersistence:
    """Startup must fail fast when PostgreSQL-backed subsystems cannot initialize."""

    def test_initialize_persistence_runs_all_init_steps_in_order(self):
        calls: list[str] = []

        def _record(name: str):
            return lambda: calls.append(name)

        with patch.object(_server, "init_templates_db", side_effect=_record("templates")), \
             patch.object(_server, "init_execution_db", side_effect=_record("execution")), \
             patch.object(_server, "init_audit_db", side_effect=_record("audit")), \
             patch.object(_server, "init_debug_snapshot_db", side_effect=_record("debug_snapshot")), \
             patch.object(_server, "init_normalization_db", side_effect=_record("normalizer")):
            _server.initialize_persistence()

        assert calls == ["templates", "execution", "audit", "debug_snapshot", "normalizer"]

    @pytest.mark.parametrize(
        ("failing_attr", "expected_prefix"),
        [
            ("init_templates_db", "template_registry"),
            ("init_execution_db", "execution_db"),
            ("init_audit_db", "audit_db"),
            ("init_debug_snapshot_db", "debug_snapshot_db"),
            ("init_normalization_db", "normalization_db"),
        ],
    )
    def test_initialize_persistence_raises_on_first_failed_component(self, failing_attr: str, expected_prefix: str):
        fail = RuntimeError("db down")

        patches = {
            "init_templates_db": MagicMock(),
            "init_execution_db": MagicMock(),
            "init_audit_db": MagicMock(),
            "init_debug_snapshot_db": MagicMock(),
            "init_normalization_db": MagicMock(),
        }
        patches[failing_attr] = MagicMock(side_effect=fail)

        with patch.object(_server, "init_templates_db", patches["init_templates_db"]), \
             patch.object(_server, "init_execution_db", patches["init_execution_db"]), \
             patch.object(_server, "init_audit_db", patches["init_audit_db"]), \
             patch.object(_server, "init_debug_snapshot_db", patches["init_debug_snapshot_db"]), \
             patch.object(_server, "init_normalization_db", patches["init_normalization_db"]):
            with pytest.raises(RuntimeError, match=rf"^{expected_prefix} initialization failed$"):
                _server.initialize_persistence()

        if failing_attr == "init_templates_db":
            patches["init_execution_db"].assert_not_called()
            patches["init_audit_db"].assert_not_called()
            patches["init_debug_snapshot_db"].assert_not_called()
            patches["init_normalization_db"].assert_not_called()
        elif failing_attr == "init_execution_db":
            patches["init_templates_db"].assert_called_once()
            patches["init_audit_db"].assert_not_called()
            patches["init_debug_snapshot_db"].assert_not_called()
            patches["init_normalization_db"].assert_not_called()
        elif failing_attr == "init_audit_db":
            patches["init_templates_db"].assert_called_once()
            patches["init_execution_db"].assert_called_once()
            patches["init_debug_snapshot_db"].assert_not_called()
            patches["init_normalization_db"].assert_not_called()
        elif failing_attr == "init_debug_snapshot_db":
            patches["init_templates_db"].assert_called_once()
            patches["init_execution_db"].assert_called_once()
            patches["init_audit_db"].assert_called_once()
            patches["init_normalization_db"].assert_not_called()
        else:
            patches["init_templates_db"].assert_called_once()
            patches["init_execution_db"].assert_called_once()
            patches["init_audit_db"].assert_called_once()
            patches["init_debug_snapshot_db"].assert_called_once()
