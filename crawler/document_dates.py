"""Conservative date contract shared by SQLite, PostgreSQL and repair tools.

Only complete, unambiguous calendar dates enter the searchable date columns.
Raw text is retained even when parsing fails; partial dates never gain a day.
Numeric DMY/MDY requires an explicitly reviewed per-site rule.
"""
from datetime import date, datetime
import re
import unicodedata

DATE_FIELDS = ("published_date", "listed_date")
DATE_METADATA_FIELDS = tuple(f"{field}_{suffix}" for field in DATE_FIELDS
                             for suffix in ("raw", "status", "precision"))
# Add site rules only with a source-format regression fixture and evidence.
SITE_DATE_ORDERS: dict[str, str] = {}

_MONTHS = {}
for number, names in enumerate((
    "january jan janvier", "february feb fevrier", "march mar mars",
    "april apr avril", "may mai", "june jun juin", "july jul juillet",
    "august aug aout", "september sep sept septembre", "october oct octobre",
    "november nov novembre", "december dec decembre",
), 1):
    for name in names.split():
        _MONTHS[name] = number


def normalize_date(value, *, order=None):
    if order not in (None, "DMY", "MDY"):
        raise ValueError("date order must be DMY, MDY or unspecified")
    raw = None if value is None else str(value)
    text = (raw or "").strip()
    result = {"value": None, "raw": raw, "status": "missing", "precision": None}
    if not text:
        return result
    result["status"] = "unrecognized"
    # RSS timestamps: require the entire RFC-style value, including a valid
    # clock and explicit zone. Keep the source's calendar day, not UTC's day.
    rfc = re.fullmatch(
        r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+)?"
        r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
        r"(\d{4})\s+(\d{2}):(\d{2})(?::(\d{2}))?\s+(GMT|UT|UTC|[+-]\d{4})",
        text, re.IGNORECASE)
    if rfc:
        day, month, year, hour, minute, second, zone = rfc.groups()
        result["precision"] = "day"
        try:
            parsed = datetime(int(year), _MONTHS[month.lower()], int(day),
                              int(hour), int(minute), int(second or 0))
            if zone[0] in '+-' and (int(zone[1:3]) > 23 or int(zone[3:]) > 59):
                raise ValueError('invalid timezone offset')
            result.update(value=parsed.date().isoformat(), status="normalized")
        except ValueError:
            result["status"] = "invalid"
        return result
    folded = ''.join(c for c in unicodedata.normalize('NFKD', text.lower())
                     if not unicodedata.combining(c))
    parts = None
    if re.fullmatch(r"\d{4}", text):
        result.update(status="partial" if 1 <= int(text) <= 9999 else "invalid", precision="year")
        return result
    match = re.fullmatch(r"([a-z]+)\.?\s+(\d{4})", folded)
    if match and match[1] in _MONTHS:
        result.update(status="partial" if 1 <= int(match[2]) <= 9999 else "invalid", precision="month")
        return result
    match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})", text)
    if match:
        y, m = map(int, match.groups())
        result.update(status="partial" if 1 <= y <= 9999 and 1 <= m <= 12 else "invalid", precision="month")
        return result
    # Month/year has no day-order ambiguity; retain the original source text.
    match = re.fullmatch(r"(\d{1,2})/(\d{4})", text)
    if match:
        m, y = map(int, match.groups())
        result.update(status="partial" if 1 <= y <= 9999 and 1 <= m <= 12 else "invalid", precision="month")
        return result
    match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\.?", text)
    korean = re.fullmatch(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", text)
    compact = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", text)
    if match or korean or compact:
        parts = tuple(map(int, (match or korean or compact).groups()))
    elif re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", text):
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
            parts = (parsed.year, parsed.month, parsed.day)
        except ValueError:
            result.update(status="invalid", precision="day")
            return result
    else:
        match = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", text)
        if match:
            a, b, y = map(int, match.groups())
            if order is None:
                # Even apparently unambiguous numeric formats need site evidence.
                valid = False
                for m, d in ((a, b), (b, a)):
                    try:
                        date(y, m, d)
                        valid = True
                    except ValueError:
                        pass
                result.update(status="ambiguous" if valid else "invalid", precision="day")
                return result
            parts = (y, b, a) if order == "DMY" else (y, a, b)
        else:
            match = re.fullmatch(r"(\d{1,2})\s+([a-z]+)\.?\s+(\d{4})", folded)
            reverse = re.fullmatch(r"([a-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", folded)
            if match and match[2] in _MONTHS:
                parts = (int(match[3]), _MONTHS[match[2]], int(match[1]))
            elif reverse and reverse[1] in _MONTHS:
                parts = (int(reverse[3]), _MONTHS[reverse[1]], int(reverse[2]))
    if parts:
        result["precision"] = "day"
        try:
            result.update(value=date(*parts).isoformat(), status="normalized")
        except ValueError:
            result["status"] = "invalid"
    return result


def normalize_document_dates(doc, *, order=None):
    result = dict(doc)
    order = order or SITE_DATE_ORDERS.get(doc.get("site_id"))
    for field in DATE_FIELDS:
        parsed = normalize_date(doc.get(field), order=order)
        result[field] = parsed["value"]
        for suffix in ("raw", "status", "precision"):
            result[f"{field}_{suffix}"] = parsed[suffix]
    return result


def migrate_date_columns(conn, *, postgres=False):
    """Additive migration; existing values remain explicitly unchecked."""
    if postgres:
        for column in DATE_METADATA_FIELDS:
            default = " DEFAULT 'legacy_unchecked'" if column.endswith('_status') else ''
            conn.execute(f"ALTER TABLE documents ADD COLUMN IF NOT EXISTS {column} TEXT{default}")
    else:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(documents)')}
        for column in DATE_METADATA_FIELDS:
            if column not in columns:
                default = " DEFAULT 'legacy_unchecked'" if column.endswith('_status') else ''
                conn.execute(f"ALTER TABLE documents ADD COLUMN {column} TEXT{default}")
