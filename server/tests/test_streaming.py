"""
server/tests/test_streaming.py
================================

Tests for server/services/streaming.py — no real MediaMTX, no real Pi,
no real WebSocket connections needed.

Pattern used throughout
-----------------------
  - settings patched via unittest.mock.patch so tests are not coupled to .env
  - urllib.request.urlopen patched with a fake context manager that returns
    controlled JSON — same boundary the real code hits
  - ws_registry.broadcast_from_thread patched to a MagicMock so we can
    assert what was sent without needing a real event loop or WebSocket

What is tested
--------------
build_stream_url
  1.  Returns correct type="stream_url"
  2.  URL contains MEDIAMTX_PI_HOST when set
  3.  URL contains rack_id in path
  4.  WHEP URL ends with /whep
  5.  MJPEG URL ends with /mjpeg
  6.  Returns empty strings when MEDIAMTX_PI_HOST is blank

build_stream_close
  7.  Returns correct type="stream_close"
  8.  data.rack_id matches the argument

check_mediamtx_stream
  9.  Returns True when MEDIAMTX_INTERNAL_URL is blank (skip probe)
  10. Returns True when rack_id found in API response (ready=False is OK)
  11. Returns False when rack_id NOT in API response
  12. Returns False when API returns non-200
  13. Returns False when API is unreachable (OSError)
  14. Returns True when rack_id found with ready=True

register_path_on_central_mediamtx (new function)
  15. Calls correct URL: http://host:9997/v3/config/paths/add/{rack_id}
  16. Sends correct JSON body: source + sourceOnDemand
  17. Returns True on HTTP 200
  18. Returns False when API call raises OSError (central server down)
"""

from __future__ import annotations

import json
import sys
import os
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from contextlib import contextmanager

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────
SERVER_DIR = Path(__file__).parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

os.environ.setdefault("DATABASE_URL",        "sqlite:///:memory:")
os.environ.setdefault("PROVISIONING_SECRET", "test-secret")
os.environ.setdefault("MQTT_BROKER",         "localhost")
os.environ.setdefault("S3_ENABLED",          "false")
os.environ.setdefault("CACHE_BACKEND",       "sqlite")


# ── Fake HTTP response helper ─────────────────────────────────────────────────

class _FakeResponse:
    """
    Minimal stand-in for the object returned by urllib.request.urlopen().
    Supports the context-manager protocol that streaming.py uses:
        with urllib.request.urlopen(req, timeout=...) as resp:
            resp.status
            resp.read()
    """
    def __init__(self, status: int, body: dict):
        self.status = status
        self._body = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _fake_urlopen(status: int, body: dict):
    """Return a patcher-compatible callable that yields _FakeResponse."""
    return lambda *args, **kwargs: _FakeResponse(status, body)


def _mediamtx_paths_response(*rack_ids: str, ready: bool = False) -> dict:
    """Build the JSON structure MediaMTX /v3/paths/list returns."""
    return {
        "itemCount": len(rack_ids),
        "pageCount": 1,
        "items": [{"name": r, "ready": ready} for r in rack_ids],
    }


# =============================================================================
# build_stream_url
# =============================================================================

class TestBuildStreamUrl:

    def _build(self, host: str, webrtc_port: int = 8889, mjpeg_port: int = 8888):
        from services.streaming import build_stream_url
        with patch("services.streaming.settings") as mock_settings:
            mock_settings.MEDIAMTX_PI_HOST    = host
            mock_settings.MEDIAMTX_WEBRTC_PORT = webrtc_port
            mock_settings.MEDIAMTX_MJPEG_PORT  = mjpeg_port
            return build_stream_url("rack-001")

    def test_returns_stream_url_type(self):
        msg = self._build("192.168.1.50")
        assert msg["type"] == "stream_url"

    def test_url_contains_pi_host(self):
        msg = self._build("192.168.1.50")
        assert "192.168.1.50" in msg["data"]["url"]

    def test_url_contains_rack_id(self):
        msg = self._build("192.168.1.50")
        assert "rack-001" in msg["data"]["url"]

    def test_whep_url_ends_with_whep(self):
        msg = self._build("192.168.1.50")
        assert msg["data"]["url"].endswith("/whep")

    def test_mjpeg_url_ends_with_mjpeg(self):
        msg = self._build("192.168.1.50")
        assert msg["data"]["mjpeg_url"].endswith("/mjpeg")

    def test_empty_host_returns_empty_urls(self):
        msg = self._build("")
        assert msg["data"]["url"] == ""
        assert msg["data"]["mjpeg_url"] == ""

    def test_data_contains_rack_id_field(self):
        msg = self._build("192.168.1.50")
        assert msg["data"]["rack_id"] == "rack-001"

    def test_webrtc_port_in_url(self):
        msg = self._build("192.168.1.50", webrtc_port=9999)
        assert "9999" in msg["data"]["url"]

    def test_mjpeg_port_in_url(self):
        msg = self._build("192.168.1.50", mjpeg_port=7777)
        assert "7777" in msg["data"]["mjpeg_url"]


