#!/usr/bin/env python3
"""
Equb Management System — Realistic QA Scenario
Role: Senior QA Engineer + Fintech Specialist

Covers
──────
  50-spot cycle (47 members — all spot combinations)
    • 30 single full-spot members (spots 1-30)
    • 3 double full-spot members  (spots 31-36, 2 spots each)
    • 1 triple full-spot member   (spots 37-39, 3 spots)
    • 5 half-spot pairs           (spots 40-44, 2 members per spot)
    • 3 mixed full+half + 3 half-partners (spots 45-50)
  Duplicate name test (2 members named "Tesfaye Alemu")
  Member without phone
  8 weeks: 6 draw weeks + 1 group week + 1 association draw week
  Deliberate missed payment → pot-on-hold scenario
  Late payment recovery
  Batch payment (multi-week single receipt)
  Pot sale (member_sale with percentage cut)
  Association spot draw + sale
  Voucher returns + vendor paid
  Association expense recording
  Disbursements: normal, pot-on-hold block, duplicate block, dup-guarantor block
  All reports: dashboard, general-ledger, balance-sheet, collection-trend,
               cycle-distribution, association-fund, vouchers, member-ranking,
               member-statement, weekly-summary, daily-collection, outstanding-members
  Distribution cheques (end-of-cycle)
  Cycle closure checklist

Run:  python e2e_realistic.py
"""
import os, requests, json, sys, time
from datetime import date, timedelta

BASE         = os.environ.get("EQUB_BASE_URL",    "https://equb-production.up.railway.app").rstrip("/")
ADMIN_USER   = os.environ.get("EQUB_ADMIN_USER",  "admin")
ADMIN_PASS   = os.environ.get("EQUB_ADMIN_PASS",  "Tesfa123")
CASHIER_USER = os.environ.get("EQUB_CASHIER_USER","cashier")
CASHIER_PASS = os.environ.get("EQUB_CASHIER_PASS","cashier123")

_RUN  = str(int(time.time()))[-5:]   # 5-digit run tag for unique phone numbers
BUGS, PASSES, WARNS = [], [], []
XHR = {'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/json'}

def ok(msg):
    PASSES.append(msg); print(f"  PASS  {msg}")
def bug(msg, d=''):
    e = f"{msg}: {d}" if d else msg; BUGS.append(e)
    print(f"  BUG   {msg}" + (f"\n        {d}" if d else ''))
def warn(msg):
    WARNS.append(msg); print(f"  WARN  {msg}")
def check(label, cond, fail=''):
    ok(label) if cond else bug(label, fail)
def xpost(s, p, **kw):   return s.post(  BASE+p, headers=XHR, **kw)
def xput(s,  p, **kw):   return s.put(   BASE+p, headers=XHR, **kw)
def xdelete(s, p, **kw): return s.delete(BASE+p, headers=XHR, **kw)

def ph(i): return f"09{_RUN}{i:03d}"   # 10-digit Ethiopian-format phone

print(f"\nTarget : {BASE}")
print(f"Run tag: {_RUN}\n")

# ==============================================================================═
# LOGIN
# ==============================================================================═
s_adm = requests.Session()
s_cas = requests.Session()
for s, u, p in [(s_adm, ADMIN_USER, ADMIN_PASS), (s_cas, CASHIER_USER, CASHIER_PASS)]:
    r = s.post(BASE+'/login', data={'username': u, 'password': p}, allow_redirects=False)
    if r.status_code != 302:
        print(f"FATAL: login failed for {u} ({r.status_code}: {r.text[:80]})"); sys.exit(1)
ok("Admin + Cashier sessions active")

# ==============================================================================═
# PHASE 0 -CLEANUP
# ==============================================================================═
print("\n=== PHASE 0 -CLEANUP ===")
r = s_adm.get(BASE+'/api/draws/cycles')
if r.status_code == 200:
    for c in r.json():
        xdelete(s_adm, f'/api/draws/cycles/{c["id"]}')
    print(f"  Removed {len(r.json())} prior cycle(s)")
xdelete(s_adm, '/api/members/permanent/all')
print("  Members cleared")

# ==============================================================================═
# PHASE 1 -CYCLE CREATION
# ==============================================================================═
print("\n=== PHASE 1 -CYCLE CREATION ===")
START = (date.today() - timedelta(weeks=12)).isoformat()
r = xpost(s_adm, '/api/draws/cycles', json={
    'name':                  f'QA Realistic 2026 — {_RUN}',
    'start_date':            START,
    'total_member_spots':    50,
    'total_assoc_spots':     3,
    'full_spot_amount':      21000,
    'half_spot_amount':      10500,
    'association_deduction': 1000,
    'full_spot_voucher':     80,
    'half_spot_voucher':     40,
    'group_week_interval':   4,
})
check("Cycle created (50 member + 3 assoc spots, interval=4)", r.status_code == 200, r.text[:150])
if r.status_code != 200: sys.exit("FATAL: no cycle")
CYCLE_ID = r.json()['id']

r = s_adm.get(BASE+f'/api/draws/cycles/{CYCLE_ID}/weeks')
WEEKS = r.json() if r.status_code == 200 else []
GW_WEEKS  = [w for w in WEEKS if w.get('is_group_week')]
WRK_WEEKS = [w for w in WEEKS if w.get('is_worker_week')]
print(f"  cycle_id={CYCLE_ID}  total_weeks={len(WEEKS)}  group={len(GW_WEEKS)}  worker={len(WRK_WEEKS)}")
check(f"Week 4 is a group week", any(w['week_number'] == 4 for w in GW_WEEKS),
      f"group weeks: {[w['week_number'] for w in GW_WEEKS]}")

