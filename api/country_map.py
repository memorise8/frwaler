COUNTRY_INFO = {
    "KR": {"name": "South Korea", "flag": "🇰🇷", "name_ko": "대한민국"},
    "US": {"name": "United States", "flag": "🇺🇸", "name_ko": "미국"},
    "DE": {"name": "Germany", "flag": "🇩🇪", "name_ko": "독일"},
    "GB": {"name": "United Kingdom", "flag": "🇬🇧", "name_ko": "영국"},
    "OTHER": {"name": "Other", "flag": "🌐", "name_ko": "기타"},
}

_KR_EXACT = {"fsc", "mohw", "humoruniv", "inven-diablo2"}

_US_PREFIXES = (
    "dhs-gov", "doi-gov", "dol-gov", "ed-gov", "ed-annual", "ed-foia",
    "ed-infra", "ed-other", "eia-gov", "home-treasury-gov", "justice-gov",
    "news-va-gov", "oig-treasury-gov", "tigta-gov", "va-gov",
    "directives-doe-gov", "ntrs",
)

_DE_PREFIXES = (
    "bmg-", "bmi-", "bmv-", "bmwe-", "bmwsb-", "bmz-", "bundesfinanz-",
    "bmbfsfj-", "bmftr-", "bmleh-", "bmukn-", "auswaertiges-amt-",
    "daten-berlin", "datenportal-bmbf", "fraunhofer-", "publikationen-bund-",
    "statistischebibliothek", "publikationen-bundesregierung",
)

_GB_PREFIXES = (
    "adruk-", "babraham-", "committees-parliament", "data-gov-uk", "earlham-",
    "economy-ni-", "education-ni-", "eprints-soton", "finance-ni-",
    "geoportal-stats", "gov-scot-", "gov-uk-", "gov-wales-", "health-ni-",
    "infrastructure-ni-", "jic-", "justice-ni-", "lboro-repository",
    "lordslibrary", "mrc-lmb-", "naturalengland", "nisra", "noc-",
    "nora-nerc", "nrscotland", "ons-", "opendatacommunities", "osr-",
    "parliament-", "post-parliament", "publichealthscotland", "rfi-",
    "skillsdev-scot-", "ukdataservice",
)


def classify_country(site_id: str, site_name: str, base_url: str) -> str:
    sid = site_id.lower()

    if sid in _KR_EXACT:
        return "KR"

    for prefix in _US_PREFIXES:
        if sid == prefix or sid.startswith(prefix) or prefix in sid:
            return "US"

    for prefix in _DE_PREFIXES:
        if sid == prefix or sid.startswith(prefix) or prefix in sid:
            return "DE"

    for prefix in _GB_PREFIXES:
        if sid == prefix or sid.startswith(prefix) or prefix in sid:
            return "GB"

    return "OTHER"
