"""Restore missing separators between explicit date parts in display text.

No calendar components or month aliases are invented. The site's existing
parser still decides which month names and precisions it supports.
"""
import re

_MONTH_NAMES = frozenset((
    'january february march april may june july august september october november december '
    'jan feb mar apr jun jul aug sep sept oct nov dec '
    'janvier février fevrier mars avril mai juin juillet août aout septembre octobre novembre décembre decembre'
).split())


def separate_date_parts(value):
    if not isinstance(value,str) or re.search(r'\d{4}-\d{2}-\d{2}',value):
        return value
    value=re.sub(r'(?<!\d)(\d{1,2})([^\W\d_]+)(\d{4})(?!\d)',
                 lambda m: f'{m[1]} {m[2]} {m[3]}' if m[2].lower() in _MONTH_NAMES else m[0],value)
    value=re.sub(r'([^\W\d_]+)(\d{1,2})(,?)(\d{4})(?!\d)',
                 lambda m: f'{m[1]} {m[2]}{m[3]} {m[4]}' if m[1].lower() in _MONTH_NAMES else m[0],value)
    return re.sub(r'([^\W\d_]+)(\d{4})(?!\d)',
                  lambda m: f'{m[1]} {m[2]}' if m[1].lower() in _MONTH_NAMES else m[0],value)
