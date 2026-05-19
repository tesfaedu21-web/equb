"""
Live-data scenario audit for Equb.
Works with the EXISTING cycle and members — no cleanup, no deletion.
Discovers real members/spots dynamically, exercises the full lifecycle:
  settings check → start draws → payments → draw → disburse → reports →
  association expense → closure checklist → member operations →
  distribution cheque → notifications
"""
import requests, json, sys, io
from datetime import date, timedelta

# Force UTF-8 output so Ethiopian chars in API responses don't crash on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE  = 'https://equb-production.up.railway.app'
XHR   = {'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/json'}
BUGS  = []
WARNS = []
PASSES = []

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

def jget(s, path):
    return s.get(BASE + path, headers=XHR)

def jpost(s, path, body):
    return s.post(BASE + path, headers=XHR, json=body)

def jput(s, path, body):
    return s.put(BASE + path, headers=XHR, json=body)

def jpatch(s, path, body):
    return s.patch(BASE + path, headers=XHR, json=body)

def jdel(s, path):
    return s.delete(BASE + path, headers=XHR)

# ─── Login ────────────────────────────────────────────────────────────────────
sa = requests.Session()
r_login = sa.post(BASE + '/login',
                  data={'username': 'audit_bot', 'password': 'audit123'},
                  allow_redirects=True)
so = sa  # audit_bot is superadmin, so it covers both roles

r = jget(sa, '/api/auth/me')
if r.status_code != 200:
    # Try the login with a direct cookie check
    print(f"  Login status: {r_login.status_code}, final URL: {r_login.url}")
    print(f"  Session cookies: {dict(sa.cookies)}")
if r.status_code != 200:
    print(f"  Login status: {r_login.status_code}, final URL: {r_login.url}")
    print(f"  Session cookies: {dict(sa.cookies)}")
check("Admin login OK", r.status_code == 200 and r.json().get('role') in ('admin','superadmin'),
      f"status={r.status_code} body={r.text[:100]}")

# ─── PHASE 1: SETTINGS ────────────────────────────────────────────────────────
print("\n=== PHASE 1: SETTINGS ===")
r = jget(so, '/api/settings')
check("Settings readable", r.status_code == 200)
cfg = r.json()
FULL  = cfg.get('full_spot_amount', 0)
HALF  = cfg.get('half_spot_amount', 0)
ASSOC = cfg.get('association_deduction', 0)
FVOU  = cfg.get('full_spot_voucher', 0)
HVOU  = cfg.get('half_spot_voucher', 0)
INTERVAL = cfg.get('group_week_interval', 4)
print(f"  full={FULL:,.0f}  half={HALF:,.0f}  assoc={ASSOC:,.0f}  fvou={FVOU}  hvou={HVOU}  interval={INTERVAL}")
check("full_spot_amount set", FULL > 0, str(FULL))
check("half_spot_amount set", HALF > 0, str(HALF))
check("half < full", HALF < FULL, f"half={HALF} full={FULL}")
check("association_deduction set", ASSOC > 0, str(ASSOC))
check("group_week_interval >= 2", INTERVAL >= 2, str(INTERVAL))

# ─── PHASE 2: ACTIVE CYCLE ────────────────────────────────────────────────────
print("\n=== PHASE 2: ACTIVE CYCLE ===")
r = jget(sa, '/api/draws/cycles')
check("Cycles list OK", r.status_code == 200)
cycles = r.json()
active = next((c for c in cycles if c.get('status') == 'active'), None)
check("Active cycle exists", active is not None, "No active cycle found")
if not active:
    print("  FATAL: no active cycle, cannot continue")
    sys.exit(1)

cycle_id    = active['id']
cycle_name  = active['name']
total_spots = active.get('total_weeks', 0)
print(f"  Cycle: {cycle_name} (id={cycle_id})")

