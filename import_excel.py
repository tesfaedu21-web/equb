"""
Import real member/cycle data from ekub.xlsx into the Equb database.
Clears existing test data, then loads:
  - Cycle starting 2022-11-06 (118 weeks)
  - All members with spot assignments from ዕቁብ sheet
  - Draw winners (weeks 1-75) from Draw Winner sheet
Run: python import_excel.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from datetime import datetime
from collections import defaultdict
from database import (
    SessionLocal, Member, MemberSpot, Spot, Cycle, Week,
    Payment, PaymentBatch, PotTransaction, Settings,
)

db = SessionLocal()

# ── 1. Clear existing test data ──────────────────────────────────────────────
print("Clearing existing data...")
db.query(Payment).delete()
db.query(PaymentBatch).delete()
db.query(PotTransaction).delete()
db.query(Week).delete()
db.query(Cycle).delete()
db.query(MemberSpot).delete()
db.query(Member).delete()
db.query(Spot).update({"status": "active"})
db.commit()
print("  Done.")

# ── 2. Load Excel ─────────────────────────────────────────────────────────────
print("Reading ekub.xlsx...")
xl = pd.read_excel("ekub_import_copy.xlsx", sheet_name=None, header=None)
main = xl["ዕቁብ"]
draw_sheet = xl["Draw Winner"]
print(f"  Main sheet: {main.shape[0]} rows × {main.shape[1]} cols")

# ── 3. Extract weekly draw dates from row 1, cols 4–121 ──────────────────────
week_dates = []
for col in range(4, 4 + 118):
    if col >= main.shape[1]:
        break
    val = main.iloc[1, col]
    if pd.notna(val):
        try:
            week_dates.append(pd.to_datetime(val))
        except Exception:
            pass

total_weeks = len(week_dates)
print(f"  Found {total_weeks} week dates: {week_dates[0].date()} to {week_dates[-1].date()}")

# ── 4. Create cycle ───────────────────────────────────────────────────────────
settings = db.query(Settings).first()
start_date = week_dates[0].to_pydatetime() if week_dates else datetime(2022, 11, 6)

cycle = Cycle(
    name="ዑደት 1 — 2022/2025",
    start_date=start_date,
    draw_phase="active",
    draw_start_week=1,
    draw_started_at=start_date,
    status="active",
    notes="Imported from ekub.xlsx",
)
db.add(cycle)
db.flush()
print(f"  Created cycle id={cycle.id}")

# ── 5. Create weeks ───────────────────────────────────────────────────────────
# Pot size based on 113 member spots (the active ones contributing)
# Full spot: 21,000 ETB; association deduction: 1,000 ETB/spot
GROSS_PER_WEEK = 113 * settings.full_spot_amount        # 2,373,000
ASSOC_PER_WEEK = 113 * settings.association_deduction   # 113,000
NET_PER_WEEK   = GROSS_PER_WEEK - ASSOC_PER_WEEK        # 2,260,000

interval = settings.group_week_interval   # 4
weeks_by_number: dict[int, Week] = {}

for i, draw_dt in enumerate(week_dates, 1):
    w = Week(
        cycle_id=cycle.id,
        week_number=i,
        draw_date=draw_dt.to_pydatetime(),
        is_group_week=(i % interval == 0),
        gross_pot=GROSS_PER_WEEK,
        association_amount=ASSOC_PER_WEEK,
        net_pot=NET_PER_WEEK,
        status="pending",
    )
    db.add(w)
    db.flush()
    weeks_by_number[i] = w

db.commit()
print(f"  Created {total_weeks} weeks.")

# ── 6. Read member rows (row 5 onward) ────────────────────────────────────────
member_rows = []
for idx in range(5, main.shape[0]):
    row = main.iloc[idx]
    spot_num = row.iloc[0]
    name     = row.iloc[1]
    amount   = row.iloc[2]

    if pd.isna(spot_num) or pd.isna(name):
        continue
    if not isinstance(name, str) or not name.strip():
        continue

    member_rows.append({
        "spot_number": int(float(spot_num)),
        "name": name.strip(),
        "amount": float(amount) if pd.notna(amount) else 21000.0,
    })

print(f"  Found {len(member_rows)} member-slot rows.")

# Group by spot number (same spot = half-share partners)
spot_groups: dict[int, list] = defaultdict(list)
for row in member_rows:
    spot_groups[row["spot_number"]].append(row)

# ── 7. Get spot objects keyed by spot number ──────────────────────────────────
spots_by_number = {s.number: s for s in db.query(Spot).all()}

# ── 8. Create Members + MemberSpot assignments ────────────────────────────────
created_members = 0
for spot_num in sorted(spot_groups.keys()):
    rows = spot_groups[spot_num]
    spot = spots_by_number.get(spot_num)
    if not spot:
        print(f"  WARNING: spot #{spot_num} not found in DB — skipping")
        continue

    for row in rows:
        m = Member(name=row["name"])
        db.add(m)
        db.flush()

        # If two members share the same spot, they both get half-share
        share = "half" if (len(rows) > 1 or row["amount"] == 10500.0) else "full"
        contrib = row["amount"]

        db.add(MemberSpot(
            member_id=m.id,
            spot_id=spot.id,
            share=share,
            weekly_contribution=contrib,
            is_active=True,
        ))
        created_members += 1

db.commit()
print(f"  Created {created_members} member-spot assignments.")

# ── 9. Import draw winners from "Draw Winner" sheet ───────────────────────────
# Cols: 0=week_num, 1=winning_spot, 2=association_flag, 3=extra
drawn_count = 0
group_week_count = 0

for _, drow in draw_sheet.iterrows():
    week_num = drow.iloc[0]
    spot_num = drow.iloc[1]
    assoc_flag = drow.iloc[2]

    if pd.isna(week_num):
        continue
    try:
        week_num = int(float(week_num))
        spot_num_val = None if pd.isna(spot_num) else int(float(spot_num))
    except (ValueError, TypeError):
        continue

    w = weeks_by_number.get(week_num)
    if not w:
        continue

    # Group/association week — flag but no winner spot
    if pd.notna(assoc_flag) and str(assoc_flag).strip():
        w.is_group_week = True
        w.notes = str(assoc_flag).strip()
        group_week_count += 1
        # Mark as "sold" placeholder — no transaction details available
        w.status = "sold"
        db.flush()
        continue

    if spot_num_val is None:
        continue

    spot = spots_by_number.get(spot_num_val)
    if not spot:
        continue

    w.winner_spot_id = spot.id
    w.status = "drawn"

    # Mark spot + members as received
    spot.status = "received"
    for sa in spot.spot_assignments:
        if sa.is_active:
            sa.member.status = "received"

    db.flush()
    drawn_count += 1

db.commit()
print(f"  Imported {drawn_count} draw winners, {group_week_count} group/association weeks marked.")

# ── 10. Import payment history from ቦኖ sheet ─────────────────────────────────
# ቦኖ has same row order as ዕቁብ: col0=spot, col1=name, col2=amount, cols4+= week 1/0 flags
print("Importing payment history from bono sheet...")
bono = xl["ቦኖ"]

# Build member lookup: (spot_number, row_index_within_spot) → Member
# We'll match by iterating member_rows in the same order as before
from sqlalchemy import and_

member_lookup = []   # list of (spot_num, name) in order
for spot_num in sorted(spot_groups.keys()):
    for row in spot_groups[spot_num]:
        member_lookup.append((spot_num, row["name"]))

# Build DB member list in same order
members_ordered = []
for spot_num, name in member_lookup:
    spot = spots_by_number.get(spot_num)
    if not spot:
        members_ordered.append(None)
        continue
    # Find member assigned to this spot with this name
    m = next(
        (sa.member for sa in spot.spot_assignments
         if sa.is_active and sa.member.name == name),
        None
    )
    members_ordered.append(m)

# Map week_number → Week object (already have weeks_by_number)
paid_count = 0
missed_count = 0
payment_records = []

for bono_row_idx, db_member in enumerate(members_ordered):
    main_row_idx = bono_row_idx + 5   # data starts at row 5
    if main_row_idx >= bono.shape[0] or db_member is None:
        continue

    bono_row = bono.iloc[main_row_idx]

    for week_col_offset, week_num in enumerate(range(1, total_weeks + 1)):
        col_idx = 4 + week_col_offset
        if col_idx >= bono.shape[1]:
            break

        val = bono_row.iloc[col_idx]
        w = weeks_by_number.get(week_num)
        if not w:
            continue

        try:
            paid_flag = int(float(val)) if pd.notna(val) else 0
        except (ValueError, TypeError):
            paid_flag = 0

        status = "paid" if paid_flag == 1 else "missed"
        paid_date = w.draw_date if paid_flag == 1 else None

        payment_records.append(Payment(
            member_id=db_member.id,
            week_id=w.id,
            amount=sum(sa.weekly_contribution for sa in db_member.spot_assignments if sa.is_active) or 0,
            status=status,
            paid_date=paid_date,
            payment_method="cash" if paid_flag == 1 else None,
        ))

        if paid_flag == 1:
            paid_count += 1
        else:
            missed_count += 1

    if (bono_row_idx + 1) % 20 == 0:
        print(f"  Processed {bono_row_idx + 1}/{len(members_ordered)} members...")

db.bulk_save_objects(payment_records)
db.commit()
print(f"  Payment records: {paid_count} paid, {missed_count} missed/not-paid.")

# ── 11. Summary ───────────────────────────────────────────────────────────────
total_members = db.query(Member).count()
active_members = db.query(Member).filter(Member.status == "active").count()
received_members = db.query(Member).filter(Member.status == "received").count()
pending_weeks = db.query(Week).filter(Week.status == "pending").count()

print("\n=== Import complete ===")
print(f"  Members total   : {total_members}")
print(f"  Active (not drawn) : {active_members}")
print(f"  Received (pot taken): {received_members}")
print(f"  Weeks pending   : {pending_weeks}")
print(f"  Weeks drawn/sold: {total_weeks - pending_weeks}")

db.close()
print("\nDone. Restart the server if it is already running.")