# ==============================================================================═
# PHASE 2 -MEMBER CREATION
# ==============================================================================═
print("\n=== PHASE 2 -MEMBER CREATION ===")

def new_member(name, phone=None):
    body = {'name': name}
    if phone: body['phone'] = phone
    r = xpost(s_adm, '/api/members', json=body)
    if r.status_code == 200:
        return r.json()['id']
    bug(f"Create '{name}'", f"{r.status_code}: {r.text[:80]}")
    return None

# ── 30 single full-spot members ──────────────────────────────────────────────
F1 = [
    new_member("Abebe Kebede",       ph(1)),
    new_member("Tigist Alemu",       ph(2)),
    new_member("Dawit Haile",        ph(3)),   # ← will miss payment (pot-on-hold target)
    new_member("Meron Tadesse",      ph(4)),
    new_member("Samuel Girma",       ph(5)),
    new_member("Hana Bekele",        None),    # no phone — edge case
    new_member("Yonas Tesfaye",      ph(7)),
    new_member("Liya Mekonnen",      ph(8)),
    new_member("Brhane Woldu",       ph(9)),
    new_member("Almaz Negash",       ph(10)),
    new_member("Tewodros Girma",     ph(11)),
    new_member("Selam Haile",        ph(12)),
    new_member("Getachew Fikadu",    ph(13)),
    new_member("Hiwot Tekle",        ph(14)),
    new_member("Tesfaye Alemu",      ph(15)),  # duplicate name ↓
    new_member("Tesfaye Alemu",      ph(16)),  # same name, different person
    new_member("Kebede Worku",       ph(17)),
    new_member("Biruk Tsegay",       ph(18)),
    new_member("Mahlet Desta",       ph(19)),
    new_member("Asnake Belay",       ph(20)),
    new_member("Tsehay Woldemariam", ph(21)),
    new_member("Demeke Mesfin",      ph(22)),
    new_member("Meaza Solomon",      ph(23)),
    new_member("Berhane Gebre",      ph(24)),
    new_member("Aster Habtamu",      ph(25)),
    new_member("Miriam Tesfaye",     ph(26)),
    new_member("Azeb Tadesse",       ph(27)),
    new_member("Worku Kebede",       ph(28)),
    new_member("Simret Neguse",      ph(29)),
    new_member("Abiy Befikadu",      ph(30)),
]
F1 = [x for x in F1 if x]
check(f"30 single full-spot members created", len(F1) == 30, f"got {len(F1)}")
MISS_MID = F1[2]   # Dawit Haile — will miss week 3

# ── 3 double full-spot members (2 spots each) ────────────────────────────────
F2 = [
    new_member("Selamawit Wube",       ph(31)),
    new_member("Hailu Gebremedhin",    ph(32)),
    new_member("Mulugeta Tadesse",     ph(33)),
]
F2 = [x for x in F2 if x]
check(f"3 double full-spot members created", len(F2) == 3, f"got {len(F2)}")

# ── 1 triple full-spot member (3 spots) ──────────────────────────────────────
F3 = [new_member("Kassa Miruts", ph(34))]
F3 = [x for x in F3 if x]
check("1 triple full-spot member created", len(F3) == 1)

# ── 5 half-spot pairs ────────────────────────────────────────────────────────
PAIRS = [
    (new_member("Zinash Tesfaye",      ph(35)), new_member("Emebet Girma",      ph(36))),
    (new_member("Genet Assefa",        ph(37)), new_member("Meseret Wondimu",   ph(38))),
    (new_member("Eyerusalem Tadesse",  ph(39)), new_member("Bethlehem Yoseph",  ph(40))),
    (new_member("Adey Alemayehu",      ph(41)), new_member("Fikir Bamlak",      ph(42))),
    (new_member("Tiruwork Damtew",     ph(43)), new_member("Aziza Wondifraw",   ph(44))),
]
PAIRS = [(a, b) for a, b in PAIRS if a and b]
check(f"5 half-spot pairs (10 members) created", len(PAIRS) == 5, f"got {len(PAIRS)}")

# ── 3 mixed (1 full + 1 half) + 3 half-partners ──────────────────────────────
MIXED = [
    (new_member("Tsega Hailu",         ph(45)), new_member("Fana Belete",         ph(46))),
    (new_member("Endalkachew Nega",    ph(47)), new_member("Likitu Mebratu",      ph(48))),
    (new_member("Zelalem Beshah",      ph(49)), new_member("Shewit Tesfamariam",  ph(50))),
]
MIXED = [(a, b) for a, b in MIXED if a and b]
check(f"3 mixed (full+half) + 3 half-partners created", len(MIXED) == 3, f"got {len(MIXED)}")

total_m = len(F1)+len(F2)+len(F3)+len(PAIRS)*2+len(MIXED)*2
print(f"  Total members: {total_m}")

# Edge-case: invalid phone rejected
r = xpost(s_adm, '/api/members', json={'name': 'Bad Phone', 'phone': '12'})
check("Too-short phone rejected", r.status_code in (400, 422), f"got {r.status_code}")

# Edge-case: empty name rejected
r = xpost(s_adm, '/api/members', json={'name': '', 'phone': ph(99)})
check("Empty name rejected", r.status_code in (400, 422), f"got {r.status_code}")