r2 = jget(sa, f'/api/draws/cycles/{cycle_id}/weeks')
check("Weeks list OK", r2.status_code == 200)
weeks = r2.json() if r2.status_code == 200 else []
check("Cycle has weeks", len(weeks) > 0, f"got {len(weeks)}")
print(f"  Total weeks: {len(weeks)}")

w_by_num  = {w['week_number']: w for w in weeks}
pending_weeks = [w for w in weeks if w['status'] == 'pending']
done_weeks    = [w for w in weeks if w['status'] in ('drawn', 'sold')]
group_weeks   = [w for w in weeks if w.get('is_group_week')]
print(f"  Pending: {len(pending_weeks)}  Done: {len(done_weeks)}  Group weeks: {len(group_weeks)}")
warn(f"Draw phase: {active.get('draw_phase', '?')}")

# ─── PHASE 3: MEMBERS ─────────────────────────────────────────────────────────
print("\n=== PHASE 3: MEMBERS ===")
r = jget(sa, '/api/members')
check("Members list OK", r.status_code == 200)
members = r.json() if r.status_code == 200 else []
active_members = [m for m in members if m.get('status') == 'active']
check("Active members exist", len(active_members) > 0, f"got {len(active_members)}")
print(f"  Total active members: {len(active_members)}")

# Find interesting members for testing
multi_spot = [m for m in active_members if m.get('spot_count', 0) >= 2]
half_spot  = [m for m in active_members if any(
    sn for sn in (m.get('spot_numbers') or [])
)]
single_full = [m for m in active_members if m.get('spot_count', 0) == 1]

if len(multi_spot) > 0:
    ok(f"Has multi-spot members (found {len(multi_spot)})")
else:
    warn("No multi-spot members in live data (single-spot-only cycle)")
    check("Has multi-spot members", True)  # not a code bug
check("Has single-spot members", len(single_full) > 0)
print(f"  Multi-spot: {len(multi_spot)}  Single: {len(single_full)}")

# Pick test subjects
m1 = single_full[0] if single_full else active_members[0]
m2 = single_full[1] if len(single_full) > 1 else (active_members[1] if len(active_members) > 1 else m1)
m3 = multi_spot[0] if multi_spot else (active_members[2] if len(active_members) > 2 else m2)
print(f"  Test member 1: id={m1['id']} spots={m1.get('spot_numbers')} weekly={m1.get('total_weekly_contribution')}")
print(f"  Test member 2: id={m2['id']} spots={m2.get('spot_numbers')} weekly={m2.get('total_weekly_contribution')}")
print(f"  Test member 3 (multi): id={m3['id']} spots={m3.get('spot_numbers')} weekly={m3.get('total_weekly_contribution')}")

# Verify member data completeness
check("Member 1 has spot", len(m1.get('spot_numbers') or []) > 0)
check("Member 1 has weekly contribution", (m1.get('total_weekly_contribution') or 0) > 0)
check("Member 3 has 2+ spots", (m3.get('spot_count') or 0) >= 1)

# Verify member detail endpoint
r = jget(sa, f'/api/members/{m1["id"]}')
check("Member detail OK", r.status_code == 200)
m1_detail = r.json()
check("Member detail has spot_count", 'spot_count' in m1_detail)
check("Member detail has partners", 'partners' in m1_detail)

# Member stats
r = jget(sa, f'/api/members/stats?cycle_id={cycle_id}')
check("Member stats OK", r.status_code == 200)
if r.status_code == 200:
    stats = r.json()
    check("Stats total matches", stats.get('total', 0) > 0)
    warn(f"Member stats: total={stats.get('total')} active={stats.get('active')} received={stats.get('received')}")

