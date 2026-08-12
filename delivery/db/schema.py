# -*- coding: utf-8 -*-
"""납품 전용 Postgres 스키마 (documents/sites 는 crawler.db_pg 담당)."""
from __future__ import annotations


def init_delivery_schema(conn) -> None:
    """Create delivery control tables if absent (idempotent). Phase 0: crawl_jobs."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crawl_jobs (
            id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            site_id       TEXT NOT NULL,
            mode          TEXT NOT NULL DEFAULT 'incremental',
            status        TEXT NOT NULL DEFAULT 'queued',
            limit_n       INTEGER,
            saved_count   INTEGER NOT NULL DEFAULT 0,
            error         TEXT,
            requested_by  TEXT,
            created_at    TIMESTAMPTZ DEFAULT now(),
            started_at    TIMESTAMPTZ,
            finished_at   TIMESTAMPTZ,
            retry_of      BIGINT REFERENCES crawl_jobs(id),
            retried_by    BIGINT REFERENCES crawl_jobs(id),
            cancel_requested_at TIMESTAMPTZ
        )
        """
    )
    # Upgrade Phase 0 databases without requiring a destructive migration.
    conn.execute("ALTER TABLE crawl_jobs ADD COLUMN IF NOT EXISTS retry_of BIGINT REFERENCES crawl_jobs(id)")
    conn.execute("ALTER TABLE crawl_jobs ADD COLUMN IF NOT EXISTS retried_by BIGINT REFERENCES crawl_jobs(id)")
    conn.execute("ALTER TABLE crawl_jobs ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_retry_of ON crawl_jobs(retry_of) WHERE retry_of IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON crawl_jobs(status, created_at)"
    )
    conn.execute("""CREATE TABLE IF NOT EXISTS crawl_job_logs(
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,job_id BIGINT NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
      level TEXT NOT NULL CHECK(level IN('info','warning','error')),event TEXT NOT NULL,message TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crawl_job_logs_job ON crawl_job_logs(job_id,id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS crawl_schedules(
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,site_id TEXT NOT NULL REFERENCES sites(site_id),
      interval_hours INTEGER NOT NULL CHECK(interval_hours BETWEEN 1 AND 8760),mode TEXT NOT NULL DEFAULT 'incremental',
      limit_n INTEGER CHECK(limit_n BETWEEN 1 AND 1000),enabled BOOLEAN NOT NULL DEFAULT true,
      next_run_at TIMESTAMPTZ NOT NULL,last_run_at TIMESTAMPTZ,created_by TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),UNIQUE(site_id))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crawl_schedules_due ON crawl_schedules(enabled,next_run_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS translation_batches (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            provider TEXT NOT NULL CHECK (provider IN ('external','internal')),
            model_version TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            tasks JSONB NOT NULL,
            filters JSONB NOT NULL DEFAULT '{}'::jsonb,
            requested_limit INTEGER NOT NULL CHECK (requested_limit BETWEEN 1 AND 1000),
            target_count INTEGER NOT NULL DEFAULT 0,
            created_count INTEGER NOT NULL DEFAULT 0,
            existing_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','completed','paused','cancelled')),
            stop_reason TEXT,
            requested_by TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS translation_jobs (
            id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            seq_id             BIGINT NOT NULL REFERENCES documents(seq_id) ON DELETE CASCADE,
            source_field       TEXT NOT NULL CHECK (source_field IN ('title', 'description')),
            task_type          TEXT NOT NULL DEFAULT 'translate'
                               CHECK (task_type IN ('translate','summarize')),
            source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint)=64),
            source_lang        TEXT NOT NULL DEFAULT 'unknown',
            target_locale      TEXT NOT NULL DEFAULT 'ko-KR',
            provider           TEXT NOT NULL CHECK (provider IN ('external', 'internal')),
            model_version      TEXT NOT NULL,
            prompt_version     TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'pending'
                               CHECK (status IN ('pending','running','completed','failed','skipped','cancelled')),
            attempts           INTEGER NOT NULL DEFAULT 0,
            max_attempts       INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
            next_attempt_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            claimed_at         TIMESTAMPTZ,
            finished_at        TIMESTAMPTZ,
            error_code         TEXT,
            error_message      TEXT,
            input_chars        INTEGER,
            output_chars       INTEGER,
            latency_ms         INTEGER,
            requested_by       TEXT,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            batch_id           BIGINT REFERENCES translation_batches(id),
            worker_id          TEXT,
            lease_expires_at   TIMESTAMPTZ,
            UNIQUE (seq_id, source_field, target_locale, source_fingerprint,
                    provider, model_version, prompt_version)
        )
        """
    )
    conn.execute("ALTER TABLE translation_jobs ADD COLUMN IF NOT EXISTS task_type TEXT NOT NULL DEFAULT 'translate'")
    conn.execute("ALTER TABLE translation_jobs ADD COLUMN IF NOT EXISTS batch_id BIGINT REFERENCES translation_batches(id)")
    conn.execute("ALTER TABLE translation_jobs ADD COLUMN IF NOT EXISTS worker_id TEXT")
    conn.execute("ALTER TABLE translation_jobs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ")
    conn.execute("""DO $$ BEGIN
      ALTER TABLE translation_jobs ADD CONSTRAINT translation_jobs_task_type_check
        CHECK (task_type IN ('translate','summarize'));
    EXCEPTION WHEN duplicate_object THEN NULL; END $$""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_translation_jobs_queue ON translation_jobs(status, next_attempt_at, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_translation_jobs_batch ON translation_jobs(batch_id,status)")
    conn.execute("""CREATE TABLE IF NOT EXISTS translation_job_attempts (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      job_id BIGINT NOT NULL REFERENCES translation_jobs(id) ON DELETE CASCADE,
      batch_id BIGINT REFERENCES translation_batches(id) ON DELETE CASCADE,
      attempt_no INTEGER NOT NULL,
      worker_id TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('running','completed','failed','skipped','stale_lease')),
      error_code TEXT,
      retryable BOOLEAN,
      prompt_tokens INTEGER,
      completion_tokens INTEGER,
      finish_reason TEXT,
      input_chars INTEGER,
      output_chars INTEGER,
      latency_ms INTEGER,
      started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      finished_at TIMESTAMPTZ,
      UNIQUE(job_id,attempt_no)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_translation_attempts_recent ON translation_job_attempts(finished_at DESC,status)")
    conn.execute("""CREATE TABLE IF NOT EXISTS translation_worker_heartbeats (
      worker_id TEXT PRIMARY KEY,status TEXT NOT NULL,last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      current_job_id BIGINT REFERENCES translation_jobs(id) ON DELETE SET NULL,
      started_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS translation_system_observations (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,worker_id TEXT NOT NULL,
      metric TEXT NOT NULL,value_numeric DOUBLE PRECISION,value_boolean BOOLEAN,
      observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      CHECK ((value_numeric IS NOT NULL)::int + (value_boolean IS NOT NULL)::int = 1)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_translation_observations_metric ON translation_system_observations(metric,observed_at DESC)")
    conn.execute("""
      CREATE TABLE IF NOT EXISTS document_summaries (
        summary_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        seq_id BIGINT NOT NULL REFERENCES documents(seq_id) ON DELETE CASCADE,
        target_locale TEXT NOT NULL DEFAULT 'ko-KR',
        source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint)=64),
        model_version TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('completed','failed')),
        summary_text TEXT NOT NULL,
        key_points JSONB NOT NULL DEFAULT '[]'::jsonb,
        institutions JSONB NOT NULL DEFAULT '[]'::jsonb,
        source_facts JSONB NOT NULL DEFAULT '{}'::jsonb,
        completed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (seq_id,target_locale,source_fingerprint,model_version,prompt_version)
      )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_document_summaries_current ON document_summaries(seq_id,target_locale,state,completed_at DESC)")
    conn.execute("""
      CREATE TABLE IF NOT EXISTS document_summary_quality (
        summary_id BIGINT PRIMARY KEY REFERENCES document_summaries(summary_id) ON DELETE CASCADE,
        gate_version TEXT NOT NULL,
        decision TEXT NOT NULL CHECK (decision IN ('auto_approved','review_recommended','rejected')),
        score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
        reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
        checks JSONB NOT NULL DEFAULT '{}'::jsonb,
        evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
        evaluated_at TIMESTAMPTZ NOT NULL DEFAULT now()
      )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_summary_quality_decision ON document_summary_quality(decision,evaluated_at DESC)")
    conn.commit()
