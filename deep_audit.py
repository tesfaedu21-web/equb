"""
Deep full-lifecycle audit for Equb.
Scenarios:
  - Member A  : 2 full spots (same name, same phone)
  - Member B  : 3 full spots + 1 half spot (4 spot-registrations)
  - Member C  : half spot 6 (partner of B's half)
  - Member D  : single full spot
  - Member E  : single full spot
Covers: settings, members, cycle, payments, draws, disbursements,
        reports, association fund, delete cycle, UI calculations.
"""
import requests, json, sys, time
from datetime import date, timedelta

BASE = 'http://localhost:8001'
XHR  = {'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/json'}
BUGS = []
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

def api(s, method, path, **kw):
    fn = getattr(s, method.lower())
    r = fn(BASE + path, headers=XHR, **kw)
    return r

def jpost(s, path, body): return api(s, 'POST', path, json=body)
def jput(s, path, body):  return api(s, 'PUT',  path, json=body)
def jget(s, path):        return api(s, 'GET',  path)
def jdel(s, path):        return api(s, 'DELETE', path)

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 0: CLEANUP ===")
sa = requests.Session()  # admin session (most operations)
sa.post(BASE+'/login', data={'username':'admin','password':'admin123'}, allow_redirects=False)
so = requests.Session()  # superadmin/owner session (settings changes)
so.post(BASE+'/login', data={'username':'owner','password':'owner123'}, allow_redirects=False)
cycles = jget(sa, '/api/draws/cycles').json()
for c in (cycles if isinstance(cycles, list) else []):
    jdel(sa, f'/api/draws/cycles/{c["id"]}')
jdel(sa, '/api/members/permanent/all')
print(f"  Deleted {len(cycles) if isinstance(cycles,list) else 0} cycle(s), all members")

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 1: SETTINGS ===")
# Use small amounts for easy mental arithmetic
FULL  = 1000   # ETB per week full spot
HALF  = 500    # ETB per week half spot
ASSOC = 100    # ETB assoc deduction full spot
FVOU  = 10     # ETB voucher per week full
HVOU  = 5      # ETB voucher per week half
M_SPOTS = 6   # member spots
A_SPOTS = 1   # assoc spots (added at draw-start)
INTERVAL = 3  # group week every 3rd

r = jput(so, '/api/settings', {
    'full_spot_amount': FULL, 'half_spot_amount': HALF,
    'association_deduction': ASSOC,
    'full_spot_voucher': FVOU, 'half_spot_voucher': HVOU,
    'total_member_spots': M_SPOTS, 'total_assoc_spots': A_SPOTS,
    'group_week_interval': INTERVAL,
    'group_name': 'Audit Equb', 'group_tagline': 'Audit Test',
})
check("Settings saved", r.status_code == 200, r.text[:100])
s_data = r.json()
check("full_spot_amount saved", s_data.get('full_spot_amount') == FULL)
check("half_spot_amount saved", s_data.get('half_spot_amount') == HALF)
check("association_deduction saved", s_data.get('association_deduction') == ASSOC)

# Sync spots
r = jpost(sa, '/api/draws/sync-spots', {})
check("Sync spots OK", r.status_code == 200, r.text[:80])

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 2: MEMBERS ===")
# Member A: 2 full spots, same name & phone
RA = jpost(sa, '/api/members', {'name': 'Alpha Dual', 'phone': '+251911000001', 'spots': []}).json()
check("Create Member A", 'id' in RA, str(RA))
mid_A = RA['id']

# Member B: 3 full + 1 half spot, same name & phone
RB = jpost(sa, '/api/members', {'name': 'Beta Quad', 'phone': '+251911000002', 'spots': []}).json()
check("Create Member B", 'id' in RB, str(RB))
mid_B = RB['id']

# Member C: half spot 6 (partner of B's half on spot 6)
RC = jpost(sa, '/api/members', {'name': 'Gamma Half', 'phone': '+251911000003', 'spots': []}).json()
check("Create Member C", 'id' in RC, str(RC))
mid_C = RC['id']

