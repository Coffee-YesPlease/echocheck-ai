"""Deterministic local demonstration engine. Not an LLM or production-grade language parser."""
import re
from typing import Dict, Any

NUM_WORDS = {'zero':0,'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,'nine':9,'ten':10,'twelve':12,'twenty':20,'twenty-four':24,'fifty':50,'hundred':100,'दस':10,'बारह':12,'चौबीस':24,'पचास':50,'बारा':12,'चोवीस':24}
COLORS={'blue':['blue','नीला','नीले','निळा','निळे'],'green':['green','हरा','हरे','हिरवा'],'red':['red','लाल'],'yellow':['yellow','पीला','पिवळा']}
CARTON=r'(?:cartons?|boxes|डिब्बों|डिब्बे|कार्टन|बॉक्स|खोके)'
UNITS=r'(?:bottles?|units?|pieces?|पीस|बोतलें|बोतल|नग)'

def clean(t):
    return re.sub(r'\s+',' ',t.strip().lower().replace('–','-'))

def _numbers(s):
    t=clean(s)
    for word,num in sorted(NUM_WORDS.items(),key=lambda x:-len(x[0])):
        t=re.sub(r'(?<!\w)'+re.escape(word)+r'(?!\w)',str(num),t)
    return t

def extract_local(text: str) -> Dict[str,Any]:
    """Small bilingual pattern parser for illustrative packing scenarios; unsupported fields stay unknown."""
    raw=clean(text); t=_numbers(text)
    fields={}
    # explicit grouping: 12 cartons with 24 bottles each / 24 cartons mein 12-12 bottles
    patterns=[
       rf'(\d+)\s*{CARTON}\s*(?:with|containing|of|में|me|mein|के अंदर)?\s*(\d+)(?:\s*-\s*\2)?\s*{UNITS}',
       rf'(\d+)\s*{CARTON}\s*(?:में|me|mein)?\s*(\d+)\s*-\s*\2\s*{UNITS}',
       rf'(\d+)\s*{CARTON}\s*(?:each|every|per|प्रत्येक|हर)?\s*(\d+)\s*{UNITS}',
    ]
    for pattern in patterns:
        m=re.search(pattern,t,re.IGNORECASE)
        if m:
            fields['cartons']=int(m.group(1)); fields['units_per_carton']=int(m.group(2));break
    if 'cartons' not in fields:
        m=re.search(rf'(\d+)\s*{CARTON}',t)
        if m: fields['cartons']=int(m.group(1))
    if 'units_per_carton' not in fields:
        m=re.search(rf'(\d+)\s*{UNITS}\s*(?:per|in each|each|प्रति|हर)\s*{CARTON}',t)
        if m:fields['units_per_carton']=int(m.group(1))
    for col,aliases in COLORS.items():
        if any(re.search(re.escape(w),t) for w in aliases): fields['label_color']=col;break
    if any(x in t for x in ['inspect','inspection','check every','check each','जांच','जाँच','तपास','निरीक्षण']): fields['inspection']='all_cartons'
    hour=re.search(r'\b(?:by|before|at|तक|बजे तक|बजे)\s*(\d{1,2})\s*(?::(\d{2}))?\s*(am|pm)?',t)
    if not hour: hour=re.search(r'\b(\d{1,2})\s*(?::(\d{2}))?\s*(pm|am|बजे)',t)
    if hour:
        hh=int(hour.group(1));mm=int(hour.group(2) or 0); suffix=(hour.group(3) or '').lower()
        if suffix=='pm' and hh<12:hh+=12
        if suffix=='am' and hh==12:hh=0
        # Hindi "शाम 4" means pm
        if ('शाम' in t or 'evening' in t) and hh<12:hh+=12
        fields['deadline']=f'{hh:02d}:{mm:02d}'
    if 'cartons' in fields and 'units_per_carton' in fields: fields['total_units']=fields['cartons']*fields['units_per_carton']
    return fields

LABELS={'cartons':'Number of cartons','units_per_carton':'Units per carton','total_units':'Total units','label_color':'Label color','inspection':'Inspection step','deadline':'Deadline'}
CRITICAL={'cartons','units_per_carton','label_color','inspection','deadline'}
def compare(a:Dict[str,Any],b:Dict[str,Any]):
    rows=[]
    for key in ['cartons','units_per_carton','total_units','label_color','inspection','deadline']:
        if key not in a and key not in b:continue
        av=a.get(key);bv=b.get(key)
        if av is None: status='extra'
        elif bv is None: status='unconfirmed'
        elif av==bv: status='matched'
        else:status='contradiction'
        rows.append({'field':key,'label':LABELS[key],'expected':av,'understood':bv,'status':status,'critical':key in CRITICAL})
    problems=[r for r in rows if r['status'] in ['contradiction','unconfirmed']]
    questions=[]
    for r in problems:
        if r['status']=='contradiction':questions.append(f"Please confirm {r['label'].lower()}: the instruction specifies {r['expected']}, but the explanation says {r['understood']}.")
        else:questions.append(f"Please explain the required {r['label'].lower()} ({r['expected']}) before starting.")
    if not a: questions.insert(0,'The original instruction lacks identifiable structured details. Supervisor must review it.')
    if not b: questions.insert(0,'The response does not contain enough recognized details. Please describe the task again.')
    consequences=[]
    if any(r['field'] in ['cartons','units_per_carton'] and r['status']=='contradiction' for r in problems):
        consequences.append('Packaging configuration differs; the total product count can match while shipping-carton requirements remain wrong.')
    if any(r['field']=='inspection' and r['status']=='unconfirmed' for r in problems):
        consequences.append('Required inspection has not been confirmed; ask before dispatch.')
    if any(r['field']=='label_color' and r['status']=='contradiction' for r in problems):
        consequences.append('Incorrect labeling could disrupt identification or dispatch.')
    if any(r['field']=='deadline' and r['status']=='contradiction' for r in problems):
        consequences.append('Different deadlines may create scheduling and delivery delays.')
    # conservative: if local parser cannot extract all, do not declare clearance
    if not rows or not a or not b: decision='review_required'
    elif problems:decision='clarification_required'
    else:decision='ready_for_confirmation'
    return {'requirements':rows,'questions':questions,'consequences':consequences,'decision':decision,'summary':f"{sum(x['status']=='contradiction' for x in rows)} contradictions, {sum(x['status']=='unconfirmed' for x in rows)} unconfirmed requirements",'engine':'local_rule_demo'}
