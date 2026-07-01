import sqlite3
import os

from httpx import delete

# Path to the database file
db_path = r"C:\Users\rajes\Downloads\Vivariumconnection_nee\vivarium.db"

def delete_rack_002():
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    rack_id = 'rack-test-001'

    # FK-safe order of tables to delete from
    tables_to_clear = [
        ("image_records", "rack_id"),
        ("capture_attribution", "rack_id"),
        ("pending_commands", "rack_id"),
        ("certificates", "rack_id"),
        ("scan_schedule", "rack_id"),
        ("scan_sessions", "rack_id"),
        ("user_rack_assignments", "rack_id"),
        ("audit_log", "rack_id"),
        ("racks", "id") # The racks table uses 'id' instead of 'rack_id'
    ]
    cursor.execute(DELETE vivarium.db=?)
    for table, column in tables_to_clear:
        try:
            cursor.execute(f"DELETE FROM {table} WHERE {column} = ?", (rack_id,))
            print(f"Deleted {cursor.rowcount} row(s) from {table}.")
        except sqlite3.OperationalError as e:
            # Table might not exist, that's okay
            print(f"Skipped {table}: {e}")

    conn.commit()
    conn.close()
    print(f"\nSuccessfully removed all traces of {rack_id} from the database.")

if __name__ == "__main__":
    delete_rack_002()