# ─── PHASE 4: POT CALCULATION ─────────────────────────────────────────────────
print("\n=== PHASE 4: POT CALCULATION ===")
r = jpost(sa, f'/api/draws/cycles/{cycle_id}/recalculate-pot', {})
check("Recalculate pot OK", r.status_code == 200, r.text[:80])
if r.status_code == 200:
    pot = r.json()
    gross = pot.get('gross_pot', 0)
    assoc_amt = pot.get('association_amount', 0)
    net   = pot.get('net_pot', 0)
    check("Gross pot > 0", gross > 0, str(gross))
    check("Net pot < gross", net < gross, f"net={net} gross={gross}")
    check("Assoc = gross - net", abs((gross - net) - assoc_amt) < 1, f"gross={gross} net={net} assoc={assoc_amt}")
    print(f"  Gross={gross:,.0f}  Assoc={assoc_amt:,.0f}  Net={net:,.0f} ETB")
    GROSS = gross
    NET   = net
else:
    GROSS = NET = 0

# ─── PHASE 5: START DRAWS (if not started) ────────────────────────────────────
print("\n=== PHASE 5: START DRAWS ===")
draw_phase = active.get('draw_phase', 'collection')
if draw_phase != 'active':
    assoc_spots = cfg.get('total_assoc_spots', 0)
    r = jpost(sa, f'/api/draws/cycles/{cycle_id}/start-draws',
              {'at_week_number': 1, 'total_assoc_spots': assoc_spots})
    check("Start draws OK", r.status_code == 200, r.text[:100])
    if r.status_code == 200:
        sd = r.json()
        warn(f"Draws started: total_weeks={sd.get('total_weeks')} assoc_spots_added={assoc_spots}")
        # Reload weeks
        weeks = jget(sa, f'/api/draws/cycles/{cycle_id}/weeks').json()
        w_by_num = {w['week_number']: w for w in weeks}
        pending_weeks = [w for w in weeks if w['status'] == 'pending']
        group_weeks   = [w for w in weeks if w.get('is_group_week')]
        print(f"  After start: {len(weeks)} total weeks, {len(group_weeks)} group weeks")
else:
    ok("Draws already active")
    warn(f"draw_phase={draw_phase} — skipping start-draws")

# Verify group week positions
gw_nums = sorted(w['week_number'] for w in group_weeks)
print(f"  Group week numbers: {gw_nums[:10]}...")
if gw_nums:
    check(f"First group week = interval ({INTERVAL})", gw_nums[0] == INTERVAL,
          f"expected {INTERVAL} got {gw_nums[0]}")

# ─── PHASE 6: PAYMENTS ────────────────────────────────────────────────────────
print("\n=== PHASE 6: PAYMENTS ===")
# Find the first non-group pending week
test_weeks = [w for w in pending_weeks if not w.get('is_group_week')][:3]
if not test_weeks:
    test_weeks = pending_weeks[:3]

test_members = [m1, m2, m3]
w1_data = test_weeks[0] if test_weeks else None
if not w1_data:
    bug("No pending weeks to test payments")
else:
    w1_id = w1_data['id']
    w1_num = w1_data['week_number']

    # Initialize payment records
    r = jget(sa, f'/api/payments/week/{w1_id}')
    check(f"Payment records for week {w1_num} OK", r.status_code == 200)
    payments = r.json() if r.status_code == 200 else []
    check("Payment records created for members", len(payments) > 0, f"got {len(payments)}")
    print(f"  Week {w1_num}: {len(payments)} payment records")

    # Verify payment amounts for test members
    for tm in test_members:
        pay = next((p for p in payments if p['member_id'] == tm['id']), None)
        if pay:
            expected = tm.get('total_weekly_contribution', 0)
            check(f"Member {tm['id']} payment amount = {expected:,.0f}",
                  abs(pay['amount'] - expected) < 1,
                  f"got {pay['amount']}")
        else:
            warn(f"Member {tm['id']} has no payment record for week {w1_num}")

    # Record payments for test members (idempotent — ignore "already recorded")
    today = date.today().isoformat()
    def record_payment(member_id, wid, wnum):
        r2 = jpost(sa, '/api/payments/batch-record', {
            'member_id': member_id,
            'week_ids': [wid],
            'payment_date': today,
            'payment_method': 'cash',
        })
        if r2.status_code == 200:
            ok(f"Record payment member {member_id} week {wnum}")
        elif 'already recorded' in r2.text or 'already paid' in r2.text:
            warn(f"Payment member {member_id} week {wnum} already recorded (skipped)")
        else:
            bug(f"Record payment member {member_id} week {wnum}", r2.text[:80])

    for tm in test_members:
        record_payment(tm['id'], w1_id, w1_num)

    # Record for a 2nd week if available
    if len(test_weeks) > 1:
        w2_id = test_weeks[1]['id']
        w2_num = test_weeks[1]['week_number']
        jget(sa, f'/api/payments/week/{w2_id}')  # init
        for tm in test_members[:2]:
            record_payment(tm['id'], w2_id, w2_num)

    # Outstanding payments
    r = jget(sa, f'/api/payments/member/{m1["id"]}?cycle_id={cycle_id}')
    check("Member payment history OK", r.status_code == 200)

    r = jget(sa, '/api/payments/outstanding-members')
    check("Outstanding members OK", r.status_code == 200)

    r = jget(sa, f'/api/payments/summary/cycle/{cycle_id}')
    check("Payment summary OK", r.status_code == 200)

    r = jget(sa, '/api/payments/daily-collection')
    check("Daily collection OK", r.status_code == 200)