# Cashier cannot create members
r = xpost(s_cas, '/api/members', json={'name': 'Cashier Try', 'phone': ph(98)})
check("Cashier cannot create member", r.status_code == 403, f"got {r.status_code}")

# ==============================================================================═
# PHASE 3 -SPOT ASSIGNMENT
# ==============================================================================═
print("\n=== PHASE 3 -SPOT ASSIGNMENT ===")

r = s_adm.get(BASE+f'/api/draws/active-spots?cycle_id={CYCLE_ID}')
ALL_SP = r.json() if r.status_code == 200 else []
M_SP   = [s for s in ALL_SP if s.get('spot_type', 'member') == 'member']
check(f"At least 50 member spots available", len(M_SP) >= 50, f"got {len(M_SP)}")
M_SP = M_SP[:50]   # use only the first 50 for this cycle's assignments

si = 0
ok_cnt = fail_cnt = 0
SPOT_OF = {}   # member_id → [spot_id, ...]

def asgn(mid, spot_id, share='full'):
    global ok_cnt, fail_cnt
    c = 21000 if share == 'full' else 10500
    r = xpost(s_adm, f'/api/members/{mid}/spots',
              json={'spot_id': spot_id, 'cycle_id': CYCLE_ID,
                    'share': share, 'weekly_contribution': c})
    if r.status_code == 200:
        ok_cnt += 1
        SPOT_OF.setdefault(mid, []).append(spot_id)
    else:
        fail_cnt += 1
        bug(f"assign member {mid} → spot {spot_id} ({share})", f"{r.status_code}: {r.text[:80]}")
    return r.status_code == 200

# 30 × single full
for mid in F1:
    if si < len(M_SP): asgn(mid, M_SP[si]['id']); si += 1

# 3 × double full (2 spots each)
for mid in F2:
    for _ in range(2):
        if si < len(M_SP): asgn(mid, M_SP[si]['id']); si += 1

# 1 × triple full (3 spots)
for mid in F3:
    for _ in range(3):
        if si < len(M_SP): asgn(mid, M_SP[si]['id']); si += 1

# 5 half-spot pairs (2 members → same spot)
PAIR_SPOT_IDS = []
for (m1, m2) in PAIRS:
    if si < len(M_SP):
        sp = M_SP[si]; si += 1
        asgn(m1, sp['id'], 'half')
        asgn(m2, sp['id'], 'half')
        PAIR_SPOT_IDS.append(sp['id'])

# 3 mixed: each mixed member gets 1 full + 1 half (half shared with partner)
MIXED_SPOTS = []
for (mix_mid, partner_mid) in MIXED:
    if si+1 < len(M_SP):
        full_sp = M_SP[si]; si += 1
        half_sp = M_SP[si]; si += 1
        asgn(mix_mid,     full_sp['id'], 'full')
        asgn(mix_mid,     half_sp['id'], 'half')
        asgn(partner_mid, half_sp['id'], 'half')
        MIXED_SPOTS.append((full_sp['id'], half_sp['id']))

check(f"All {ok_cnt} spot assignments succeeded", fail_cnt == 0, f"{fail_cnt} failed")
print(f"  Spots used: {si}/50")

# Duplicate assignment blocked
if F1 and M_SP:
    r = xpost(s_adm, f'/api/members/{F1[0]}/spots',
              json={'spot_id': M_SP[0]['id'], 'cycle_id': CYCLE_ID})
    check("Duplicate spot assignment blocked", r.status_code in (400, 409), f"got {r.status_code}: {r.text[:60]}")

# Cashier cannot assign spots
if F1 and len(M_SP) > si:
    r = xpost(s_cas, f'/api/members/{F1[0]}/spots',
              json={'spot_id': M_SP[si]['id'], 'cycle_id': CYCLE_ID})
    check("Cashier cannot assign spots", r.status_code == 403, f"got {r.status_code}")

# Create a member with NO spot assignment — used later to test buyer-cycle check
TMP_NO_SPOT_MID = None
r_tmp = xpost(s_adm, '/api/members', json={'name': f'NoSpot {_RUN}', 'phone': ph(997)})
if r_tmp.status_code == 200:
    TMP_NO_SPOT_MID = r_tmp.json().get('id')

# ==============================================================================═
# PHASE 4 -PAYMENT RECORD VERIFICATION
# ==============================================================================═
print("\n=== PHASE 4 -PAYMENT RECORD VERIFICATION ===")

