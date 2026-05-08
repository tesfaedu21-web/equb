"""
setup_local_pg.py — Sets up local PostgreSQL for Equb development.

Usage:
    python setup_local_pg.py <RAILWAY_DATABASE_URL>

Example:
    python setup_local_pg.py "postgresql://postgres:secret@containers.railway.app:5432/railway"

What it does:
1. Creates local PostgreSQL database 'equb_local'
2. Dumps data from Railway PostgreSQL
3. Restores it into equb_local
4. Creates .env file pointing to local DB
"""

import subprocess, sys, os, re

PG_BIN = r"C:\Program Files\PostgreSQL\17\bin"
LOCAL_USER = "postgres"
LOCAL_DB   = "equb_local"
LOCAL_URL  = f"postgresql://{LOCAL_USER}@localhost:5432/{LOCAL_DB}"
ENV_FILE   = os.path.join(os.path.dirname(__file__), ".env")
DUMP_FILE  = os.path.join(os.path.dirname(__file__), "railway_dump.sql")


def run(cmd, env=None, check=True):
    print(f"  >> {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.stdout.strip():
        print("    ", result.stdout.strip()[:300])
    if result.returncode != 0 and check:
        print("  ERROR:", result.stderr.strip()[:500])
        sys.exit(1)
    return result


def pg(sql, dbname="postgres"):
    psql = os.path.join(PG_BIN, "psql.exe")
    return run([psql, "-U", LOCAL_USER, "-d", dbname, "-c", sql], check=False)


def main():
    if len(sys.argv) < 2:
        print("Usage: python setup_local_pg.py <RAILWAY_DATABASE_URL>")
        print()
        print("Get DATABASE_URL from:")
        print("  Railway dashboard → your project → Variables tab → DATABASE_URL")
        sys.exit(1)

    railway_url = sys.argv[1].strip()
    if railway_url.startswith("postgres://"):
        railway_url = railway_url.replace("postgres://", "postgresql://", 1)

    print("\n=== Step 1: Create local database ===")
    pg(f"DROP DATABASE IF EXISTS {LOCAL_DB};")
    pg(f"CREATE DATABASE {LOCAL_DB};")
    print(f"  Created database '{LOCAL_DB}'")

    print("\n=== Step 2: Dump data from Railway ===")
    pg_dump = os.path.join(PG_BIN, "pg_dump.exe")
    env = os.environ.copy()
    # Extract password from Railway URL and set PGPASSWORD
    m = re.match(r"postgresql://[^:]+:([^@]+)@", railway_url)
    if m:
        env["PGPASSWORD"] = m.group(1)
    run([pg_dump, "--no-owner", "--no-acl", "-f", DUMP_FILE, railway_url], env=env)
    size = os.path.getsize(DUMP_FILE) / 1024
    print(f"  Dumped {size:.0f} KB to {DUMP_FILE}")

    print("\n=== Step 3: Restore to local PostgreSQL ===")
    psql = os.path.join(PG_BIN, "psql.exe")
    run([psql, "-U", LOCAL_USER, "-d", LOCAL_DB, "-f", DUMP_FILE])
    print("  Restored successfully")

    print("\n=== Step 4: Install psycopg2 ===")
    run([sys.executable, "-m", "pip", "install", "psycopg2-binary", "-q"])

    print("\n=== Step 5: Create .env file ===")
    with open(ENV_FILE, "w") as f:
        f.write(f"DATABASE_URL={LOCAL_URL}\n")
    print(f"  Created {ENV_FILE}")
    print(f"  DATABASE_URL={LOCAL_URL}")

    print("\n=== Step 6: Verify connection ===")
    run([psql, "-U", LOCAL_USER, "-d", LOCAL_DB, "-c",
         "SELECT 'cycles' as tbl, COUNT(*) FROM cycles UNION ALL "
         "SELECT 'members', COUNT(*) FROM members UNION ALL "
         "SELECT 'payments', COUNT(*) FROM payments UNION ALL "
         "SELECT 'weeks', COUNT(*) FROM weeks;"])

    print("\n✅ Done! Local PostgreSQL is ready.")
    print(f"   Start app with: uvicorn main:app --reload")
    print(f"   The .env file points to: {LOCAL_URL}")
    print()
    print("   To resync with Railway later, just run this script again.")


if __name__ == "__main__":
    main()