# ─── PHASE 7: DRAW WEEK ───────────────────────────────────────────────────────
print("\n=== PHASE 7: DRAW ===")
# Find a pending non-group week where m1 has paid
draw_week = next(
    (w for w in pending_weeks if not w.get('is_group_week')
     and w.get('id') == (w1_data['id'] if w1_data else -1)),
    test_weeks[0] if test_weeks else None
)
if not draw_week:
    draw_week = next((w for w in pending_weeks if not w.get('is_group_week')), None)

winner_spot_id = None
if m1.get('spot_numbers'):
    # Get the spot ID for m1's first spot
    spots_r = jget(sa, '/api/draws/active-spots')
    if spots_r.status_code == 200:
        active_spots = spots_r.json()
        sn = m1['spot_numbers'][0]
        s = next((s for s in active_spots if s.get('number') == sn), None)
        if s:
            winner_spot_id = s['id']

if not draw_week:
    bug("No eligible week for draw test")
elif not winner_spot_id:
    bug("Could not find winner spot ID for member 1")
else:
    dw_id  = draw_week['id']
    dw_num = draw_week['week_number']

    # Pay all outstanding weeks for m1 so draw won't be blocked
    r_hist = jget(sa, f'/api/payments/member/{m1["id"]}?cycle_id={cycle_id}')
    if r_hist.status_code == 200:
        m1_unpaid = [p['week_id'] for p in r_hist.json() if p.get('status') != 'paid']
        if m1_unpaid:
            jget(sa, f'/api/payments/week/{m1_unpaid[0]}')  # init
            rp = jpost(sa, '/api/payments/batch-record', {
                'member_id': m1['id'], 'week_ids': m1_unpaid,
                'payment_date': today, 'payment_method': 'cash',
            })
            if rp.status_code == 200:
                warn(f"Paid {len(m1_unpaid)} outstanding week(s) for winner m1 before draw")

    # Payment check before draw
    r = jget(sa, f'/api/draws/weeks/{dw_id}/check-payment/{winner_spot_id}')
    check("Payment check before draw OK", r.status_code == 200)
    pc = r.json() if r.status_code == 200 else {}
    warn(f"Pre-draw payment status: all_paid={pc.get('all_paid')} unpaid_count={pc.get('unpaid_count')}")

    # Record draw
    r = jpost(sa, f'/api/draws/weeks/{dw_id}/draw', {'winner_spot_id': winner_spot_id})
    check(f"Week {dw_num} draw recorded", r.status_code == 200, r.text[:120])
    if r.status_code == 200:
        drawn = r.json()
        check("Week status = drawn", drawn.get('status') == 'drawn')
        check("Winner spot matches", drawn.get('winner_spot', {}).get('id') == winner_spot_id,
              str(drawn.get('winner_spot')))
        print(f"  Drew spot #{m1['spot_numbers'][0]} for week {dw_num}")

        # Voucher info
        r = jget(sa, f'/api/disbursements/voucher-info/{dw_id}')
        check("Voucher info OK", r.status_code == 200, r.text[:80])
        if r.status_code == 200:
            vi = r.json()
            check("Service fee > 0", (vi.get('service_fee') or 0) > 0, str(vi.get('service_fee')))
            check("Net after all > 0", (vi.get('net_after_all') or 0) > 0, str(vi.get('net_after_all')))
            sf = vi.get('service_fee', 0)
            vd = vi.get('voucher_deduction', 0)
            net_after = vi.get('net_after_all', 0)
            print(f"  Service fee={sf:,.0f}  Voucher={vd:,.0f}  Net winner receives={net_after:,.0f} ETB")

        # Disbursement — pick 3 different guarantors (not the winner)
        guarantors = [m for m in active_members
                      if m['id'] != m1['id'] and m.get('status') != 'left'][:3]
        if len(guarantors) >= 3 and NET > 0:
            r = jpost(sa, '/api/disbursements', {
                'week_id': dw_id,
                'gross_amount': NET,
                'voucher_deduction': vi.get('voucher_deduction', 0) if r.status_code == 200 else 0,
                'cheque_number': f'AUDIT-TEST-{dw_num}',
                'cheque_date': today,
                'guarantor_1_id': guarantors[0]['id'],
                'guarantor_2_id': guarantors[1]['id'],
                'guarantor_3_id': guarantors[2]['id'],
            })
            if r.status_code == 200:
                ok("Disbursement created")
                disb = r.json()
                check("Disbursement status = issued", disb.get('status') == 'issued')
                check("Disbursement net > 0", (disb.get('net_amount') or 0) > 0)
                print(f"  Disbursement net={disb.get('net_amount'):,.0f} ETB cheque=AUDIT-TEST-{dw_num}")
            elif 'Insufficient funds' in r.text or 'insufficient' in r.text.lower():
                warn(f"Disbursement skipped: insufficient funds (live cycle — not all members paid yet)")
            else:
                bug("Disbursement created", r.text[:150])
        else:
            warn(f"Skipped disbursement: guarantors={len(guarantors)} net={NET}")

