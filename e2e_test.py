"""
Full end-to-end scenario test for Equb project.
Tests every major workflow as admin, cashier, and owner.

Run locally:   python e2e_test.py
Run on Railway: python e2e_test.py  (BASE/credentials already set to Railway defaults)

Override via env vars:
  EQUB_BASE_URL       — target server  (default: https://equb-production.up.railway.app)
  EQUB_ADMIN_USER     — admin username (default: admin)
  EQUB_ADMIN_PASS     — admin password (default: Tesfa123)
  EQUB_CASHIER_USER   — cashier username (default: cashier)
  EQUB_CASHIER_PASS   — cashier password (default: cashier123)
  EQUB_OWNER_USER     — owner username  (default: same as admin)
  EQUB_OWNER_PASS     — owner password  (default: same as admin)
"""
import os, requests, json, sys, time
from datetime import date, timedelta

BASE           = os.environ.get("EQUB_BASE_URL",     "https://equb-production.up.railway.app").rstrip("/")
ADMIN_USER     = os.environ.get("EQUB_ADMIN_USER",   "admin")
ADMIN_PASS     = os.environ.get("EQUB_ADMIN_PASS",   "Tesfa123")
CASHIER_USER   = os.environ.get("EQUB_CASHIER_USER", "cashier")
CASHIER_PASS   = os.environ.get("EQUB_CASHIER_PASS", "cashier123")
OWNER_USER     = os.environ.get("EQUB_OWNER_USER",   ADMIN_USER)
OWNER_PASS     = os.environ.get("EQUB_OWNER_PASS",   ADMIN_PASS)

print(f"Target: {BASE}")

# Unique 4-digit suffix so each test run uses fresh phone numbers
_RUN = str(int(time.time()) % 10000).zfill(4)
BUGS = []
PASSES = []
WARNS = []

XHR = {'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/json'}

def ok(msg):
    PASSES.append(msg)
    print(f"  PASS: {msg}")

def bug(msg, detail=''):
    entry = f"{msg}: {detail}" if detail else msg
    BUGS.append(entry)
    print(f"  BUG:  {msg}" + (f"\n        {detail}" if detail else ''))

def warn(msg):
    WARNS.append(msg)
    print(f"  WARN: {msg}")

def check(label, cond, fail_detail=''):
    if cond: ok(label)
    else: bug(label, fail_detail)

def xpost(session, path, **kwargs):
    return session.post(BASE + path, headers=XHR, **kwargs)

def xput(session, path, **kwargs):
    return session.put(BASE + path, headers=XHR, **kwargs)

def xdelete(session, path, **kwargs):
    return session.delete(BASE + path, headers=XHR, **kwargs)

# ============================================================
print("\n=== PHASE 0: CLEANUP PREVIOUS TEST DATA ===")
# ============================================================

s_cleanup = requests.Session()
s_cleanup.post(BASE+'/login', data={'username': ADMIN_USER, 'password': ADMIN_PASS}, allow_redirects=False)

# Delete all previous cycles (cascades to payments, disbursements, pot transactions, weeks)
r_cyc = s_cleanup.get(BASE+'/api/draws/cycles')
if r_cyc.status_code == 200:
    prev_cycles = r_cyc.json() if isinstance(r_cyc.json(), list) else []
    for c in prev_cycles:
        xdelete(s_cleanup, f'/api/draws/cycles/{c["id"]}')
    print(f"  Deleted {len(prev_cycles)} previous cycles")

# Hard-delete all members (those with no remaining payment records)
r_del = xdelete(s_cleanup, '/api/members/permanent/all')
if r_del.status_code == 200:
    d = r_del.json()
    print(f"  {d.get('message','Members cleaned')}")
else:
    print(f"  Member cleanup: {r_del.status_code}")

# ============================================================
print("\n=== PHASE 1: AUTH & ROLES ===")
# ============================================================

s_owner   = requests.Session()
s_admin   = requests.Session()
s_cashier = requests.Session()
s_anon    = requests.Session()

r = s_owner.post(BASE+'/login', data={'username': OWNER_USER, 'password': OWNER_PASS}, allow_redirects=False)
check("Owner login", r.status_code == 302, f"got {r.status_code}")

r = s_admin.post(BASE+'/login', data={'username': ADMIN_USER, 'password': ADMIN_PASS}, allow_redirects=False)
check("Admin login", r.status_code == 302, f"got {r.status_code}")

