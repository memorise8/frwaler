"""Preserve the legacy registry ID using the current DOE library crawler."""
from importlib import import_module

_library = import_module("crawler.sites.custom.directives-doe-gov-directives-browse")


class DirectivesDoeGovLegacyCrawler(_library.DirectivesDoeGovDirectivesBrowseCrawler):
    site_id = "directives-doe-gov-directives-"
    site_name = "DOE — Directives Library"