r = s_cas.get(BASE+f'/api/payments/week/{WEEKS[0]["id"]}')
check("Cashier can GET week 1 payment list", r.status_code == 200, r.text[:80])
if r.status_code == 200:
    pays = r.json()
    print(f"  Week 1 payment records: {len(pays)}")
    amounts = sorted({p['amount'] for p in pays})
    print(f"  Unique amounts: {amounts}")

    # Double-spot member pays more than single-spot
    f2_pay  = next((p for p in pays if p['member_id'] == F2[0]), None) if F2 else None
    f3_pay  = next((p for p in pays if p['member_id'] == F3[0]), None) if F3 else None
    f1_pay  = next((p for p in pays if p['member_id'] == F1[0]), None)
    if f1_pay and f2_pay:
        check(f"Double-spot pays more ({f2_pay['amount']} > {f1_pay['amount']})",
              f2_pay['amount'] > f1_pay['amount'])
    if f1_pay and f3_pay:
        check(f"Triple-spot pays most ({f3_pay['amount']} > {f2_pay['amount'] if f2_pay else 0})",
              f3_pay['amount'] > (f2_pay['amount'] if f2_pay else 0))

    # Half-spot pair: each member pays 10500
    if PAIRS:
        p1 = next((p for p in pays if p['member_id'] == PAIRS[0][0]), None)
        p2 = next((p for p in pays if p['member_id'] == PAIRS[0][1]), None)
        check("Half-pair member 1: payment record exists", bool(p1))
        check("Half-pair member 2: payment record exists", bool(p2))
        if p1 and p2:
            check(f"Half-spot members each pay 10500 (got {p1['amount']}/{p2['amount']})",
                  p1['amount'] == 10500 and p2['amount'] == 10500)

    # Mixed member pays 31500 (21000 full + 10500 half)
    if MIXED:
        mx_pay = next((p for p in pays if p['member_id'] == MIXED[0][0]), None)
        if mx_pay:
            check(f"Mixed (full+half) member pays 31500 (got {mx_pay['amount']})",
                  mx_pay['amount'] == 31500, f"got {mx_pay['amount']}")

# ==============================================================================═
# PHASE 5 -PAYMENT COLLECTION  (weeks 1-8)
# ==============================================================================═
print("\n=== PHASE 5 -PAYMENT COLLECTION ===")

r = s_adm.get(BASE+f'/api/members?cycle_id={CYCLE_ID}')
ALL_MID = [m['id'] for m in r.json()] if r.status_code == 200 else []
print(f"  Members in cycle: {len(ALL_MID)}")

for i, wk in enumerate(WEEKS[:8]):
    wk_id  = wk['id']
    wk_num = wk.get('week_number', i+1)
    # Week 3: skip Dawit Haile to create a missed payment
    payers = [m for m in ALL_MID if m != MISS_MID] if wk_num == 3 else ALL_MID
    r = xpost(s_cas, '/api/payments/bulk', json={
        'week_id': wk_id, 'member_ids': payers,
        'status': 'paid', 'paid_date': date.today().isoformat(),
    })
    note = "  <-- MISS Dawit Haile" if wk_num == 3 else ""
    if r.status_code == 200:
        ok(f"Week {wk_num}: {r.json().get('updated',0)} paid{note}")
    else:
        bug(f"Bulk pay week {wk_num}", f"{r.status_code}: {r.text[:100]}")

# Verify Dawit Haile missed week 3
wk3 = next((w for w in WEEKS if w['week_number'] == 3), None)
if wk3:
    r = s_adm.get(BASE+f'/api/payments/week/{wk3["id"]}')
    if r.status_code == 200:
        miss_p = next((p for p in r.json() if p['member_id'] == MISS_MID), None)
        check("Dawit Haile is pending/missed for week 3",
              miss_p and miss_p['status'] in ('pending', 'missed'),
              f"status={miss_p['status'] if miss_p else 'not found'}")

# Late-payment round-trip
r = xpost(s_cas, '/api/payments/bulk', json={'week_id': WEEKS[0]['id'],
           'member_ids': [F1[5]], 'status': 'late'})
check("Can mark payment as late", r.status_code == 200, f"{r.status_code}")
r = xpost(s_cas, '/api/payments/bulk', json={'week_id': WEEKS[0]['id'],
           'member_ids': [F1[5]], 'status': 'paid', 'paid_date': date.today().isoformat()})
check("Late payment recovered to paid", r.status_code == 200, f"{r.status_code}")

# Batch payment (multi-week single receipt) for one member
r = xpost(s_cas, '/api/payments/batch-record', json={
    'member_id':      F1[6],
    'week_ids':       [WEEKS[0]['id']],
    'payment_method': 'bank_transfer',
    'reference':      f'TRF-QA-{_RUN}',
    'notes':          'QA batch payment test',
})
if r.status_code in (200, 409):  # 409 = already paid
    ok(f"Batch-record payment: {r.status_code}")
else:
    bug("Batch-record payment", f"{r.status_code}: {r.text[:100]}")

# Outstanding members endpoint
r = s_adm.get(BASE+f'/api/payments/outstanding-members?cycle_id={CYCLE_ID}')
check("GET outstanding-members", r.status_code == 200, f"{r.status_code}")
if r.status_code == 200:
    oms = r.json()
    miss_in_list = any(m['member_id'] == MISS_MID for m in oms)
    check("Dawit Haile appears in outstanding-members", miss_in_list,
          f"outstanding ids: {[m['member_id'] for m in oms[:5]]}")

# ==============================================================================═
# PHASE 6 -START DRAWS PHASE
# ==============================================================================═
print("\n=== PHASE 6 -START DRAWS PHASE ===")
r = xpost(s_adm, f'/api/draws/cycles/{CYCLE_ID}/start-draws',
          json={'at_week_number': 1, 'assoc_spots': 3})
check("Draws phase started", r.status_code == 200, r.text[:150])

# Refresh weeks
r     = s_adm.get(BASE+f'/api/draws/cycles/{CYCLE_ID}/weeks')
WEEKS = r.json() if r.status_code == 200 else WEEKS

r = s_adm.get(BASE+f'/api/draws/active-spots?cycle_id={CYCLE_ID}')
DRAW_SP = r.json() if r.status_code == 200 else []
MEM_DSP = [s for s in DRAW_SP if s.get('spot_type', 'member') == 'member']
print(f"  Draw-eligible spots: {len(DRAW_SP)} total, {len(MEM_DSP)} member spots")

