"""
server/tests/test_provisioning.py
==================================

Tests for POST /provision — Pi first-boot provisioning.

What is tested
--------------
  1.  Wrong provisioning_secret → 401
  2.  Valid secret → 200 with all required credential fields
  3.  device_id follows rack-NNN pattern
  4.  rtsp_password is non-empty  (BUG-15 regression)
  5.  mqtt_password is non-empty  (256-bit token)
  6.  presign_api_key is non-empty
  7.  server_host starts with "http"
  8.  Idempotent: same cpu_serial → same device_id
  9.  Different cpu_serials → different device_ids
"""

from __future__ import annotations

import pytest

# Must match os.environ default set in conftest.py
VALID_SECRET = "test-provisioning-secret"


def provision(client, cpu_serial: str, secret: str = VALID_SECRET):
    return client.post("/provision", json={
        "cpu_serial": cpu_serial,
        "provisioning_secret": secret,
    })


class TestProvisioning:

    def test_wrong_secret_is_401(self, client):
        resp = provision(client, "SER-WRONG", secret="bad-secret")
        assert resp.status_code == 401

    def test_valid_provision_returns_200(self, client):
        resp = provision(client, "SER-001")
        assert resp.status_code == 200

    def test_has_device_id(self, client):
        body = provision(client, "SER-002").json()
        assert "device_id" in body

    def test_device_id_pattern(self, client):
        device_id = provision(client, "SER-003").json()["device_id"]
        parts = device_id.split("-")
        assert len(parts) == 2
        assert parts[0] == "rack"
        assert parts[1].isdigit()

    def test_rtsp_password_not_empty(self, client):
        """BUG-15 regression: rtsp_password must never be absent or empty."""
        body = provision(client, "SER-004").json()
        assert "rtsp_password" in body
        assert len(body["rtsp_password"]) > 10

    def test_mqtt_password_not_empty(self, client):
        body = provision(client, "SER-005").json()
        assert len(body["mqtt_password"]) > 10

    def test_presign_api_key_not_empty(self, client):
        body = provision(client, "SER-006").json()
        assert len(body["presign_api_key"]) > 10

    def test_server_host_is_http_url(self, client):
        body = provision(client, "SER-007").json()
        assert body["server_host"].startswith("http")

    def test_idempotent_same_device_id(self, client):
        """Same cpu_serial always returns the same device_id (safe to re-flash)."""
        r1 = provision(client, "SER-IDEM")
        r2 = provision(client, "SER-IDEM")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["device_id"] == r2.json()["device_id"]

    def test_different_serials_get_different_ids(self, client):
        r1 = provision(client, "SER-DIFF-A")
        r2 = provision(client, "SER-DIFF-B")
        assert r1.json()["device_id"] != r2.json()["device_id"]
