"""Native persistence for one explicitly bounded Federal Register date range.

This is a building block for the all-range scheduler. Finishing a date range
never marks the entire site exhausted. Resume validates the last source page;
it does not claim that the publisher provides an immutable snapshot.
"""
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlencode

VERSION = 'federal-date-range-anchor-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def source_hash(crawler):
    return source_hash_for_class(type(crawler))


def source_hash_for_class(cls):
    root = Path(__file__).parent
    source = hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes())
    for path in (__file__, *(root/name for name in ('base_crawler.py','document_dates.py','document_identity.py',
                                                  'libertree_adapter.py','db_libertree.py','db_pg.py'))):
        source.update(Path(path).read_bytes())
    return source.hexdigest()


def crawl_range(crawler, lower, upper, expected_count, *, limit=None, max_pages=200, wall_seconds=1500):
    crawler.delivery_date_range_exhausted = False
    crawler._check_cancelled()
    saved = 0
    try:
        if (date.fromisoformat(lower).isoformat() != lower or date.fromisoformat(upper).isoformat() != upper
                or lower > upper or type(expected_count) is not int or not 0 <= expected_count < 1500
                or type(max_pages) is not int or max_pages <= 0 or wall_seconds <= 0
                or (limit is not None and (type(limit) is not int or limit < 0))):
            raise ValueError('invalid explicit date scope')
    except (TypeError, ValueError):
        crawler._report_source_failure('configuration')
        return saved
    if limit == 0:
        return saved
    signature = source_hash(crawler)
    contract = {'version':VERSION,'site_id':crawler.site_id,'source_hash':signature,
                'lower':lower,'upper':upper,'expected_count':expected_count,'rows':crawler._PAGE_SIZE}
    pairs = [('conditions[agencies][]',crawler._AGENCY_SLUG),('conditions[search_type_id]','3'),
             ('order','newest'),('per_page',str(crawler._PAGE_SIZE)),('page','1'),
             ('conditions[publication_date][gte]',lower),('conditions[publication_date][lte]',upper)]
    request_url = crawler._API_BASE+'?'+urlencode(pairs)
    cursor = crawler.delivery_cursor
    seen = []; page_urls = []; skip = 0; anchor = None
    if cursor:
        try:
            if not isinstance(cursor,dict) or any(cursor.get(k)!=v for k,v in contract.items()):
                raise ValueError('different source contract')
            request_url = crawler._checked_next_url(cursor['request_url'], date_range=(lower,upper))
            skip = cursor['processed_in_page'];anchor = cursor['page_hash']
            seen = json.loads(cursor['seen_keys_json']);page_urls = json.loads(cursor['page_urls_json'])
            if (type(skip) is not int or not 1 <= skip <= crawler._PAGE_SIZE
                    or not isinstance(anchor,str) or len(anchor)!=64
                    or not isinstance(seen,list) or not skip <= len(seen) <= expected_count
                    or any(not isinstance(k,str) or len(k)!=64 for k in seen) or len(set(seen))!=len(seen)
                    or not isinstance(page_urls,list) or not page_urls or page_urls[-1]!=request_url
                    or len(page_urls)>expected_count or len(set(page_urls))!=len(page_urls)):
                raise ValueError('invalid cursor shape')
            for url in page_urls: crawler._checked_next_url(url,date_range=(lower,upper))
        except (KeyError,TypeError,ValueError):
            crawler._report_source_failure('configuration')
            return saved

    started = time.monotonic()
    def expired():
        crawler._check_cancelled()
        return time.monotonic()-started >= wall_seconds

    # A resume must be able to validate an already-completed anchor page and
    # then move forward even when the configured new-page budget is one.
    for _ in range(max_pages + int(bool(cursor))):
        if expired() or (limit is not None and saved >= limit):
            return saved
        try:
            response = crawler._request(request_url)
        except Exception:
            crawler._report_source_failure('fetch_failed')
            return saved
        if response is None:
            crawler._report_source_failure('fetch_failed')
            return saved
        try:
            data = response.json()
            if not isinstance(data,dict) or type(data.get('count')) is not int or data['count']!=expected_count:
                raise ValueError('changed count')
            docs = data.get('results')
            if not isinstance(docs,list) or len(docs)>crawler._PAGE_SIZE or len(docs)>expected_count:
                raise ValueError('invalid source window')
            keys = [];days = []
            for doc in docs:
                number, url = doc['document_number'],doc['html_url']
                if not isinstance(number,str) or not number.strip() or not isinstance(url,str) or not url.strip():
                    raise ValueError('missing source identity')
                keys.append(digest((number.strip(),url.strip())))
                day = date.fromisoformat(doc['publication_date']).isoformat()
                if not lower <= day <= upper: raise ValueError('date filter ignored')
                days.append(day)
            if len(keys)!=len(set(keys)) or days!=sorted(days,reverse=True):
                raise ValueError('duplicate or unordered source records')
            fingerprint = digest(data)
            if anchor is not None:
                if fingerprint!=anchor or skip>len(docs) or keys[:skip]!=seen[-skip:]:
                    raise ValueError('resume page changed')
            else:
                if request_url in page_urls: raise ValueError('repeated page URL')
                page_urls.append(request_url)
            if set(keys[skip:]).intersection(seen) or len(seen)+len(docs)-skip>expected_count:
                raise ValueError('source overlap or count exceeded')
            next_url = data.get('next_page_url')
            if next_url:
                next_url = crawler._checked_next_url(next_url,date_range=(lower,upper))
                if next_url in page_urls: raise ValueError('nonadvancing continuation')
            if not docs and (expected_count or next_url): raise ValueError('premature empty page')
        except (KeyError,TypeError,ValueError,AttributeError):
            crawler._report_source_failure('invalid_response')
            return saved

        for index in range(skip,len(docs)):
            if expired() or (limit is not None and saved >= limit):
                return saved
            try:
                paper = crawler._paper_from_item(docs[index])
                if paper is not None:
                    crawler._save_paper(paper)
                    if crawler._last_save_created: saved += 1
            except Exception:
                crawler._report_source_failure('item_failed')
                return saved
            if crawler.delivery_source_failure: return saved
            seen.append(keys[index])
            crawler._advance_cursor(dict(contract,request_url=request_url,page_hash=fingerprint,
                processed_in_page=index+1,seen_keys_json=json.dumps(seen),page_urls_json=json.dumps(page_urls)),items_done=1)
        if not next_url:
            if len(seen)!=expected_count:
                crawler._report_source_failure('invalid_response')
            else:
                crawler.delivery_date_range_exhausted = True
            return saved
        if len(seen)>=expected_count:
            crawler._report_source_failure('invalid_response')
            return saved
        request_url = next_url;skip = 0;anchor = None
    return saved