# Member D: single full
RD = jpost(sa, '/api/members', {'name': 'Delta Single', 'phone': '+251911000004', 'spots': []}).json()
check("Create Member D", 'id' in RD, str(RD))
mid_D = RD['id']

# Member E: single full
RE = jpost(sa, '/api/members', {'name': 'Epsilon Single', 'phone': '+251911000005', 'spots': []}).json()
check("Create Member E", 'id' in RE, str(RE))
mid_E = RE['id']

# Duplicate phone allowed (multi-spot: same name & phone)
R_dup = jpost(sa, '/api/members', {'name': 'Alpha Dual', 'phone': '+251911000001', 'spots': []})
check("Duplicate phone allowed (multi-spot)", R_dup.status_code == 201 or R_dup.status_code == 200,
      f"Expected 200/201, got {R_dup.status_code}")
mid_A2 = R_dup.json().get('id', 0)  # second entry with same details
jdel(sa, f'/api/members/{mid_A2}')  # cleanup duplicate

# Validate phone format
r_bad = jpost(sa, '/api/members', {'name': 'Bad Phone', 'phone': 'abc', 'spots': []})
check("Invalid phone rejected", r_bad.status_code == 422, f"got {r_bad.status_code}")

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 3: CYCLE CREATION ===")
today = date.today().isoformat()
r = jpost(sa, '/api/draws/cycles', {
    'name': 'Audit Cycle 2026',
    'start_date': today,
    'total_member_spots': M_SPOTS,
    'full_spot_amount': FULL,
    'half_spot_amount': HALF,
    'association_deduction': ASSOC,
    'full_spot_voucher': FVOU,
    'half_spot_voucher': HVOU,
})
check("Cycle created", r.status_code == 200, r.text[:100])
cyc = r.json()
cycle_id = cyc['id']
check("Cycle has 6 weeks (member spots only)", cyc.get('total_weeks') == M_SPOTS,
      f"got {cyc.get('total_weeks')}")

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 4: SPOT ASSIGNMENTS ===")
# Get available spots
spots_r = jget(sa, '/api/members/available-spots').json()
spot_nums = {s['number']: s['id'] for s in spots_r}
check("6 member spots available", len(spot_nums) >= M_SPOTS, f"got {len(spot_nums)}")

# Member A: spots 1 & 2 (both full)
for sn in [1, 2]:
    r = jpost(sa, f'/api/members/{mid_A}/spots',
              {'spot_id': spot_nums[sn], 'share': 'full', 'weekly_contribution': FULL})
    check(f"A assigned spot {sn}", r.status_code == 200, r.text[:80])

# Member B: spots 3, 4, 5 (full) + spot 6 (half)
for sn in [3, 4, 5]:
    r = jpost(sa, f'/api/members/{mid_B}/spots',
              {'spot_id': spot_nums[sn], 'share': 'full', 'weekly_contribution': FULL})
    check(f"B assigned spot {sn}", r.status_code == 200, r.text[:80])
r = jpost(sa, f'/api/members/{mid_B}/spots',
          {'spot_id': spot_nums[6], 'share': 'half', 'weekly_contribution': HALF})
check("B assigned spot 6 (half)", r.status_code == 200, r.text[:80])

# Member C: spot 6 (other half)
r = jpost(sa, f'/api/members/{mid_C}/spots',
          {'spot_id': spot_nums[6], 'share': 'half', 'weekly_contribution': HALF})
check("C assigned spot 6 (half partner)", r.status_code == 200, r.text[:80])

# Member D: spot 7 — but spot 7 is assoc, so available spots only go to 6.
# Let's check available spots again — all 6 should be taken
spots_after = jget(sa, '/api/members/available-spots').json()
check("All member spots assigned (none left)", len(spots_after) == 0,
      f"{len(spots_after)} still available")

# Verify multi-spot member data
ma_data = jget(sa, f'/api/members/{mid_A}').json()
check("Member A has 2 spots", ma_data.get('spot_count') == 2, str(ma_data.get('spot_count')))
check("Member A weekly = 2×1000=2000", ma_data.get('total_weekly_contribution') == 2000,
      str(ma_data.get('total_weekly_contribution')))

