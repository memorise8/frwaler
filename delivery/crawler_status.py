"""Read-only per-crawler execution evidence; history never masks latest failure."""
from datetime import datetime, timezone


def result_state(job):
    if not job:
        return 'unrun'
    if job['status'] == 'failed':
        return 'failed'
    if job['status'] == 'cancelled':
        return 'cancelled'
    if job['status'] == 'done':
        return 'collected' if job['saved_count'] > 0 else 'empty'
    return 'unrun'


def collect_status(conn, site_id=None):
    columns = 'id, site_id, status, saved_count, error, created_at, started_at, finished_at'
    scope = 'site_id=%s' if site_id is not None else 'TRUE'
    args = (site_id,) if site_id is not None else ()
    latest = conn.execute(f'''SELECT DISTINCT ON (site_id) {columns} FROM crawl_jobs
        WHERE {scope} ORDER BY site_id, created_at DESC, id DESC''', args).fetchall()
    finished = conn.execute(f'''SELECT DISTINCT ON (site_id) {columns} FROM crawl_jobs
        WHERE {scope} AND status IN ('done','failed','cancelled')
        ORDER BY site_id, finished_at DESC NULLS LAST, id DESC''', args).fetchall()
    successes = conn.execute(f'''SELECT DISTINCT ON (site_id) {columns} FROM crawl_jobs
        WHERE {scope} AND status='done' AND saved_count>0
        ORDER BY site_id, finished_at DESC NULLS LAST, id DESC''', args).fetchall()
    active = conn.execute(f'''SELECT site_id,status,count(*) AS n FROM crawl_jobs
        WHERE {scope} AND status IN ('queued','running','cancelling') GROUP BY site_id,status''', args).fetchall()
    rows = {row['site_id']: {'site_id': row['site_id'], 'latest_job': dict(row),
            'last_result': None, 'last_success': None, 'active_counts': {}} for row in latest}
    for row in finished:
        rows.setdefault(row['site_id'], {'site_id': row['site_id'], 'latest_job': dict(row), 'last_result': None, 'last_success': None, 'active_counts': {}})
        rows[row['site_id']]['last_result'] = dict(row)
    for row in successes:
        rows.setdefault(row['site_id'], {'site_id': row['site_id'], 'latest_job': dict(row), 'last_result': None, 'last_success': None, 'active_counts': {}})
        rows[row['site_id']]['last_success'] = dict(row)
    for row in active:
        rows.setdefault(row['site_id'], {'site_id': row['site_id'], 'latest_job': None, 'last_result': None, 'last_success': None, 'active_counts': {}})
        rows[row['site_id']]['active_counts'][row['status']] = row['n']
    for row in rows.values():
        row['result_state'] = result_state(row['last_result'])
    return {'measured_at': datetime.now(timezone.utc).isoformat(),
            'heartbeat_available': False, 'items': list(rows.values())}