r = s_cashier.post(BASE+'/login', data={'username': CASHIER_USER, 'password': CASHIER_PASS}, allow_redirects=False)
check("Cashier login", r.status_code == 302, f"got {r.status_code}")

# Wrong password blocked
r = s_anon.post(BASE+'/login', data={'username':'admin','password':'WRONG'}, allow_redirects=False)
check("Wrong password blocked", r.status_code == 401, f"got {r.status_code}")

# Anonymous blocked from protected pages
r = s_anon.get(BASE+'/members', allow_redirects=False)
check("Anonymous blocked from /members", r.status_code == 302 and '/login' in r.headers.get('Location',''), f"got {r.status_code}")

# Cashier blocked from settings API
r = xput(s_cashier, '/api/settings', json={'group_name':'Hacked'})
check("Cashier cannot PUT /api/settings", r.status_code == 403, f"got {r.status_code}: {r.text[:80]}")

# Admin blocked from settings (owner-only per permissions table)
r = xput(s_admin, '/api/settings', json={'group_name':'Hacked'})
check("Admin cannot PUT /api/settings (owner-only)", r.status_code == 403, f"got {r.status_code}: {r.text[:80]}")

# Owner CAN update settings
r = xput(s_owner, '/api/settings', json={'group_name':'Test Equb E2E', 'group_tagline':'Full E2E Test'})
check("Owner CAN PUT /api/settings", r.status_code == 200, f"got {r.status_code}: {r.text[:80]}")

# Cashier blocked from notification logs
r = s_cashier.get(BASE+'/api/notifications/logs')
check("Cashier blocked from notification logs", r.status_code == 403, f"got {r.status_code}")

# Cashier blocked from general-ledger (financial report)
r = s_cashier.get(BASE+'/api/reports/general-ledger')
check("Cashier blocked from general-ledger", r.status_code == 403, f"got {r.status_code}")

# ============================================================
print("\n=== PHASE 2: MEMBER MANAGEMENT ===")
# ============================================================

members_created = []
member_data = [
    {'name': 'Abebe Kebede',   'phone': f'09{_RUN}0001', 'share': 'full'},   # Ethiopian local 09xx
    {'name': 'Tigist Alemu',   'phone': f'09{_RUN}0002', 'share': 'full'},
    {'name': 'Dawit Haile',    'phone': f'+2519{_RUN}0003', 'share': 'full'}, # International
    {'name': 'Meron Tadesse',  'phone': f'09{_RUN}0004', 'share': 'full'},
    {'name': 'Samuel Girma',   'phone': f'09{_RUN}0005', 'share': 'full'},
    {'name': 'Hana Bekele',    'phone': None,             'share': 'full'},   # No phone - should work
    {'name': 'Yonas Tesfaye',  'phone': f'09{_RUN}0007', 'share': 'half'},
    {'name': 'Liya Mekonnen',  'phone': f'09{_RUN}0008', 'share': 'half'},
]

for m in member_data:
    body = {'name': m['name'], 'share': m['share']}
    if m['phone']: body['phone'] = m['phone']
    r = xpost(s_admin, '/api/members', json=body)
    if r.status_code == 200:
        member_obj = r.json()
        member_obj['share'] = m['share']  # API response lacks top-level share; inject from source
        members_created.append(member_obj)
        ok(f"Created member: {m['name']} ({m['share']}) phone={m['phone']}")
    else:
        bug(f"Failed to create {m['name']}", f"{r.status_code}: {r.text[:120]}")

# Cashier cannot add members (admin-only operation)
r = xpost(s_cashier, '/api/members', json={'name':'Cashier Member', 'phone': f'09{_RUN}0009', 'share':'full'})
check("Cashier cannot add member (admin-only)", r.status_code == 403, f"got {r.status_code}: {r.text[:80]}")

check(f"All 8 members created", len(members_created) == 8, f"got {len(members_created)}")

# Same phone allowed — one person can hold multiple spots (e.g. 4 full spots in same cycle)
r = xpost(s_admin, '/api/members', json={'name':'Abebe Kebede', 'phone': f'09{_RUN}0001', 'share':'full'})
check("Same phone allowed (multi-spot member)", r.status_code == 200, f"got {r.status_code}: {r.text[:80]}")

# Empty name rejected
r = xpost(s_admin, '/api/members', json={'name':'', 'phone':'0911200000', 'share':'full'})
check("Empty name rejected", r.status_code in (400, 422), f"got {r.status_code}: {r.text[:80]}")