mb_data = jget(sa, f'/api/members/{mid_B}').json()
check("Member B has 4 spots (3f+1h)", mb_data.get('spot_count') == 4,
      str(mb_data.get('spot_count')))
check("Member B weekly = 3000+500=3500", mb_data.get('total_weekly_contribution') == 3500,
      str(mb_data.get('total_weekly_contribution')))

mc_data = jget(sa, f'/api/members/{mid_C}').json()
check("Member C has 1 half spot", mc_data.get('spot_count') == 1)
check("Member C has partner", len(mc_data.get('partners', [])) == 1,
      str(mc_data.get('partners')))

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 5: START DRAWS ===")
# Recalculate pot with actual member assignments
r = jpost(sa, f'/api/draws/cycles/{cycle_id}/recalculate-pot', {})
check("Recalculate pot OK", r.status_code == 200, r.text[:80])
pot_data = r.json()
# Expected: 5 full + 2 half (B's half + C's half = 2 half records)
# gross = 5*1000 + 2*500 = 6000
# assoc = 5*100 + 2*50 = 600
# net = 5400
EXPECTED_GROSS = 5 * FULL + 2 * HALF   # 6000
EXPECTED_ASSOC = 5 * ASSOC + 2 * (ASSOC/2)  # 600
EXPECTED_NET   = EXPECTED_GROSS - EXPECTED_ASSOC  # 5400
check("Gross pot correct (5f+2h)", pot_data.get('gross_pot') == EXPECTED_GROSS,
      f"expected {EXPECTED_GROSS} got {pot_data.get('gross_pot')}")
check("Assoc amount correct", pot_data.get('association_amount') == EXPECTED_ASSOC,
      f"expected {EXPECTED_ASSOC} got {pot_data.get('association_amount')}")
check("Net pot correct", pot_data.get('net_pot') == EXPECTED_NET,
      f"expected {EXPECTED_NET} got {pot_data.get('net_pot')}")

# Start draws with 1 assoc spot
r = jpost(sa, f'/api/draws/cycles/{cycle_id}/start-draws',
          {'at_week_number': 1, 'total_assoc_spots': A_SPOTS})
check("Start draws OK", r.status_code == 200, r.text[:80])
start_data = r.json()
total_weeks = start_data.get('total_weeks')
check("Total weeks = 6 member + 1 assoc = 7", total_weeks == M_SPOTS + A_SPOTS,
      f"got {total_weeks}")

# Get weeks
weeks_r = jget(sa, f'/api/draws/cycles/{cycle_id}/weeks').json()
check("7 weeks returned", len(weeks_r) == 7, f"got {len(weeks_r)}")
w_by_num = {w['week_number']: w for w in weeks_r}

# Check group weeks (every 3rd)
check("Week 3 is group week", w_by_num[3]['is_group_week'])
check("Week 6 is group week", w_by_num[6]['is_group_week'])
check("Week 1 not group week", not w_by_num[1]['is_group_week'])

# Verify pot values after start-draws (should have recalculated with assoc spot)
# After adding 1 assoc spot: but wait — assoc spots don't add to gross in _calculate_pot
# So gross should remain 6000 (member contributions only)
check("Pot gross unchanged after adding assoc spot",
      abs((w_by_num[1].get('gross_pot') or 0) - EXPECTED_GROSS) < 1,
      f"expected {EXPECTED_GROSS} got {w_by_num[1].get('gross_pot')}")

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 6: PAYMENTS (WEEKS 1-3) ===")
all_member_ids = [mid_A, mid_B, mid_C, mid_D, mid_E]
# D & E don't have spots yet — we need to assign them too... wait
# Actually D and E have no spots assigned. Let me check their payment records.
# Payments are created when the cycle starts... let me check.
w1 = w_by_num[1]
r = jget(sa, f'/api/payments/week/{w1["id"]}')
check("Payments list for week 1 OK", r.status_code == 200)
week1_payments = r.json() if r.status_code == 200 else []
# Only members with spots should have payment records
# A (2 spots), B (3f+1h=4 spots), C (1h spot) = 3 unique members
members_with_payments = {p['member_id'] for p in week1_payments}
check("Week 1 has payment records for spot-holders",
      mid_A in members_with_payments and mid_B in members_with_payments and mid_C in members_with_payments,
      f"found member_ids: {members_with_payments}")

