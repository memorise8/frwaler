"""Plan-driven overrides for existing crawlers' common defects.

Defaults are untouched: nothing changes unless a plan supplies a ``legacy``
section for the site. Supported keys:

* ``page_cap``: replaces hard-coded page caps (``MAX_PAGES = 200`` and its
  spellings) on the instance, or on the crawler module for module constants.
  ``null`` means effectively unlimited; the crawler's own runtime budget remains.
* ``start_page``: first page number for GenericCrawler configs (0 or 1).
* ``decode_preencoded_params``: GenericCrawler sends pre-encoded params keys
  (``type%5B170%5D``) encoded once instead of twice.

Module-level constants are process-wide; ``restore_all()`` undoes every module
change (the worker calls it before each job), so overrides never leak from one
job into the next.
"""
from __future__ import annotations

import copy
import os
import sys

from .plan import load_plan

PAGE_CAP_NAMES = ("MAX_PAGES", "max_pages", "_MAX_PAGES", "MAX_PAGE", "_max_pages", "MAX_LIST_PAGES")
UNLIMITED = 10 ** 9
ENV_PLAN = "LIBERTREE_ENUMERATION_PLAN"
_MODULE_ORIGINALS = {}  # (module name, attribute) -> original value


def restore_all():
    """Undo every module-level override applied in this process."""
    for (module_name, name), value in list(_MODULE_ORIGINALS.items()):
        module = sys.modules.get(module_name)
        if module is not None:
            setattr(module, name, value)
    _MODULE_ORIGINALS.clear()


def apply_overrides(crawler, overrides):
    """Apply a site's ``legacy`` overrides. Returns {name: (old, new)} actually changed."""
    changed = {}
    if not overrides:
        return changed
    if "page_cap" in overrides:
        cap = overrides["page_cap"]
        if cap is not None and (not isinstance(cap, int) or cap < 1):
            raise ValueError("legacy.page_cap must be null or a positive integer")
        value = UNLIMITED if cap is None else cap
        module = sys.modules.get(type(crawler).__module__)
        for name in PAGE_CAP_NAMES:
            current = getattr(type(crawler), name, None)
            if isinstance(current, int) and not isinstance(current, bool):
                setattr(crawler, name, value)
                changed[name] = (current, value)
            elif module is not None and isinstance(module.__dict__.get(name), int):
                old = module.__dict__[name]
                _MODULE_ORIGINALS.setdefault((module.__name__, name), old)
                setattr(module, name, value)
                changed[f"module.{name}"] = (old, value)
    if overrides.get("decode_preencoded_params"):
        config = getattr(crawler, "_config", None)
        if not isinstance(config, dict):
            raise ValueError("legacy.decode_preencoded_params is supported for GenericCrawler configs only")
        config = copy.deepcopy(config)
        config.setdefault("options", {})["decode_preencoded_params"] = True
        crawler._config = config
        changed["options.decode_preencoded_params"] = (False, True)
    if "start_page" in overrides:
        start = overrides["start_page"]
        if not isinstance(start, int) or start < 0:
            raise ValueError("legacy.start_page must be a non-negative integer")
        config = getattr(crawler, "_config", None)
        if not isinstance(config, dict) or not isinstance(config.get("list_page"), dict):
            raise ValueError("legacy.start_page is supported for GenericCrawler configs only")
        config = copy.deepcopy(config)
        pagination = config["list_page"].setdefault("pagination", {})
        changed["pagination.start"] = (pagination.get("start", 1), start)
        pagination["start"] = start
        crawler._config = config
    return changed


def apply_overrides_from_env(crawler, environ=None):
    """Worker hook: no-op unless LIBERTREE_ENUMERATION_PLAN names a plan with a ``legacy`` entry."""
    path = (environ if environ is not None else os.environ).get(ENV_PLAN)
    if not path:
        return {}
    plan = load_plan(path)
    site = (plan.get("sites") or {}).get(getattr(crawler, "site_id", None)) or {}
    return apply_overrides(crawler, site.get("legacy"))