# Invalid phone rejected
r = xpost(s_admin, '/api/members', json={'name':'Bad Phone', 'phone':'12', 'share':'full'})
check("Too-short phone rejected", r.status_code in (400, 422), f"got {r.status_code}: {r.text[:80]}")

# Cashier can GET members list
r = s_cashier.get(BASE+'/api/members')
check("Cashier can GET members list", r.status_code == 200, f"got {r.status_code}")
if r.status_code == 200:
    all_members = r.json()
    check(f"Members list has >= 8 entries", len(all_members) >= 8, f"got {len(all_members)}")

full_member_ids = [m['id'] for m in members_created if m.get('share') == 'full']
half_member_ids = [m['id'] for m in members_created if m.get('share') == 'half']
print(f"  Full-spot members: {len(full_member_ids)}, Half-spot: {len(half_member_ids)}")

# Update member
if members_created:
    mid = members_created[0]['id']
    r = xput(s_admin, f'/api/members/{mid}', json={'notes': 'Updated by E2E test'})
    check("Admin can update member notes", r.status_code == 200, f"got {r.status_code}")
    r = xput(s_cashier, f'/api/members/{mid}', json={'notes': 'Cashier update'})
    check("Cashier cannot update member (admin-only)", r.status_code == 403, f"got {r.status_code}: {r.text[:80]}")

# ============================================================
print("\n=== PHASE 3: CYCLE CREATION ===")
# ============================================================

# Cashier cannot create cycle
start_date = (date.today() - timedelta(weeks=5)).isoformat()
r = xpost(s_cashier, '/api/draws/cycles', json={'name':'CASHIER ATTEMPT', 'start_date': start_date, 'total_member_spots': 4})
check("Cashier cannot create cycle", r.status_code == 403, f"got {r.status_code}: {r.text[:80]}")

# Admin creates a small 4-spot cycle
r = xpost(s_admin, '/api/draws/cycles', json={
    'name': 'Test Cycle E2E',
    'start_date': start_date,
    'total_member_spots': 4,       # 4 spots, 1 week per spot
    'full_spot_amount': 21000,
    'half_spot_amount': 10500,
    'association_deduction': 1000,
    'full_spot_voucher': 80,
    'half_spot_voucher': 40,
})
check("Admin creates cycle", r.status_code == 200, f"got {r.status_code}: {r.text[:200]}")
if r.status_code != 200:
    print("FATAL: Cannot continue without cycle"); sys.exit(1)

cycle_data = r.json()
cycle_id = cycle_data['id']
ok(f"Cycle id={cycle_id}, total_weeks={cycle_data.get('total_weeks')}")

# Verify weeks created
r = s_admin.get(BASE+f'/api/draws/cycles/{cycle_id}/weeks')
check("Weeks created for cycle", r.status_code == 200, f"{r.status_code}")
weeks = r.json() if r.status_code == 200 else []
check(f"4 weeks created", len(weeks) == 4, f"got {len(weeks)}")
if not weeks: print("FATAL: No weeks"); sys.exit(1)

# Duplicate cycle name test — any new cycle auto-completes the current active one;
# if it succeeds, reactivate the original cycle before continuing.
dup_r = xpost(s_admin, '/api/draws/cycles', json={'name': 'Test Cycle E2E', 'start_date': start_date, 'total_member_spots': 2})
if dup_r.status_code == 200:
    dup_cycle_id = dup_r.json()['id']
    warn("Duplicate cycle name allowed (no uniqueness constraint on cycle names)")
    # Reactivate original cycle (auto-completes the dup) to keep test state clean
    xpost(s_admin, f'/api/draws/cycles/{cycle_id}/reactivate')
else:
    ok("Duplicate cycle name blocked")

# ============================================================
print("\n=== PHASE 4: SPOT ASSIGNMENT ===")
# ============================================================

r = s_admin.get(BASE+f'/api/draws/active-spots?cycle_id={cycle_id}')
check("GET active spots", r.status_code == 200, f"{r.status_code}")
spots = r.json() if r.status_code == 200 else []
ok(f"Spots generated: {len(spots)}")