class _PlanBudget(BaseException):
    """Stop at a saved checkpoint without converting a budget into fetch failure."""


def normalize_plan(lower, upper, ranges):
    """Validate the exact contiguous date scope before queuing or fetching."""
    if (not isinstance(ranges, list) or not 1 <= len(ranges) <= 10000
            or date.fromisoformat(lower).isoformat() != lower
            or date.fromisoformat(upper).isoformat() != upper):
        raise ValueError('invalid plan configuration')
    normalized = []; next_lower = lower
    for entry in ranges:
        lo, hi, count = entry['lower'], entry['upper'], entry['expected_count']
        if (lo != next_lower or date.fromisoformat(lo).isoformat() != lo
                or date.fromisoformat(hi).isoformat() != hi or lo > hi
                or type(count) is not int or not 0 <= count < 1500):
            raise ValueError('noncontiguous or overlarge date range')
        normalized.append({'lower': lo, 'upper': hi, 'expected_count': count})
        next_lower = (date.fromisoformat(hi) + timedelta(days=1)).isoformat()
    if normalized[-1]['upper'] != upper:
        raise ValueError('bounds do not match partition')
    return normalized


def crawl_plan(crawler, lower, upper, ranges, *, limit=None, max_requests=200, wall_seconds=1500):
    """Walk an explicit complete date partition; keep whole-site completion separate.

    The request budget includes anchor rereads. At least two requests permit a
    resumed completed page to validate its anchor and still move forward.
    """
    crawler.delivery_date_plan_exhausted = False
    crawler._check_cancelled()
    try:
        if (type(max_requests) is not int or max_requests < 2 or wall_seconds <= 0
                or (limit is not None and (type(limit) is not int or limit < 0))):
            raise ValueError('invalid plan configuration')
        normalized = normalize_plan(lower, upper, ranges)
    except (KeyError,TypeError,ValueError,OverflowError):
        crawler._report_source_failure('configuration')
        return 0
    contract={'version':'federal-date-plan-anchor-v1','site_id':crawler.site_id,
              'source_hash':source_hash(crawler),'plan_hash':digest(normalized),'lower':lower,'upper':upper}
    index=0;child=None
    if crawler.delivery_cursor:
        try:
            cursor=crawler.delivery_cursor
            if not isinstance(cursor,dict) or any(cursor.get(k)!=v for k,v in contract.items()):
                raise ValueError('different plan or source')
            index=cursor['range_index'];child=json.loads(cursor['range_cursor_json'])
            if (type(index) is not int or not 0 <= index <= len(normalized)
                    or (child is not None and not isinstance(child,dict))
                    or (index==len(normalized) and child is not None)
                    or cursor['completed_source_records']!=sum(r['expected_count'] for r in normalized[:index])):
                raise ValueError('invalid plan cursor')
        except (KeyError,TypeError,ValueError):
            crawler._report_source_failure('configuration')
            return 0
    saved=0;requests=0;started=time.monotonic()
    original_cursor=crawler.delivery_cursor
    original_advance=crawler._advance_cursor;original_request=crawler._request;original_save=crawler._save_paper
    def publish(items_done=0):
        original_advance(dict(contract,range_index=index,range_cursor_json=json.dumps(child),
            completed_source_records=sum(r['expected_count'] for r in normalized[:index])),items_done=items_done)
    def advance(value,items_done=0):
        nonlocal child
        child=dict(value);publish(items_done)
    def request(*args,**kwargs):
        nonlocal requests
        crawler._check_cancelled()
        if requests>=max_requests or time.monotonic()-started>=wall_seconds:raise _PlanBudget()
        requests+=1
        return original_request(*args,**kwargs)
    def save_paper(paper):
        nonlocal saved
        result=original_save(paper)
        if crawler._last_save_created:saved+=1
        return result
    crawler._advance_cursor=advance;crawler._request=request;crawler._save_paper=save_paper
    try:
        publish()
        while index<len(normalized):
            crawler._check_cancelled()
            remaining_time=wall_seconds-(time.monotonic()-started)
            if remaining_time<=0 or requests>=max_requests or (limit is not None and saved>=limit):break
            entry=normalized[index];crawler.delivery_cursor=child
            crawl_range(crawler,**entry,limit=None if limit is None else limit-saved,
                        max_pages=max_requests,wall_seconds=remaining_time)
            if crawler.delivery_source_failure or not crawler.delivery_date_range_exhausted:break
            index+=1;child=None;publish()
        if index==len(normalized):crawler.delivery_date_plan_exhausted=True
    except _PlanBudget:
        pass
    finally:
        crawler.delivery_cursor=original_cursor
        crawler._advance_cursor=original_advance;crawler._request=original_request;crawler._save_paper=original_save
    return saved
