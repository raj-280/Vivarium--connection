"""
server/tests/conftest.py - Shared fixtures for all server tests.

DB ISOLATION STRATEGY
---------------------
All tests share one in-memory SQLite engine (StaticPool = single physical
connection).  To keep tests from contaminating each other we use the
"subtransaction / savepoint" pattern:

  outer connection
  |-- BEGIN  (transaction)
       |-- SAVEPOINT sp  (nested)
            |-- test code + route handlers run here
       ROLLBACK TO SAVEPOINT sp   <- test teardown undoes everything
  ROLLBACK                        <- outer also rolled back

The key: every session created inside the test - the fixture's `db` session
AND every session opened by route handlers / background tasks - must bind
to the *same* DBAPI connection so they all see each other's unflushed data
and all get rolled back together.

We achieve this by:
  1. Opening one `connection` per test and starting a real transaction on it.
  2. Creating a Session bound to that connection.  All route-handler sessions
     (via the get_db override) use this same session object.
  3. Patching `db_session()` (used by background tasks) to also yield the
     same session so sweep threads, MQTT handlers, etc. see test data.
  4. After the test, rolling back the connection-level transaction.

SQLite savepoints: SQLAlchemy automatically issues SAVEPOINT / RELEASE
SAVEPOINT when `session.commit()` is called inside an already-open
connection-level transaction, so route-handler commits are safe - they flush
to the connection but don't escape the outer rollback.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# -- Path setup (must be first) -----------------------------------------------
SERVER_DIR = Path(__file__).parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# -- Environment (must be before ANY server imports) --------------------------
os.environ["DATABASE_URL"]          = "sqlite:///:memory:"
os.environ["SECRET_KEY"]            = "test-secret-key-not-for-production"
os.environ["PROVISIONING_SECRET"]   = "test-provisioning-secret"
os.environ["MQTT_BROKER"]           = "localhost"
os.environ["S3_ENABLED"]            = "false"
os.environ["CACHE_BACKEND"]         = "sqlite"

# -- Single shared in-memory engine (StaticPool = one connection throughout) --
TEST_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(
    bind=TEST_ENGINE,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

# -- PATCH the app's engine BEFORE importing main or any server module --------
# This ensures every call to db_session(), get_db(), sweep_expired_locks(),
# provisioning raw connections, etc. all hit the same in-memory DB.
import db.database as _db_module         # noqa: E402
_db_module.engine       = TEST_ENGINE
_db_module.SessionLocal = TestSessionLocal

# -- Now import the app (after patching) --------------------------------------
from db.database import Base, get_db      # noqa: E402
from db.models import User, Rack, UserRackAssignment  # noqa: E402
from core.security import hash_password   # noqa: E402
import core.locking as _locking_module    # noqa: E402
import main as app_module                 # noqa: E402


# -- Create all tables once (session-scoped) ----------------------------------
@pytest.fixture(scope="session", autouse=True)
def create_schema():
    Base.metadata.create_all(bind=TEST_ENGINE)
    yield
    Base.metadata.drop_all(bind=TEST_ENGINE)


# -- Per-test connection with full rollback isolation -------------------------
@pytest.fixture()
def db():
    """
    Yield a SQLAlchemy Session bound to a single DBAPI connection that is
    wrapped in an outer transaction.  All commits that happen *inside* the
    test (including those issued by route handlers via the get_db override)
    are demoted to savepoints by SQLAlchemy, so they never escape to the
    physical SQLite file.  At teardown the outer transaction is rolled back,
    leaving the DB clean for the next test.
    """
    connection = TEST_ENGINE.connect()
    # Begin an outer transaction that we will roll back at teardown.
    trans = connection.begin()
    # Bind a session to this connection.
    session = TestSessionLocal(bind=connection)
    # Open a savepoint so the session's flush calls are scoped
    session.begin_nested()

    yield session

    session.close()
    trans.rollback()   # wipes everything the test inserted
    connection.close()


# -- Sweep-thread helpers ------------------------------------------------------
def _stop_sweep():
    """Stop the lock sweep thread and wait for it to exit cleanly."""
    _locking_module.stop_lock_sweep_task()
    t = _locking_module._sweep_thread
    if t and t.is_alive():
        t.join(timeout=3.0)
    # Disarm the stop event and clear the thread reference so the next
    # start_lock_sweep_task() call (from the lifespan) starts a fresh thread.
    _locking_module._sweep_stop.clear()
    _locking_module._sweep_thread = None


# -- FastAPI TestClient --------------------------------------------------------
@pytest.fixture()
def client(db):
    """
    TestClient wired to the per-test DB session.

    The get_db dependency override returns the *same* session object as the
    `db` fixture so route handlers, provisioning code, and the test itself
    all share one connection/transaction and see each other's data.

    db_session() (used by background tasks) is also patched to the same
    session for the duration of this test so that the sweep thread and any
    MQTT handlers that fire can see test data.

    The lock sweep thread is stopped before and after each test so it never
    fires against a partially-created or rolled-back schema.
    """
    from fastapi.testclient import TestClient

    # Kill any sweep thread left over from a previous test.
    _stop_sweep()

    # Patch db_session() to yield the same per-test session.
    @contextmanager
    def _test_db_session():
        yield db
        # Do NOT commit or close here - the outer `db` fixture manages that.

    original_db_session = _db_module.db_session
    _db_module.db_session = _test_db_session

    def _override():
        yield db

    app_module.app.dependency_overrides[_db_module.get_db] = _override
    with TestClient(app_module.app, raise_server_exceptions=False) as c:
        yield c
    app_module.app.dependency_overrides.clear()
    _db_module.db_session = original_db_session

    # Kill the sweep thread that the app lifespan started during this test.
    _stop_sweep()


# -- User helpers --------------------------------------------------------------
def make_user(db, user_id: str, username: str, password: str, role: str) -> User:
    u = User(
        id=user_id,
        username=username,
        password_hash=hash_password(password),
        role=role,
    )
    db.add(u)
    db.flush()   # write to DB within the current transaction (NOT commit)
    return u


def get_token(client, username: str, password: str) -> str:
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"Login failed for {username!r}: {resp.text}"
    return resp.json()["access_token"]


@pytest.fixture()
def admin_user(db):
    return make_user(db, "admin-001", "testadmin", "adminpass", "admin")


@pytest.fixture()
def admin_token(client, admin_user):
    return get_token(client, "testadmin", "adminpass")


@pytest.fixture()
def operator_user(db):
    return make_user(db, "op-001", "testoperator", "oppass", "operator")


@pytest.fixture()
def operator_token(client, operator_user):
    return get_token(client, "testoperator", "oppass")


# -- Rack helpers --------------------------------------------------------------
def make_rack(db, rack_id: str = "rack-001") -> Rack:
    rack = Rack(
        id=rack_id,
        display_name=rack_id,
        grid_rows=12,
        grid_cols=7,
        mqtt_status="offline",
        camera_status="unknown",
        scan_state="idle",
        maintenance_required=False,
    )
    db.add(rack)
    db.flush()   # within transaction, no commit
    return rack


def assign_rack(db, user_id: str, rack_id: str) -> None:
    db.add(UserRackAssignment(user_id=user_id, rack_id=rack_id))
    db.flush()   # within transaction, no commit


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
