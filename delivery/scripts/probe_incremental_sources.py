"""Read-only bounded source contracts; writes only an explicitly named output dir."""
import argparse
import hashlib
import json
import time
from pathlib import Path
import requests


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--out',required=True);args=ap.parse_args()
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    queries=[
      ('doaj', 'https://doaj.org/api/search/articles/*', {'pageSize':3,'page':1,'sort':'created_date:desc'}),
      ('hal-global','https://api.archives-ouvertes.fr/search/',{'q':'*','rows':3,'wt':'json','sort':'docid desc','fl':'docid,halId_s,submittedDate_tdate'}),
      ('ntrs','https://ntrs.nasa.gov/api/citations/search',{'page[size]':3,'page[from]':0,'sort[field]':'created','sort[order]':'desc'}),
      ('cern','https://cds.cern.ch/collection/Published%20Articles',{'ln':'en'})]
    records=[]
    for name,url,params in queries:
        try:
            response=requests.get(url,params=params,timeout=30,allow_redirects=False)
            body=response.content;(out/(name+'.body')).write_bytes(body)
            record={'name':name,'url':response.url,'status':response.status_code,'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()}
            try:
                data=response.json()
                if name=='doaj':record.update(total=data.get('total'),ids=[r.get('id') for r in data.get('results',[])],dates=[r.get('created_date') for r in data.get('results',[])])
                if name=='hal-global':record.update(total=data.get('response',{}).get('numFound'),ids=[r.get('docid') for r in data.get('response',{}).get('docs',[])])
                if name=='ntrs':record.update(total=data.get('stats',{}).get('total'),ids=[r.get('id') for r in data.get('results',[])],dates=[r.get('created') for r in data.get('results',[])])
            except ValueError:record['challenge']='anubis' if b'anubis' in body.lower() else 'unclassified_html'
        except requests.RequestException as exc:record={'name':name,'error':type(exc).__name__}
        records.append(record);(out/'probe.json').write_text(json.dumps(records,indent=2));print(json.dumps(record),flush=True);time.sleep(1.5)

if __name__=='__main__':main()
