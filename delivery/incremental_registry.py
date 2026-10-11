"""Extension point for rollout batches; preserves fd420d8 adapter contracts."""
from crawler.incremental_adapters import adapter_for as original_adapter_for
from crawler.incremental_federal import FederalAdapter
from crawler.incremental_dspace import NemertesAdapter
from crawler.incremental_ots import OTSAdapter
from crawler.incremental_bankofcanada import BankOfCanadaAdapter
from crawler.incremental_carabinieri import CarabinieriAdapter
from crawler.incremental_kisdi import KisdiAdapter
from crawler.incremental_open_science import OpenScienceAdapter

ADDITIONAL_ADAPTERS = {'federalregister-gov-documents': FederalAdapter,
                       'nemertes-library-upatras-gr-browse': NemertesAdapter,
                       'ots-at-pressemappe': OTSAdapter,
                       'bankofcanada-ca-publications': BankOfCanadaAdapter,
                       'carabinieri-it-in-vostro-aiuto': CarabinieriAdapter,
                       'kisdi-re-kr-report': KisdiAdapter,
                       'open-science-canada-ca-communities': OpenScienceAdapter}


def adapter_for(crawler):
    adapter = ADDITIONAL_ADAPTERS.get(crawler.site_id)
    return adapter(crawler) if adapter else original_adapter_for(crawler)
