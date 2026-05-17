import requests, json
from datetime import date, timedelta
BASE = 'http://localhost:8000'
XHR = {'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/json'}

s = requests.Session()
s.post(BASE+'/login', data={'username':'admin','password':'admin123'}, allow_redirects=False)
start = (date.today() - timedelta(weeks=5)).isoformat()

# Clean up first
cycles = s.get(BASE+'/api/draws/cycles').json()
for c in cycles:
    s.delete(BASE+f'/api/draws/cycles/{c["id"]}', headers=XHR)
s.delete(BASE+'/api/members/permanent/all', headers=XHR)
print('Cleaned up')

# Fresh cycle
c = s.post(BASE+'/api/draws/cycles', headers=XHR, json={
    'name': 'DBG3', 'start_date': start, 'total_member_spots': 2,
    'full_spot_amount': 21000, 'half_spot_amount': 10500
}).json()
cid = c['id']
print('Cycle:', cid)

# 4 members
mids = []
for i in range(1, 5):
    r = s.post(BASE+'/api/members', headers=XHR, json={
        'name': f'D3M{i}', 'phone': f'09555{i:05d}', 'share': 'full'
    })
    mids.append(r.json()['id'])
print('Members:', mids)

# Assign first 2 to spots
spots = s.get(BASE+f'/api/draws/active-spots?cycle_id={cid}').json()
for i, sp in enumerate(spots[:2]):
    r = s.post(BASE+f'/api/members/{mids[i]}/spots', headers=XHR, json={'spot_id': sp['id']})
    print(f'  Spot assign {mids[i]}: {r.status_code}')

# Pay all weeks
weeks = s.get(BASE+f'/api/draws/cycles/{cid}/weeks').json()
for wk in weeks:
    s.post(BASE+'/api/payments/bulk', headers=XHR, json={
        'week_id': wk['id'], 'member_ids': mids, 'status': 'paid',
        'paid_date': date.today().isoformat()
    })
print('Payments done')

# Start draws
r = s.post(BASE+f'/api/draws/cycles/{cid}/start-draws', headers=XHR, json={'at_week_number':1,'assoc_spots':0})
print('Start draws:', r.status_code)

# Draw week 1
weeks = s.get(BASE+f'/api/draws/cycles/{cid}/weeks').json()
act_spots = s.get(BASE+f'/api/draws/active-spots?cycle_id={cid}').json()
pend = [w for w in weeks if w['status'] == 'pending']
print(f'Pending weeks: {len(pend)}, Active spots: {len(act_spots)}')

dr = s.post(BASE+f'/api/draws/weeks/{pend[0]["id"]}/draw', headers=XHR, json={'winner_spot_id': act_spots[0]['id']})
dw = dr.json()
print('Draw status:', dr.status_code)
print('Draw response keys:', list(dw.keys()))
print('winner_spot_id:', dw.get('winner_spot_id'))
ws = dw.get('winner_spot') or {}
print('winner_spot id:', ws.get('id'), 'number:', ws.get('number'))
winner_members = [sa.get('member_id') for sa in ws.get('spot_assignments', [])]
print('Winner member IDs:', winner_members)

# Pick guarantors (not the winner)
guarantors = [m for m in mids if m not in winner_members]
print('Guarantors:', guarantors[:3])

if len(guarantors) < 3:
    print('ERROR: Not enough non-winner guarantors')
else:
    week_id = pend[0]['id']
    vi = s.get(BASE+f'/api/disbursements/voucher-info/{week_id}').json()
    print('Voucher: net_pot=', vi.get('net_pot'), 'service_fee=', vi.get('service_fee'))
    disb_r = s.post(BASE+'/api/disbursements', headers=XHR, json={
        'week_id': week_id,
        'gross_amount': vi.get('net_pot', 21000),
        'service_fee': vi.get('service_fee', 0),
        'voucher_deduction': vi.get('voucher_deduction', 0),
        'cheque_number': 'CHQ-DBG3-001',
        'cheque_date': date.today().isoformat(),
        'guarantor_1_id': guarantors[0],
        'guarantor_2_id': guarantors[1],
        'guarantor_3_id': guarantors[2],
    })
    print('Disbursement status:', disb_r.status_code)
    print('Response:', disb_r.text[:800])
