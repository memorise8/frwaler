"""Reviewed document identity rules; unrelated crawler fallbacks stay intact."""
import re
from urllib.parse import parse_qs, urlsplit


_KEI_ELIB_MIDS = {
    'researchreport': 'a10101010000',
    'environmentalforum': 'a10102010000',
}


def kei_elib_id(url):
    """Document identity from KEI's published catalogue links, not row position."""
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'www.kei.re.kr'
            or parsed.path != '/elibList.es'):
        raise ValueError('invalid_kei_document_destination')
    query = parse_qs(parsed.query, keep_blank_values=True)
    ids = query.get('c_id', [])
    if len(ids) != 1 or not re.fullmatch('[0-9]+', ids[0]) or query.get('act') != ['view']:
        raise ValueError('invalid_kei_document_identifier')
    names = query.get('elibName', [])
    if not names:
        names = [name for name, mid in _KEI_ELIB_MIDS.items() if query.get('mid') == [mid]]
    if len(names) != 1 or names[0] not in _KEI_ELIB_MIDS or query.get('mid') != [_KEI_ELIB_MIDS[names[0]]]:
        raise ValueError('invalid_kei_catalogue_scope')
    return names[0], ids[0]


def find_kei_document(conn, site_id, post_number, meta_url, *, postgres=False):
    if site_id != 'kei-re-kr-eliblistes':
        return None
    wanted = kei_elib_id(meta_url)
    mark = '%s' if postgres else '?'
    # Historical imports can have original ordinal IDs; don't require replacing
    # them with c_id or rewriting their links. Resolve the source identity first
    # and let the backend return the exact existing row, including its seq_id,
    # file flags and translation references. Ambiguous histories remain held.
    # Bound returned rows without assuming historical post_number == c_id.
    # Percent-encoded URLs stay in the candidate set (including encoded query
    # parameter names); Python performs the final, exact identity comparison.
    rows = conn.execute(
        f'SELECT seq_id,post_number,meta_url FROM documents WHERE site_id={mark} '
        f'AND (post_number={mark} OR meta_url LIKE {mark} OR meta_url LIKE {mark} '
        f"OR meta_url LIKE {mark} OR meta_url LIKE {mark} ESCAPE '\\')",
        (site_id, post_number, '%c_id=' + wanted[1] + '&%', '%c_id=' + wanted[1],
         '%c_id=' + wanted[1] + '#%', '%\\%%'),
    ).fetchall()
    matches, conflicting_ids = [], []
    for row in rows:
        seq, stored_id, url = (row['seq_id'], row['post_number'], row['meta_url']) if hasattr(row, 'keys') else row
        try:
            key = kei_elib_id(url)
        except (TypeError, ValueError):
            query = parse_qs(urlsplit(url or '').query, keep_blank_values=True)
            if stored_id == post_number or wanted[1] in query.get('c_id', []):
                raise ValueError('kei_legacy_identity_requires_review')
            continue
        if key == wanted:
            matches.append(seq)
        elif stored_id == post_number or key[1] == wanted[1]:
            conflicting_ids.append(seq)
    if len(matches) > 1:
        raise ValueError('kei_identity_collision_requires_review')
    if matches:
        return matches[0]
    if conflicting_ids:
        raise ValueError('kei_reused_identifier_requires_review')
    return None


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
