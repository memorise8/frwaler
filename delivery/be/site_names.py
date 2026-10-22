"""Display form of site names.

Generated crawlers store ``site_name = "Custom: <slug>"``; the prefix only marks
how the crawler was made and means nothing to an operator. Stored rows keep the
original value; API responses show the name without the prefix.
"""
from __future__ import annotations

CUSTOM_PREFIX = "Custom:"


def display_site_name(name: str | None) -> str | None:
    if not name or not name.startswith(CUSTOM_PREFIX):
        return name
    return name[len(CUSTOM_PREFIX):].strip() or name
