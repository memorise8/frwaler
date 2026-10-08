"""Site search credentials: names and diagnostics only, never expose values."""
from __future__ import annotations

import os

SEARCH_KEYS = {
    'cancer-fr-catalogue-des-public': 'CANCER_FR_CATALOGUE_DES_PUBLIC_KEY',
    'education-govt-nz-our-work': 'EDUCATION_GOVT_NZ_OUR_WORK_KEY',
    'education-govt-nz-search': 'EDUCATION_GOVT_NZ_SEARCH_KEY',
    'inrae-fr-actualites': 'INRAE_FR_ACTUALITES_KEY',
    'nkvts-no-english': 'NKVTS_NO_ENGLISH_KEY',
    'phfscience-nz-digital-library': 'PHFSCIENCE_NZ_DIGITAL_LIBRARY_KEY',
    'phfscience-nz-news-publications': 'PHFSCIENCE_NZ_NEWS_PUBLICATIONS_KEY',
    'transparency-gov-au-publications': 'TRANSPARENCY_GOV_AU_PUBLICATIONS_KEY',
}
OPTIONAL_KEYS = {'cancer-fr-catalogue-des-public'}


def credential_status(site_id):
    name = SEARCH_KEYS.get(site_id)
    return {'name': name, 'required': bool(name and site_id not in OPTIONAL_KEYS),
            'configured': bool(name and os.environ.get(name, '').strip()),
            'authentication': 'not_verified'}


def missing_credentials(site_id):
    state = credential_status(site_id)
    missing = [state['name']] if state['required'] and not state['configured'] else []
    if site_id in ('scienceon-api', 'scienceon-foreign-api'):
        missing.extend(k for k in ('SCIENCEON_KEY','SCIENCEON_CLIENT_ID','SCIENCEON_MAC_ADDRESS')
                       if not os.environ.get(k, '').strip())
    return missing


def redact(text):
    for name in (*SEARCH_KEYS.values(), 'SCIENCEON_KEY', 'TRANSLATION_EXTERNAL_API_KEY', 'DELIVERY_API_TOKEN'):
        value = os.environ.get(name, '')
        if value:
            text = text.replace(value, '[REDACTED]')
    return text


def search_failure(crawler, reason='fetch_failed'):
    # Preserve a specific authentication/format error when a caller also reports
    # its generic failed-fetch condition.
    if not crawler.delivery_source_failure:
        crawler._report_source_failure(reason)
    return None


def check_search_response(crawler, payload, field):
    if isinstance(payload, dict) and payload.get('status') in (401,403):
        return search_failure(crawler, 'authentication_failed')
    if isinstance(payload, dict) and payload.get('status') == 429:
        return search_failure(crawler, 'rate_limited')
    if not isinstance(payload, dict) or not isinstance(payload.get(field), list):
        return search_failure(crawler, 'invalid_response')
    if field == 'results':
        if not payload[field] or any(not isinstance(r,dict) or not isinstance(r.get('hits'),list) for r in payload[field]):
            return search_failure(crawler, 'invalid_response')
    return payload
