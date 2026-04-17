"""Parse messy datasheet strings into SI-normalized floats for BJT screening."""
import re
from typing import Optional

SI_PREFIXES = {
    'p': 1e-12, 'n': 1e-9, 'u': 1e-6, 'μ': 1e-6, 'µ': 1e-6,
    'm': 1e-3, '': 1, 'k': 1e3, 'K': 1e3, 'M': 1e6, 'G': 1e9,
}

_BASE_UNITS = {'V', 'A', 'W', 'Hz', 'C', 'F', 'Ω', 'ohm'}


def to_si(value: float, unit: Optional[str]) -> float:
    """Apply SI prefix multiplier to value given a unit string like 'mA', 'MHz', 'nA'."""
    if not unit:
        return value
    # Match optional prefix + base unit
    m = re.match(r'^([pnuμµmkKMG]?)([A-Za-zΩ°]+)$', unit)
    if m:
        prefix, _ = m.group(1), m.group(2)
        multiplier = SI_PREFIXES.get(prefix, 1.0)
        return value * multiplier
    return value


def parse_value(raw) -> tuple:
    """Returns (si_value, original_unit, note).

    Handles:
    - '100 nA' -> (1e-7, 'nA', None)
    - '-5.0 V' -> (-5.0, 'V', None)
    - '1A (pulsed 10A)' -> (1.0, 'A', 'pulsed 10A')
    - '40-200' or '40–200' -> (40.0, None, 'range: 40..200')  take min
    - 'min 40' -> (40.0, None, 'min')
    - 'max 200' -> (200.0, None, 'max')
    - '125°C' / '125 C' -> (125.0, 'C', None)
    """
    if raw is None:
        return (None, None, None)
    if isinstance(raw, (int, float)):
        return (float(raw), None, None)

    s = str(raw).strip()
    if not s:
        return (None, None, None)

    note: Optional[str] = None

    # Extract parenthetical note first, e.g. '1A (pulsed 10A)'
    paren_m = re.search(r'\(([^)]+)\)', s)
    if paren_m:
        note = paren_m.group(1).strip()
        s = s[:paren_m.start()].strip()

    # min / max prefix
    min_m = re.match(r'^min\s+(.+)$', s, re.IGNORECASE)
    max_m = re.match(r'^max\s+(.+)$', s, re.IGNORECASE)
    if min_m:
        note = 'min'
        s = min_m.group(1).strip()
    elif max_m:
        note = 'max'
        s = max_m.group(1).strip()

    # Range: '40–200' or '40-200' or '40 - 200' (unicode en-dash too)
    range_m = re.match(
        r'^([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*[–\-]\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([a-zA-ZμµΩ°]*)$',
        s
    )
    if range_m:
        lo = float(range_m.group(1))
        hi = float(range_m.group(2))
        unit_str = range_m.group(3).strip() or None
        range_note = f'range: {lo}..{hi}'
        if note:
            range_note = f'{note}; {range_note}'
        si_val = to_si(lo, unit_str)
        return (si_val, unit_str if unit_str else None, range_note)

    # degree symbol variants: '125°C' -> unit 'C', value 125
    s = re.sub(r'°\s*', ' ', s)

    # Main pattern: optional sign + number + optional unit
    num_unit_m = re.match(
        r'^([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([a-zA-ZμµΩ]*)$',
        s
    )
    if num_unit_m:
        value = float(num_unit_m.group(1))
        unit_str = num_unit_m.group(2).strip() or None
        si_val = to_si(value, unit_str)
        return (si_val, unit_str, note)

    # Fallback: try to extract first number
    fallback_m = re.search(r'[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?', s)
    if fallback_m:
        value = float(fallback_m.group())
        return (value, None, note if note else f'unparsed: {raw}')

    return (None, None, f'unparsed: {raw}')


# Key normalisation aliases (lowercase)
_KEY_MAP = {
    'vceo': 'vceo_v', 'bvceo': 'vceo_v',
    'vcbo': 'vcbo_v', 'bvcbo': 'vcbo_v',
    'vebo': 'vebo_v', 'bvebo': 'vebo_v',
    'ic_max': 'ic_max_a', 'ic': 'ic_max_a', 'ic(max)': 'ic_max_a', 'icmax': 'ic_max_a',
    'hfe_min': 'hfe_min', 'hfe(min)': 'hfe_min', 'hfemin': 'hfe_min',
    'hfe_max': 'hfe_max', 'hfe(max)': 'hfe_max', 'hfemax': 'hfe_max',
    'icbo': 'icbo_a_at_vcb', 'ices': 'icbo_a_at_vcb',
    'ft': 'ft_hz', 'transition_freq': 'ft_hz',
    'pd': 'pd_w', 'power_dissipation': 'pd_w',
    'tj': 'tj_max_c', 'tj_max': 'tj_max_c', 'junction_temp': 'tj_max_c',
    'polarity': 'polarity', 'type': 'polarity',
    'package': 'package', 'case': 'package',
}

_STRING_FIELDS = {'polarity', 'package'}


