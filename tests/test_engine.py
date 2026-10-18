from backend.engine import extract_local,compare
SUP='Pack 12 cartons with 24 bottles in each carton. Use blue labels, inspect every carton, and keep the shipment ready by 4 PM.'
WORK='24 कार्टन में 12-12 बोतलें रखनी हैं, नीला लेबल लगाना है और शाम 4 बजे तक तैयार करना है।'
def test_supervisor():
 d=extract_local(SUP)
 assert d['cartons']==12 and d['units_per_carton']==24 and d['total_units']==288
 assert d['inspection']=='all_cartons' and d['label_color']=='blue' and d['deadline']=='16:00'
def test_worker_hindi():
 d=extract_local(WORK)
 assert d['cartons']==24 and d['units_per_carton']==12 and d['total_units']==288 and d['label_color']=='blue'
def test_mismatch():
 r=compare(extract_local(SUP),extract_local(WORK))
 issues={x['field']:x['status'] for x in r['requirements']}
 assert issues['cartons']=='contradiction' and issues['units_per_carton']=='contradiction'
 assert issues['total_units']=='matched' and issues['inspection']=='unconfirmed'
 assert r['decision']=='clarification_required'
def test_corrected():
 worker='12 cartons with 24 bottles each, blue labels, inspect every carton, ready by 4 PM'
 r=compare(extract_local(SUP),extract_local(worker))
 assert r['decision']=='ready_for_confirmation',r
