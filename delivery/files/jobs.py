"""Per-crawl file tasks. File work never changes the metadata job's outcome."""
from __future__ import annotations

import uuid


def backfill(conn, site_id, *, after_seq=0, limit=100, include_existing=False, dry_run=True,
             requested_by=None, collected_from=None, collected_to=None):
    """Explicit bounded sweep of persisted metadata; no crawler invocation.

    Concurrent requests for the same site serialize selection and enqueue. A
    cursor is returned for rechecks; failed queued tasks are retried separately.
    """
    if not 1 <= limit <= 1000 or after_seq < 0 or (collected_from and collected_to and collected_from > collected_to):
        raise ValueError('invalid file backfill bounds')
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('file-backfill:' + site_id,))
    rows = conn.execute("""SELECT d.seq_id FROM documents d
        WHERE d.site_id=%s AND d.seq_id>%s AND
          (%s::date IS NULL OR d.collected_at >= (%s::date::timestamp AT TIME ZONE 'UTC')) AND
          (%s::date IS NULL OR d.collected_at < ((%s::date + 1)::timestamp AT TIME ZONE 'UTC')) AND
          (%s OR ((COALESCE(d.pdf_downloaded,0)=0 OR COALESCE(d.text_extracted,0)=0)
                    AND (NULLIF(trim(d.pdf_url),'') IS NOT NULL OR d.pdf_downloaded=1)))
          AND NOT EXISTS (SELECT 1 FROM document_file_jobs f WHERE f.seq_id=d.seq_id
                          AND f.status IN ('queued','running'))
        ORDER BY d.seq_id LIMIT %s""", (site_id, after_seq, collected_from, collected_from,
                                       collected_to, collected_to, include_existing, limit)).fetchall()
    seqs = [r['seq_id'] for r in rows]
    result = {'selected':len(seqs), 'after_seq':after_seq, 'next_after_seq':max(seqs, default=after_seq),
              'job_id':None, 'dry_run':dry_run, 'selection':'metadata_flags' if not include_existing else 'recheck_files',
              'has_more_unverified':len(seqs) == limit}
    if not dry_run and seqs:
        row = conn.execute("""INSERT INTO crawl_jobs(site_id,mode,status,process_files,job_type,
                         saved_count,requested_by,finished_at)
                         VALUES(%s,'full','done',true,'file_backfill',0,%s,now()) RETURNING id""",
                           (site_id,requested_by)).fetchone()
        result['job_id'] = row['id']
        with conn.cursor() as cursor:
            cursor.executemany('INSERT INTO document_file_jobs(crawl_job_id,seq_id) VALUES(%s,%s) ON CONFLICT DO NOTHING',
                               [(row['id'], seq) for seq in seqs])
        conn.execute("""INSERT INTO crawl_job_logs(job_id,level,event,message)
                         VALUES(%s,'info','file_backfill','기존 문서의 파일 처리 작업이 등록되었습니다.')""", (row['id'],))
    conn.commit()
    return result


def enqueue(conn, crawl_job_id, seq_id):
    conn.execute("""INSERT INTO document_file_jobs(crawl_job_id,seq_id)
        SELECT j.id,d.seq_id FROM crawl_jobs j CROSS JOIN documents d
        WHERE j.id=%s AND d.seq_id=%s AND j.process_files
          AND j.status IN ('running','done','failed')
        ON CONFLICT(crawl_job_id,seq_id) DO NOTHING""", (crawl_job_id, seq_id))
    conn.commit()


def enqueue_new(conn, crawl_job_id, site_id, after_seq):
    """Fallback for crawlers that insert directly instead of using BaseCrawler."""
    conn.execute("""INSERT INTO document_file_jobs(crawl_job_id,seq_id)
        SELECT j.id,d.seq_id FROM documents d CROSS JOIN crawl_jobs j
        WHERE j.id=%s AND d.site_id=%s AND d.seq_id>%s AND j.process_files
          AND j.status IN ('running','done','failed')
        ON CONFLICT(crawl_job_id,seq_id) DO NOTHING""", (crawl_job_id, site_id, after_seq))
    conn.commit()


