"""Common live metadata probes: page boundaries, WP, filters and sitemaps."""
from datetime import datetime, timedelta
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from delivery.health.results import utc_now
from delivery.scripts.discovery_structure import same_listing, query_replace
from delivery.scripts.live_check_contracts import CHECKS, at_page, comparison, is_empty_boundary, page_route, parse_listing
from delivery.scripts.live_check_transport import AccessStopped, BudgetReached
from delivery.scripts.probe_wordpress_structure import FIELDS, parse_metadata


def unknown(reason): return dict(status='unconfirmed',reason=reason)


class ProbeSession:
    def __init__(self, plan, transport, result, persist):
        self.plan,self.transport,self.result,self.persist=plan,transport,result,persist
        self.cache={}
        def record(response):
            response['number']=len(result['requests'])+1
            result['requests'].append(response);persist()
        def reserve():
            if len(result['requests'])>=result['request_budget']: raise BudgetReached('site_request_budget_exhausted')
        record.reserve=reserve;self.record=record

    def read(self, url, purpose, *, repeat=False, error_json=False):
        if not repeat and url in self.cache: return self.cache[url]
        policy=self.transport.allowed(url,self.record)
        response,raw=self.transport.fetch_checked(url,purpose,self.record,error_json)
        response['robots_policy']=policy
        if raw is not None:
            try: response['summary']=parse_listing(raw,url,response.get('content_type',''),self.plan)
            except Exception as exc:response['parse_error']=type(exc).__name__
            if response.get('summary',{}).get('login_required'):
                self.transport.hold(urlsplit(url).hostname,dict(url=url,reason='login_form_required',at=utc_now()))
                response['error']='login_form_required';raw=None
        self.persist();self.cache[url]=(response,raw)
        if response.get('error') and any(t in response['error'] for t in ('access_refus','access_hold','authentication','challenge','verification','login','rate_limited','request_gate_cooldown')):
            raise AccessStopped(response['error'])
        return response,raw

    def entry(self,url):
        redirects=[];current=url
        for _ in range(4):
            response,raw=self.read(current,'requested_entry_or_redirect')
            target=response.get('redirect')
            if not target: return response,raw,redirects
            if target in [url]+[x['to'] for x in redirects]: return response,None,redirects
            if re.search(r'login|signin|auth|captcha|challenge',urlsplit(target).path,re.I):
                raise AccessStopped('authentication_or_challenge_redirect')
            redirects.append(dict(status=response['status'],source=response['number'],from_url=current,to=target,
                                  old_host=urlsplit(current).hostname,new_host=urlsplit(target).hostname))
            current=target
        return response,None,redirects


def initial_scope(url):
    return dict(url=url,checks={name:unknown('not_observed') for name in CHECKS},status='unconfirmable_custom',
                strategy='불가',confidence='낮음',limitations=[],comparisons=[])


