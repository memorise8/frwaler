# -*- coding: utf-8 -*-
"""Crawler registry with auto-discovery of JSON configs."""

import glob
import json
import os

from .ntrs import NTRSCrawler
from .mohw import MOHWCrawler
from .fsc import FSCCrawler

CRAWLERS = {
    'ntrs': NTRSCrawler,
    'mohw': MOHWCrawler,
    'fsc': FSCCrawler,
}

# Auto-discover JSON config files and register GenericCrawler instances
_configs_dir = os.path.join(os.path.dirname(__file__), "configs")
if os.path.isdir(_configs_dir):
    from ..generic_crawler import GenericCrawler

    for _cfg_file in sorted(glob.glob(os.path.join(_configs_dir, "*.json"))):
        try:
            with open(_cfg_file, encoding="utf-8") as _f:
                _cfg = json.load(_f)
            _site_id = _cfg.get("site_id")
            if _site_id and _site_id not in CRAWLERS:
                def _make_factory(path, cfg):
                    class _ConfiguredCrawler(GenericCrawler):
                        _config_path = path
                        # Expose as class attributes for registration
                        site_id = cfg.get("site_id", "")
                        site_name = cfg.get("site_name", "")
                        base_url = cfg.get("base_url", "")
                        def __init__(self, db_conn, delay=None):
                            super().__init__(self._config_path, db_conn, delay)
                    return _ConfiguredCrawler
                CRAWLERS[_site_id] = _make_factory(_cfg_file, _cfg)
        except (json.JSONDecodeError, KeyError):
            pass

# Auto-discover custom Python crawlers from sites/custom/
_custom_dir = os.path.join(os.path.dirname(__file__), "custom")
if os.path.isdir(_custom_dir):
    import importlib.util
    for _py_file in sorted(glob.glob(os.path.join(_custom_dir, "*.py"))):
        if os.path.basename(_py_file) == "__init__.py":
            continue
        try:
            _mod_name = os.path.basename(_py_file)[:-3]
            _spec = importlib.util.spec_from_file_location(f"custom_{_mod_name}", _py_file)
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            for _attr_name in dir(_mod):
                _attr = getattr(_mod, _attr_name)
                # Only register concrete crawlers: a class carrying a *string*
                # site_id + a crawl(). This excludes the abstract BaseCrawler
                # (site_id is an unbound property) and any crawler that forgot
                # to set a string site_id (would otherwise pollute the registry
                # with a non-string key and break sorted()/join() over it).
                if (isinstance(_attr, type)
                        and hasattr(_attr, 'crawl')
                        and isinstance(getattr(_attr, 'site_id', None), str)
                        and _attr.site_id
                        and _attr.site_id not in CRAWLERS):
                    CRAWLERS[_attr.site_id] = _attr
        except Exception:
            pass
