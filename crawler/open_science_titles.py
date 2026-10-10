"""Open Science catalogue titles are plain metadata, not Markdown."""
from html import unescape
import re


def raw_title(item):
    entries = (item.get('metadata') or {}).get('dc.title') or []
    if isinstance(entries, dict):
        entries = [entries]
    elif not isinstance(entries, list):
        entries = [{'value': entries}]
    entries = sorted(entries, key=lambda e: e.get('place', 0) if isinstance(e, dict) else 0)
    english = [e for e in entries if isinstance(e, dict) and str(e.get('language') or '').lower() == 'en']
    for entry in english or entries:
        value = entry.get('value') if isinstance(entry, dict) else entry
        if clean_title(value):
            return value
    return item.get('name') or ''


def clean_title(value):
    if value is None:
        return ''
    # Retain source inline-tag handling and whitespace normalization, but keep
    # literal symbols such as Yield*Trait, series #1, C# and identifiers.
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', unescape(str(value)))).strip()


def source_title(item):
    return clean_title(raw_title(item))


def legacy_title(item):
    """Exact former normalization, solely to bound historical repair plans."""
    text = clean_title(raw_title(item))
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    return re.sub(r'\s+', ' ', re.sub(r'[*_`#>]+', ' ', text)).strip()