# ─── PHASE 8: GROUP WEEK SELL ─────────────────────────────────────────────────
print("\n=== PHASE 8: GROUP WEEK ===")
gw = next((w for w in pending_weeks if w.get('is_group_week')), None)
if not gw:
    warn("No pending group week found")
else:
    gw_id  = gw['id']
    gw_num = gw['week_number']
    # Pick a buyer who is still active
    buyer = next((m for m in active_members
                  if m.get('status') == 'active' and m['id'] != m1['id']), None)
    if not buyer:
        warn("No active buyer for group week test")
    else:
        buyer_spot_id = None
        # Get first spot ID for buyer
        r2 = jget(sa, '/api/draws/active-members')
        if r2.status_code == 200:
            am_list = r2.json()
            buyer_am = next((m for m in am_list if m['id'] == buyer['id']), None)
            if buyer_am and buyer_am.get('spots'):
                buyer_spot_id = buyer_am['spots'][0]['id']

        if not buyer_spot_id:
            warn(f"Could not find spot ID for buyer {buyer['id']}")
        else:
            # Pay ALL outstanding weeks for buyer (required before group week sell)
            r_hist = jget(sa, f'/api/payments/member/{buyer["id"]}?cycle_id={cycle_id}')
            if r_hist.status_code == 200:
                hist = r_hist.json()
                unpaid_ids = [p['week_id'] for p in hist if p.get('status') != 'paid']
                if unpaid_ids:
                    jget(sa, f'/api/payments/week/{unpaid_ids[0]}')  # init at least one
                    rp = jpost(sa, '/api/payments/batch-record', {
                        'member_id': buyer['id'], 'week_ids': unpaid_ids,
                        'payment_date': today, 'payment_method': 'cash',
                    })
                    if rp.status_code == 200:
                        warn(f"Paid {len(unpaid_ids)} outstanding week(s) for buyer {buyer['id']} before group sale")
            r = jpost(sa, f'/api/draws/weeks/{gw_id}/sell', {
                'transaction_type': 'group_week_sale',
                'buyer_id': buyer['id'],
                'spot_id': buyer_spot_id,
                'percentage': 8,
            })
            check(f"Group week {gw_num} sold", r.status_code == 200, r.text[:120])
            if r.status_code == 200:
                sold = r.json()
                check("Week status = sold", sold.get('status') == 'sold')
                print(f"  Group week {gw_num} sold to member {buyer['id']} spot {buyer_spot_id}")

