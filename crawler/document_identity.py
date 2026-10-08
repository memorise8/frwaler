"""Reviewed document identity rules; unrelated crawler fallbacks stay intact."""
import re
from urllib.parse import parse_qs, urlsplit


def krivet_id(url):
    parsed = urlsplit(url)
    if parsed.hostname not in ('krivet.re.kr', 'www.krivet.re.kr'):
        raise ValueError('invalid_krivet_document_host')
    values = parse_qs(parsed.query, keep_blank_values=True).get('pstNo', [])
    if len(values) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]+', values[0]):
        raise ValueError('missing_or_invalid_krivet_pstNo')
    return values[0]


def find_krivet_document(conn, site_id, post_number, meta_url, *, postgres=False):
    if site_id != 'krivet-re-kr':
        return None
    wanted = krivet_id(meta_url)
    if post_number != wanted:
        raise ValueError('krivet_post_number_must_match_pstNo')
    # Match the stable ID across query order/menu changes. Old incorrect rows
    # must be repaired before collection resumes; silently creating a new row
    # would detach translations and files from the existing seq_id.
    marker = '%s' if postgres else '?'
    rows = conn.execute(
        f'SELECT seq_id, post_number, meta_url FROM documents WHERE site_id={marker}',
        (site_id,),
    ).fetchall()
    matches = []
    for row in rows:
        seq, stored_id, url = (row['seq_id'], row['post_number'], row['meta_url']) if postgres else row
        try:
            same = krivet_id(url) == wanted
        except ValueError:
            same = False
        if same:
            matches.append((seq, stored_id))
    if len(matches) > 1:
        raise ValueError('krivet_identity_collision_requires_review')
    if matches:
        if matches[0][1] != wanted:
            raise ValueError('krivet_identity_repair_required')
        return matches[0][0]
    return None
