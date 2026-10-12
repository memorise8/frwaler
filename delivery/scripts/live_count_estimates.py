"""Estimate collection sizes from existing list metadata, never detail pages."""
import re
import copy
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from delivery.scripts.live_check_contracts import family

SOURCE='소스 보고 총건수'
ARITHMETIC='마지막 페이지 산술 페이지수x페이지당+마지막 페이지 건수'
PARTITIONS='구간별 합계'
SITEMAP='사이트맵 URL 수'
UNKNOWN='알 수 없음'


def unknown(reason):
    return dict(expected_items=None,count_basis=UNKNOWN,count_confidence='낮음',
                hidden_cap_suspected=False,count_evidence=dict(reason=reason))


def scope_identity(url):
    p=urlsplit(family(url))
    ignored={'per_page','orderby','order','_fields'} if '/wp-json/' in p.path else set()
    return urlunsplit(p._replace(query=urlencode([(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if k not in ignored])))


def scope_count(scope):
    estimate=unknown('no_verified_total_or_complete_boundary_arithmetic')
    checks=scope.get('checks',{});arithmetic=checks.get('total_arithmetic',{});end=checks.get('last_and_after',{})
    raw=arithmetic.get('totals',[])
    reported=arithmetic.get('reported_candidates',[])+[v['value'] for v in raw if type(v.get('value')) is int]
    reported=[n for n in reported if type(n) is int and n>=0]
    evidence=dict(scope=scope['url'],total_arithmetic=arithmetic,last_and_after=end)
    if arithmetic.get('status')=='verified' and type(arithmetic.get('total')) is int:
        estimate.update(expected_items=arithmetic['total'],count_basis=SOURCE,count_confidence='높음')
        reported.append(arithmetic['total'])
    elif end.get('root')=='urlset' and len(set(reported))==1:
        estimate.update(expected_items=reported[0],count_basis=SITEMAP,count_confidence='낮음')
        evidence['limitation']='XML URL entry count; document type and original collection membership are not proven.'
    elif end.get('root')=='sitemapindex':
        evidence['reason']='Sitemap index counts child maps, not items; no complete disjoint child union has been measured.'
    elif len(set(reported))==1 and arithmetic.get('status')!='contradicted':
        estimate.update(expected_items=reported[0],count_basis=SOURCE,count_confidence='보통' if arithmetic.get('status')=='matches_reported_candidate' else '낮음')
        evidence['limitation']='Visible reported total; exact record membership may require source-specific reconciliation.'
    elif end.get('status')=='verified' and arithmetic.get('status')!='contradicted' and type(arithmetic.get('implied_total')) is int:
        estimate.update(expected_items=arithmetic['implied_total'],count_basis=ARITHMETIC,count_confidence='보통')
        evidence['formula']='(page_count - 1) * page_size + last_count'
        evidence['limitation']='First and last page sizes measured; intermediate pages not exhaustively enumerated.'
    hidden=checks.get('hidden_cap',{});reasons=[]
    if any(n>0 and n%100==0 for n in reported):reasons.append('reported_total_is_a_positive_multiple_of_100')
    if hidden.get('status','').startswith('observed_'):reasons.append('page_repeat_wraparound_or_clamp_observed_or_suspected')
    text=' '.join(str(v.get('source','')) for v in raw)
    if re.search(r'(?:recent|latest|last|top|최근|최신)\s*\d+',text,re.I):reasons.append('reported_label_may_describe_recent_N_only')
    if reasons:
        estimate['hidden_cap_suspected']=True
        if estimate['count_confidence']=='높음':estimate['count_confidence']='보통'
    evidence['hidden_cap_reasons']=reasons;estimate['count_evidence']=evidence
    return estimate


def estimate_counts(result):
    scopes=[dict(url=s['url'],**scope_count(s)) for s in result.get('scopes',[])]
    estimate=unknown('multiple_scopes_not_proven_disjoint_or_no_total_observed')
    selected=result.get('strategy_review',{}).get('selected_scope')
    if selected:
        matches=[s for s in scopes if s['url']==selected]
        if len(matches)==1:estimate={k:v for k,v in matches[0].items() if k!='url'}
    elif scopes and len({scope_identity(s['url']) for s in scopes})==1:
        values={s['expected_items'] for s in scopes}
        if len(values)==1 and None not in values:
            # Prefer the weakest confidence among repeated representations.
            ordered=sorted(scopes,key=lambda s:('낮음','보통','높음').index(s['count_confidence']))
            estimate={k:v for k,v in ordered[0].items() if k!='url'}
    elif result.get('verified_disjoint_partition_counts'):
        partition=result['verified_disjoint_partition_counts']
        if partition.get('complete') is True and partition.get('disjoint') is True and partition.get('evidence'):
            counts=partition.get('counts',[])
            if counts and all(type(n) is int and n>=0 for n in counts):
                estimate.update(expected_items=sum(counts),count_basis=PARTITIONS,count_confidence='보통',count_evidence=partition)
    instruction=result.get('live_check','')
    instruction_suspicion=bool(re.search(r'(?:최근|최신)\s*(?:\d+|N)\s*건|(?:recent|latest|last)\s+(?:\d+|N)\s+(?:items|records|results)',instruction,re.I))
    estimate['hidden_cap_suspected']=estimate['hidden_cap_suspected'] or any(s['hidden_cap_suspected'] for s in scopes) or instruction_suspicion
    if estimate['hidden_cap_suspected'] and estimate['count_confidence']=='높음':estimate['count_confidence']='보통'
    estimate=copy.deepcopy(estimate)
    estimate['count_evidence']['scope_estimates']=scopes
    # A common probe can stop before completing a scope's boundary checks.
    # Preserve totals already observed on successful listing responses too;
    # never turn a sitemap index's number of children into a document total.
    observations=[]
    for response in result.get('requests',[]):
        if response.get('status')!=200 or response.get('error') or response.get('purpose','').startswith('robots_'):continue
        summary=response.get('summary',{})
        if summary.get('xml_root')=='sitemapindex':continue
        totals=list(summary.get('totals',[]))
        header=response.get('source_headers',{}).get('X-WP-Total')
        if header is not None and str(header).isdigit():totals.append(dict(value=int(header),source='X-WP-Total'))
        values=sorted({t['value'] for t in totals if type(t.get('value')) is int and t['value']>=0})
        if not values:continue
        observations.append(dict(url=response['url'],response=response['number'],values=values,
            count_basis=SITEMAP if summary.get('xml_root')=='urlset' else SOURCE,
            sources=totals,scope_equivalence_verified=False))
    estimate['count_evidence']['observed_reported_counts']=observations
    if any(o['count_basis']==SOURCE and any(v>0 and v%100==0 for v in o['values']) for o in observations):
        estimate['hidden_cap_suspected']=True
        estimate['count_evidence']['round_total_in_observed_listing_response']=True
        if estimate['count_confidence']=='높음':estimate['count_confidence']='보통'
    # Only a single, unchanged collection identity can supply a fallback total.
    # Date-filtered subsets and unfiltered alternatives have different identities.
    identities={scope_identity(s['url']) for s in scopes}
    if estimate['expected_items'] is None and len(identities)==1 and all(s.get('checks',{}).get('total_arithmetic',{}).get('status')!='contradicted' for s in result.get('scopes',[])):
        candidates=[o for o in observations if scope_identity(o['url']) in identities]
        values={v for o in candidates for v in o['values']};bases={o['count_basis'] for o in candidates}
        if len(values)==1 and len(bases)==1:
            estimate.update(expected_items=next(iter(values)),count_basis=next(iter(bases)),count_confidence='낮음')
            estimate['count_evidence']['fallback_reason']='Consistent reported total for the sole observed collection identity; boundary arithmetic remains unverified.'
    if result.get('URL_closure',{}).get('scoped_sitemap_observations'):
        estimate['count_evidence']['scoped_sitemap_observations']=result['URL_closure']['scoped_sitemap_observations']
    estimate['count_evidence']['instruction_mentions_recent_N']=instruction_suspicion
    return estimate