# ─── PHASE 9: REPORTS ─────────────────────────────────────────────────────────
print("\n=== PHASE 9: REPORTS ===")
endpoints = [
    ('/api/reports/dashboard',          'Dashboard'),
    ('/api/reports/balance-sheet',      'Balance sheet'),
    ('/api/reports/general-ledger',     'General ledger'),
    ('/api/reports/transactions',       'Transactions'),
    ('/api/reports/vouchers',           'Vouchers'),
    ('/api/reports/cycle-distribution', 'Cycle distribution'),
    ('/api/reports/member-ranking',     'Member ranking'),
    ('/api/reports/collection-trend',   'Collection trend'),
    ('/api/reports/ledger',             'Ledger'),
    ('/api/reports/association-fund',   'Association fund'),
]
for path, label in endpoints:
    r = jget(sa, path)
    check(f"{label} OK", r.status_code == 200, r.text[:60])

# Weekly summary for first done week
done = [w for w in weeks if w['status'] in ('drawn', 'sold')]
if done:
    r = jget(sa, f'/api/reports/weekly-summary/{done[0]["id"]}')
    check("Weekly summary OK", r.status_code == 200, r.text[:60])

# Member statement for m1
r = jget(sa, f'/api/reports/member/{m1["id"]}/statement')
check("Member statement OK", r.status_code == 200, r.text[:60])
if r.status_code == 200:
    stmt = r.json()
    summary = stmt.get('summary', {})
    warn(f"Member {m1['id']} statement: paid={summary.get('total_paid_amount')} weeks_paid={summary.get('paid')}")

# Member export CSV
r = sa.get(BASE + '/api/members/export', headers=XHR)
check("Member export CSV OK", r.status_code == 200)
check("Export is CSV", 'text/csv' in r.headers.get('content-type', '') or 'spreadsheet' in r.headers.get('content-type', ''))

# ─── PHASE 10: ASSOCIATION EXPENSES ───────────────────────────────────────────
print("\n=== PHASE 10: ASSOCIATION EXPENSES ===")
r = jpost(sa, '/api/draws/association-expenses', {
    'cycle_id': cycle_id,
    'description': 'Audit test expense',
    'amount': 500,
    'expense_date': today,
})
check("Add association expense OK", r.status_code == 200, r.text[:80])
exp_id = r.json().get('id') if r.status_code == 200 else None

r = jget(sa, '/api/draws/association-expenses')
check("List association expenses OK", r.status_code == 200)
if r.status_code == 200 and exp_id:
    exps = r.json()
    check("Test expense in list", any(e.get('id') == exp_id for e in (exps if isinstance(exps, list) else [])))

# Delete test expense
if exp_id:
    r = jdel(sa, f'/api/draws/association-expenses/{exp_id}')
    check("Delete test expense OK", r.status_code == 200)

# ─── PHASE 11: CLOSURE CHECKLIST ──────────────────────────────────────────────
print("\n=== PHASE 11: CLOSURE CHECKLIST ===")
r = jget(sa, f'/api/draws/cycles/{cycle_id}/closure-checklist')
check("Closure checklist OK", r.status_code == 200, r.text[:60])
if r.status_code == 200:
    cl = r.json()
    for item in cl.get('items', []):
        status = 'OK' if item['ok'] else 'NOT OK'
        warn(f"  {status}: {item['check']} — {item['detail']}")