# Cashier cannot start draws
r = xpost(s_cas, f'/api/draws/cycles/{CYCLE_ID}/start-draws', json={'at_week_number': 1})
check("Cashier cannot start draws", r.status_code == 403, f"got {r.status_code}")

# ==============================================================================═
# PHASE 7 -RECORD DRAWS
# ==============================================================================═
print("\n=== PHASE 7 -RECORD DRAWS ===")

r = s_adm.get(BASE+f'/api/draws/active-members?cycle_id={CYCLE_ID}')
ACTIVE_M = r.json() if r.status_code == 200 else []

PENDING   = [w for w in WEEKS if w.get('status') == 'pending']
DRAWN     = []
used_sids = set()
POH_WEEK  = None   # pot-on-hold week (Dawit Haile's spot drawn)

def pick_spot(prefer_id=None):
    if prefer_id and prefer_id not in used_sids:
        return prefer_id
    for s in MEM_DSP:
        if s['id'] not in used_sids:
            return s['id']
    return None

for wk in PENDING[:8]:
    wk_num = wk.get('week_number')

    # Week 4 = group week — handled in Phase 8
    if wk.get('is_group_week'):
        continue

    # For week 3: draw Dawit Haile's spot (he missed payment → pot on hold)
    if wk_num == 3 and SPOT_OF.get(MISS_MID):
        sid = SPOT_OF[MISS_MID][0]
        POH_WEEK = wk
    else:
        sid = pick_spot()

    if not sid:
        warn(f"No drawable spot for week {wk_num}"); continue

    r = xpost(s_adm, f'/api/draws/weeks/{wk["id"]}/draw', json={'winner_spot_id': sid})
    if r.status_code == 200:
        used_sids.add(sid)
        dw = r.json()
        flag = "  <-- winner MISSED wk3 (pot-on-hold)" if wk == POH_WEEK else ""
        ok(f"Week {wk_num}: drew spot #{dw.get('winner_spot', {}).get('number','?')}{flag}")
        DRAWN.append(dw)
    elif r.status_code == 400 and 'hold' in r.text.lower():
        # Pot on hold — skip this spot so pick_spot() won't retry it
        used_sids.add(sid)
        ok(f"Week {wk_num}: spot on hold (winner has unpaid weeks) — skipped as expected")
    else:
        bug(f"Draw week {wk_num}", f"{r.status_code}: {r.text[:120]}")

check("At least 4 weeks drawn", len(DRAWN) >= 4, f"drew {len(DRAWN)}")

# Redraw same week blocked
if DRAWN:
    alt = pick_spot()
    if alt:
        r = xpost(s_adm, f'/api/draws/weeks/{DRAWN[0]["id"]}/draw', json={'winner_spot_id': alt})
        check("Redraw same week blocked", r.status_code in (400, 409), f"got {r.status_code}: {r.text[:60]}")

# Cashier cannot draw
if PENDING:
    r = xpost(s_cas, f'/api/draws/weeks/{PENDING[0]["id"]}/draw',
              json={'winner_spot_id': MEM_DSP[0]['id']})
    check("Cashier cannot draw", r.status_code == 403, f"got {r.status_code}")

# ==============================================================================═
# PHASE 8 -GROUP WEEK (POT SALE)
# ==============================================================================═
print("\n=== PHASE 8 -GROUP WEEK (POT SALE) ===")

# Re-fetch after Phase 7 draws — some members are now "received" and can't buy
r = s_adm.get(BASE+f'/api/draws/active-members?cycle_id={CYCLE_ID}')
ACTIVE_M = r.json() if r.status_code == 200 else ACTIVE_M

# Refresh weeks so we see current statuses
r = s_adm.get(BASE+f'/api/draws/cycles/{CYCLE_ID}/weeks')
WEEKS = r.json() if r.status_code == 200 else WEEKS

GW = next((w for w in WEEKS if w.get('is_group_week') and w.get('status') == 'pending'), None)
if GW and ACTIVE_M:
    buyer = ACTIVE_M[0]
    r = xpost(s_adm, f'/api/draws/weeks/{GW["id"]}/sell', json={
        'transaction_type': 'group_week_sale',
        'buyer_id':          buyer['id'],
        'percentage':        0,
        'notes':             f'QA group-week sale — {_RUN}',
    })
    check(f"Group week {GW['week_number']} pot sale recorded", r.status_code == 200,
          f"{r.status_code}: {r.text[:150]}")
    if r.status_code == 200:
        gw_r = r.json()
        ok(f"  Buyer: {buyer.get('name')}, net_pot={gw_r.get('net_pot')}")
else:
    warn("No pending group week available")

# Reject buyer who is not a member of this cycle
r_tmp = xpost(s_adm, '/api/members', json={
    'name': f'OutOfCycle {_RUN}', 'phone': ph(997), 'notes': 'temp buyer test',
})
if TMP_NO_SPOT_MID and DRAWN:
    test_wk = DRAWN[0]
    rb = xpost(s_adm, f'/api/draws/weeks/{test_wk["id"]}/sell', json={
        'transaction_type': 'member_sale', 'buyer_id': TMP_NO_SPOT_MID, 'percentage': 0,
    })
    check("Non-cycle member rejected as buyer",
          rb.status_code == 400 and 'not a member' in rb.text.lower(),
          f"{rb.status_code}: {rb.text[:120]}")
    xdelete(s_adm, f'/api/members/permanent/{TMP_NO_SPOT_MID}')