def native_wp(session,url,scope):
    parsed=urlsplit(url);base_pairs=[(k,v) for k,v in parse_qsl(parsed.query,keep_blank_values=True)
        if k not in ('per_page','page','orderby','order','_fields')]
    def fetch(page=1,order='desc',before=None,purpose='WP_metadata'):
        pairs=base_pairs+[('per_page','2'),('page',str(page)),('orderby','date'),('order',order),('_fields',FIELDS)]
        if before:pairs=[(k,v) for k,v in pairs if k!='before']+[('before',before)]
        target=urlunsplit(parsed._replace(query=urlencode(pairs)))
        response,raw=session.read(target,purpose,repeat=purpose=='WP_repeat',error_json=True)
        if raw is None: raise ValueError('WP metadata unavailable')
        if response.get('status')==400:
            data=json.loads(raw);response['error_code']=data.get('code');session.persist();return response,None
        summary,total,pages,items=parse_metadata(raw,response,url,2)
        response['summary']=summary;session.persist()
        return response,dict(total=total,pages=pages,items=items)
    zero,z=fetch(0,purpose='WP_zero_index')
    first,head=fetch(purpose='WP_first_index_one')
    if not head or head['total']==0: raise ValueError('empty_or_unavailable_WP_collection')
    asc,old=fetch(order='asc',purpose='WP_oldest_sort')
    last,tail=fetch(head['pages'],purpose='WP_last_reported_page')
    after,end=fetch(head['pages']+1,purpose='WP_after_reported_end')
    again,repeat=fetch(order='asc',purpose='WP_repeat')
    if not all((head,old,tail,repeat)) or len({x['total'] for x in (head,old,tail,repeat)})!=1:
        raise ValueError('WP_total_or_schema_changed')
    arithmetic=head['total']==(head['pages']-1)*2+len(tail['items'])
    dates=[i['source_datetime'] for i in old['items']]
    oldest=old['items'][0]
    cross=oldest['source_datetime']==min(i['source_datetime'] for i in tail['items'])
    stable=old['items']==repeat['items']
    before_date=(datetime.fromisoformat(oldest['source_datetime'])-timedelta(seconds=1)).isoformat()
    before,empty=fetch(before=before_date,purpose='WP_before_oldest_filter')
    before_empty=bool(empty is not None and empty['total']==0 and not empty['items'])
    explicit_end=after.get('status')==400 and after.get('error_code')=='rest_post_invalid_page_number'
    check=scope['checks']
    check['first_page_number']=dict(status='observed',first=1,zero_status=zero.get('status'),zero_error=zero.get('error_code'),response=first['number'])
    check['page_parameter']=dict(status='observed',first_vs_last=comparison(first,last),single_page=head['pages']==1)
    check['last_and_after']=dict(status='verified' if explicit_end and arithmetic else 'unconfirmed',last=head['pages'],last_response=last['number'],after_response=after['number'],after_error=after.get('error_code'))
    check['oldest_sort_or_date_filter']=dict(status='verified' if dates==sorted(dates) and cross and stable and before_empty else 'unconfirmed',ascending_response=asc['number'],before_response=before['number'],repeat_stable=stable)
    check['oldest_item']=dict(status='cross_confirmed' if cross and before_empty else 'sample_only',item=oldest,unique_oldest_across_ties_proven=False)
    check['total_arithmetic']=dict(status='verified' if arithmetic else 'contradicted',total=head['total'],pages=head['pages'],page_size=2,last_count=len(tail['items']),formula='(pages - 1) * page_size + last_count')
    check['hidden_cap']=dict(status='no_cap_observed_through_reported_end',absence_of_all_hidden_caps_proven=False)
    zero_rejected=zero.get('status')==400 and zero.get('error_code') in ('rest_invalid_param','rest_post_invalid_page_number')
    if explicit_end and arithmetic and dates==sorted(dates) and cross and stable and before_empty and zero_rejected:
        scope.update(status='confirmed',strategy='오래된 순 정렬',confidence='높음')
    scope['limitations'].append('WordPress CMS date is not research publication date; entity/category scope preserved from requested endpoint')


def sitemap_probe(session,response,scope):
    summary=response.get('summary',{});root=summary.get('xml_root')
    if root not in ('urlset','sitemapindex'):return
    check=scope['checks'];members=[];responses=[response['number']]
    if summary.get('parse_limit_reached'):scope['limitations'].append('sitemap_entry_parse_cap_10000')
    if root=='sitemapindex':
        for url in summary.get('sitemaps',[])[:3]:
            child,_=session.read(url,'advertised_child_sitemap');responses.append(child['number'])
            members.append(dict(url=url,root=child.get('summary',{}).get('xml_root'),totals=child.get('summary',{}).get('totals',[])))
        scope['limitations'].append('sitemap_index_children_bounded_not_complete_union')
    check['last_and_after']=dict(status='not_applicable_to_XML_sitemap',root=root,responses=responses)
    check['page_parameter']=dict(status='not_applicable_to_XML_sitemap')
    check['first_page_number']=dict(status='not_applicable_to_XML_sitemap')
    check['total_arithmetic']=dict(status='observed_XML_entry_count_not_document_total',totals=summary.get('totals'),children=members)
    check['oldest_item']=dict(status='unconfirmed',item=summary.get('oldest_observed'),reason='sitemap_lastmod_is_not_publication_date')
    scope.update(strategy='사이트맵',confidence='낮음')
    scope['limitations'].append('document_type_and_original_collection_membership_require_custom_reconciliation')