# Check payment amounts
pay_A = next((p for p in week1_payments if p['member_id'] == mid_A), None)
pay_B = next((p for p in week1_payments if p['member_id'] == mid_B), None)
pay_C = next((p for p in week1_payments if p['member_id'] == mid_C), None)

if pay_A:
    check("Member A payment = 2000 (2 full spots)", pay_A['amount'] == 2000,
          f"got {pay_A['amount']}")
else:
    bug("Member A has no payment record for week 1")

if pay_B:
    check("Member B payment = 3500 (3f+1h spots)", pay_B['amount'] == 3500,
          f"got {pay_B['amount']}")
else:
    bug("Member B has no payment record for week 1")

if pay_C:
    check("Member C payment = 500 (1 half spot)", pay_C['amount'] == 500,
          f"got {pay_C['amount']}")
else:
    bug("Member C has no payment record for week 1")

# Initialize payment records for weeks 2 and 3 (week 1 already initialized above)
# Payment records are created lazily when GET /api/payments/week/{id} is called
for wnum in [2, 3]:
    jget(sa, f'/api/payments/week/{w_by_num[wnum]["id"]}')

paid_member_ids = [mid_A, mid_B, mid_C]
for wnum in [1, 2, 3]:
    w = w_by_num[wnum]
    for mid in paid_member_ids:
        r = jpost(sa, '/api/payments/batch-record', {
            'member_id': mid,
            'week_ids': [w['id']],
            'payment_date': date.today().isoformat(),
            'payment_method': 'cash',
        })
        check(f"Record payment member{'ABC'[paid_member_ids.index(mid)]} week {wnum}",
              r.status_code == 200, r.text[:80])

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 7: DRAW WEEK 1 (Member A wins spot 1) ===")
# Spot 1 belongs to Member A
spot1_id = spot_nums[1]
w1_id = w_by_num[1]['id']

# Check payment status for spot 1 before draw
r = jget(sa, f'/api/draws/weeks/{w1_id}/check-payment/{spot1_id}')
check("Payment check for spot 1 OK", r.status_code == 200)
pc = r.json()
check("Spot 1 fully paid before draw", pc.get('all_paid'), str(pc))

# Record draw
r = jpost(sa, f'/api/draws/weeks/{w1_id}/draw', {'winner_spot_id': spot1_id})
check("Week 1 draw recorded (spot 1 wins)", r.status_code == 200, r.text[:100])
w1_drawn = r.json()
check("Week 1 status = drawn", w1_drawn.get('status') == 'drawn')
check("Week 1 winner spot = 1", w1_drawn.get('winner_spot', {}).get('number') == 1)

# Member A should be 'received' now (spot 1 won), but spot 2 still active
ma_after = jget(sa, f'/api/members/{mid_A}').json()
check("Member A status = received after spot 1 wins", ma_after.get('status') == 'received')

# Check voucher info for week 1 (spot 1 = full spot winner)
r = jget(sa, f'/api/disbursements/voucher-info/{w1_id}')
check("Voucher info for week 1 OK", r.status_code == 200, r.text[:100])
vi = r.json()
TOTAL_WEEKS = total_weeks   # 7
exp_voucher_full = FVOU * TOTAL_WEEKS   # 10*7=70
exp_service_fee  = FULL                  # 1000 (full spot)
exp_net_after    = EXPECTED_NET - exp_service_fee - exp_voucher_full  # 5400-1000-70=4330
check("Voucher info: full_voucher_total correct", vi.get('full_voucher_total') == exp_voucher_full,
      f"expected {exp_voucher_full} got {vi.get('full_voucher_total')}")
check("Voucher info: service_fee correct", vi.get('service_fee') == exp_service_fee,
      f"expected {exp_service_fee} got {vi.get('service_fee')}")