# Assign full members to spots 1-3, half members to spot 4 (if half spot exists)
spot_assignments = []
for i, spot in enumerate(spots[:4]):
    spot_id = spot['id']
    spot_share = spot.get('share', 'full')
    if spot_share == 'full' and i < len(full_member_ids):
        mid = full_member_ids[i]
        r = xpost(s_admin, f'/api/members/{mid}/spots', json={'spot_id': spot_id, 'cycle_id': cycle_id})
        if r.status_code == 200:
            ok(f"Assigned member {mid} -> spot #{spot.get('number')} (full)")
            spot_assignments.append((mid, spot_id))
        else:
            bug(f"Spot assignment failed for member {mid}", f"{r.status_code}: {r.text[:120]}")
    elif spot_share == 'half' and len(half_member_ids) >= 2:
        for hm in half_member_ids[:2]:
            r = xpost(s_admin, f'/api/members/{hm}/spots', json={'spot_id': spot_id, 'cycle_id': cycle_id})
            if r.status_code == 200:
                ok(f"Assigned half-member {hm} -> spot #{spot.get('number')}")
            else:
                bug(f"Half-spot assignment failed", f"{r.status_code}: {r.text[:120]}")

# All spots are full by default; assign first 4 full members to first 4 spots if no half spot
if not spot_assignments and full_member_ids:
    for i, spot in enumerate(spots[:min(4, len(full_member_ids))]):
        mid = full_member_ids[i]
        r = xpost(s_admin, f'/api/members/{mid}/spots', json={'spot_id': spot['id'], 'cycle_id': cycle_id})
        if r.status_code == 200:
            ok(f"Assigned member {mid} -> spot #{spot.get('number')}")
            spot_assignments.append((mid, spot['id']))
        else:
            bug(f"Spot assignment failed", f"{r.status_code}: {r.text[:120]}")

check("At least 3 spot assignments made", len(spot_assignments) >= 3, f"only {len(spot_assignments)}")

# Cashier cannot assign spots
if full_member_ids and len(spots) > len(spot_assignments):
    extra_spot = spots[len(spot_assignments)]
    extra_member = full_member_ids[-1] if len(full_member_ids) > len(spot_assignments) else full_member_ids[0]
    r = xpost(s_cashier, f'/api/members/{extra_member}/spots', json={'spot_id': extra_spot['id'], 'cycle_id': cycle_id})
    if r.status_code == 403:
        ok("Cashier cannot assign spots")
    elif r.status_code == 200:
        warn("Cashier can assign spots - check if this is intended")
    else:
        warn(f"Spot assignment by cashier: {r.status_code}")

# ============================================================
print("\n=== PHASE 5: PAYMENT COLLECTION ===")
# ============================================================

week1 = weeks[0]
week1_id = week1['id']
week1_num = week1.get('week_number', 1)
print(f"  Week 1: id={week1_id}, draw_date={week1.get('draw_date')}, status={week1.get('status')}")

# Get week's payment list
r = s_cashier.get(BASE+f'/api/payments/week/{week1_id}')
check("Cashier can GET week payments", r.status_code == 200, f"{r.status_code}: {r.text[:100]}")
week1_payments = r.json() if r.status_code == 200 else []
check("Week 1 has payment records", len(week1_payments) > 0, f"got {len(week1_payments)} (members may not be assigned)")

# Get member IDs for this cycle
r = s_admin.get(BASE+f'/api/members?cycle_id={cycle_id}')
cycle_members = r.json() if r.status_code == 200 else []
cycle_member_ids = [m['id'] for m in cycle_members]
if not cycle_member_ids and spot_assignments:
    # Fallback: use the members we assigned to spots
    cycle_member_ids = [mid for mid, _ in spot_assignments]
    print(f"  Members via spot assignments (fallback): {len(cycle_member_ids)}")
else:
    print(f"  Members in cycle: {len(cycle_member_ids)}")

# Bulk-mark all as paid for weeks 1-4 (use status field, not action)
for wk in weeks[:4]:
    wk_id = wk['id']
    r = xpost(s_cashier, '/api/payments/bulk', json={
        'week_id': wk_id,
        'member_ids': cycle_member_ids,
        'status': 'paid',
        'paid_date': date.today().isoformat(),
    })
    if r.status_code == 200:
        result = r.json()
        ok(f"Bulk paid week {wk.get('week_number','?')}: {result.get('updated',0)} payments")
    else:
        bug(f"Bulk paid week {wk.get('week_number','?')} failed", f"{r.status_code}: {r.text[:120]}")

