"""Machine-readable eligibility, separate from source health/full verification."""
from __future__ import annotations
import json
from pathlib import Path

from crawler.incremental_adapters import HAL_SCOPES


def eligibility(site_id, cls):
    reason = 'listing_identity_order_and_end_contract_unverified'
    adapter = None
    if site_id in HAL_SCOPES:
        adapter = 'HALAdapter'
    elif site_id == 'doaj-org-search':
        adapter = 'DOAJAdapter'
    elif site_id in ('ntrs','ntrs-nasa-gov-search'):
        adapter, reason = 'NTRSAdapter', 'newest_order_not_verified_in_live_api'
    elif site_id == 'cds-cern-ch-collection':
        reason = 'source_access_challenge_and_proxy_collection_equivalence_unverified'
    elif getattr(cls, '_config_path', None):
        config = json.loads(Path(cls._config_path).read_text())
        policy = config.get('incremental', {})
        if policy.get('stable_ids') and policy.get('end_selector'):
            adapter = 'HTMLAdapter'
        else:
            reason = 'html_stable_identity_and_explicit_end_not_configured'
    supported_candidate = adapter is not None and adapter != 'NTRSAdapter'
    return {'site_id': site_id, 'adapter': adapter,
            'classification': 'pending_initial_exhaustion' if supported_candidate else 'full_rescan_required',
            'reason': 'order_inferred_after_first_complete_scan' if supported_candidate else reason,
            'declared_order_before': getattr(cls, 'DELIVERY_ORDER', 'arbitrary'),
            'automatic_order': 'infer_first_last_and_all_non_pinned_items' if adapter else 'unavailable_without_list_contract',
            'periodic_full_check': True, 'baseline_complete': False,
            'runtime_verification': 'not_verified_by_static_classification'}


def inventory():
    from crawler.sites import CRAWLERS
    return [eligibility(site, cls) for site,cls in sorted(CRAWLERS.items())]
