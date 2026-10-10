"""Reviewed document identity rules; unrelated crawler fallbacks stay intact."""
import re
from urllib.parse import parse_qs, urlsplit


_KEI_ELIB_MIDS = {
    'researchreport': 'a10101010000',
    'environmentalforum': 'a10102010000',
}
_KEI_BOARD_MIDS = {
    '0028': 'a10102020000', '0031': 'a10102050000',
    '0032': 'a10102060000', '0033': 'a10102070200',
    '0054': 'a10102100000', '0057': 'a10102120000',
    '0075': 'a10102130000', '0081': 'a10102180000',
    '0009': 'a10307020000',
}


def is_cso_press_release_url(url):
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (parsed.scheme == 'https' and parsed.netloc == 'www.cso.ie'
            and not parsed.query and not parsed.fragment
            and re.fullmatch(r'/en/csolatestnews/pressreleases/\d{4}pressreleases/[^/]+/',
                             parsed.path) is not None)


def find_cso_document(conn, site_id, meta_url, *, postgres=False):
    """Keep historical IDs when the current CSO list supplies only a URL.

    Current month modules share an ID between several links. Link positions
    and a detail page's unrelated content spans cannot replace old IDs. Match
    only the exact published release URL, preserving every existing field.
    Multiple historical rows for that URL require review, never a merge.
    """
    if site_id != 'cso-ie-en' or not is_cso_press_release_url(meta_url):
        raise ValueError('invalid_cso_release_identity')
    mark = '%s' if postgres else '?'
    rows = conn.execute(
        f'SELECT seq_id FROM documents WHERE site_id={mark} AND meta_url={mark} LIMIT 2',
        (site_id, meta_url),
    ).fetchall()
    if len(rows) > 1:
        raise ValueError('cso_identity_collision_requires_review')
    if not rows:
        return None
    row = rows[0]
    return row['seq_id'] if hasattr(row, 'keys') else row[0]


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


def kei_document_id(url):
    """Keep catalogue IDs and each delivered board's IDs in separate scopes."""
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if parsed.path == '/elibList.es':
        if 'list_no' in query or 'bid' in query:
            raise ValueError('ambiguous_kei_catalogue_identifier')
        return kei_elib_id(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'www.kei.re.kr'
            or parsed.path != '/board.es'):
        raise ValueError('invalid_kei_document_destination')
    boards, ids = query.get('bid', []), query.get('list_no', [])
    if (len(boards) != 1 or boards[0] not in _KEI_BOARD_MIDS
            or query.get('mid') != [_KEI_BOARD_MIDS[boards[0]]]):
        raise ValueError('invalid_kei_board_scope')
    if (len(ids) != 1 or not re.fullmatch('[0-9]+', ids[0])
            or query.get('act') != ['view'] or 'c_id' in query or 'elibName' in query):
        raise ValueError('invalid_kei_board_identifier')
    return 'board-' + boards[0], ids[0]


def find_kei_document(conn, site_id, post_number, meta_url, *, postgres=False):
    if site_id != 'kei-re-kr-eliblistes':
        return None
    wanted = kei_document_id(meta_url)
    scoped = post_number == ':'.join(wanted)
    board = wanted[0].startswith('board-')
    # New boards must not insert a bare list_no into the old catalogue ID
    # namespace. Matching historical rows may still retain their original ID.
    known_scopes = set(_KEI_ELIB_MIDS) | {'board-' + bid for bid in _KEI_BOARD_MIDS}
    named_scope = post_number.split(':', 1)[0] if isinstance(post_number, str) and ':' in post_number else None
    if (board and not scoped) or (named_scope in known_scopes and not scoped):
        raise ValueError('kei_scoped_post_number_required')
    parameter = 'list_no' if board else 'c_id'
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
        (site_id, post_number, '%' + parameter + '=' + wanted[1] + '&%',
         '%' + parameter + '=' + wanted[1],
         '%' + parameter + '=' + wanted[1] + '#%', '%\\%%'),
    ).fetchall()
    matches, conflicting_ids = [], []
    for row in rows:
        seq, stored_id, url = (row['seq_id'], row['post_number'], row['meta_url']) if hasattr(row, 'keys') else row
        try:
            key = kei_document_id(url)
        except (TypeError, ValueError):
            query = parse_qs(urlsplit(url or '').query, keep_blank_values=True)
            if stored_id == post_number or wanted[1] in query.get(parameter, []):
                raise ValueError('kei_legacy_identity_requires_review')
            continue
        if key == wanted:
            matches.append(seq)
        elif stored_id == post_number or (not scoped and key[1] == wanted[1]):
            conflicting_ids.append(seq)
    if len(matches) > 1:
        raise ValueError('kei_identity_collision_requires_review')
    if scoped and conflicting_ids:
        raise ValueError('kei_scoped_identifier_collision_requires_review')
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
