"""Conservative capability declarations; source signals never certify live health."""
from __future__ import annotations

from functools import lru_cache
import ast
import hashlib
from pathlib import Path

from crawler.search_credentials import credential_status, missing_credentials

HAL_RESUME = frozenset(('anr-hal-science-search','amu-hal-science-search','cea-hal-science-cnrgh',
                       'ehess-hal-science-search','ens-lyon-hal-science-search','inserm-hal-science-search'))
HAL_CHECKPOINT_RESUME = frozenset(('envt-hal-science-search', 'inria-hal-science-search',
                                 'polytechnique-hal-science-search'))
COUNT_REVIEWED = HAL_RESUME | HAL_CHECKPOINT_RESUME | {'doaj-org-search','scienceon-api','scienceon-foreign-api','krivet-re-kr'}


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
                 'crawler/db_libertree.py', 'crawler/db_pg.py', 'crawler/libertree_adapter.py',
                 'crawler/document_dates.py', 'crawler/document_identity.py',
                 'delivery/capabilities.py', 'delivery/worker/worker.py',
                 'delivery/worker/jobs.py', 'delivery/worker/scopes.py', 'delivery/db/schema.py',
                 'crawler/incremental.py', 'crawler/incremental_adapters.py',
                 'crawler/hal_incremental_scopes.json', 'delivery/incremental_status.py',
                 'delivery/incremental_registry.py', 'crawler/incremental_federal.py', 'crawler/incremental_dspace.py',
                 'delivery/incremental_holds.json', 'crawler/incremental_ots.py',
                 'crawler/incremental_bankofcanada.py', 'crawler/bankofcanada_incremental_scopes.json',
                 'crawler/incremental_carabinieri.py', 'crawler/incremental_kisdi.py',
                 'crawler/incremental_open_science.py', 'crawler/incremental_acpr.py', 'crawler/incremental_adruk.py', 'crawler/incremental_audencia.py', 'crawler/incremental_ens_lyon.py', 'crawler/ens_lyon_traversal.py', 'crawler/incremental_onera.py', 'crawler/onera_traversal.py', 'crawler/incremental_ungeneva.py', 'crawler/ungeneva_traversal.py', 'crawler/incremental_bfr.py', 'crawler/incremental_archives_nationales.py', 'crawler/incremental_asn.py', 'crawler/incremental_adaptcentre.py', 'crawler/incremental_afp_news.py', 'crawler/incremental_alrc.py', 'crawler/incremental_apra.py', 'crawler/incremental_arcnl.py', 'crawler/incremental_arcom.py', 'crawler/incremental_artsetmetiers.py'):
        digest.update((root/name).read_bytes())
    config = getattr(cls, '_config_path', None)
    if config:
        digest.update(Path(config).read_bytes())
    if site_id in HAL_CHECKPOINT_RESUME:
        digest.update((root/'crawler/hal_checkpoint.py').read_bytes())
    if site_id == 'federalregister-gov-documents':
        digest.update((root/'crawler/federal_checkpoint.py').read_bytes())
    from delivery.incremental_status import eligibility
    shared_adapter = eligibility(site_id, cls)['adapter']
    return {'source_hash':digest.hexdigest(), 'resume_signals':shared_adapter is not None or site_id in HAL_CHECKPOINT_RESUME or {'delivery_cursor','_advance_cursor','_mark_exhausted'} <= attrs}


def capabilities(site_id):
    source = _source(site_id)
    if source is None:
        raise CapabilityError('등록된 수집기가 없습니다.')
    from delivery.incremental_status import eligibility
    from crawler.sites import CRAWLERS
    shared = eligibility(site_id, CRAWLERS[site_id])
    hal = site_id in HAL_RESUME
    scienceon = site_id.startswith('scienceon-')
    reviewed = site_id in COUNT_REVIEWED
    order = '문서 ID 오름차순' if hal else ('등록일 내림차순' if site_id == 'doaj-org-search' else ('최근 연도·자료 유형·API 응답 순서' if scienceon else ('사이트 목록 순서 · 첫 페이지부터 중복 제거 재탐색' if site_id == 'krivet-re-kr' else '수집기별 순서 · 미검증')))
    if shared['adapter'] == 'HALAdapter':
        order = '제출일·고유 ID: 최초 오름차순, 재수집 내림차순'
    def mode(label, support='unknown', allowed=True, trial=True, reason='실제 사이트 검증 전 시험 실행입니다.'):
        return dict(label=label, support=support, allowed=allowed, requires_trial=trial, reason=reason)
    return {'site_id':site_id, **source, 'shared_incremental':shared, 'order':order,
            'count_semantics':'new_documents' if reviewed else 'unverified',
            'verification':{'level':'code_review' if reviewed else 'unverified',
                            'live':'not_verified', 'checked_at':None},
            'credential':credential_status(site_id), 'missing_credentials':missing_credentials(site_id),
            'features':{'chronological_resume':'unsupported',
                        'id_resume':'limited' if hal else 'unknown',
                        'latest':'limited' if site_id == 'doaj-org-search' else 'unknown',
                        'pdf':'conditional_on_pdf_url', 'text':'conditional_on_text_layer'},
            'modes':{
                'incremental':mode('신규 확인 · 제출일순' if shared['adapter'] == 'HALAdapter' else ('신규 확인 · 최신 등록순' if site_id == 'doaj-org-search' else '신규 확인 · 시험 수집'),
                                   'limited' if reviewed else 'unknown'),
                'full':mode('전체 범위 탐색 · 시험', 'unsupported' if scienceon else 'unknown', not scienceon,
                            reason='ScienceON은 과거 수집 모드를 사용해야 합니다.' if scienceon else '내부 페이지·시간 제한이 남아 있습니다.'),
                'backfill':mode('과거 수집 · 자동 이어받기', 'limited' if source['resume_signals'] else 'unsupported',
                                source['resume_signals'], reason='목록 변경 시 재개 위치의 완전성은 미검증입니다.' if source['resume_signals'] else '재개 계약이 확인되지 않은 수집기입니다.')},
            'limitations':['공통 증분은 최초 소스 소진과 대기 항목 저장 완료 후 활성화됩니다. 미지원 목록은 주기적 전체 재점검 대상입니다.',
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
