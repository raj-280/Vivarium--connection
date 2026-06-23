"""
server/tests/test_auth.py
=========================

Tests for POST /auth/login, POST /setup, and POST /admin/users.

What is tested
--------------
  1.  /setup creates first admin → 201 with role="admin"
  2.  /setup second call → 409 Conflict
  3.  /auth/login correct credentials → 200 + JWT token
  4.  /auth/login wrong password → 401
  5.  /auth/login unknown username → 401
  6.  /auth/login returns correct role in payload
  7.  /auth/login returns token_type="bearer"
  8.  /admin/users admin can create operator → 201
  9.  /admin/users duplicate username → 409
  10. /admin/users invalid role → 400
  11. /admin/users non-admin → 403
  12. /admin/users unauthenticated → 401
"""

from __future__ import annotations

import pytest
from .conftest import auth, make_user, get_token


class TestSetup:
    """/setup — first-time admin creation (no auth required)."""

    def test_creates_first_admin(self, client):
        resp = client.post("/setup", json={"username": "firstadmin", "password": "secret123"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["role"] == "admin"
        assert body["username"] == "firstadmin"
        assert "user_id" in body

    def test_second_setup_call_is_409(self, client):
        client.post("/setup", json={"username": "firstadmin", "password": "secret123"})
        resp = client.post("/setup", json={"username": "anotheradmin", "password": "pass"})
        assert resp.status_code == 409

    def test_duplicate_username_is_409(self, client):
        client.post("/setup", json={"username": "firstadmin", "password": "secret123"})
        # Admin already exists → any second call is blocked at that gate
        resp = client.post("/setup", json={"username": "firstadmin", "password": "other"})
        assert resp.status_code == 409


class TestLogin:
    """/auth/login."""

    def test_login_success_returns_200(self, client, admin_token):
        # admin_token fixture creates admin + logs in; token is a non-empty string
        assert isinstance(admin_token, str) and len(admin_token) > 20

    def test_wrong_password_is_401(self, client, db):
        make_user(db, "u-wp", "alice", "correct", "viewer")
        resp = client.post("/auth/login", json={"username": "alice", "password": "wrong"})
        assert resp.status_code == 401

    def test_unknown_user_is_401(self, client):
        resp = client.post("/auth/login", json={"username": "nobody", "password": "pass"})
        assert resp.status_code == 401

    def test_returns_correct_role(self, client, db):
        make_user(db, "u-role", "bob", "bobpass", "operator")
        resp = client.post("/auth/login", json={"username": "bob", "password": "bobpass"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "operator"

    def test_returns_token_type_bearer(self, client, db):
        make_user(db, "u-tt", "carol", "carolpass", "viewer")
        resp = client.post("/auth/login", json={"username": "carol", "password": "carolpass"})
        assert resp.json()["token_type"] == "bearer"

    def test_returns_user_id(self, client, db):
        make_user(db, "u-uid", "dave", "davepass", "operator")
        resp = client.post("/auth/login", json={"username": "dave", "password": "davepass"})
        assert "user_id" in resp.json()


class TestCreateUser:
    """POST /admin/users — admin only."""

    def test_admin_creates_operator(self, client, admin_token):
        resp = client.post(
            "/admin/users",
            json={"username": "newop", "password": "oppass", "role": "operator"},
            headers=auth(admin_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["role"] == "operator"
        assert body["username"] == "newop"

    def test_admin_creates_viewer(self, client, admin_token):
        resp = client.post(
            "/admin/users",
            json={"username": "viewer1", "password": "vpass", "role": "viewer"},
            headers=auth(admin_token),
        )
        assert resp.status_code == 201
        assert resp.json()["role"] == "viewer"

    def test_duplicate_username_is_409(self, client, admin_token):
        client.post("/admin/users", json={"username": "dup", "password": "p", "role": "viewer"}, headers=auth(admin_token))
        resp = client.post("/admin/users", json={"username": "dup", "password": "p2", "role": "viewer"}, headers=auth(admin_token))
        assert resp.status_code == 409

    def test_invalid_role_is_400(self, client, admin_token):
        resp = client.post(
            "/admin/users",
            json={"username": "badrole", "password": "pass", "role": "superuser"},
            headers=auth(admin_token),
        )
        assert resp.status_code == 400

    def test_operator_cannot_create_user(self, client, operator_token):
        resp = client.post(
            "/admin/users",
            json={"username": "other", "password": "pass", "role": "viewer"},
            headers=auth(operator_token),
        )
        assert resp.status_code == 403

    def test_unauthenticated_is_401(self, client):
        resp = client.post(
            "/admin/users",
            json={"username": "other", "password": "pass", "role": "viewer"},
        )
        assert resp.status_code == 401