# Verify week 1 all paid
r = s_cashier.get(BASE+f'/api/payments/week/{week1_id}')
w1_pays = r.json() if r.status_code == 200 else []
paid_count = sum(1 for p in w1_pays if p.get('status') == 'paid')
check(f"Week 1 all paid ({paid_count}/{len(w1_pays)})",
      paid_count > 0 and paid_count == len(w1_pays),
      f"{paid_count} of {len(w1_pays)} paid")

# Mark missed then re-pay (cashier can do both)
if cycle_member_ids:
    r = xpost(s_cashier, '/api/payments/bulk', json={
        'week_id': week1_id, 'member_ids': cycle_member_ids[:1], 'status': 'missed'
    })
    check("Cashier can mark missed", r.status_code == 200, f"{r.status_code}: {r.text[:80]}")
    r = xpost(s_cashier, '/api/payments/bulk', json={
        'week_id': week1_id, 'member_ids': cycle_member_ids[:1], 'status': 'paid',
        'paid_date': date.today().isoformat()
    })
    check("Cashier re-marks to paid", r.status_code == 200, f"{r.status_code}")

# Individual payment update
r = s_cashier.get(BASE+f'/api/payments/week/{week1_id}')
if r.status_code == 200 and r.json():
    pay = r.json()[0]
    r2 = xput(s_cashier, f'/api/payments/{pay["id"]}', json={'status': 'paid', 'paid_date': date.today().isoformat()})
    check("Cashier can update individual payment", r2.status_code == 200, f"{r2.status_code}: {r2.text[:80]}")

# Check payment summary endpoint (not /api/reports/payment-summary - that doesn't exist)
r = s_admin.get(BASE+f'/api/payments/summary/cycle/{cycle_id}')
check("GET payment summary by cycle", r.status_code == 200, f"{r.status_code}: {r.text[:100]}")

# ============================================================
print("\n=== PHASE 6: DRAWS PHASE ===")
# ============================================================

# Admin starts draws phase (correct field: at_week_number, not draw_start_week)
r = xpost(s_admin, f'/api/draws/cycles/{cycle_id}/start-draws', json={
    'at_week_number': 1,
    'assoc_spots': 1,
})
check("Admin starts draws phase", r.status_code == 200, f"{r.status_code}: {r.text[:200]}")

# Refresh weeks
r = s_admin.get(BASE+f'/api/draws/cycles/{cycle_id}/weeks')
weeks = r.json() if r.status_code == 200 else weeks

# Get updated spots
r = s_admin.get(BASE+f'/api/draws/active-spots?cycle_id={cycle_id}')
active_spots = r.json() if r.status_code == 200 else []
ok(f"Active spots for draw: {len(active_spots)}")

pending_weeks = [w for w in weeks if w.get('status') == 'pending']
ok(f"Pending weeks: {len(pending_weeks)}")

# Cashier cannot record draw
if pending_weeks and active_spots:
    r = xpost(s_cashier, f'/api/draws/weeks/{pending_weeks[0]["id"]}/draw',
              json={'winner_spot_id': active_spots[0]['id']})
    check("Cashier cannot record draw", r.status_code == 403, f"got {r.status_code}: {r.text[:80]}")

drawn_weeks = []
for i, wk in enumerate(pending_weeks[:3]):  # Draw first 3 weeks
    # winner_spot_id is nested in winner_spot.id (not a top-level key in draw response)
    used_spots = {(dw.get('winner_spot') or {}).get('id') for dw in drawn_weeks}
    available = [s for s in active_spots if s['id'] not in used_spots]
    if not available:
        break
    spot = available[0]
    r = xpost(s_admin, f'/api/draws/weeks/{wk["id"]}/draw', json={'winner_spot_id': spot['id']})
    if r.status_code == 200:
        dw = r.json()
        drawn_weeks.append(dw)
        ok(f"Admin drew week {wk.get('week_number','?')}: spot #{spot.get('number')}")
    else:
        bug(f"Admin draw failed for week {wk.get('week_number','?')}", f"{r.status_code}: {r.text[:150]}")

check("At least 1 week drawn", len(drawn_weeks) >= 1, f"drawn {len(drawn_weeks)}")

# Redraw same week blocked
if drawn_weeks and active_spots:
    used = drawn_weeks[0]
    used_spot_id = (used.get('winner_spot') or {}).get('id')
    remaining = [s for s in active_spots if s['id'] != used_spot_id]
    if remaining:
        r = xpost(s_admin, f'/api/draws/weeks/{used["id"]}/draw', json={'winner_spot_id': remaining[0]['id']})
        check("Cannot draw same week twice", r.status_code in (400, 409), f"got {r.status_code}: {r.text[:80]}")