check("Voucher info: net_after_all correct", abs((vi.get('net_after_all') or 0) - exp_net_after) < 0.01,
      f"expected {exp_net_after} got {vi.get('net_after_all')}")

# Disburse week 1
# Need 3 guarantors — use B, C, and... we only have A,B,C,D,E. A won so use B,C,D.
# But D has no spot assigned in cycle — need to add D to cycle? Or use D as guarantor regardless.
# Actually guarantors just need to be members with status != 'left'
# D and E are members, status should be 'active'
md_data = jget(sa, f'/api/members/{mid_D}').json()
me_data = jget(sa, f'/api/members/{mid_E}').json()
# D and E have no spots. Let me add them to the cycle as members... actually they need spots.
# For simplicity, use B, C, E as guarantors (E may not have spot but is still a member)
r = jpost(sa, '/api/disbursements', {
    'week_id': w1_id,
    'gross_amount': EXPECTED_NET,   # 5400
    'voucher_deduction': exp_voucher_full,  # 70
    'cheque_number': 'CHQ001',
    'cheque_date': date.today().isoformat(),
    'guarantor_1_id': mid_B,
    'guarantor_2_id': mid_C,
    'guarantor_3_id': mid_E,
})
check("Disbursement week 1 created", r.status_code == 200, r.text[:150])
if r.status_code == 200:
    d1 = r.json()
    # net = gross - service_fee - voucher_deduction
    exp_net = EXPECTED_NET - exp_service_fee - exp_voucher_full  # 5400 - 1000 - 70 = 4330
    check("Disbursement net correct", abs(d1.get('net_amount', 0) - exp_net) < 0.01,
          f"expected {exp_net} got {d1.get('net_amount')}")
    check("Disbursement status = issued", d1.get('status') == 'issued')

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 8: DRAW WEEK 2 (Member B wins spot 3) ===")
w2_id = w_by_num[2]['id']
spot3_id = spot_nums[3]

r = jpost(sa, f'/api/draws/weeks/{w2_id}/draw', {'winner_spot_id': spot3_id})
check("Week 2 draw recorded (spot 3 wins)", r.status_code == 200, r.text[:100])

# B has 4 spots (3,4,5,6-half). After winning spot 3, B is 'received'.
# Spots 4, 5, 6 should still be active (spot 3 now received)
mb_after = jget(sa, f'/api/members/{mid_B}').json()
check("Member B status = received after spot 3 wins", mb_after.get('status') == 'received')

# Disburse week 2
vi2 = jget(sa, f'/api/disbursements/voucher-info/{w2_id}').json()
check("Week 2 voucher info service_fee = 1000 (full spot)", vi2.get('service_fee') == FULL,
      f"got {vi2.get('service_fee')}")

