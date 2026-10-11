"""Extension point for rollout batches; preserves fd420d8 adapter contracts."""
from crawler.incremental_adapters import adapter_for as original_adapter_for
from crawler.incremental_federal import FederalAdapter

ADDITIONAL_ADAPTERS = {'federalregister-gov-documents': FederalAdapter}


def adapter_for(crawler):
    adapter = ADDITIONAL_ADAPTERS.get(crawler.site_id)
    return adapter(crawler) if adapter else original_adapter_for(crawler)