# =============================================================================
# build_stream_close
# =============================================================================

class TestBuildStreamClose:

    def test_returns_stream_close_type(self):
        from services.streaming import build_stream_close
        msg = build_stream_close("rack-001")
        assert msg["type"] == "stream_close"

    def test_data_rack_id_matches(self):
        from services.streaming import build_stream_close
        msg = build_stream_close("rack-042")
        assert msg["data"]["rack_id"] == "rack-042"

    def test_different_rack_ids(self):
        from services.streaming import build_stream_close
        assert build_stream_close("rack-001")["data"]["rack_id"] == "rack-001"
        assert build_stream_close("rack-099")["data"]["rack_id"] == "rack-099"


# =============================================================================
# check_mediamtx_stream
# =============================================================================

class TestCheckMediamtxStream:

    def _check(self, rack_id: str, internal_url: str, urlopen_side_effect=None):
        from services.streaming import check_mediamtx_stream
        with patch("services.streaming.settings") as mock_settings:
            mock_settings.MEDIAMTX_INTERNAL_URL = internal_url
            if urlopen_side_effect is not None:
                with patch("services.streaming.urllib.request.urlopen",
                           side_effect=urlopen_side_effect):
                    return check_mediamtx_stream(rack_id)
            return check_mediamtx_stream(rack_id)

    def test_blank_url_returns_true(self):
        # No probe at all when MEDIAMTX_INTERNAL_URL is blank
        result = self._check("rack-001", internal_url="")
        assert result is True

    def test_rack_found_ready_false_returns_true(self):
        # Path registered but ready=False (sourceOnDemand, no viewer yet)
        # must still return True — camera opens on first WHEP connection
        body = _mediamtx_paths_response("rack-001", ready=False)
        result = self._check(
            "rack-001",
            internal_url="http://192.168.1.50:9997",
            urlopen_side_effect=_fake_urlopen(200, body),
        )
        assert result is True

    def test_rack_found_ready_true_returns_true(self):
        body = _mediamtx_paths_response("rack-001", ready=True)
        result = self._check(
            "rack-001",
            internal_url="http://192.168.1.50:9997",
            urlopen_side_effect=_fake_urlopen(200, body),
        )
        assert result is True

    def test_rack_not_found_returns_false(self):
        # API is up but this rack's path is not registered
        body = _mediamtx_paths_response("rack-002", "rack-003")
        result = self._check(
            "rack-001",
            internal_url="http://192.168.1.50:9997",
            urlopen_side_effect=_fake_urlopen(200, body),
        )
        assert result is False

    def test_non_200_returns_false(self):
        body = {"error": "not found"}
        result = self._check(
            "rack-001",
            internal_url="http://192.168.1.50:9997",
            urlopen_side_effect=_fake_urlopen(500, body),
        )
        assert result is False

    def test_oserror_returns_false(self):
        # MediaMTX not running — connection refused
        result = self._check(
            "rack-001",
            internal_url="http://192.168.1.50:9997",
            urlopen_side_effect=OSError("Connection refused"),
        )
        assert result is False

    def test_empty_items_returns_false(self):
        body = {"itemCount": 0, "pageCount": 0, "items": []}
        result = self._check(
            "rack-001",
            internal_url="http://192.168.1.50:9997",
            urlopen_side_effect=_fake_urlopen(200, body),
        )
        assert result is False

    def test_multiple_racks_correct_one_found(self):
        body = _mediamtx_paths_response("rack-001", "rack-002", "rack-003")
        result = self._check(
            "rack-002",
            internal_url="http://192.168.1.50:9997",
            urlopen_side_effect=_fake_urlopen(200, body),
        )
        assert result is True

    def test_multiple_racks_correct_one_missing(self):
        body = _mediamtx_paths_response("rack-002", "rack-003")
        result = self._check(
            "rack-001",
            internal_url="http://192.168.1.50:9997",
            urlopen_side_effect=_fake_urlopen(200, body),
        )
        assert result is False


# =============================================================================
# register_path_on_central_mediamtx  (new function in provisioning.py)
# =============================================================================

