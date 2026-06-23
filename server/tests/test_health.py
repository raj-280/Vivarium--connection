"""
server/tests/test_health.py
===========================

Tests for GET /health and GET /racks.

What is tested
--------------
  1.  /health returns 200 with status="ok"
  2.  /health response has timestamp (ISO string)
  3.  /health response has mqtt_connected (bool)
  4.  /health requires no authentication
  5.  /racks returns 200 for an authenticated operator
  6.  /racks returns empty list when no racks provisioned
  7.  /racks returns 401 without a token
"""

from __future__ import annotations

import pytest
from .conftest import auth, make_rack


class TestHealth:
    """GET /health — liveness probe (no auth)."""

    def test_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_has_status_ok(self, client):
        assert client.get("/health").json()["status"] == "ok"

    def test_has_timestamp(self, client):
        data = client.get("/health").json()
        assert "timestamp" in data
        assert isinstance(data["timestamp"], str)
        assert len(data["timestamp"]) > 10

    def test_has_mqtt_connected_flag(self, client):
        data = client.get("/health").json()
        assert "mqtt_connected" in data
        assert isinstance(data["mqtt_connected"], bool)

    def test_no_auth_required(self, client):
        resp = client.get("/health")
        assert resp.status_code not in (401, 403)


class TestListRacks:
    """GET /racks — any authenticated browser user."""

    def test_returns_200_for_operator(self, client, operator_token):
        resp = client.get("/racks", headers=auth(operator_token))
        assert resp.status_code == 200

    def test_returns_empty_list_when_no_racks(self, client, operator_token):
        resp = client.get("/racks", headers=auth(operator_token))
        assert resp.json() == []

    def test_returns_rack_after_provisioning(self, client, db, operator_token):
        make_rack(db, "rack-001")
        resp = client.get("/racks", headers=auth(operator_token))
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.json()]
        assert "rack-001" in ids

    def test_returns_401_without_token(self, client):
        resp = client.get("/racks")
        assert resp.status_code == 401
