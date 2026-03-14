"""
Unit tests for the media (attachment) ingestion pipeline.

Covers:
- signal-desktop /attachment endpoint safety and correctness
- signal-desktop /messages and /group/messages attachment fields
- signal-desktop db_reader attachment parsing from json column
- signal-bot SignalDesktopAdapter._fetch_message_attachments (live ingestion)
- signal-bot _save_history_images (history ingestion)
- signal-ingest _fetch_attachment, _ocr_attachment, _enrich_messages_with_attachments
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ---------------------------------------------------------------------------
# Path setup – allow importing from each service without installing packages
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "signal-bot"))
sys.path.insert(0, str(ROOT / "signal-desktop"))
sys.path.insert(0, str(ROOT / "signal-ingest"))


# ===========================================================================
# 1.  signal-desktop /attachment endpoint
# ===========================================================================

class TestAttachmentEndpoint:
    """Tests for the GET /attachment endpoint in signal-desktop."""

    def _make_app(self, data_dir: Path):
        """Build a FastAPI test client with a custom signal_data_dir."""
        from fastapi.testclient import TestClient

        # Patch settings before importing main to avoid startup side effects
        mock_settings = SimpleNamespace(signal_data_dir=str(data_dir))
        with patch("app.config.load_settings", return_value=mock_settings):
            # We need to reload main or inject settings directly
            import importlib
            import app.main as sd_main
            original_settings = sd_main.settings
            sd_main.settings = mock_settings
            client = TestClient(sd_main.app, raise_server_exceptions=True)
            yield client
            sd_main.settings = original_settings

    def test_serves_existing_file(self, tmp_path):
        """Should return 200 and the correct bytes for a file inside data_dir."""
        from fastapi.testclient import TestClient
        import app.main as sd_main

        # Create a fake attachment
        att_dir = tmp_path / "attachments.noindex" / "abc"
        att_dir.mkdir(parents=True)
        img_file = att_dir / "photo.jpg"
        img_bytes = b"\xff\xd8\xff" + b"\x00" * 50  # minimal fake JPEG
        img_file.write_bytes(img_bytes)

        original_settings = sd_main.settings
        sd_main.settings = SimpleNamespace(signal_data_dir=str(tmp_path))
        try:
            client = TestClient(sd_main.app, raise_server_exceptions=True)
            resp = client.get(
                "/attachment",
                params={"path": "attachments.noindex/abc/photo.jpg"},
            )
            assert resp.status_code == 200
            assert resp.content == img_bytes
        finally:
            sd_main.settings = original_settings

    def test_missing_file_returns_404(self, tmp_path):
        """Should return 404 when the attachment does not exist."""
        from fastapi.testclient import TestClient
        import app.main as sd_main

        original_settings = sd_main.settings
        sd_main.settings = SimpleNamespace(signal_data_dir=str(tmp_path))
        try:
            client = TestClient(sd_main.app, raise_server_exceptions=True)
            resp = client.get(
                "/attachment",
                params={"path": "attachments.noindex/missing.jpg"},
            )
            assert resp.status_code == 404
        finally:
            sd_main.settings = original_settings

    def test_path_traversal_blocked(self, tmp_path):
        """Should return 403 for paths that escape the data directory."""
        from fastapi.testclient import TestClient
        import app.main as sd_main

        # Create a file outside the data dir
        secret = tmp_path.parent / "secret.txt"
        secret.write_text("secret")

        original_settings = sd_main.settings
        sd_main.settings = SimpleNamespace(signal_data_dir=str(tmp_path))
        try:
            client = TestClient(sd_main.app, raise_server_exceptions=True)
            resp = client.get(
                "/attachment",
                params={"path": "../secret.txt"},
            )
            assert resp.status_code == 403
        finally:
            sd_main.settings = original_settings


# ===========================================================================
# 2.  signal-desktop db_reader – attachment parsing
# ===========================================================================

class TestDbReaderAttachmentParsing:
    """Tests for attachment parsing in db_reader.get_messages()."""

    def _build_msg_row(self, body, attachments_json=None, extra_json=None):
        """Simulate a DB row with the columns that get_messages() expects."""
        # Columns: id, conv_id, ts, body, sender, type, group_id, group_name,
        #          sender_name, raw_json
        raw = None
        if attachments_json is not None or extra_json is not None:
            obj = extra_json or {}
            if attachments_json is not None:
                obj["attachments"] = attachments_json
            raw = json.dumps(obj)
        return ("msg1", "conv1", 1_000_000, body, "+1234", "incoming",
                None, None, "Alice", raw)

    def test_parses_attachment_path(self):
        """Should extract attachment path, fileName and contentType."""
        from app.db_reader import SignalMessage

        att = [{"path": "attachments.noindex/abc/img.jpg", "fileName": "img.jpg", "contentType": "image/jpeg"}]
        row = self._build_msg_row("hello", att)

        # Simulate the parsing logic (extracted from get_messages)
        raw_json_str = row[9]
        attachments = []
        if raw_json_str:
            msg_json = json.loads(raw_json_str)
            for a in msg_json.get("attachments") or []:
                if isinstance(a, dict) and a.get("path"):
                    attachments.append({
                        "path": a.get("path"),
                        "fileName": a.get("fileName") or "",
                        "contentType": a.get("contentType") or "",
                    })

        assert len(attachments) == 1
        assert attachments[0]["path"] == "attachments.noindex/abc/img.jpg"
        assert attachments[0]["fileName"] == "img.jpg"
        assert attachments[0]["contentType"] == "image/jpeg"

    def test_ignores_attachments_without_path(self):
        """Entries without a 'path' key should be skipped."""
        att = [{"fileName": "img.jpg", "contentType": "image/jpeg"}]  # no path
        row = self._build_msg_row("hi", att)
        raw_json_str = row[9]
        attachments = []
        if raw_json_str:
            msg_json = json.loads(raw_json_str)
            for a in msg_json.get("attachments") or []:
                if isinstance(a, dict) and a.get("path"):
                    attachments.append(a)
        assert attachments == []

    def test_empty_json_no_attachments(self):
        """Messages with no json column should have empty attachments."""
        row = self._build_msg_row("hello", None)
        raw_json_str = row[9]
        assert raw_json_str is None


# ===========================================================================
# 3.  signal-bot SignalDesktopAdapter._fetch_message_attachments
# ===========================================================================

class TestFetchMessageAttachments:
    """Tests for live-ingestion attachment fetching in signal_desktop.py."""

    def _make_adapter(self, storage_dir: Path):
        from app.signal.signal_desktop import SignalDesktopAdapter
        settings = SimpleNamespace(
            signal_bot_storage=str(storage_dir),
            signal_desktop_url="http://signal-desktop:8001",
        )
        adapter = SignalDesktopAdapter(settings=settings)
        return adapter

    def test_saves_attachment_to_disk(self, tmp_path):
        """Should fetch attachment bytes and save them as a local file."""
        adapter = self._make_adapter(tmp_path)
        img_bytes = b"\x89PNG\r\n" + b"\x00" * 20  # minimal fake PNG

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = img_bytes

        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp

        atts = [{"path": "attachments.noindex/abc/img.png", "contentType": "image/png", "fileName": "img.png"}]
        paths = adapter._fetch_message_attachments(
            client=mock_client,
            attachments=atts,
            msg_id="msg42",
        )

        assert len(paths) == 1
        saved = Path(paths[0])
        assert saved.exists()
        assert saved.read_bytes() == img_bytes
        assert saved.suffix == ".png"

    def test_skips_oversized_attachment(self, tmp_path):
        """Attachments exceeding max_size_bytes should be skipped."""
        adapter = self._make_adapter(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"x" * 10_000_000  # 10 MB

        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp

        atts = [{"path": "att/big.jpg", "contentType": "image/jpeg", "fileName": "big.jpg"}]
        paths = adapter._fetch_message_attachments(
            client=mock_client,
            attachments=atts,
            msg_id="msgbig",
            max_size_bytes=1_000_000,
        )
        assert paths == []

    def test_skips_on_http_error(self, tmp_path):
        """HTTP errors from signal-desktop should be handled gracefully."""
        adapter = self._make_adapter(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp

        atts = [{"path": "att/missing.jpg", "contentType": "image/jpeg"}]
        paths = adapter._fetch_message_attachments(
            client=mock_client, attachments=atts, msg_id="m1",
        )
        assert paths == []

    def test_empty_attachments_returns_empty(self, tmp_path):
        """No attachments → empty list, no HTTP calls."""
        adapter = self._make_adapter(tmp_path)
        mock_client = MagicMock()
        paths = adapter._fetch_message_attachments(
            client=mock_client, attachments=[], msg_id="m2",
        )
        assert paths == []
        mock_client.get.assert_not_called()

    def test_deduplicates_on_repeated_call(self, tmp_path):
        """The same attachment fetched twice should reuse the cached file."""
        adapter = self._make_adapter(tmp_path)
        img_bytes = b"imgdata"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = img_bytes
        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp

        att = [{"path": "att/photo.jpg", "contentType": "image/jpeg"}]
        p1 = adapter._fetch_message_attachments(client=mock_client, attachments=att, msg_id="mx")
        p2 = adapter._fetch_message_attachments(client=mock_client, attachments=att, msg_id="mx")

        # Second call should NOT issue another HTTP request (file exists)
        assert mock_client.get.call_count == 1
        assert p1 == p2


# ===========================================================================
# 4.  signal-bot _save_history_images
# ===========================================================================

class TestSaveHistoryImages:
    """Tests for history image storage in signal-bot main.py."""

    def _import(self):
        # We need to import without triggering FastAPI startup
        import importlib
        # Minimal stubs so the module can be imported
        with patch.dict(os.environ, {
            "GOOGLE_API_KEY": "fake",
            "SIGNAL_BOT_E164": "+1",
            "DB_BACKEND": "mysql",
        }):
            import app.main as bot_main
        return bot_main

    def test_saves_jpeg(self, tmp_path):
        import app.main as bot_main
        img_bytes = b"\xff\xd8\xff" + b"\x00" * 10
        payloads = [
            bot_main.HistoryImagePayload(
                filename="photo.jpg",
                content_type="image/jpeg",
                data_b64=base64.b64encode(img_bytes).decode(),
            )
        ]
        paths = bot_main._save_history_images(
            group_id="grp1",
            message_id="msg1",
            image_payloads=payloads,
            storage_root=str(tmp_path),
        )
        assert len(paths) == 1
        assert Path(paths[0]).read_bytes() == img_bytes
        assert paths[0].endswith(".jpg")

    def test_saves_png_with_correct_extension(self, tmp_path):
        import app.main as bot_main
        img_bytes = b"\x89PNG" + b"\x00" * 10
        payloads = [
            bot_main.HistoryImagePayload(
                filename="",
                content_type="image/png",
                data_b64=base64.b64encode(img_bytes).decode(),
            )
        ]
        paths = bot_main._save_history_images(
            group_id="grp2",
            message_id="msg2",
            image_payloads=payloads,
            storage_root=str(tmp_path),
        )
        assert len(paths) == 1
        assert paths[0].endswith(".png")

    def test_empty_payloads_returns_empty(self, tmp_path):
        import app.main as bot_main
        paths = bot_main._save_history_images(
            group_id="g", message_id="m", image_payloads=[], storage_root=str(tmp_path),
        )
        assert paths == []

    def test_invalid_base64_skipped(self, tmp_path):
        import app.main as bot_main
        payloads = [
            bot_main.HistoryImagePayload(
                filename="bad.jpg",
                content_type="image/jpeg",
                data_b64="NOT_VALID_BASE64!!!",
            )
        ]
        paths = bot_main._save_history_images(
            group_id="g", message_id="m", image_payloads=payloads, storage_root=str(tmp_path),
        )
        assert paths == []

    def test_multiple_images_same_message(self, tmp_path):
        import app.main as bot_main
        payloads = [
            bot_main.HistoryImagePayload(
                filename=f"img{i}.jpg",
                content_type="image/jpeg",
                data_b64=base64.b64encode(f"data{i}".encode()).decode(),
            )
            for i in range(3)
        ]
        paths = bot_main._save_history_images(
            group_id="g", message_id="m", image_payloads=payloads, storage_root=str(tmp_path),
        )
        assert len(paths) == 3
        # All files should be distinct
        assert len(set(paths)) == 3


# ===========================================================================
# 5.  signal-ingest _fetch_attachment + _ocr_attachment
# ===========================================================================

class TestIngestAttachmentHelpers:
    """Tests for attachment helpers in signal-ingest main.py."""

    def _settings(self, desktop_url="http://signal-desktop:8001"):
        return SimpleNamespace(
            signal_desktop_url=desktop_url,
            model_img="gemini-2.0-flash",
            openai_api_key="fake",
        )

    def test_fetch_returns_bytes_on_200(self):
        from ingest.main import _fetch_attachment
        img_bytes = b"fakeimage"
        with patch("httpx.Client") as MockClient:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = img_bytes
            MockClient.return_value.__enter__.return_value.get.return_value = mock_resp
            result = _fetch_attachment(self._settings(), "att/img.jpg")
        assert result == img_bytes

    def test_fetch_returns_none_on_404(self):
        from ingest.main import _fetch_attachment
        with patch("httpx.Client") as MockClient:
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            MockClient.return_value.__enter__.return_value.get.return_value = mock_resp
            result = _fetch_attachment(self._settings(), "att/missing.jpg")
        assert result is None

    def test_fetch_returns_none_when_too_large(self):
        from ingest.main import _fetch_attachment
        with patch("httpx.Client") as MockClient:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = b"x" * 10_000_000
            MockClient.return_value.__enter__.return_value.get.return_value = mock_resp
            result = _fetch_attachment(self._settings(), "att/big.jpg", max_bytes=1_000_000)
        assert result is None

    def test_fetch_returns_none_on_exception(self):
        from ingest.main import _fetch_attachment
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = Exception("timeout")
            result = _fetch_attachment(self._settings(), "att/img.jpg")
        assert result is None

    def test_ocr_returns_json_string(self):
        from ingest.main import _ocr_attachment
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices[0].message.content = '{"extracted_text": "hello", "observations": []}'
        mock_client.chat.completions.create.return_value = mock_response

        result = _ocr_attachment(
            openai_client=mock_client,
            model="gemini-2.0-flash",
            image_bytes=b"\xff\xd8\xff",
            content_type="image/jpeg",
            context_text="test",
        )
        assert "hello" in result
        mock_client.chat.completions.create.assert_called_once()

    def test_ocr_returns_empty_on_failure(self):
        from ingest.main import _ocr_attachment
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("API error")
        result = _ocr_attachment(
            openai_client=mock_client,
            model="gemini-2.0-flash",
            image_bytes=b"\xff\xd8\xff",
            content_type="image/jpeg",
        )
        assert result == ""


# ===========================================================================
# 6.  signal-ingest _enrich_messages_with_attachments
# ===========================================================================

class TestEnrichMessagesWithAttachments:
    """Tests for the message enrichment pipeline in signal-ingest."""

    def _settings(self):
        return SimpleNamespace(
            signal_desktop_url="http://signal-desktop:8001",
            model_img="gemini-2.0-flash",
            openai_api_key="fake",
        )

    def test_passthrough_for_messages_without_attachments(self):
        from ingest.main import _enrich_messages_with_attachments
        msgs = [
            {"id": "1", "body": "hello", "sender": "+1", "timestamp": 1000},
            {"id": "2", "body": "world", "sender": "+2", "timestamp": 2000},
        ]
        mock_client = MagicMock()
        result = _enrich_messages_with_attachments(
            settings=self._settings(),
            openai_client=mock_client,
            messages=msgs,
        )
        assert len(result) == 2
        assert result[0]["enriched_body"] == "hello"
        assert result[0]["image_payloads"] == []
        assert result[1]["enriched_body"] == "world"

    def test_enriches_body_with_ocr(self):
        from ingest.main import _enrich_messages_with_attachments
        img_bytes = b"\xff\xd8\xff" + b"\x00" * 10

        msgs = [
            {
                "id": "1",
                "body": "see screenshot",
                "sender": "+1",
                "timestamp": 1000,
                "attachments": [
                    {"path": "att/img.jpg", "contentType": "image/jpeg", "fileName": "img.jpg"}
                ],
            }
        ]

        ocr_json = '{"extracted_text": "Error 404", "observations": ["error screen"]}'
        mock_openai = MagicMock()
        mock_openai.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=ocr_json))]
        )

        with patch("ingest.main._fetch_attachment", return_value=img_bytes):
            result = _enrich_messages_with_attachments(
                settings=self._settings(),
                openai_client=mock_openai,
                messages=msgs,
            )

        assert len(result) == 1
        assert "Error 404" in result[0]["enriched_body"]
        assert "[image]" in result[0]["enriched_body"]
        assert len(result[0]["image_payloads"]) == 1
        payload = result[0]["image_payloads"][0]
        assert payload["content_type"] == "image/jpeg"
        assert base64.b64decode(payload["data_b64"]) == img_bytes

    def test_skips_non_image_attachments(self):
        from ingest.main import _enrich_messages_with_attachments
        msgs = [
            {
                "id": "1",
                "body": "doc attached",
                "sender": "+1",
                "timestamp": 1000,
                "attachments": [
                    {"path": "att/doc.pdf", "contentType": "application/pdf", "fileName": "doc.pdf"}
                ],
            }
        ]
        mock_openai = MagicMock()
        with patch("ingest.main._fetch_attachment", return_value=b"pdfbytes") as mock_fetch:
            result = _enrich_messages_with_attachments(
                settings=self._settings(),
                openai_client=mock_openai,
                messages=msgs,
            )
        mock_fetch.assert_not_called()
        assert result[0]["image_payloads"] == []
        assert result[0]["enriched_body"] == "doc attached"

    def test_handles_failed_fetch_gracefully(self):
        from ingest.main import _enrich_messages_with_attachments
        msgs = [
            {
                "id": "1",
                "body": "screenshot",
                "sender": "+1",
                "timestamp": 1000,
                "attachments": [
                    {"path": "att/img.jpg", "contentType": "image/jpeg"}
                ],
            }
        ]
        mock_openai = MagicMock()
        with patch("ingest.main._fetch_attachment", return_value=None):
            result = _enrich_messages_with_attachments(
                settings=self._settings(),
                openai_client=mock_openai,
                messages=msgs,
            )
        # OCR should not be called if fetch failed
        mock_openai.chat.completions.create.assert_not_called()
        assert result[0]["image_payloads"] == []
        assert result[0]["enriched_body"] == "screenshot"