# ============================================================
print("\n=== PHASE 7: DISBURSEMENT ===")
# ============================================================

if not drawn_weeks:
    warn("No drawn weeks — skipping disbursement phase")
else:
    dw = drawn_weeks[0]
    dw_id = dw['id']

    # Voucher info
    r = s_admin.get(BASE+f'/api/disbursements/voucher-info/{dw_id}')
    check("GET voucher info", r.status_code == 200, f"{r.status_code}: {r.text[:150]}")
    vi = r.json() if r.status_code == 200 else {}
    if vi:
        ok(f"net_pot={vi.get('net_pot')}, service_fee={vi.get('service_fee')}, voucher={vi.get('voucher_deduction')}, net_after_all={vi.get('net_after_all')}")

    # Build guarantors (not the winner) — look up winner members from spot_assignments
    winner_spot_id = (dw.get('winner_spot') or {}).get('id')
    # Use tracked spot assignments: which member sits on the winner spot?
    winner_mids = {mid for mid, sid in spot_assignments if sid == winner_spot_id}
    guarantors = [m for m in cycle_member_ids if m not in winner_mids][:3]
    if len(guarantors) < 3:
        all_mid = [m['id'] for m in members_created]
        guarantors = [m for m in all_mid if m not in winner_mids][:3]

    # Cashier cannot disburse
    if guarantors:
        r = xpost(s_cashier, '/api/disbursements', json={
            'week_id': dw_id, 'gross_amount': vi.get('net_pot', 84000),
            'service_fee': 0, 'voucher_deduction': 0,
            'cheque_number': 'CHQ-CASHIER', 'cheque_date': date.today().isoformat(),
            'guarantor_1_id': guarantors[0] if len(guarantors) > 0 else 1,
            'guarantor_2_id': guarantors[1] if len(guarantors) > 1 else 2,
            'guarantor_3_id': guarantors[2] if len(guarantors) > 2 else 3,
        })
        check("Cashier cannot disburse", r.status_code == 403, f"got {r.status_code}: {r.text[:80]}")

    disb = None
    if len(guarantors) >= 3:
        r = xpost(s_admin, '/api/disbursements', json={
            'week_id': dw_id,
            'gross_amount': vi.get('net_pot', 84000),
            'service_fee': vi.get('service_fee', 21000),
            'voucher_deduction': vi.get('voucher_deduction', 0),
            'cheque_number': 'CHQ-E2E-001',
            'cheque_date': date.today().isoformat(),
            'guarantor_1_id': guarantors[0],
            'guarantor_2_id': guarantors[1],
            'guarantor_3_id': guarantors[2],
            'notes': 'E2E test disbursement',
        })
        check("Admin records disbursement", r.status_code == 200, f"{r.status_code}: {r.text[:300]}")
        if r.status_code == 200:
            disb = r.json()
            ok(f"Disbursement net_amount={disb.get('net_amount')}, status={disb.get('status')}")

            # Duplicate blocked
            r2 = xpost(s_admin, '/api/disbursements', json={
                'week_id': dw_id, 'gross_amount': vi.get('net_pot', 84000),
                'service_fee': vi.get('service_fee', 21000), 'voucher_deduction': 0,
                'cheque_number': 'CHQ-DUP', 'cheque_date': date.today().isoformat(),
                'guarantor_1_id': guarantors[0], 'guarantor_2_id': guarantors[1], 'guarantor_3_id': guarantors[2],
            })
            check("Duplicate disbursement blocked", r2.status_code == 400, f"got {r2.status_code}: {r2.text[:80]}")

            # Same guarantor twice blocked
            r3 = xpost(s_admin, '/api/disbursements', json={
                'week_id': drawn_weeks[1]['id'] if len(drawn_weeks) > 1 else dw_id,
                'gross_amount': vi.get('net_pot', 84000), 'service_fee': 0, 'voucher_deduction': 0,
                'cheque_number': 'CHQ-DUP-G', 'cheque_date': date.today().isoformat(),
                'guarantor_1_id': guarantors[0],
                'guarantor_2_id': guarantors[0],  # Same person twice - SHOULD FAIL
                'guarantor_3_id': guarantors[1],
            })
            check("Duplicate guarantors blocked", r3.status_code == 400, f"got {r3.status_code}: {r3.text[:100]}")

            # Mark collected
            r4 = xput(s_admin, f'/api/disbursements/{disb["id"]}', json={'status': 'collected'})
            check("Mark disbursement collected", r4.status_code == 200, f"{r4.status_code}")
    else:
        warn("Not enough non-winner guarantors — disbursement skipped")