def normalize_bjt_params(raw: dict) -> dict:
    """Normalise an LLM-extracted raw dict to BjtParameters field names."""
    result: dict = {}
    for raw_key, raw_val in raw.items():
        canonical = _KEY_MAP.get(raw_key.lower().strip())
        if canonical is None:
            continue
        if canonical in _STRING_FIELDS:
            if raw_val is None:
                result[canonical] = None
            elif canonical == 'polarity':
                result[canonical] = str(raw_val).strip().upper()
            else:
                result[canonical] = str(raw_val).strip()
        else:
            si_val, _unit, _note = parse_value(raw_val)
            result[canonical] = si_val
    return result


MOSFET_KEY_MAP = {
    'bvdss': 'bvdss_v', 'vdss': 'bvdss_v', 'breakdown': 'bvdss_v',
    'vgs_th': 'vgs_th_v', 'vth': 'vgs_th_v', 'threshold': 'vgs_th_v',
    'rds_on': 'rds_on_ohm', 'rdson': 'rds_on_ohm',
    'id_max': 'id_max_a', 'id': 'id_max_a', 'drain_current': 'id_max_a',
    'idss': 'idss_a', 'drain_leakage': 'idss_a',
    'qg': 'qg_c', 'gate_charge': 'qg_c',
    'pd': 'pd_w', 'power': 'pd_w',
    'tj': 'tj_max_c', 'tj_max': 'tj_max_c',
    'gate_oxide': 'gate_oxide',
    'polarity': 'polarity', 'type': 'polarity', 'channel': 'polarity',
    'package': 'package', 'case': 'package',
}

_MOSFET_STRING_FIELDS = {'gate_oxide', 'polarity', 'package'}


def normalize_mosfet_params(raw: dict) -> dict:
    """Normalise an LLM-extracted raw dict to MosfetParameters field names."""
    result: dict = {}
    for raw_key, raw_val in raw.items():
        canonical = MOSFET_KEY_MAP.get(raw_key.lower().strip())
        if canonical is None:
            continue
        if canonical in _MOSFET_STRING_FIELDS:
            if raw_val is None:
                result[canonical] = None
            else:
                result[canonical] = str(raw_val).strip()
        else:
            si_val, _unit, _note = parse_value(raw_val)
            result[canonical] = si_val
    return result


if __name__ == '__main__':
    # --- parse_value tests ---
    v, u, n = parse_value('100 nA')
    assert abs(v - 1e-7) < 1e-20, f'nA: {v}'
    assert u == 'nA', f'unit nA: {u}'
    assert n is None

    v, u, n = parse_value('-5.0 V')
    assert v == -5.0, f'-5V: {v}'
    assert u == 'V'
    assert n is None

    v, u, n = parse_value('1A (pulsed 10A)')
    assert v == 1.0, f'1A pulsed: {v}'
    assert u == 'A'
    assert n == 'pulsed 10A', f'note: {n}'

    v, u, n = parse_value('40-200')
    assert v == 40.0, f'range min: {v}'
    assert 'range' in n, f'range note: {n}'

    v, u, n = parse_value('40\u2013200')  # en-dash
    assert v == 40.0, f'en-dash range: {v}'

    v, u, n = parse_value('min 40')
    assert v == 40.0, f'min 40: {v}'
    assert n == 'min', f'min note: {n}'

    v, u, n = parse_value('max 200')
    assert v == 200.0, f'max 200: {v}'
    assert n == 'max', f'max note: {n}'

    v, u, n = parse_value('125°C')
    assert v == 125.0, f'125C: {v}'
    assert u == 'C', f'unit C: {u}'

    v, u, n = parse_value('125 C')
    assert v == 125.0, f'125 C: {v}'
    assert u == 'C', f'unit C2: {u}'

    v, u, n = parse_value(None)
    assert v is None

    v, u, n = parse_value(3.3)
    assert v == 3.3

    # --- normalize_bjt_params tests ---
    raw = {
        'VCEO': '40 V',
        'IC_MAX': '100 mA',
        'hfe_min': '100',
        'hfe_max': '300',
        'FT': '250 MHz',
        'PD': '500 mW',
        'TJ_MAX': '150°C',
        'polarity': 'npn',
        'package': ' TO-92 ',
    }
    out = normalize_bjt_params(raw)
    assert abs(out['vceo_v'] - 40.0) < 1e-9, f'vceo: {out["vceo_v"]}'
    assert abs(out['ic_max_a'] - 0.1) < 1e-9, f'ic: {out["ic_max_a"]}'
    assert out['hfe_min'] == 100.0
    assert out['hfe_max'] == 300.0
    assert abs(out['ft_hz'] - 250e6) < 1, f'ft: {out["ft_hz"]}'
    assert abs(out['pd_w'] - 0.5) < 1e-9, f'pd: {out["pd_w"]}'
    assert out['tj_max_c'] == 150.0, f'tj: {out["tj_max_c"]}'
    assert out['polarity'] == 'NPN'
    assert out['package'] == 'TO-92'
