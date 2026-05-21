"""
migrate_sqlite_to_pg.py — Migrate local SQLite equb.db to local PostgreSQL equb_local.

Usage:
    python migrate_sqlite_to_pg.py

Prerequisites:
    - PostgreSQL running on localhost:5432
    - Database 'equb_local' already created
    - equb.db exists in the same directory
"""

import os, sys, sqlite3
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker

# Boolean columns in each table (SQLite stores as 0/1, PG needs True/False)
BOOL_COLS = {
    "users":         {"is_active"},
    "member_spots":  {"is_active"},
    "weeks":         {"is_group_week", "is_worker_week"},
    "notification_settings": {"is_active"},
    "notification_templates": {"is_active"},
}

# ── Config ────────────────────────────────────────────────────────────────────
SQLITE_URL = "sqlite:///./equb.db"
PG_URL     = "postgresql://postgres@localhost:5432/equb_local"
ENV_FILE   = os.path.join(os.path.dirname(__file__), ".env")

# ── Engines ───────────────────────────────────────────────────────────────────
print("Connecting to SQLite...")
sqlite_engine = create_engine(SQLITE_URL, connect_args={"check_same_thread": False})

print("Connecting to PostgreSQL...")
pg_engine = create_engine(PG_URL)
try:
    with pg_engine.connect() as c:
        c.execute(text("SELECT 1"))
    print("  PostgreSQL connection OK")
except Exception as e:
    print(f"  ERROR: Cannot connect to PostgreSQL: {e}")
    sys.exit(1)

# ── Create schema in PostgreSQL ───────────────────────────────────────────────
print("\nCreating schema in PostgreSQL...")
# Temporarily set DATABASE_URL so database.py uses PostgreSQL
os.environ["DATABASE_URL"] = PG_URL

# Import after setting env var so engine picks up PG
sys.path.insert(0, os.path.dirname(__file__))
from database import Base, init_db

# We need to reinitialize with PG engine
import database as db_module
db_module.DATABASE_URL = PG_URL
db_module.engine = pg_engine
db_module.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=pg_engine)

Base.metadata.create_all(bind=pg_engine)
print("  Schema created")

# ── Migrate data ──────────────────────────────────────────────────────────────
sqlite_conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), "equb.db"))
sqlite_conn.row_factory = sqlite3.Row

def convert_row(table_name: str, row: sqlite3.Row) -> dict:
    """Convert SQLite row to dict, fixing boolean types for PostgreSQL."""
    d = dict(row)
    bool_cols = BOOL_COLS.get(table_name, set())
    for col in bool_cols:
        if col in d and d[col] is not None:
            d[col] = bool(d[col])
    return d


def migrate_table(table_name: str, pk_seq: str = None):
    """Copy all rows from SQLite table to PostgreSQL."""
    rows = sqlite_conn.execute(f"SELECT * FROM {table_name}").fetchall()
    if not rows:
        print(f"  {table_name}: 0 rows (skipped)")
        return

    cols = list(rows[0].keys())
    placeholders = ", ".join([f":{c}" for c in cols])
    col_names = ", ".join(cols)
    insert_sql = f"INSERT INTO {table_name} ({col_names}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"

    with pg_engine.begin() as conn:
        for row in rows:
            conn.execute(text(insert_sql), convert_row(table_name, row))

        # Reset sequence if table has serial PK
        if pk_seq:
            conn.execute(text(
                f"SELECT setval('{pk_seq}', COALESCE((SELECT MAX(id) FROM {table_name}), 1))"
            ))

    print(f"  {table_name}: {len(rows)} rows migrated")


print("\nMigrating data...")
tables_with_seqs = [
    ("settings",               "settings_id_seq"),
    ("users",                  "users_id_seq"),
    ("cycles",                 "cycles_id_seq"),
    ("spots",                  "spots_id_seq"),
    ("members",                "members_id_seq"),
    ("member_spots",           "member_spots_id_seq"),
    ("weeks",                  "weeks_id_seq"),
    ("payment_batches",        "payment_batches_id_seq"),
    ("payments",               "payments_id_seq"),
    ("pot_transactions",       "pot_transactions_id_seq"),
    ("pot_disbursements",      "pot_disbursements_id_seq"),
    ("notification_settings",  "notification_settings_id_seq"),
    ("notification_templates", "notification_templates_id_seq"),
    ("notification_logs",      "notification_logs_id_seq"),
]

for tbl, seq in tables_with_seqs:
    try:
        migrate_table(tbl, seq)
    except Exception as e:
        # Table may not exist in old SQLite
        print(f"  {tbl}: SKIPPED ({e})")

sqlite_conn.close()

# ── Write .env ────────────────────────────────────────────────────────────────
print(f"\nWriting {ENV_FILE}...")
with open(ENV_FILE, "w") as f:
    f.write(f"DATABASE_URL={PG_URL}\n")
print(f"  DATABASE_URL={PG_URL}")

# ── Verify ────────────────────────────────────────────────────────────────────
print("\nVerifying row counts in PostgreSQL:")
with pg_engine.connect() as conn:
    for tbl, _ in tables_with_seqs[:10]:
        try:
            count = conn.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar()
            print(f"  {tbl}: {count}")
        except Exception:
            pass

print("\n✅ Migration complete!")
print("   Start the app with: uvicorn main:app --reload")