# List disbursements
r = s_admin.get(BASE+f'/api/disbursements?cycle_id={cycle_id}')
check("GET disbursements list", r.status_code == 200, f"{r.status_code}")

# ============================================================
print("\n=== PHASE 8: REPORTS & DATA INTEGRITY ===")
# ============================================================

# Dashboard (replaces payment-summary)
r = s_cashier.get(BASE+f'/api/reports/dashboard?cycle_id={cycle_id}')
check("Cashier can GET /api/reports/dashboard", r.status_code == 200, f"{r.status_code}: {r.text[:100]}")

# General ledger - admin only
r = s_cashier.get(BASE+f'/api/reports/general-ledger?cycle_id={cycle_id}')
check("Cashier blocked from general-ledger", r.status_code == 403, f"got {r.status_code}: {r.text[:80]}")

r = s_admin.get(BASE+f'/api/reports/general-ledger?cycle_id={cycle_id}')
check("Admin can GET general-ledger", r.status_code == 200, f"{r.status_code}: {r.text[:100]}")

# Collection trend
r = s_admin.get(BASE+f'/api/reports/collection-trend?cycle_id={cycle_id}')
check("GET collection-trend", r.status_code == 200, f"{r.status_code}")

# Cycle distribution
r = s_admin.get(BASE+f'/api/reports/cycle-distribution?cycle_id={cycle_id}')
check("GET cycle-distribution", r.status_code == 200, f"{r.status_code}")

# Vouchers
r = s_admin.get(BASE+f'/api/reports/vouchers?cycle_id={cycle_id}')
check("GET vouchers", r.status_code == 200, f"{r.status_code}: {r.text[:100]}")

# Mark voucher paid - admin only
if drawn_weeks:
    r = xput(s_cashier, f'/api/reports/vouchers/week/{drawn_weeks[0]["id"]}/mark-paid')
    check("Cashier blocked from mark-voucher-paid", r.status_code == 403, f"{r.status_code}")
    r = xput(s_admin, f'/api/reports/vouchers/week/{drawn_weeks[0]["id"]}/mark-paid')
    check("Admin can mark voucher paid", r.status_code == 200, f"{r.status_code}: {r.text[:100]}")

# Weekly summary
if weeks:
    r = s_admin.get(BASE+f'/api/reports/weekly-summary/{weeks[0]["id"]}')
    check("GET weekly-summary", r.status_code == 200, f"{r.status_code}: {r.text[:100]}")

# ============================================================
print("\n=== PHASE 9: SOLD WEEK SCENARIO ===")
# ============================================================

# Refresh week statuses — the draw loop changed them to 'drawn' but weeks is stale
r = s_admin.get(BASE+f'/api/draws/cycles/{cycle_id}/weeks')
weeks = r.json() if r.status_code == 200 else weeks

# If there are still pending weeks, record a pot sale
remaining_pending = [w for w in weeks if w.get('status') == 'pending']
if remaining_pending:
    sale_week = remaining_pending[0]
    # Get active members for buyer
    r = s_admin.get(BASE+f'/api/draws/active-members?cycle_id={cycle_id}')
    active_mems = r.json() if r.status_code == 200 else []
    if active_mems:
        buyer = active_mems[0]
        r = xpost(s_admin, f'/api/draws/weeks/{sale_week["id"]}/sell', json={
            'transaction_type': 'member_sale',
            'buyer_id': buyer['id'],
            'percentage': 10,
            'notes': 'E2E test pot sale',
        })
        check("Admin records pot sale", r.status_code == 200, f"{r.status_code}: {r.text[:200]}")
        if r.status_code == 200:
            ok(f"Sold week {sale_week.get('week_number')} to member {buyer.get('name')}")
            # Disburse sold week — build guarantors excluding the buyer (sale winner)
            r = s_admin.get(BASE+f'/api/draws/cycles/{cycle_id}/weeks')
            wks_updated = r.json() if r.status_code == 200 else []
            sold_wk = next((w for w in wks_updated if w.get('status') == 'sold'), None)
            sale_guarantors = [m for m in cycle_member_ids if m != buyer['id']][:3]
            if not sale_guarantors or len(sale_guarantors) < 3:
                sale_guarantors = [m for m in [mm['id'] for mm in members_created] if m != buyer['id']][:3]
            if sold_wk and len(sale_guarantors) >= 3:
                r = xpost(s_admin, '/api/disbursements', json={
                    'week_id': sold_wk['id'],
                    'gross_amount': sold_wk.get('net_pot', 84000),
                    'service_fee': 0, 'voucher_deduction': 0,
                    'cheque_number': 'CHQ-SALE-001',
                    'cheque_date': date.today().isoformat(),
                    'guarantor_1_id': sale_guarantors[0],
                    'guarantor_2_id': sale_guarantors[1],
                    'guarantor_3_id': sale_guarantors[2],
                })
                check("Admin disburses sold week", r.status_code == 200, f"{r.status_code}: {r.text[:200]}")
