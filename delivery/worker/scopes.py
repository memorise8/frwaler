"""Explicit observed source scopes, isolated from whole-site backfill progress."""
import json

from crawler.federal_checkpoint import digest, normalize_plan, source_hash_for_class

FEDERAL_SITE = 'federalregister-gov-documents'


def validate(site_id, definition, cls):
    if site_id != FEDERAL_SITE or not isinstance(definition, dict):
        raise ValueError('unsupported collection scope')
    if set(definition) != {'kind', 'observation_id', 'lower', 'upper', 'ranges', 'source_hash'}:
        raise ValueError('invalid collection scope fields')
    observation = definition['observation_id']
    if (definition['kind'] != 'federal_date_plan' or not isinstance(observation, str)
            or not observation.strip() or len(observation) > 200
            or definition['source_hash'] != source_hash_for_class(cls)):
        raise ValueError('scope observation or source version mismatch')
    if normalize_plan(definition['lower'], definition['upper'], definition['ranges']) != definition['ranges']:
        raise ValueError('noncanonical collection scope')


def load(conn, scope_id, site_id, cls=None):
    row = conn.execute('SELECT * FROM crawl_scopes WHERE scope_id=%s AND site_id=%s',
                       (scope_id, site_id)).fetchone()
    if row is None:
        raise ValueError('collection scope missing or belongs to another site')
    definition = row['definition']
    if digest({'site_id': site_id, 'definition': definition}) != scope_id:
        raise ValueError('collection scope was modified')
    if cls is not None:
        validate(site_id, definition, cls)
    return dict(row)


def enqueue_date_plan(conn, *, site_id, lower, upper, ranges, observation_id,
                      limit_n=None, requested_by=None, process_files=False):
    """Queue a reviewed date-count observation, never an implicit whole-site job.

    An observation ID identifies the supporting source evidence. Reusing it
    with the same plan/source resumes that observation; it is not a fresh scan.
    """
    from crawler.sites import CRAWLERS
    from delivery.worker import jobs
    if site_id != FEDERAL_SITE or site_id not in CRAWLERS:
        raise ValueError('unsupported collection scope')
    if limit_n is not None and (type(limit_n) is not int or limit_n <= 0):
        raise ValueError('scoped job limit must be positive or unlimited')
    cls = CRAWLERS[site_id]
    definition = dict(kind='federal_date_plan', observation_id=observation_id,
                      lower=lower, upper=upper, ranges=normalize_plan(lower, upper, ranges),
                      source_hash=source_hash_for_class(cls))
    validate(site_id, definition, cls)
    scope_id = digest({'site_id': site_id, 'definition': definition})
    conn.execute('''INSERT INTO crawl_scopes(scope_id,site_id,definition)
      VALUES(%s,%s,%s::jsonb) ON CONFLICT(scope_id) DO NOTHING''',
      (scope_id, site_id, json.dumps(definition)))
    return jobs.enqueue_job(conn, site_id, mode='backfill', limit_n=limit_n,
                            requested_by=requested_by, process_files=process_files, scope_id=scope_id)


def save_progress(conn, scope_id, cursor, items_delta):
    conn.execute('''UPDATE crawl_scopes SET cursor=%s::jsonb,items_done=items_done+%s,
      updated_at=clock_timestamp(),completed_at=NULL WHERE scope_id=%s''',
      (json.dumps(cursor), int(items_delta), scope_id))
    conn.commit()


def mark_complete(conn, scope_id):
    conn.execute('''UPDATE crawl_scopes SET completed_at=clock_timestamp(),
      updated_at=clock_timestamp() WHERE scope_id=%s''', (scope_id,))
    conn.commit()