class TestRegisterPathOnCentralMediamtx:
    """
    Tests for the function that hot-adds a rack path to the central
    MediaMTX server after a Pi provisions itself.

    The function signature expected:
        register_path_on_central_mediamtx(rack_id: str, pi_ip: str,
                                           pi_rtsp_port: int = 8554) -> bool
    """

    def _call(self, rack_id: str, pi_ip: str, pi_rtsp_port: int = 8554,
              urlopen_side_effect=None, mediamtx_url: str = "http://127.0.0.1:9997"):
        from services.provisioning import register_path_on_central_mediamtx
        with patch("services.provisioning.settings") as mock_settings:
            mock_settings.MEDIAMTX_INTERNAL_URL = mediamtx_url
            if urlopen_side_effect is not None:
                with patch("services.provisioning.urllib.request.urlopen",
                           side_effect=urlopen_side_effect):
                    return register_path_on_central_mediamtx(rack_id, pi_ip, pi_rtsp_port)
            with patch("services.provisioning.urllib.request.urlopen",
                       side_effect=_fake_urlopen(200, {})):
                return register_path_on_central_mediamtx(rack_id, pi_ip, pi_rtsp_port)

    def test_returns_true_on_200(self):
        result = self._call("rack-001", "192.168.1.50")
        assert result is True

    def test_returns_false_on_oserror(self):
        result = self._call(
            "rack-001", "192.168.1.50",
            urlopen_side_effect=OSError("Connection refused"),
        )
        assert result is False

    def test_calls_correct_url(self):
        from services.provisioning import register_path_on_central_mediamtx
        captured = {}

        def fake_urlopen(req, **kwargs):
            captured["url"] = req.full_url
            return _FakeResponse(200, {})

        with patch("services.provisioning.settings") as mock_settings:
            mock_settings.MEDIAMTX_INTERNAL_URL = "http://127.0.0.1:9997"
            with patch("services.provisioning.urllib.request.urlopen",
                       side_effect=fake_urlopen):
                register_path_on_central_mediamtx("rack-001", "192.168.1.50")

        assert captured["url"] == "http://127.0.0.1:9997/v3/config/paths/add/rack-001"

    def test_sends_correct_source_in_body(self):
        from services.provisioning import register_path_on_central_mediamtx
        captured = {}

        def fake_urlopen(req, **kwargs):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResponse(200, {})

        with patch("services.provisioning.settings") as mock_settings:
            mock_settings.MEDIAMTX_INTERNAL_URL = "http://127.0.0.1:9997"
            with patch("services.provisioning.urllib.request.urlopen",
                       side_effect=fake_urlopen):
                register_path_on_central_mediamtx("rack-001", "192.168.1.50", 8554)

        assert captured["body"]["source"] == "rtsp://192.168.1.50:8554/rack-001"

    def test_sends_source_on_demand_true(self):
        from services.provisioning import register_path_on_central_mediamtx
        captured = {}

        def fake_urlopen(req, **kwargs):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResponse(200, {})

        with patch("services.provisioning.settings") as mock_settings:
            mock_settings.MEDIAMTX_INTERNAL_URL = "http://127.0.0.1:9997"
            with patch("services.provisioning.urllib.request.urlopen",
                       side_effect=fake_urlopen):
                register_path_on_central_mediamtx("rack-001", "192.168.1.50")

        assert captured["body"]["sourceOnDemand"] is True

    def test_custom_rtsp_port_in_source(self):
        from services.provisioning import register_path_on_central_mediamtx
        captured = {}

        def fake_urlopen(req, **kwargs):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResponse(200, {})

        with patch("services.provisioning.settings") as mock_settings:
            mock_settings.MEDIAMTX_INTERNAL_URL = "http://127.0.0.1:9997"
            with patch("services.provisioning.urllib.request.urlopen",
                       side_effect=fake_urlopen):
                register_path_on_central_mediamtx("rack-001", "192.168.1.50", 9999)

        assert "9999" in captured["body"]["source"]

    def test_skips_when_mediamtx_url_blank(self):
        from services.provisioning import register_path_on_central_mediamtx
        with patch("services.provisioning.settings") as mock_settings:
            mock_settings.MEDIAMTX_INTERNAL_URL = ""
            with patch("services.provisioning.urllib.request.urlopen") as mock_urlopen:
                result = register_path_on_central_mediamtx("rack-001", "192.168.1.50")

        # urlopen must never be called when URL is blank
        mock_urlopen.assert_not_called()
        assert result is False
