"""
server/add_admin.py

One-off helper script to create (or reset) an admin user in the `users` table.

It reuses the exact same hashing function (core.security.hash_password,
bcrypt-based) and the exact same DB session/engine setup (db.database) that
main.py uses at runtime, so the account this creates will work with the
real login endpoint with no extra steps.

IMPORTANT: run this from inside the server/ folder, the same way you run
main.py, so the relative imports (config.settings, db.models, etc.) resolve
correctly:

    cd C:\\Users\\rajes\\Downloads\\Vivariumconnection_nee\\server
    venv\\Scripts\\activate          (if you use the project's venv)
    python add_admin.py

Re-running this script is safe: if the username already exists, it just
resets that user's password/role instead of creating a duplicate.
"""

import uuid

from db.database import db_session, create_tables
from db.models import User
from core.security import hash_password

USERNAME = "admin"
PASSWORD = "admin123"
ROLE = "admin"


def add_admin() -> None:
    # Idempotent — does nothing if tables already exist.
    create_tables()

    with db_session() as db:
        existing = db.query(User).filter_by(username=USERNAME).first()

        if existing:
            existing.password_hash = hash_password(PASSWORD)
            existing.role = ROLE
            print(f"Updated existing user '{USERNAME}' -> role={ROLE}, password reset to '{PASSWORD}'.")
        else:
            user = User(
                id=str(uuid.uuid4()),
                username=USERNAME,
                password_hash=hash_password(PASSWORD),
                role=ROLE,
            )
            db.add(user)
            print(f"Created new admin user '{USERNAME}' (role={ROLE}) with password '{PASSWORD}'.")


if __name__ == "__main__":
    add_admin()
