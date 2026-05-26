"""
One-time script: update member #2379 email and print result.
Run via: railway run --service equb python _update_test_email.py
(Requires Railway internal network — delete after use)
"""
import os, sys

db_url = os.environ.get("DATABASE_URL", "")
if not db_url:
    print("ERROR: DATABASE_URL not set"); sys.exit(1)

from sqlalchemy import create_engine, text
engine = create_engine(db_url)
with engine.connect() as conn:
    conn.execute(text(
        "UPDATE members SET email = 'tesfaedu21@gmail.com' WHERE id = 2379"
    ))
    conn.commit()
    row = conn.execute(text(
        "SELECT id, name, email, phone FROM members WHERE id = 2379"
    )).fetchone()
    print(f"Updated member: id={row[0]}, email={row[3]}")
    print("Done.")