else:
    warn("No remaining pending weeks for sale scenario")

# ============================================================
print("\n=== PHASE 10: NOTIFICATIONS ===")
# ============================================================

r = s_admin.get(BASE+'/api/notifications/settings')
check("GET notification settings", r.status_code == 200, f"{r.status_code}")

r = s_admin.get(BASE+'/api/notifications/logs')
check("Admin can GET notification logs", r.status_code == 200, f"{r.status_code}")

r = s_cashier.get(BASE+'/api/notifications/logs')
check("Cashier blocked from notification logs", r.status_code == 403, f"got {r.status_code}")

# Cashier cannot broadcast (week_id is a query param, not JSON body)
r = xpost(s_cashier, f'/api/notifications/broadcast/payment-reminder?week_id={week1_id}')
check("Cashier blocked from broadcast", r.status_code == 403, f"got {r.status_code}")

# ============================================================
print("\n=== PHASE 11: CYCLE CLOSURE ===")
# ============================================================

# Close checklist
r = s_admin.get(BASE+f'/api/draws/cycles/{cycle_id}/closure-checklist')
check("GET closure checklist", r.status_code == 200, f"{r.status_code}: {r.text[:200]}")
if r.status_code == 200:
    cl = r.json()
    ok(f"Checklist: {json.dumps({k: v for k, v in cl.items() if not isinstance(v, list)})[:150]}")

# Cashier cannot close cycle
r = xpost(s_cashier, f'/api/draws/cycles/{cycle_id}/close')
check("Cashier cannot close cycle", r.status_code == 403, f"got {r.status_code}")

# Admin closes cycle (may be blocked if not all requirements met)
r = xpost(s_admin, f'/api/draws/cycles/{cycle_id}/close')
if r.status_code == 200:
    ok("Admin closed cycle successfully")
elif r.status_code == 400:
    warn(f"Cycle close rejected (not all weeks complete): {r.json().get('detail','')[:150]}")
    ok("Cycle close guard works (blocks premature close)")
else:
    bug("Unexpected cycle close result", f"{r.status_code}: {r.text[:150]}")

# ============================================================
print("\n=== PHASE 12: DELETE & CLEANUP GUARDS ===")
# ============================================================

# Cashier cannot delete cycle
r = xdelete(s_cashier, f'/api/draws/cycles/{cycle_id}')
check("Cashier cannot delete cycle", r.status_code == 403, f"got {r.status_code}")

# Cashier cannot delete member
if members_created:
    r = xdelete(s_cashier, f'/api/members/{members_created[-1]["id"]}')
    check("Cashier cannot delete member", r.status_code == 403, f"got {r.status_code}")

# ============================================================
print("\n=== SUMMARY ===")
# ============================================================

total = len(PASSES) + len(BUGS)
print(f"\nTotal tests: {total}")
print(f"  PASS: {len(PASSES)}")
print(f"  BUGS: {len(BUGS)}")
print(f"  WARN: {len(WARNS)}")

if BUGS:
    print(f"\n--- BUGS FOUND ({len(BUGS)}) ---")
    for i, b in enumerate(BUGS, 1):
        print(f"  {i}. {b}")

if WARNS:
    print(f"\n--- WARNINGS ({len(WARNS)}) ---")
    for i, w in enumerate(WARNS, 1):
        print(f"  {i}. {w}")

if not BUGS:
    print("\nAll checks passed!")
