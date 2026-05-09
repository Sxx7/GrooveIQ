"""
GrooveIQ — Tests for the CLAP auto-installer (issue #91).

Verifies the no-op paths (CLAP disabled, files already present) and the
download path with a mocked HTTP fetch — we don't pull 400 MB in CI.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.services import clap_setup

# Tiny stand-in sizes so tests don't allocate hundreds of MB of zeroes
# just to satisfy the production min-size validation. Each test that
# writes a placeholder file monkeypatches this in.
_TEST_MIN_SIZES = {"text": 1024, "audio": 1024, "tokenizer": 256}


@contextmanager
def _mock_httpx_stream(payload: bytes):
    """Patch ``httpx.Client`` so the streaming GET returns ``payload`` once."""
    fake_resp = MagicMock()
    fake_resp.headers = {"Content-Length": str(len(payload))}
    fake_resp.raise_for_status = MagicMock()
    fake_resp.iter_bytes = MagicMock(return_value=iter([payload]))
    fake_resp.__enter__ = MagicMock(return_value=fake_resp)
    fake_resp.__exit__ = MagicMock(return_value=False)

    fake_client = MagicMock()
    fake_client.stream = MagicMock(return_value=fake_resp)
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)

    with patch.object(clap_setup.httpx, "Client", return_value=fake_client):
        yield fake_client


class TestEnsureClapModelsSync:
    def test_noop_when_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clap_setup.settings, "CLAP_ENABLED", False)
        monkeypatch.setattr(clap_setup.settings, "CLAP_MODEL_DIR", str(tmp_path))

        with patch.object(clap_setup, "_download_one") as m:
            clap_setup._ensure_clap_models_sync()

        assert m.call_count == 0
        assert list(tmp_path.iterdir()) == []

    def test_noop_when_files_already_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clap_setup, "_MIN_SIZES", _TEST_MIN_SIZES)
        monkeypatch.setattr(clap_setup.settings, "CLAP_ENABLED", True)
        monkeypatch.setattr(clap_setup.settings, "CLAP_MODEL_DIR", str(tmp_path))
        monkeypatch.setattr(clap_setup.settings, "CLAP_TEXT_MODEL_FILE", "clap_text.onnx")
        monkeypatch.setattr(clap_setup.settings, "CLAP_AUDIO_MODEL_FILE", "clap_audio.onnx")
        monkeypatch.setattr(clap_setup.settings, "CLAP_TOKENIZER_FILE", "clap_tokenizer.json")

        # Pre-create files at min sizes so _file_ok returns True for all three.
        (tmp_path / "clap_text.onnx").write_bytes(b"x" * _TEST_MIN_SIZES["text"])
        (tmp_path / "clap_audio.onnx").write_bytes(b"x" * _TEST_MIN_SIZES["audio"])
        (tmp_path / "clap_tokenizer.json").write_bytes(b"x" * _TEST_MIN_SIZES["tokenizer"])

        with patch.object(clap_setup, "_download_one") as m:
            clap_setup._ensure_clap_models_sync()

        assert m.call_count == 0

    def test_downloads_only_missing_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clap_setup, "_MIN_SIZES", _TEST_MIN_SIZES)
        monkeypatch.setattr(clap_setup.settings, "CLAP_ENABLED", True)
        monkeypatch.setattr(clap_setup.settings, "CLAP_MODEL_DIR", str(tmp_path))
        monkeypatch.setattr(clap_setup.settings, "CLAP_TEXT_MODEL_FILE", "clap_text.onnx")
        monkeypatch.setattr(clap_setup.settings, "CLAP_AUDIO_MODEL_FILE", "clap_audio.onnx")
        monkeypatch.setattr(clap_setup.settings, "CLAP_TOKENIZER_FILE", "clap_tokenizer.json")

        # Pre-create only the tokenizer at sufficient size.
        (tmp_path / "clap_tokenizer.json").write_bytes(b"x" * _TEST_MIN_SIZES["tokenizer"])

        with patch.object(clap_setup, "_download_one") as m:
            clap_setup._ensure_clap_models_sync()

        called_labels = sorted(call.args[3] for call in m.call_args_list)
        assert called_labels == ["audio", "text"]

    def test_partial_file_redownloaded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clap_setup, "_MIN_SIZES", _TEST_MIN_SIZES)
        monkeypatch.setattr(clap_setup.settings, "CLAP_ENABLED", True)
        monkeypatch.setattr(clap_setup.settings, "CLAP_MODEL_DIR", str(tmp_path))
        monkeypatch.setattr(clap_setup.settings, "CLAP_TEXT_MODEL_FILE", "clap_text.onnx")
        monkeypatch.setattr(clap_setup.settings, "CLAP_AUDIO_MODEL_FILE", "clap_audio.onnx")
        monkeypatch.setattr(clap_setup.settings, "CLAP_TOKENIZER_FILE", "clap_tokenizer.json")

        # Tiny "text" file — too small, must be re-fetched.
        (tmp_path / "clap_text.onnx").write_bytes(b"x" * 10)
        (tmp_path / "clap_audio.onnx").write_bytes(b"x" * _TEST_MIN_SIZES["audio"])
        (tmp_path / "clap_tokenizer.json").write_bytes(b"x" * _TEST_MIN_SIZES["tokenizer"])

        with patch.object(clap_setup, "_download_one") as m:
            clap_setup._ensure_clap_models_sync()

        called_labels = sorted(call.args[3] for call in m.call_args_list)
        assert called_labels == ["text"]

    def test_download_failure_is_swallowed(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(clap_setup.settings, "CLAP_ENABLED", True)
        monkeypatch.setattr(clap_setup.settings, "CLAP_MODEL_DIR", str(tmp_path))
        monkeypatch.setattr(clap_setup.settings, "CLAP_TEXT_MODEL_FILE", "clap_text.onnx")
        monkeypatch.setattr(clap_setup.settings, "CLAP_AUDIO_MODEL_FILE", "clap_audio.onnx")
        monkeypatch.setattr(clap_setup.settings, "CLAP_TOKENIZER_FILE", "clap_tokenizer.json")

        def _explode(*_, **__):
            raise OSError("network down")

        with patch.object(clap_setup, "_download_one", side_effect=_explode):
            clap_setup._ensure_clap_models_sync()  # must NOT raise

        # Files still missing — that's expected; the warning gets logged.
        assert not (tmp_path / "clap_text.onnx").exists()


class TestDownloadOne:
    def test_atomic_rename_on_success(self, tmp_path):
        dest = tmp_path / "clap_text.onnx"
        # Use a small min_size local to this test; the production constant
        # is 300 MB and we don't want to allocate that in CI.
        min_size = 1024
        payload = b"x" * (min_size + 256)

        with _mock_httpx_stream(payload):
            clap_setup._download_one("https://example.invalid/text.onnx", dest, min_size, "text")

        assert dest.is_file()
        assert dest.stat().st_size == len(payload)
        # No leftover temp files.
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".clap_text")]
        assert leftovers == []

    def test_non_https_url_refused(self, tmp_path):
        dest = tmp_path / "clap_text.onnx"
        # file://, http://, ftp://, etc. must all be refused before we hit urlopen.
        for bad in (
            "file:///etc/passwd",
            "http://example.invalid/text.onnx",
            "ftp://example.invalid/text.onnx",
        ):
            with pytest.raises(ValueError, match="non-https"):
                clap_setup._download_one(bad, dest, clap_setup._MIN_SIZES["text"], "text")
        assert not dest.exists()

    def test_too_small_response_rejected(self, tmp_path):
        dest = tmp_path / "clap_text.onnx"
        # Payload smaller than min size — likely an HTML error page, not a model.
        payload = b"<html>404</html>"
        min_size = 1024

        with _mock_httpx_stream(payload), pytest.raises(RuntimeError, match="too small"):
            clap_setup._download_one("https://example.invalid/text.onnx", dest, min_size, "text")

        assert not dest.exists()
        # And the temp file is cleaned up.
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".clap_text")]
        assert leftovers == []
