from pathlib import Path

from conftest import load_server_module, run_async


_server = load_server_module(use_real_pil=False)
import utils as _utils
from models import ConvertRequest


def test_resolve_path_rejects_path_outside_allowed_roots(monkeypatch, tmp_path):
    allowed_root = (tmp_path / "allowed").resolve()
    blocked_root = (tmp_path / "blocked").resolve()
    allowed_root.mkdir()
    blocked_root.mkdir()

    monkeypatch.setattr(_utils, "DATA_DIR", allowed_root)
    monkeypatch.setattr(_utils, "ALLOWED_PATH_ROOTS", (allowed_root,))
    monkeypatch.setattr(_utils, "ALLOW_SYMLINK_PATHS", False)

    outside_file = blocked_root / "secret.txt"

    try:
        _utils.resolve_path(str(outside_file))
        assert False, "resolve_path should reject paths outside ALLOWED_PATH_ROOTS"
    except _utils.PathPolicyError as exc:
        assert exc.reason == "path_outside_allowed_roots"


def test_resolve_path_rejects_symlink_when_disabled(monkeypatch, tmp_path):
    allowed_root = (tmp_path / "allowed").resolve()
    allowed_root.mkdir()
    real_dir = allowed_root / "real"
    real_dir.mkdir()
    (real_dir / "doc.txt").write_text("hello", encoding="utf-8")
    symlink_dir = allowed_root / "alias"
    symlink_dir.symlink_to(real_dir, target_is_directory=True)

    monkeypatch.setattr(_utils, "DATA_DIR", allowed_root)
    monkeypatch.setattr(_utils, "ALLOWED_PATH_ROOTS", (allowed_root,))
    monkeypatch.setattr(_utils, "ALLOW_SYMLINK_PATHS", False)

    try:
        _utils.resolve_path(str(symlink_dir / "doc.txt"))
        assert False, "resolve_path should reject symlink components when ALLOW_SYMLINK_PATHS=false"
    except _utils.PathPolicyError as exc:
        assert exc.reason == "symlink_not_allowed"


def test_api_convert_rejects_path_outside_allowed_roots(monkeypatch, tmp_path):
    blocked_root = (tmp_path / "blocked").resolve()
    blocked_root.mkdir()
    blocked_file = blocked_root / "doc.txt"
    blocked_file.write_text("secret", encoding="utf-8")

    api_rest = load_server_module(use_real_pil=False)
    request = ConvertRequest(path=str(blocked_file))
    response = run_async(api_rest._api_convert_impl(request))

    assert response.success is False
    assert response.error is not None
    assert response.error.code == "PATH_NOT_ALLOWED"
    assert response.meta.path_policy_reason == "path_outside_allowed_roots"
