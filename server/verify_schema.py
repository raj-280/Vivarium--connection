"""
verify_schema.py — Stage 2 schema verification

Run from the server/ directory:
    python verify_schema.py

What it does:
1. Imports settings (reads .env if present, uses defaults otherwise).
2. Calls create_tables() to create vivarium.db with all 12 tables.
3. Queries sqlite_master to list every table actually created.
4. Prints each table name and its column list for manual cross-check against Section 3.
5. Verifies the UNIQUE constraint on image_records.s3_key.
6. Verifies FK enforcement (PRAGMA foreign_keys=ON).
"""

import sys
import os

# Make sure we can import server packages when run from server/
sys.path.insert(0, os.path.dirname(__file__))

from config.settings import settings
from db.database import engine, create_tables
from sqlalchemy import inspect, text

EXPECTED_TABLES = {
    "users",
    "racks",
    "user_rack_assignments",
    "image_records",
    "audit_log",
    "scan_sessions",
    "scan_schedule",
    "device_pool",
    "provision_tokens",
    "certificates",
    "pending_commands",
    "capture_attribution",
}


def main():
    print("=" * 65)
    print("  Vivarium Gantry System — Stage 2 Schema Verification")
    print("=" * 65)
    print(f"\nDATABASE_URL : {settings.DATABASE_URL}")
    print(f"S3_ENABLED   : {settings.S3_ENABLED}  (should be False)")
    print(f"CACHE_BACKEND: {settings.CACHE_BACKEND}  (should be sqlite)")
    print()

    # ------------------------------------------------------------------
    # Create all tables
    # ------------------------------------------------------------------
    print("Creating tables...")
    create_tables()
    print("Done.\n")

    # ------------------------------------------------------------------
    # List tables actually present
    # ------------------------------------------------------------------
    inspector = inspect(engine)
    actual_tables = set(inspector.get_table_names())

    print(f"{'Table':<30} {'Section 3 ref':<20} {'Status'}")
    print("-" * 65)

    section_refs = {
        "users":                  "3.2",
        "racks":                  "3.1",
        "user_rack_assignments":  "3.3",
        "image_records":          "3.4",
        "audit_log":              "3.5",
        "scan_sessions":          "3.6",
        "scan_schedule":          "3.7",
        "device_pool":            "3.8",
        "provision_tokens":       "3.9",
        "certificates":           "3.10",
        "pending_commands":       "3.11",
        "capture_attribution":    "3.12",
    }

    all_ok = True
    for table in sorted(EXPECTED_TABLES):
        present = table in actual_tables
        status = "[OK]  present" if present else "[MISSING]"
        ref = section_refs.get(table, "?")
        print(f"  {table:<28} {ref:<20} {status}")
        if not present:
            all_ok = False

    # Catch any unexpected extra tables
    extras = actual_tables - EXPECTED_TABLES
    if extras:
        for t in sorted(extras):
            print(f"  {t:<28} {'--':<20} [WARN] UNEXPECTED")

    print()

    # ------------------------------------------------------------------
    # Column detail for each table
    # ------------------------------------------------------------------
    print("Column details:")
    print("-" * 65)
    for table in sorted(actual_tables):
        cols = inspector.get_columns(table)
        print(f"\n  [{table}]")
        for col in cols:
            nullable = "" if col["nullable"] else " NOT NULL"
            pk = " PK" if col.get("primary_key") else ""
            print(f"    {col['name']:<35} {str(col['type']):<20}{nullable}{pk}")

    print()

    # ------------------------------------------------------------------
    # Verify UNIQUE constraint on image_records.s3_key (Section 3.4)
    # ------------------------------------------------------------------
    print("Constraint checks:")
    print("-" * 65)
    uq_constraints = inspector.get_unique_constraints("image_records")
    uq_cols = [set(u["column_names"]) for u in uq_constraints]
    s3_key_unique = {"s3_key"} in uq_cols

    # Also check via index (SQLite UNIQUE constraints appear as unique indexes)
    indexes = inspector.get_indexes("image_records")
    s3_key_idx_unique = any(
        idx.get("unique") and "s3_key" in idx["column_names"]
        for idx in indexes
    )

    if s3_key_unique or s3_key_idx_unique:
        print("  image_records.s3_key UNIQUE constraint : [OK]  present")
    else:
        print("  image_records.s3_key UNIQUE constraint : [MISSING]")
        all_ok = False

    # ------------------------------------------------------------------
    # Verify FK enforcement is ON (SQLite PRAGMA)
    # ------------------------------------------------------------------
    with engine.connect() as conn:
        fk_result = conn.execute(text("PRAGMA foreign_keys")).fetchone()
        fk_on = fk_result[0] == 1 if fk_result else False
    print(f"  SQLite PRAGMA foreign_keys=ON         : {'[OK]  ON' if fk_on else '[FAIL] OFF'}")
    if not fk_on:
        all_ok = False

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print()
    print("=" * 65)
    if all_ok:
        print("  [PASS]  ALL 12 TABLES PRESENT -- schema matches Section 3")
    else:
        print("  [FAIL]  VERIFICATION FAILED -- see errors above")
    print("=" * 65)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
