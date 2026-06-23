import sqlite3
import os

db_path = r"C:\Users\rajes\Downloads\Vivariumconnection_nee\vivarium.db"

def update_rack_id():
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        return

    old_id = 'rack-001'
    new_id = 'rack-test-001'

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # All tables that reference rack_id as a foreign key first
    fk_tables = [
        ("image_records", "rack_id"),
        ("capture_attribution", "rack_id"),
        ("pending_commands", "rack_id"),
        ("certificates", "rack_id"),
        ("scan_schedule", "rack_id"),
        ("scan_sessions", "rack_id"),
        ("user_rack_assignments", "rack_id"),
        ("audit_log", "rack_id"),
    ]

    # Update FK tables first
    for table, column in fk_tables:
        try:
            cursor.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (new_id, old_id))
            print(f"Updated {cursor.rowcount} row(s) in {table}.")
        except sqlite3.OperationalError as e:
            print(f"Skipped {table}: {e}")

    # Update the primary racks table last
    try:
        cursor.execute("UPDATE racks SET id = ? WHERE id = ?", (new_id, old_id))
        print(f"Updated {cursor.rowcount} row(s) in racks (id).")
    except sqlite3.OperationalError as e:
        print(f"Skipped racks: {e}")

    conn.commit()
    conn.close()
    print(f"\nSuccessfully renamed rack '{old_id}' to '{new_id}' across all tables.")

if __name__ == "__main__":
    update_rack_id()