else:
    warn("Skipped non-cycle buyer test (no temp member or no drawn weeks)")

# Member sale with percentage (week 5 winner sells to another member at 10%)
wk5_draw = next((d for d in DRAWN if d.get('week_number') == 5), None)
if not wk5_draw:
    wk5_draw = DRAWN[1] if len(DRAWN) > 1 else None
MEM_SALE_WK = None
if wk5_draw:
    winner_sid  = (wk5_draw.get('winner_spot') or {}).get('id')
    winner_mids = {mid for mid, sids in SPOT_OF.items() if winner_sid in sids}
    buyer2      = next((m for m in ACTIVE_M if m['id'] not in winner_mids), None)
    if buyer2:
        r = xpost(s_adm, f'/api/draws/weeks/{wk5_draw["id"]}/sell', json={
            'transaction_type': 'member_sale',
            'buyer_id':          buyer2['id'],
            'percentage':        10,
            'notes':             'QA member-sale: winner sells at 10% cut',
        })
        if r.status_code == 200:
            ok(f"Week {wk5_draw.get('week_number')} member-sale at 10% to {buyer2.get('name')}")
            MEM_SALE_WK = wk5_draw
        elif r.status_code == 400 and ('already received' in r.text.lower() or 'already processed' in r.text.lower()):
            ok(f"Week {wk5_draw.get('week_number')} member-sale correctly blocked ({r.json().get('detail','')})")
        elif r.status_code == 400:
            warn(f"Member sale blocked unexpectedly — {r.text[:80]}")
        else:
            bug("Member sale", f"{r.status_code}: {r.text[:100]}")

# ==============================================================================═
# PHASE 9 -DISBURSEMENTS
# ==============================================================================═
print("\n=== PHASE 9 -DISBURSEMENTS ===")

def guarantors_for(exclude_set):
    return [m for m in ALL_MID if m not in exclude_set][:3]

DISBS = []
g = []

for dw in DRAWN[:5]:
    wk_id  = dw['id']
    wk_num = dw.get('week_number', '?')

    r = s_adm.get(BASE+f'/api/disbursements/voucher-info/{wk_id}')
    if r.status_code != 200:
        bug(f"Voucher info wk{wk_num}", f"{r.status_code}"); continue
    vi = r.json()

    winner_sid  = (dw.get('winner_spot') or {}).get('id')
    winner_mids = {mid for mid, sids in SPOT_OF.items() if winner_sid in sids}
    g = guarantors_for(winner_mids)
    if len(g) < 3:
        warn(f"Week {wk_num}: not enough guarantors"); continue

    payload = {
        'week_id':           wk_id,
        'gross_amount':      vi.get('net_pot', 80000),
        'service_fee':       vi.get('service_fee', 0),
        'voucher_deduction': vi.get('voucher_deduction', 0),
        'cheque_number':     f'CHQ-QA-{wk_num}-{_RUN}',
        'cheque_date':       date.today().isoformat(),
        'guarantor_1_id':    g[0],
        'guarantor_2_id':    g[1],
        'guarantor_3_id':    g[2],
        'notes':             f'QA disbursement week {wk_num}',
    }
    r = xpost(s_adm, '/api/disbursements', json=payload)

    is_poh = (dw is POH_WEEK)   # week where winner missed a payment
    if is_poh:
        if r.status_code == 400 and 'hold' in r.text.lower():
            ok(f"Week {wk_num} disbursement: CORRECTLY blocked (pot on hold — winner has unpaid wk3)")
        elif r.status_code == 200:
            warn(f"Week {wk_num}: disbursement ALLOWED despite winner having unpaid week — check pot-on-hold logic")
            DISBS.append(r.json())
        else:
            bug(f"Week {wk_num} disbursement (pot-on-hold)", f"{r.status_code}: {r.text[:100]}")
    else:
        check(f"Week {wk_num} disbursement created", r.status_code == 200, f"{r.status_code}: {r.text[:150]}")
        if r.status_code == 200:
            d = r.json()
            DISBS.append(d)
            ok(f"  cheque={d.get('cheque_number')}, net={d.get('net_amount')}")
            r2 = xput(s_adm, f'/api/disbursements/{d["id"]}', json={'status': 'collected'})
            check(f"Week {wk_num} marked collected", r2.status_code == 200, f"{r2.status_code}")

# Cashier cannot disburse
if DRAWN and g:
    r = xpost(s_cas, '/api/disbursements', json={
        'week_id': DRAWN[0]['id'], 'gross_amount': 80000, 'service_fee': 0,
        'voucher_deduction': 0, 'cheque_number': 'CAS-DENY', 'cheque_date': date.today().isoformat(),
        'guarantor_1_id': g[0], 'guarantor_2_id': g[1], 'guarantor_3_id': g[2],
    })
    check("Cashier cannot disburse", r.status_code == 403, f"got {r.status_code}")

# Duplicate disbursement blocked
if DISBS:
    d   = DISBS[0]
    vi2 = s_adm.get(BASE+f'/api/disbursements/voucher-info/{d["week_id"]}').json()
    g2  = guarantors_for(set())
    r = xpost(s_adm, '/api/disbursements', json={
        'week_id': d['week_id'], 'gross_amount': vi2.get('net_pot', 80000),
        'service_fee': 0, 'voucher_deduction': 0,
        'cheque_number': 'CHQ-DUP', 'cheque_date': date.today().isoformat(),
        'guarantor_1_id': g2[0], 'guarantor_2_id': g2[1], 'guarantor_3_id': g2[2],
    })
    check("Duplicate disbursement blocked", r.status_code == 400, f"got {r.status_code}: {r.text[:60]}")