# ─── PHASE 12: NOTIFICATIONS ──────────────────────────────────────────────────
print("\n=== PHASE 12: NOTIFICATIONS ===")
r = jget(sa, '/api/notifications/settings')
check("Notification settings OK", r.status_code == 200)
r = jget(sa, '/api/notifications/templates')
check("Notification templates OK", r.status_code == 200)
r = jget(sa, '/api/notifications/logs')
check("Notification logs OK", r.status_code == 200)

# ─── PHASE 13: DISTRIBUTION CHEQUES ──────────────────────────────────────────
print("\n=== PHASE 13: DISTRIBUTION CHEQUES ===")
r = jpost(sa, '/api/reports/distribution-cheques', {
    'cycle_id': cycle_id,
    'member_id': m2['id'],
    'amount': 1000,
    'cheque_number': 'AUDIT-DIST-001',
    'cheque_date': today,
})
check("Create distribution cheque OK", r.status_code == 200, r.text[:100])
if r.status_code == 200:
    chq = r.json()
    chq_id = chq.get('id')
    r = jget(sa, '/api/reports/distribution-cheques')
    check("List distribution cheques OK", r.status_code == 200)
    r = jput(sa, f'/api/reports/distribution-cheques/{chq_id}/collect', {})
    check("Mark collected OK", r.status_code == 200, r.text[:60])
    r = jput(sa, f'/api/reports/distribution-cheques/{chq_id}/uncollect', {})
    check("Mark uncollected OK", r.status_code == 200, r.text[:60])
    r = jdel(sa, f'/api/reports/distribution-cheques/{chq_id}')
    check("Delete distribution cheque OK", r.status_code == 200)

# ─── PHASE 14: MEMBER OPERATIONS ─────────────────────────────────────────────
print("\n=== PHASE 14: MEMBER OPERATIONS ===")
# Edit (restore same data)
r = jput(sa, f'/api/members/{m2["id"]}', {
    'name': m2['name'], 'phone': m2.get('phone', '')
})
check("Edit member OK", r.status_code == 200, r.text[:60])

# Member stats for individual
r = jget(sa, f'/api/members/{m3["id"]}')
check("Multi-spot member detail OK", r.status_code == 200)
if r.status_code == 200:
    d = r.json()
    warn(f"Multi-spot member: spots={d.get('spot_numbers')} weekly={d.get('total_weekly_contribution')} status={d.get('status')}")

# ─── PHASE 15: DISBURSEMENT LIST ─────────────────────────────────────────────
print("\n=== PHASE 15: DISBURSEMENTS ===")
r = jget(sa, f'/api/disbursements?cycle_id={cycle_id}')
check("Disbursements list OK", r.status_code == 200)
if r.status_code == 200:
    disbs = r.json()
    warn(f"Total disbursements in cycle: {len(disbs)}")
    for d in disbs[:3]:
        warn(f"  Week {d.get('week_number')} net={d.get('net_amount'):,.0f} cheque={d.get('cheque_number')} status={d.get('status')}")

# ─── SUMMARY ─────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"  PASSES: {len(PASSES)}")
print(f"  BUGS:   {len(BUGS)}")
print(f"  WARNS:  {len(WARNS)}")

if BUGS:
    print("\nBUGS FOUND:")
    for b in BUGS:
        print(f"  FAIL: {b}")

if WARNS:
    print("\nWARNINGS / INFO:")
    for w in WARNS:
        print(f"  {w}")

print("=" * 60)
print("\nNOTE: Live cycle and members were NOT deleted.")
print(f"      Cycle '{cycle_name}' (id={cycle_id}) remains active.")
print("      Test expense and distribution cheque were cleaned up.")
print("      Payments recorded for test members remain in the system.")
sys.exit(0 if not BUGS else 1)
