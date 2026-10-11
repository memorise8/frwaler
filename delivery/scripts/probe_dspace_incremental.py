"""Small gated public probes of the delivered Nemertes scope/order contract."""
import argparse
import json
from pathlib import Path
from crawler.sites import CRAWLERS
from delivery.health.incremental_transport import install
from delivery.health.results import atomic_json,utc_now


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--gate-dir',required=True);args=p.parse_args()
    out=Path(args.out);out.mkdir(parents=True,exist_ok=False)
    c=CRAWLERS['nemertes-library-upatras-gr-browse'](None,delay=0)
    report={'started_at':utc_now(),'requests':[],'observations':[],'global_complete':False}
    def save():atomic_json(out/'audit.json',report)
    install(c,args.gate_dir,out,report['requests'],persist=save)
    scope='f8d85de1-7de0-4348-bca3-10784f86ae71'
    base=c.base_url+'/server/api'
    for label,url,params in [
      ('title_native',base+'/discover/browses/title/items',{'scope':scope,'size':5,'page':0}),
      ('accessioned_desc',base+'/discover/search/objects',{'scope':scope,'size':5,'page':0,'sort':'dc.date.accessioned,DESC'}),
      ('sort_options',base+'/discover/search/sortOptions',{'scope':scope}),
    ]:
        try:
            r=c._session.get(url,params=params,timeout=25);d=r.json()
            row={'label':label,'status':r.status_code,'root_keys':list(d),'page':d.get('page')}
            if label=='title_native':items=d.get('_embedded',{}).get('items',[])
            elif label=='accessioned_desc':
                result=d.get('_embedded',{}).get('searchResult',{});row['page']=result.get('page');items=[i.get('_embedded',{}).get('indexableObject',{}) for i in result.get('_embedded',{}).get('objects',[])]
            else:items=[];row['data']=d
            row['items']=[{'id':i.get('uuid'),'accessioned':i.get('metadata',{}).get('dc.date.accessioned'),
                           'issued':i.get('metadata',{}).get('dc.date.issued')} for i in items]
        except Exception as exc:row={'label':label,'error':type(exc).__name__}
        report['observations'].append(row);save();print(json.dumps(row,ensure_ascii=False)[:2400],flush=True)
    report['finished_at']=utc_now();save()

if __name__=='__main__':main()