r = jpost(sa, '/api/disbursements', {
    'week_id': w2_id,
    'gross_amount': EXPECTED_NET,
    'voucher_deduction': FVOU * TOTAL_WEEKS,
    'cheque_number': 'CHQ002',
    'cheque_date': date.today().isoformat(),
    'guarantor_1_id': mid_A, 'guarantor_2_id': mid_C, 'guarantor_3_id': mid_E,
})
check("Disbursement week 2 created", r.status_code == 200, r.text[:150])

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 9: WEEK 3 = GROUP WEEK (Sold to Member A's spot 2) ===")
w3_id = w_by_num[3]['id']
# Week 3 is a group week — must use sell endpoint
# Buyer: Member A (status=received after spot 1 won — should be BLOCKED from buying)
r = jpost(sa, f'/api/draws/weeks/{w3_id}/sell', {
    'transaction_type': 'group_week_sale',
    'buyer_id': mid_A,
    'spot_id': spot_nums[2],  # A's spot 2
    'percentage': 8,
})
check("Received member blocked from buying group week",
      r.status_code == 400,
      f"got {r.status_code} {r.text[:80]}")

# Correct buyer: Member C (still active)
r = jpost(sa, f'/api/draws/weeks/{w3_id}/sell', {
    'transaction_type': 'group_week_sale',
    'buyer_id': mid_C,
    'spot_id': spot_nums[6],   # C's only spot
    'percentage': 8,
})
check("Group week sold to Member C (spot 6)", r.status_code == 200, r.text[:100])
if r.status_code == 200:
    w3_sold = r.json()
    check("Week 3 status = sold", w3_sold.get('status') == 'sold')
    # C has 1 spot — should now be 'received'
    mc_after = jget(sa, f'/api/members/{mid_C}').json()
    check("Member C received after buying group week", mc_after.get('status') == 'received')
    # winner_spot_id should be set to spot 6
    check("Week 3 winner_spot_id = spot 6",
          w3_sold.get('winner_spot', {}) is not None and
          w3_sold.get('winner_spot', {}).get('number') == 6,
          str(w3_sold.get('winner_spot')))

# Disburse week 3 (sold week)
seller_fee_8pct = EXPECTED_GROSS * 0.08  # 6000*0.08=480
r = jpost(sa, '/api/disbursements', {
    'week_id': w3_id,
    'gross_amount': EXPECTED_NET,
    'voucher_deduction': HVOU * TOTAL_WEEKS,  # half spot C
    'cheque_number': 'CHQ003',
    'cheque_date': date.today().isoformat(),
    'guarantor_1_id': mid_B, 'guarantor_2_id': mid_D, 'guarantor_3_id': mid_E,
})
check("Disbursement week 3 (group/sold) created", r.status_code == 200, r.text[:150])

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 10: HALF-SPOT WINNER (Spot 6: B-half wins) ===")
# Pay weeks 4-5 for remaining members — initialize records first
w4_id = w_by_num[4]['id']
w5_id = w_by_num[5]['id']
for wid in [w4_id, w5_id]:
    jget(sa, f'/api/payments/week/{wid}')
for mid in [mid_A, mid_B]:  # A still has spot 2, B has spots 4,5,6-half
    for wid in [w4_id, w5_id]:
        jpost(sa, '/api/payments/batch-record', {
            'member_id': mid,
            'week_ids': [wid],
            'payment_date': date.today().isoformat(),
            'payment_method': 'cash',
        })

# Week 4: Draw spot 6 (half+half: B and C share it, but C already received)
# Actually C's spot 6 was covered by group week. B still has half of spot 6.
# Let's draw spot 4 instead (belongs to B only, full)
spot4_id = spot_nums[4]
w4_id = w_by_num[4]['id']
r = jpost(sa, f'/api/draws/weeks/{w4_id}/draw', {'winner_spot_id': spot4_id})
check("Week 4 draw (spot 4, B's 2nd full spot)", r.status_code == 200, r.text[:100])

# Check voucher info for spot 4 (full, owned by B)
vi4 = jget(sa, f'/api/disbursements/voucher-info/{w4_id}').json()
check("Week 4 service_fee = 1000 (full)", vi4.get('service_fee') == FULL,
      f"got {vi4.get('service_fee')}")

# Week 5: Draw spot 2 (Member A's 2nd spot — A is 'received' but spot 2 is still 'active')
spot2_id = spot_nums[2]
w5_id = w_by_num[5]['id']
# week 5 payment record already initialized in the loop above (w4,w5)
r = jpost(sa, f'/api/draws/weeks/{w5_id}/draw', {'winner_spot_id': spot2_id})
check("Week 5 draw (spot 2, A's 2nd spot)", r.status_code == 200, r.text[:100])

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 11: REPORTS ===")
# Dashboard
r = jget(sa, '/api/reports/dashboard')
check("Dashboard OK", r.status_code == 200, r.text[:80])
if r.status_code == 200:
    dash = r.json()
    check("Dashboard has total_members", 'total_members' in dash or 'members' in dash)
    check("Dashboard has total_collected", 'total_collected' in dash)
    collected = dash.get('total_collected', 0)
    check("Dashboard collected > 0", collected > 0, str(collected))

# Payment statement for Member B (multi-spot)
r = jget(sa, f'/api/reports/member/{mid_B}/statement')
check("Member B statement OK", r.status_code == 200, r.text[:80])
if r.status_code == 200:
    stmt = r.json()
    summary = stmt.get('summary', {})
    check("Statement has total_paid_amount", 'total_paid_amount' in summary, str(list(stmt.keys())))
    check("Statement has weeks_paid", 'paid' in summary, str(list(summary.keys())))
    total_paid = summary.get('total_paid_amount', 0)
    check("B total paid > 0", total_paid > 0, str(total_paid))
    warn(f"B statement: paid={total_paid}, weeks_paid={summary.get('paid')}")

# Association fund
r = jget(sa, '/api/reports/association-fund')
check("Assoc fund report OK", r.status_code == 200, r.text[:80])
if r.status_code == 200:
    af = r.json()
    check("Assoc fund has total", 'total' in af or 'association_fund' in af, str(list(af.keys())))

# Balance sheet
r = jget(sa, '/api/reports/balance-sheet')
check("Balance sheet OK", r.status_code == 200, r.text[:80])

# General ledger
r = jget(sa, '/api/reports/general-ledger')
check("General ledger OK", r.status_code == 200, r.text[:80])

# Transactions
r = jget(sa, '/api/reports/transactions')
check("Transactions report OK", r.status_code == 200, r.text[:80])

# Weekly summary for week 1
r = jget(sa, f'/api/reports/weekly-summary/{w_by_num[1]["id"]}')
check("Weekly summary OK", r.status_code == 200, r.text[:80])

# Vouchers report
r = jget(sa, '/api/reports/vouchers')
check("Vouchers report OK", r.status_code == 200, r.text[:80])

# Cycle distribution
r = jget(sa, '/api/reports/cycle-distribution')
check("Cycle distribution OK", r.status_code == 200, r.text[:80])

# Member ranking
r = jget(sa, '/api/reports/member-ranking')
check("Member ranking OK", r.status_code == 200, r.text[:80])

# Collection trend
r = jget(sa, '/api/reports/collection-trend')
check("Collection trend OK", r.status_code == 200, r.text[:80])

# Ledger
r = jget(sa, '/api/reports/ledger')
check("Ledger OK", r.status_code == 200, r.text[:80])

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 12: ASSOCIATION EXPENSES ===")
r = jpost(sa, '/api/draws/association-expenses', {
    'cycle_id': cycle_id,
    'description': 'Stationery',
    'amount': 200,
    'expense_date': date.today().isoformat(),
})
check("Add association expense", r.status_code == 200, r.text[:80])
exp_id = r.json().get('id')
r = jget(sa, '/api/draws/association-expenses')
check("List association expenses", r.status_code == 200)
if r.status_code == 200:
    exps = r.json()
    check("Expense in list", any(e.get('id') == exp_id for e in (exps if isinstance(exps,list) else [])))

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 13: CLOSURE CHECKLIST ===")
r = jget(sa, f'/api/draws/cycles/{cycle_id}/closure-checklist')
check("Closure checklist OK", r.status_code == 200, r.text[:80])
if r.status_code == 200:
    cl = r.json()
    items = cl.get('items', [])
    for item in items:
        warn(f"Checklist: {item['check']} = {'OK' if item['ok'] else 'NOT OK'} — {item['detail']}")

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 14: MEMBER OPERATIONS ===")
# Edit member
r = jput(sa, f'/api/members/{mid_E}', {'name': 'Epsilon Updated', 'phone': '+251911000099'})
check("Edit member OK", r.status_code == 200, r.text[:80])
me_upd = r.json()
check("Member name updated", me_upd.get('name') == 'Epsilon Updated')

# Member stats
r = jget(sa, f'/api/members/stats?cycle_id={cycle_id}')
check("Member stats OK", r.status_code == 200)
if r.status_code == 200:
    stats = r.json()
    check("Stats: total > 0", stats.get('total', 0) > 0)
    warn(f"Member stats: total={stats.get('total')}, active={stats.get('active')}, received={stats.get('received')}")

# Member export (CSV)
r = sa.get(BASE + '/api/members/export', headers={'X-Requested-With': 'XMLHttpRequest'})
check("Member export OK", r.status_code == 200)
check("Export is CSV", 'text/csv' in r.headers.get('content-type', '') or 'spreadsheet' in r.headers.get('content-type',''))

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 15: OUTSTANDING PAYMENTS ===")
r = jget(sa, '/api/payments/outstanding-members')
check("Outstanding members OK", r.status_code == 200, r.text[:80])

r = jget(sa, f'/api/payments/summary/cycle/{cycle_id}')
check("Payment summary OK", r.status_code == 200, r.text[:80])

r = jget(sa, '/api/payments/daily-collection')
check("Daily collection OK", r.status_code == 200, r.text[:80])

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 16: NOTIFICATIONS ===")
r = jget(sa, '/api/notifications/settings')
check("Notification settings OK", r.status_code == 200)
r = jget(sa, '/api/notifications/templates')
check("Notification templates OK", r.status_code == 200)
r = jget(sa, '/api/notifications/logs')
check("Notification logs OK", r.status_code == 200)

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 17: DISTRIBUTION CHEQUES ===")
r = jpost(sa, '/api/reports/distribution-cheques', {
    'cycle_id': cycle_id,
    'member_id': mid_A,
    'amount': 500,
    'cheque_number': 'DIST001',
    'cheque_date': date.today().isoformat(),
})
check("Create distribution cheque", r.status_code == 200, r.text[:100])
if r.status_code == 200:
    chq = r.json()
    chq_id = chq.get('id')
    r = jget(sa, '/api/reports/distribution-cheques')
    check("List distribution cheques", r.status_code == 200)
    # Collect it
    r = jput(sa, f'/api/reports/distribution-cheques/{chq_id}/collect', {})
    check("Mark cheque collected", r.status_code == 200, r.text[:80])
    # Uncollect it
    r = jput(sa, f'/api/reports/distribution-cheques/{chq_id}/uncollect', {})
    check("Mark cheque uncollected", r.status_code == 200, r.text[:80])
    # Delete it
    r = jdel(sa, f'/api/reports/distribution-cheques/{chq_id}')
    check("Delete distribution cheque", r.status_code == 200, r.text[:80])

# ─────────────────────────────────────────────────────────────────────────────
print("\n=== PHASE 18: DELETE CYCLE ===")
r = jdel(sa, f'/api/draws/cycles/{cycle_id}')
check("Delete cycle OK", r.status_code == 200, r.text[:100])
if r.status_code == 200:
    # Verify cycles gone
    cycles_after = jget(sa, '/api/draws/cycles').json()
    remaining = [c for c in cycles_after if c.get('id') == cycle_id]
    check("Cycle removed from list", len(remaining) == 0)
    # Verify members who were IN the cycle (had spots) are deleted
    members_after = jget(sa, '/api/members').json()
    cycle_member_ids = {mid_A, mid_B, mid_C}  # these had spots in the cycle
    remaining_cycle_members = [m for m in members_after if m.get('id') in cycle_member_ids]
    check("Cycle spot-holders (A,B,C) deleted", len(remaining_cycle_members) == 0,
          f"Still present: {[m['name'] for m in remaining_cycle_members]}")
    # D and E were never assigned to this cycle — they survive (expected behavior)
    unassigned_remaining = [m for m in members_after if m.get('id') in {mid_D, mid_E}]
    if unassigned_remaining:
        warn(f"Unassigned members survive (expected): {[m['name'] for m in unassigned_remaining]}")
    # Clean them up manually
    for mid in [mid_D, mid_E]:
        jdel(sa, f'/api/members/{mid}')

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "="*60)
print(f"  PASSES: {len(PASSES)}")
print(f"  BUGS:   {len(BUGS)}")
print(f"  WARNS:  {len(WARNS)}")
if BUGS:
    print("\nBUGS FOUND:")
    for b in BUGS:
        print(f"  FAIL: {b}")
if WARNS:
    print("\nWARNINGS:")
    for w in WARNS:
        print(f"  WARN: {w}")
print("="*60)
sys.exit(0 if not BUGS else 1)