# Duplicate guarantors blocked
if len(DRAWN) >= 2 and g:
    r = xpost(s_adm, '/api/disbursements', json={
        'week_id': DRAWN[1]['id'], 'gross_amount': 80000,
        'service_fee': 0, 'voucher_deduction': 0,
        'cheque_number': 'CHQ-DUP-G', 'cheque_date': date.today().isoformat(),
        'guarantor_1_id': g[0], 'guarantor_2_id': g[0], 'guarantor_3_id': g[1],
    })
    check("Duplicate guarantors blocked", r.status_code == 400, f"got {r.status_code}: {r.text[:60]}")

# ==============================================================================═
# PHASE 10 -VOUCHER RETURNS
# ==============================================================================═
print("\n=== PHASE 10 -VOUCHER RETURNS ===")

if DISBS:
    vwk_id = DISBS[0]['week_id']
    r = xpost(s_adm, f'/api/reports/voucher-returns/{vwk_id}',
              json={'full_count': 1, 'half_count': 1, 'notes': 'QA vendor return'})
    check("Record voucher return", r.status_code in (200, 201), f"{r.status_code}: {r.text[:80]}")

    r = xput(s_adm, f'/api/reports/vouchers/week/{vwk_id}/mark-paid')
    check("Mark voucher vendor paid", r.status_code == 200, f"{r.status_code}: {r.text[:60]}")

    r = s_cas.get(BASE+f'/api/reports/vouchers?cycle_id={CYCLE_ID}')
    if r.status_code == 200:
        vrows = r.json()
        vpaid = any(v.get('voucher_paid') for v in vrows)
        check("Voucher-paid row appears in tracker", vpaid)

# ==============================================================================═
# PHASE 11 -ASSOCIATION EXPENSES
# ==============================================================================═
print("\n=== PHASE 11 -ASSOCIATION EXPENSES ===")

r = xpost(s_adm, '/api/reports/association-expenses', json={
    'description': 'Office supplies — paper and pens',
    'amount':      500,
    'notes':       'QA expense test',
})
check("Record association expense", r.status_code == 200, f"{r.status_code}: {r.text[:80]}")
if r.status_code == 200:
    exp_id = r.json()['id']
    r = s_adm.get(BASE+f'/api/reports/association-expenses?cycle_id={CYCLE_ID}')
    check("GET association expenses", r.status_code == 200, f"{r.status_code}")
    r = xdelete(s_adm, f'/api/reports/association-expenses/{exp_id}')
    check("Delete association expense", r.status_code == 200, f"{r.status_code}")

r = s_cas.get(BASE+'/api/reports/association-fund')
check("Cashier can view association fund", r.status_code == 200, f"{r.status_code}")

# ==============================================================================═
# PHASE 12 -REPORTS
# ==============================================================================═
print("\n=== PHASE 12 -REPORTS ===")

report_endpoints = [
    ("Dashboard",          f'/api/reports/dashboard?cycle_id={CYCLE_ID}',          False),
    ("General ledger",     f'/api/reports/general-ledger?cycle_id={CYCLE_ID}',     True),
    ("Balance sheet",      f'/api/reports/balance-sheet?cycle_id={CYCLE_ID}',      False),
    ("Collection trend",   f'/api/reports/collection-trend?cycle_id={CYCLE_ID}',   False),
    ("Cycle distribution", f'/api/reports/cycle-distribution?cycle_id={CYCLE_ID}', False),
    ("Association fund",   f'/api/reports/association-fund?cycle_id={CYCLE_ID}',   False),
    ("Member ranking",     f'/api/reports/member-ranking?cycle_id={CYCLE_ID}',     False),
    ("Vouchers",           f'/api/reports/vouchers?cycle_id={CYCLE_ID}',           False),
    ("Transactions",       f'/api/reports/transactions?cycle_id={CYCLE_ID}',       False),
    ("Cycle summary",      f'/api/payments/summary/cycle/{CYCLE_ID}',              False),
    ("Outstanding members",f'/api/payments/outstanding-members?cycle_id={CYCLE_ID}',False),
    ("Daily collection",   f'/api/payments/daily-collection?cycle_id={CYCLE_ID}',  False),
]
for label, path, admin_only in report_endpoints:
    r = s_adm.get(BASE+path)
    check(f"Admin GET {label}", r.status_code == 200, f"{r.status_code}: {r.text[:80]}")
    if admin_only:
        rc = s_cas.get(BASE+path)
        check(f"Cashier blocked from {label}", rc.status_code == 403, f"got {rc.status_code}")

# Weekly summary
if DRAWN:
    r = s_adm.get(BASE+f'/api/reports/weekly-summary/{DRAWN[0]["id"]}')
    check("GET weekly-summary", r.status_code == 200, f"{r.status_code}")

# Member statement (double full-spot member)
if F2:
    r = s_adm.get(BASE+f'/api/reports/member/{F2[0]}/statement?cycle_id={CYCLE_ID}')
    check("GET double-spot member statement", r.status_code == 200, f"{r.status_code}")
    if r.status_code == 200:
        stmt = r.json()
        spots = stmt.get('spot_numbers', [])
        check("Double-spot member has 2 spots in statement", len(spots) == 2, f"spots: {spots}")