def claim(conn, *, lease_seconds=300):
    token = str(uuid.uuid4())
    row = conn.execute("""UPDATE document_file_jobs SET status='running',token=%s,
        lease_expires_at=now()+make_interval(secs=>%s),attempts=attempts+1,updated_at=now()
        WHERE id=(SELECT f.id FROM document_file_jobs f JOIN crawl_jobs j ON j.id=f.crawl_job_id
          WHERE f.status='queued' AND f.next_attempt_at<=now()
            AND j.status IN ('done','failed')
          ORDER BY f.next_attempt_at,f.id FOR UPDATE OF f SKIP LOCKED LIMIT 1)
        RETURNING *""", (token, lease_seconds)).fetchone()
    conn.commit()
    return dict(row) if row else None


def recover(conn):
    conn.execute("""UPDATE document_file_jobs SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'queued' END,
        token=NULL,lease_expires_at=NULL,error='worker_interrupted',updated_at=now(),
        next_attempt_at=now()+interval '30 seconds'
        WHERE status='running' AND lease_expires_at<now()""")
    conn.execute("""UPDATE document_file_jobs f SET status='cancelled',error=NULL,updated_at=now()
        FROM crawl_jobs j WHERE j.id=f.crawl_job_id AND j.status='cancelled' AND f.status='queued'""")
    conn.commit()


def owns(conn, task):
    row = conn.execute("""SELECT id FROM document_file_jobs WHERE id=%s AND token=%s
        AND status='running' AND lease_expires_at>now() FOR UPDATE""", (task['id'], task['token'])).fetchone()
    return row is not None


def finish(conn, task, *, status, pdf_status, text_status, error=None, retry=False):
    if retry and task['attempts'] < 3:
        status = 'queued'
    conn.execute("""UPDATE document_file_jobs SET status=%s,pdf_status=%s,text_status=%s,
        error=%s,token=NULL,lease_expires_at=NULL,updated_at=now(),
        next_attempt_at=now()+make_interval(secs=>%s)
        WHERE id=%s AND token=%s AND status='running'""",
        (status, pdf_status, text_status, error, 30 * (2 ** min(task['attempts'], 3)), task['id'], task['token']))
    conn.commit()


def summary(conn, job_id):
    rows = conn.execute("""SELECT status,count(*) AS n FROM document_file_jobs
        WHERE crawl_job_id=%s GROUP BY status""", (job_id,)).fetchall()
    counts = {key: 0 for key in ('queued','running','done','failed','skipped','cancelled')}
    counts.update({r['status']: int(r['n']) for r in rows})
    stages = conn.execute("""SELECT count(*) FILTER(WHERE pdf_status='done') AS pdf_done,
        count(*) FILTER(WHERE text_status='done') AS text_done
        FROM document_file_jobs WHERE crawl_job_id=%s""", (job_id,)).fetchone()
    recent = conn.execute("""SELECT seq_id,status,pdf_status,text_status,error,attempts,updated_at
        FROM document_file_jobs WHERE crawl_job_id=%s
        ORDER BY CASE WHEN status='failed' THEN 0 ELSE 1 END,updated_at DESC,id DESC LIMIT 20""", (job_id,)).fetchall()
    heartbeat = conn.execute("SELECT last_seen_at>now()-interval '120 seconds' AS alive FROM file_worker_heartbeat WHERE id=1").fetchone()
    return {**counts, 'total': sum(counts.values()), 'pdf_done': stages['pdf_done'],
            'text_done': stages['text_done'], 'worker_alive': bool(heartbeat and heartbeat['alive']),
            'items': [dict(r) for r in recent]}


def retry_failed(conn, job_id):
    rows = conn.execute("""UPDATE document_file_jobs SET status='queued',attempts=0,error=NULL,
        next_attempt_at=now(),updated_at=now() WHERE crawl_job_id=%s AND status IN ('failed','cancelled') RETURNING id""", (job_id,)).fetchall()
    conn.commit()
    return len(rows)


def cancel(conn, job_id):
    # Invalidates tokens; an in-flight subprocess cannot publish its staged files.
    rows = conn.execute("""UPDATE document_file_jobs SET status='cancelled',token=NULL,
        lease_expires_at=NULL,error=NULL,updated_at=now()
        WHERE crawl_job_id=%s AND status IN ('queued','running') RETURNING id""", (job_id,)).fetchall()
    conn.commit()
    return len(rows)
