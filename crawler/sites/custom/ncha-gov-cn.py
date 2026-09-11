"""Preserve the legacy ID with the paginated NCHA dataproxy implementation."""
from importlib import import_module

_column = import_module("crawler.sites.custom.ncha-gov-cn-col")


class NchaGovCnLegacyCrawler(_column.NchaGovCnColCrawler):
    site_id = "ncha-gov-cn"
    site_name = "国家文物局 — 行业标准"