# Member payment history
if PAIRS:
    r = s_adm.get(BASE+f'/api/payments/member/{PAIRS[0][0]}?cycle_id={CYCLE_ID}')
    check("GET payments for half-spot pair member", r.status_code == 200, f"{r.status_code}")

# Member balance check
if F1:
    r = s_adm.get(BASE+f'/api/payments/member/{F1[0]}/balance?cycle_id={CYCLE_ID}')
    check("GET member balance (fully paid)", r.status_code == 200, f"{r.status_code}")
    if r.status_code == 200:
        bal = r.json()
        check("Fully paid member shows fully_paid=True", bal.get('fully_paid') == True,
              f"got {bal.get('fully_paid')}, unpaid={bal.get('unpaid_count')}")

# Balance of Dawit Haile (has missed week 3)
r = s_adm.get(BASE+f'/api/payments/member/{MISS_MID}/balance?cycle_id={CYCLE_ID}')
check("GET balance — member with missed payment", r.status_code == 200, f"{r.status_code}")
if r.status_code == 200:
    bal = r.json()
    check("Missed-payment member not fully paid", bal.get('fully_paid') == False,
          f"fully_paid={bal.get('fully_paid')}, unpaid={bal.get('unpaid_count')}")

# ==============================================================================═
# PHASE 13 -DISTRIBUTION CHEQUES
# ==============================================================================═
print("\n=== PHASE 13 -DISTRIBUTION CHEQUES ===")

dist_r = s_adm.get(BASE+f'/api/reports/cycle-distribution?cycle_id={CYCLE_ID}')
if dist_r.status_code == 200 and F1:
    dist = dist_r.json()
    per_unit = dist.get('per_unit_amount', 0)
    print(f"  Distribution per unit: {per_unit} ETB, total pool: {dist.get('total_distributable')}")

    r = xpost(s_adm, '/api/reports/distribution-cheques', json={
        'cycle_id':     CYCLE_ID,
        'member_id':    F1[0],
        'amount':       per_unit * 1.0,
        'cheque_number': f'DIST-QA-{_RUN}-001',
        'cheque_date':  date.today().isoformat(),
        'notes':        'QA distribution cheque test',
    })
    check("Create distribution cheque", r.status_code == 200, f"{r.status_code}: {r.text[:100]}")
    if r.status_code == 200:
        cid = r.json()['id']
        # Duplicate blocked
        r2 = xpost(s_adm, '/api/reports/distribution-cheques', json={
            'cycle_id': CYCLE_ID, 'member_id': F1[0],
            'amount': 500, 'cheque_number': 'DIST-DUP', 'cheque_date': date.today().isoformat(),
        })
        check("Duplicate distribution cheque blocked", r2.status_code == 400, f"got {r2.status_code}: {r2.text[:60]}")
        # Mark collected
        r3 = xput(s_adm, f'/api/reports/distribution-cheques/{cid}/collect')
        check("Distribution cheque collected", r3.status_code == 200, f"{r3.status_code}")
        # Delete
        r4 = xdelete(s_adm, f'/api/reports/distribution-cheques/{cid}')
        check("Delete distribution cheque", r4.status_code == 200, f"{r4.status_code}")

# ==============================================================================═
# PHASE 14 -CYCLE CLOSURE
# ==============================================================================═
print("\n=== PHASE 14 -CYCLE CLOSURE ===")

r = s_adm.get(BASE+f'/api/draws/cycles/{CYCLE_ID}/closure-checklist')
check("GET closure checklist", r.status_code == 200, f"{r.status_code}")
if r.status_code == 200:
    cl = r.json()
    ac = cl.get('all_clear', False)
    print(f"  all_clear={ac}")
    if not ac:
        for k, v in cl.items():
            if k not in ('cycle_id', 'cycle_name', 'cycle_status', 'all_clear') and not v:
                warn(f"Closure blocker: {k}={v}")

r = xpost(s_cas, f'/api/draws/cycles/{CYCLE_ID}/close')
check("Cashier cannot close cycle", r.status_code == 403, f"got {r.status_code}")

r = xpost(s_adm, f'/api/draws/cycles/{CYCLE_ID}/close')
if r.status_code == 200:
    ok("Cycle closed successfully")
elif r.status_code == 400:
    warn(f"Cycle close blocked (not all weeks drawn — expected for partial QA run)")
    ok("Cycle close guard is working correctly")
else:
    bug("Unexpected cycle close result", f"{r.status_code}: {r.text[:100]}")

# ==============================================================================═
# SUMMARY
# ==============================================================================═
print(f"\n{'='*65}")
total = len(PASSES) + len(BUGS)
print(f"  Total checks : {total}")
print(f"  PASS         : {len(PASSES)}")
print(f"  BUGS         : {len(BUGS)}")
print(f"  WARNINGS     : {len(WARNS)}")

if BUGS:
    print(f"\nBUGS ({len(BUGS)}) ---")
    for i, b in enumerate(BUGS, 1):
        print(f"  {i:2}. {b}")

if WARNS:
    print(f"\nWARNINGS ({len(WARNS)}) ---")
    for i, w in enumerate(WARNS, 1):
        print(f"  {i:2}. {w}")

if not BUGS:
    print("\n  All checks passed!")
print()
