"""Content-free diagnostics for extracted text, not a fidelity certificate."""
import re
import unicodedata

MAX_TEXT_BYTES = 64 * 1024 * 1024
_CID = re.compile(r'\(cid:\d+\)')


def inspect_text(text, *, engine=None):
    marker_count = marker_characters = 0
    for match in _CID.finditer(text):
        marker_count += 1
        marker_characters += match.end() - match.start()
    counts = dict(characters=len(text), unmapped_glyph_markers=marker_count,
                  replacement_characters=text.count('\ufffd'),
                  private_use_characters=0, control_characters=0)
    for char in text:
        category = unicodedata.category(char)
        if category == 'Co':
            counts['private_use_characters'] += 1
        elif category == 'Cc' and char not in '\t\n\r\f':
            counts['control_characters'] += 1
    suspicious = (marker_characters
                  + counts['replacement_characters'] + counts['private_use_characters']
                  + counts['control_characters'])
    signals = [key for key, value in counts.items() if key != 'characters' and value]
    # A review hold preserves the source; it does not assume these characters
    # can be silently deleted or that a different extractor necessarily repairs them.
    severe = (suspicious >= 50 and suspicious / max(1, len(text)) >= 0.10
              or counts['control_characters'] >= 20
              and counts['control_characters'] / max(1, len(text)) >= 0.005)
    return dict(version=1, state='review_required' if severe else ('review' if signals else 'no_obvious_defects'),
                signals=signals, counts=counts, suspicious_characters=suspicious,
                engine=engine, fidelity_verified=False)


def inspect_file(path, *, engine=None):
    if path.stat().st_size > MAX_TEXT_BYTES:
        return dict(version=1, state='review_required', signals=['text_size_limit'],
                    engine=engine, fidelity_verified=False)
    try:
        return inspect_text(path.read_text(encoding='utf-8'), engine=engine)
    except UnicodeError:
        return dict(version=1, state='review_required', signals=['invalid_utf8'],
                    engine=engine, fidelity_verified=False)
