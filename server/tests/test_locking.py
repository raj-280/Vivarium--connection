"""
server/tests/test_locking.py
============================

Tests for POST /rack/{id}/lock and DELETE /rack/{id}/lock.

Key insight: require_rack_operator checks user_rack_assignments.
- Admins bypass this check entirely.
- Operators need an explicit assignment row via assign_rack().

What is tested
--------------
  1.  Lock on unknown rack → 404
  2.  Lock on existing rack by admin → 200 result="acquired"
  3.  Lock by operator with rack assignment → 200 result="acquired"
  4.  Lock by operator WITHOUT rack assignment → 403
  5.  Lock response has rack_id
  6.  Double lock by different user → 409 Conflict
  7.  Invalid lock_type → 400
  8.  Unauthenticated lock → 401
  9.  Release by lock holder → 200 result="released"
  10. Release on unlocked rack → 200 result="not_locked"
  11. Release on unknown rack → 404
  12. Non-holder operator cannot release → 403
  13. Admin can force-release another user's lock
"""

from __future__ import annotations

import pytest
from .conftest import auth, make_rack, assign_rack, make_user, get_token


class TestAcquireLock:
    """POST /rack/{rack_id}/lock."""

    def test_lock_unknown_rack_is_404(self, client, admin_token):
        resp = client.post("/rack/no-such-rack/lock", json={"lock_type": "motion"}, headers=auth(admin_token))
        assert resp.status_code == 404

    def test_admin_can_lock_rack(self, client, db, admin_token):
        make_rack(db, "rack-lock-01")
        resp = client.post("/rack/rack-lock-01/lock", json={"lock_type": "motion"}, headers=auth(admin_token))
        assert resp.status_code == 200
        assert resp.json()["result"] == "acquired"

    def test_operator_with_assignment_can_lock(self, client, db, operator_user, operator_token):
        make_rack(db, "rack-lock-02")
        assign_rack(db, operator_user.id, "rack-lock-02")
        resp = client.post("/rack/rack-lock-02/lock", json={"lock_type": "motion"}, headers=auth(operator_token))
        assert resp.status_code == 200
        assert resp.json()["result"] == "acquired"

    def test_operator_without_assignment_is_403(self, client, db, operator_token):
        make_rack(db, "rack-lock-03")
        # No assignment added → 403
        resp = client.post("/rack/rack-lock-03/lock", json={"lock_type": "motion"}, headers=auth(operator_token))
        assert resp.status_code == 403

    def test_lock_response_has_rack_id(self, client, db, admin_token):
        make_rack(db, "rack-lock-04")
        resp = client.post("/rack/rack-lock-04/lock", json={"lock_type": "motion"}, headers=auth(admin_token))
        assert resp.json()["rack_id"] == "rack-lock-04"

    def test_double_lock_is_409(self, client, db, admin_token, operator_user, operator_token):
        make_rack(db, "rack-lock-05")
        assign_rack(db, operator_user.id, "rack-lock-05")
        # Admin locks first
        client.post("/rack/rack-lock-05/lock", json={"lock_type": "motion"}, headers=auth(admin_token))
        # Operator tries to lock the same rack
        resp = client.post("/rack/rack-lock-05/lock", json={"lock_type": "motion"}, headers=auth(operator_token))
        assert resp.status_code == 409

    def test_invalid_lock_type_is_400(self, client, db, admin_token):
        make_rack(db, "rack-lock-06")
        resp = client.post("/rack/rack-lock-06/lock", json={"lock_type": "superlock"}, headers=auth(admin_token))
        assert resp.status_code == 400

    def test_unauthenticated_is_401(self, client, db):
        make_rack(db, "rack-lock-07")
        resp = client.post("/rack/rack-lock-07/lock", json={"lock_type": "motion"})
        assert resp.status_code == 401


class TestReleaseLock:
    """DELETE /rack/{rack_id}/lock."""

    def test_admin_release_own_lock(self, client, db, admin_token):
        make_rack(db, "rack-rel-01")
        client.post("/rack/rack-rel-01/lock", json={"lock_type": "motion"}, headers=auth(admin_token))
        resp = client.delete("/rack/rack-rel-01/lock", headers=auth(admin_token))
        assert resp.status_code == 200
        assert resp.json()["result"] == "released"

    def test_release_when_not_locked_is_not_locked(self, client, db, admin_token):
        make_rack(db, "rack-rel-02")
        resp = client.delete("/rack/rack-rel-02/lock", headers=auth(admin_token))
        assert resp.status_code == 200
        assert resp.json()["result"] == "not_locked"

    def test_release_unknown_rack_is_404(self, client, admin_token):
        resp = client.delete("/rack/no-such-rack/lock", headers=auth(admin_token))
        assert resp.status_code == 404

    def test_admin_can_force_release_operator_lock(self, client, db, admin_token, operator_user, operator_token):
        make_rack(db, "rack-rel-03")
        assign_rack(db, operator_user.id, "rack-rel-03")
        client.post("/rack/rack-rel-03/lock", json={"lock_type": "motion"}, headers=auth(operator_token))
        # Admin force-releases
        resp = client.delete("/rack/rack-rel-03/lock", headers=auth(admin_token))
        assert resp.status_code == 200
        assert resp.json()["result"] == "released"

    def test_non_holder_operator_cannot_release(self, client, db, operator_user, operator_token, admin_token):
        make_rack(db, "rack-rel-04")
        assign_rack(db, operator_user.id, "rack-rel-04")
        # Admin locks it
        client.post("/rack/rack-rel-04/lock", json={"lock_type": "motion"}, headers=auth(admin_token))
        # Operator (not holder) tries to release
        resp = client.delete("/rack/rack-rel-04/lock", headers=auth(operator_token))
        assert resp.status_code == 403

    def test_operator_can_release_own_lock(self, client, db, operator_user, operator_token):
        make_rack(db, "rack-rel-05")
        assign_rack(db, operator_user.id, "rack-rel-05")
        client.post("/rack/rack-rel-05/lock", json={"lock_type": "motion"}, headers=auth(operator_token))
        resp = client.delete("/rack/rack-rel-05/lock", headers=auth(operator_token))
        assert resp.status_code == 200
        assert resp.json()["result"] == "released"