def page_probe(session,first,scope,route,explicit_urls):
    checks=scope['checks'];first_summary=first.get('summary',{})
    if route['offset']:
        scope['limitations'].append('offset_step_and_total_contract_require_API_specific_handler');return
    pages={}
    def read(number):
        if number not in pages:
            response,_=session.read(at_page(route,number),'page_'+str(number));pages[number]=response
        return pages[number]
    zero,one,two=read(0),read(1),read(2)
    c01=comparison(zero,one);c12=comparison(one,two)
    scope['comparisons'] += [c01,c12]
    if zero.get('summary',{}).get('item_count') and c01['item_IDs_different']:
        start=0;head=zero
    elif one.get('summary',{}).get('item_count') and (c01['item_IDs_equal'] or zero.get('status') in (400,404)
          or zero.get('redirect')==at_page(route,1)):
        start=1;head=one
    else:
        checks['first_page_number']=unknown('zero_one_alias_or_custom_listing_not_resolved')
        scope['limitations'].append('first_index_not_resolved');return
    checks['first_page_number']=dict(status='observed',first=start,comparison=c01,zero_response=zero['number'],one_response=one['number'])
    functioning=c01['item_IDs_different'] or c12['item_IDs_different']
    checks['page_parameter']=dict(status='verified_by_distinct_IDs' if functioning else 'unconfirmed',parameter=route,comparisons=[c01,c12])
    if not functioning:
        scope['limitations'].append('parameter_may_be_ignored_or_collection_has_only_one_page');return
    # Concrete requested pages take precedence over an exponential search.
    for url in explicit_urls:
        q=dict(parse_qsl(urlsplit(url).query));v=q.get(route.get('key'))
        if v and v.isdecimal():read(int(v))
    lastlinks=first_summary.get('last_links',[])
    for url in lastlinks[:1]:
        q=dict(parse_qsl(urlsplit(url).query));v=q.get(route.get('key'))
        if v and v.isdecimal() and same_listing(first['url'],url):read(int(v))
    def valid(r): return bool(r.get('status')==200 and r.get('summary',{}).get('item_count'))
    empty=[n for n,r in pages.items() if is_empty_boundary(head,r)]
    low=max((n for n,r in pages.items() if valid(r)),default=start)
    high=min(empty) if empty else None
    if high is not None:low=max(n for n,r in pages.items() if n<high and valid(r))
    probe=max(low+1,2)
    for _ in range(10):
        if high is not None:break
        candidate=read(probe);cmp=comparison(head,candidate);scope['comparisons'].append(cmp)
        if is_empty_boundary(head,candidate):high=probe;break
        if not valid(candidate):
            checks['last_and_after']=dict(status='unconfirmed',reason='non_listing_or_error_not_accepted_as_empty_end',response=candidate['number']);return
        if cmp['item_IDs_equal']:
            checks['hidden_cap']=dict(status='observed_first_page_repeat',at_page=probe,response=candidate['number'],comparison=cmp)
            scope['limitations'].append('wraparound_or_ignored_parameter_requires_alternative_route');return
        if probe>low and any(comparison(r,candidate)['item_IDs_equal'] for n,r in pages.items() if n<probe and n>start):
            checks['hidden_cap']=dict(status='observed_repeated_page_or_clamp',at_page=probe,response=candidate['number']);return
        low=probe;probe=max(probe*2,probe+1)
    if high is None:
        checks['last_and_after']=unknown('no_end_within_bounded_exponential_search');return
    while high-low>1:
        mid=(low+high)//2;candidate=read(mid)
        if is_empty_boundary(head,candidate):high=mid
        elif valid(candidate):low=mid
        else:
            checks['last_and_after']=unknown('non_listing_response_inside_boundary_search');return
    last,after=read(low),read(low+1)
    repeat,_=session.read(at_page(route,start),'repeat_first_anchor',repeat=True)
    stable=comparison(head,repeat)['item_IDs_equal']
    oldest=last.get('summary',{}).get('oldest_observed')
    checks['last_and_after']=dict(status='verified' if stable else 'unconfirmed',first=start,last=low,
       last_response=last['number'],after_response=after['number'],after_behavior='explicit_empty_same_collection',first_anchor_stable=stable)
    checks['oldest_item']=dict(status='last_page_sample',item=oldest,global_oldest_proven=False,
       all_last_page_dates_known=not last.get('summary',{}).get('date_order',{}).get('missing',1))
    page_size=head['summary']['item_count'];last_count=last['summary']['item_count'];implied=(low-start)*page_size+last_count
    totals=[x['value'] for x in head['summary'].get('totals',[]) if type(x.get('value')) is int]
    checks['total_arithmetic']=dict(status='matches_reported_candidate' if implied in totals else 'not_reported' if not totals else 'contradicted',
        reported_candidates=totals,page_count=low-start+1,page_size=page_size,last_count=last_count,implied_total=implied,
        all_intermediate_page_sizes_verified=False)
    checks['hidden_cap']=dict(status='no_cap_observed_at_verified_boundary',absence_of_all_hidden_caps_proven=False)
    head_order=head['summary']['date_order'];tail_order=last['summary']['date_order']
    hd=[x['date'] for x in head['summary']['items'] if x.get('date')]
    td=[x['date'] for x in last['summary']['items'] if x.get('date')]
    newest=bool(hd and td and not head_order['missing'] and not tail_order['missing']
                and not head_order['increasing'] and not tail_order['increasing'] and min(hd)>max(td))
    oldest_first=bool(hd and td and not head_order['missing'] and not tail_order['missing']
                and not head_order['decreasing'] and not tail_order['decreasing'] and max(hd)<min(td))
    if stable and checks['total_arithmetic']['status']!='contradicted' and (newest or oldest_first):
        scope.update(status='confirmed',strategy='마지막 페이지부터 거꾸로' if newest else '오래된 순 정렬',confidence='보통')
        if oldest_first:checks['oldest_item'].update(item=head['summary']['oldest_observed'],status='native_oldest_first_sample')
    checks['oldest_sort_or_date_filter']['native_order_evidence']={'newest_first':newest,'oldest_first':oldest_first}
    if head['summary'].get('selector_scope_requires_review'):
        scope.update(status='unconfirmable_custom',confidence='낮음');scope['limitations'].append('inferred_code_selector_scope_requires_manual_binding')


