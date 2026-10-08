"""Conservative capability declarations; source signals never certify live health."""
from __future__ import annotations

from functools import lru_cache
import ast
import hashlib
from pathlib import Path

from crawler.search_credentials import credential_status, missing_credentials

HAL_RESUME = frozenset(('anr-hal-science-search','amu-hal-science-search','cea-hal-science-cnrgh',
                       'ehess-hal-science-search','ens-lyon-hal-science-search','inserm-hal-science-search'))
COUNT_REVIEWED = HAL_RESUME | {'doaj-org-search','scienceon-api','scienceon-foreign-api'}


class CapabilityError(ValueError):
    pass


@lru_cache(maxsize=2048)
def _source(site_id):
    from crawler.sites import CRAWLERS
    cls = CRAWLERS.get(site_id)
    if cls is None:
        return None
    path = Path(cls.crawl.__code__.co_filename)
    text = path.read_text()
    attrs = {n.attr for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Attribute)}
    digest = hashlib.sha256(text.encode())
    root = Path(__file__).resolve().parents[1]
    for name in ('crawler/base_crawler.py', 'crawler/search_credentials.py',
                 'delivery/capabilities.py', 'delivery/worker/worker.py'):
        digest.update((root/name).read_bytes())
    config = getattr(cls, '_config_path', None)
    if config:
        digest.update(Path(config).read_bytes())
    return {'source_hash':digest.hexdigest(), 'resume_signals':{'delivery_cursor','_advance_cursor','_mark_exhausted'} <= attrs}


def capabilities(site_id):
    source = _source(site_id)
    if source is None:
        raise CapabilityError('등록된 수집기가 없습니다.')
    hal = site_id in HAL_RESUME
    scienceon = site_id.startswith('scienceon-')
    reviewed = site_id in COUNT_REVIEWED
    order = '문서 ID 오름차순' if hal else ('등록일 내림차순' if site_id == 'doaj-org-search' else ('최근 연도·자료 유형·API 응답 순서' if scienceon else '수집기별 순서 · 미검증'))
    def mode(label, support='unknown', allowed=True, trial=True, reason='실제 사이트 검증 전 시험 실행입니다.'):
        return dict(label=label, support=support, allowed=allowed, requires_trial=trial, reason=reason)
    return {'site_id':site_id, **source, 'order':order,
            'count_semantics':'new_documents' if reviewed else 'unverified',
            'verification':{'level':'code_review' if reviewed else 'unverified',
                            'live':'not_verified', 'checked_at':None},
            'credential':credential_status(site_id), 'missing_credentials':missing_credentials(site_id),
            'features':{'chronological_resume':'unsupported',
                        'id_resume':'limited' if hal else 'unknown',
                        'latest':'limited' if site_id == 'doaj-org-search' else 'unknown',
                        'pdf':'conditional_on_pdf_url', 'text':'conditional_on_text_layer'},
            'modes':{
                'incremental':mode('신규 이어받기 · ID순' if hal else ('신규 확인 · 최신 등록순' if site_id == 'doaj-org-search' else '신규 확인 · 시험 수집'),
                                   'limited' if reviewed else 'unknown'),
                'full':mode('전체 범위 탐색 · 시험', 'unsupported' if scienceon else 'unknown', not scienceon,
                            reason='ScienceON은 과거 수집 모드를 사용해야 합니다.' if scienceon else '내부 페이지·시간 제한이 남아 있습니다.'),
                'backfill':mode('과거 수집 · 자동 이어받기', 'limited' if source['resume_signals'] else 'unsupported',
                                source['resume_signals'], reason='목록 변경 시 재개 위치의 완전성은 미검증입니다.' if source['resume_signals'] else '재개 계약이 확인되지 않은 수집기입니다.')},
            'limitations':['날짜 기준 신규 이어받기는 아직 지원하지 않습니다.',
                            '무제한은 건수 제한만 해제하며 전체 수집 완료를 보장하지 않습니다.',
                            '페이지 위치 기반 재개는 원본 목록 변경 시 재검증이 필요합니다.']}


def validate_request(site_id, mode, *, trial=False):
    data = capabilities(site_id)
    item = data['modes'].get(mode)
    if not item or not item['allowed']:
        raise CapabilityError(item['reason'] if item else '지원하지 않는 수집 모드입니다.')
    if item['requires_trial'] and not trial:
        raise CapabilityError('검증 전 기능입니다. 시험 실행 확인이 필요합니다.')
    if data['missing_credentials']:
        raise CapabilityError('설정 필요: ' + ', '.join(data['missing_credentials']))
    return data
