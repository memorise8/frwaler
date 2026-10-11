"""Extension point for rollout batches; preserves fd420d8 adapter contracts."""
from crawler.incremental_adapters import adapter_for as original_adapter_for
from crawler.incremental_federal import FederalAdapter
from crawler.incremental_dspace import NemertesAdapter
from crawler.incremental_ots import OTSAdapter
from crawler.incremental_bankofcanada import BankOfCanadaAdapter
from crawler.incremental_carabinieri import CarabinieriAdapter
from crawler.incremental_kisdi import KisdiAdapter
from crawler.incremental_open_science import OpenScienceAdapter
from crawler.incremental_acpr import ACPRAdapter
from crawler.incremental_adruk import AdrukAdapter
from crawler.incremental_audencia import AudenciaAdapter
from crawler.incremental_ens_lyon import EnsLyonAdapter
from crawler.incremental_onera import OneraAdapter
from crawler.incremental_ungeneva import UNGenevaAdapter
from crawler.incremental_bfr import BfRAdapter
from crawler.incremental_archives_nationales import ArchivesNationalesAdapter
from crawler.incremental_asn import ASNAdapter
from crawler.incremental_adaptcentre import AdaptCentreAdapter

ADDITIONAL_ADAPTERS = {'federalregister-gov-documents': FederalAdapter,
                       'nemertes-library-upatras-gr-browse': NemertesAdapter,
                       'ots-at-pressemappe': OTSAdapter,
                       'bankofcanada-ca-publications': BankOfCanadaAdapter,
                       'carabinieri-it-in-vostro-aiuto': CarabinieriAdapter,
                       'kisdi-re-kr-report': KisdiAdapter,
                       'open-science-canada-ca-communities': OpenScienceAdapter,
                       'acpr-banque-france-fr-fr': ACPRAdapter,
                       'adruk-org-news-publications': AdrukAdapter,
                       'audencia-com-lecole': AudenciaAdapter,
                       'ens-lyon-fr-lecole': EnsLyonAdapter,
                       'onera-fr-fr': OneraAdapter,
                       'ungeneva-org-en': UNGenevaAdapter,
                       'bfr-bund-de-en': BfRAdapter,
                       'archives-nationales-culture-gouv-fr-presse': ArchivesNationalesAdapter,
                       'asn-fr-information': ASNAdapter,
                       'adaptcentre-ie-research': AdaptCentreAdapter}


def adapter_for(crawler):
    adapter = ADDITIONAL_ADAPTERS.get(crawler.site_id)
    return adapter(crawler) if adapter else original_adapter_for(crawler)