def probe_target(session, targets):
    url=targets[0]['url'];scope=initial_scope(url);scope['requested_URLs']=targets
    try:
        # REST lists use projection before the first request; never request full post content.
        if re.search(r'/wp-json/wp/v2/[\w-]+/?$',urlsplit(url).path):
            scope['checks']['domain_migration']=dict(status='direct_requested_REST_endpoint')
            native_wp(session,url,scope)
        else:
            first,raw,redirects=session.entry(url);scope['entry_response']=first['number']
            scope['checks']['domain_migration']=dict(status='observed',redirects=redirects,resolved_url=first['url'],
                old_domain_redirect_observed=any(x['old_host']!=x['new_host'] for x in redirects))
            if raw is None:
                scope['limitations'].append('entry_unavailable:'+str(first.get('error') or first.get('status')));return scope
            summary=first.get('summary',{})
            scope['checks']['oldest_item']=dict(status='entry_sample_only',item=summary.get('oldest_observed'),global_oldest_proven=False,response=first['number'])
            scope['checks']['total_arithmetic']=dict(status='reported_candidates_only',totals=summary.get('totals',[]),arithmetic_verified=False)
            if summary.get('xml_root') in ('urlset','sitemapindex'):sitemap_probe(session,first,scope)
            else:
                route=page_route(first['url'],session.plan['live_check'],session.plan.get('pagination_config'))
                if not route and summary.get('pagination'):
                    route=page_route(summary['pagination'][0]['url'])
                if route:page_probe(session,first,scope,route,[x['url'] for x in targets])
                else:scope['limitations'].append('no_concrete_paging_contract_in_URL_code_or_response')
                options=summary.get('oldest_options',[])
                if options:
                    sorted_page,_=session.read(options[0]['url'],'advertised_oldest_sort')
                    ordering=sorted_page.get('summary',{}).get('date_order',{})
                    scope['checks']['oldest_sort_or_date_filter']=dict(status='observed_ascending_sample' if ordering.get('increasing') and not ordering.get('decreasing') and not ordering.get('missing') else 'unconfirmed',response=sorted_page['number'],date_order=ordering)
                else:scope['checks']['oldest_sort_or_date_filter'].update(status='not_exposed_in_entry_response',date_controls=summary.get('date_filters',[]),date_filter_execution_verified=False)
                if summary.get('javascript_shell'):scope['limitations'].append('JS_UI_or_POST_contract_requires_custom_handler')
                if not summary.get('items'):scope['limitations'].append('listing_selector_or_JSON_record_contract_not_established')
        scope['checks']['crawler_page_cap']=dict(status='static_candidates',candidates=session.plan['static_limits'],scope_binding_verified=False)
    except AccessStopped as exc:scope.update(status='access_blocked',reason=str(exc))
    except BudgetReached as exc:scope.update(status='unconfirmable_budget',reason=str(exc))
    except (ValueError,KeyError,TypeError,IndexError) as exc:
        scope.update(status='unconfirmable_custom',reason=type(exc).__name__+': '+str(exc)[:300])
    for check in scope['checks'].values():
        if check.get('reason')=='not_observed':
            check['reason']=scope.get('reason') or '; '.join(scope['limitations']) or 'boundary_or_listing_contract_not_established_by_common_probe'
    return scope
