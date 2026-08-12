#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite(libertree.db) → 납품 Postgres 일회성 이관.

설계 문서의 "선택적 데이터 임포트 경로"(§1) 구현. 우리 운영 SQLite 를 읽기 전용으로
열어 납품 Postgres 로 옮긴다.

핵심 제약
---------
* **seq_id 보존**: PDF 블롭 경로가 seq_id 에서 파생되므로(`libertree/AAAA/BBBB/{12자리}.pdf`)
  번호가 바뀌면 30만 개 파일이 전부 짝을 잃는다. COPY 는 GENERATED ALWAYS 아이덴티티
  컬럼에도 값을 그대로 넣으므로 이를 이용한다.
* **시퀀스 이어붙이기**: 이관 후 setval 로 다음 번호를 max+1 로 맞춘다. 이게 빠지면
  클라이언트가 수집을 시작할 때 1번부터 재발급되어 기존 행/파일과 충돌한다.
* **원본 무손상**: SQLite 는 `mode=ro` 로만 연다.

psycopg 의존성 없이 동작한다 — 데이터는 `psql`의 `COPY ... FROM STDIN`(text 포맷)으로
스트리밍한다. 기본 실행기는 `docker compose exec -T postgres psql`.

사용:
    python3 delivery/scripts/migrate_sqlite_to_pg.py            # 전체 이관
    python3 delivery/scripts/migrate_sqlite_to_pg.py --limit 1000   # 소규모 리허설
    python3 delivery/scripts/migrate_sqlite_to_pg.py --verify-only  # 검증만
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SQLITE = REPO_ROOT / "libertree-app" / "data" / "libertree.db"
DEFAULT_BLOB_ROOT = REPO_ROOT / "libertree"

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------
# sites/documents 정의는 crawler/db_pg.py 의 init_db() 와 **동일해야 한다**.
# 전부 IF NOT EXISTS 이므로 나중에 BE 가 init_db() 를 불러도 무해한 no-op 이 된다.
DDL_TABLES = """
CREATE TABLE IF NOT EXISTS sites (
    site_id     TEXT PRIMARY KEY,
    site_name   TEXT NOT NULL,
    site_url    TEXT NOT NULL,
    sheet       TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS documents (
    seq_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    collected_at      TIMESTAMPTZ DEFAULT now(),
    site_id           TEXT NOT NULL REFERENCES sites(site_id),
    post_number       TEXT,
    meta_url          TEXT NOT NULL,
    title             TEXT NOT NULL,
    published_date    TEXT,
    listed_date       TEXT,
    authors           TEXT,
    publisher         TEXT,
    journal           TEXT,
    pdf_url           TEXT,
    keywords          TEXT,
    abstract          TEXT,
    original_filename TEXT,
    pdf_downloaded    INTEGER DEFAULT 0,
    text_extracted    INTEGER DEFAULT 0,
    pdf_size_bytes    BIGINT,
    pdf_sha256        TEXT,
    summary           TEXT,
    summary_model     TEXT,
    summary_at        TIMESTAMPTZ
);

-- 이하 3개는 번역 파이프라인 테이블(설계 §4.1 "존재 시 이식").
CREATE TABLE IF NOT EXISTS document_lang (
    seq_id BIGINT PRIMARY KEY REFERENCES documents(seq_id) ON DELETE CASCADE,
    lang   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_translations (
    translation_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    seq_id             BIGINT NOT NULL REFERENCES documents(seq_id) ON DELETE RESTRICT,
    source_field       TEXT NOT NULL CHECK (source_field IN ('title', 'description')),
    target_locale      TEXT NOT NULL CHECK (target_locale ~ '^[a-z][a-z]-[A-Z][A-Z]$'),
    source_fingerprint TEXT NOT NULL CHECK (length(source_fingerprint) = 64),
    model_version      TEXT NOT NULL CHECK (length(model_version) BETWEEN 1 AND 128),
    prompt_version     TEXT NOT NULL CHECK (length(prompt_version) BETWEEN 1 AND 128),
    state              TEXT NOT NULL CHECK (state IN ('pending','running','completed','failed','skipped')),
    translation_text   TEXT,
    attempts           INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error_code    TEXT CHECK (last_error_code IS NULL OR (length(last_error_code) BETWEEN 1 AND 64 AND last_error_code ~ '^[a-z0-9_]*$')),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at         TIMESTAMPTZ,
    completed_at       TIMESTAMPTZ,
    CHECK ((state <> 'completed') OR translation_text IS NOT NULL),
    CHECK ((state <> 'failed') OR last_error_code IS NOT NULL),
    UNIQUE (seq_id, source_field, target_locale, source_fingerprint, model_version, prompt_version)
);

CREATE TABLE IF NOT EXISTS document_anomaly (
    seq_id        BIGINT PRIMARY KEY REFERENCES documents(seq_id) ON DELETE CASCADE,
    verdict       TEXT NOT NULL,
    model_version TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# 적재 후에 만든다(적재 중 인덱스 유지는 느리다).
# 이름/정의는 crawler/db_pg.py init_db() 와 일치시켜 재실행 시 no-op 이 되게 한다.
DDL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_doc_site_post ON documents(site_id, post_number);
CREATE INDEX IF NOT EXISTS idx_doc_meta_url  ON documents(meta_url);
CREATE INDEX IF NOT EXISTS idx_doc_collected ON documents(collected_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_dedup ON documents(site_id, post_number, meta_url);
CREATE INDEX IF NOT EXISTS idx_tr_seq_field ON document_translations(seq_id, source_field);
CREATE INDEX IF NOT EXISTS idx_tr_state     ON document_translations(state);
CREATE INDEX IF NOT EXISTS idx_lang_lang    ON document_lang(lang);
"""

# 전문검색.
#
# 원본은 SQLite FTS5 `tokenize='trigram'`(부분일치·CJK 대응). Postgres 에 1:1 대응은
# 없어 두 축으로 나눠 재현한다:
#   1) to_tsvector('simple', ...)  — 형태소 분석 없는 토큰 검색. 다국어 혼재(en/fr/el/
#      zh/ja …)라 언어별 분석기를 쓰면 언어판별이 안 된 문서가 누락되므로 'simple'.
#   2) pg_trgm GIN on title        — 제목 부분일치. abstract 까지 trigram 을 걸면 인덱스가
#      과대해져 Phase 1 에서 결정하도록 남긴다.
#
# FTS_MAX_CHARS: tsvector 는 1,048,575 바이트가 상한이다. abstract 에 PDF 본문 전체가
# 들어간 문서가 일부 있어 그대로는 상한을 넘는다(관측 최대 1.15MB). UTF-8 최대 4바이트/자
# 기준으로 상한을 넘지 않는 25만 자로 자른다 — 검색 색인 목적에는 충분하고, 원본
# abstract 컬럼 자체는 자르지 않으므로 본문은 그대로 보존된다.
DDL_FTS = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;

ALTER TABLE documents ADD COLUMN IF NOT EXISTS fts tsvector
    GENERATED ALWAYS AS (
        to_tsvector('simple', left(
            coalesce(title, '') || ' ' || coalesce(abstract, '') || ' ' ||
            coalesce(summary, '') || ' ' || coalesce(keywords, ''), 250000))
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_doc_fts       ON documents USING GIN (fts);
CREATE INDEX IF NOT EXISTS idx_doc_title_trgm ON documents USING GIN (title gin_trgm_ops);
"""

# ---------------------------------------------------------------------------
# 이관 대상 (적재 순서 = FK 의존 순서)
# ---------------------------------------------------------------------------
DOCUMENT_COLUMNS = (
    "seq_id", "collected_at", "site_id", "post_number", "meta_url",
    "title", "published_date", "listed_date", "authors", "publisher", "journal",
    "pdf_url", "keywords", "abstract", "original_filename",
    "pdf_downloaded", "text_extracted", "pdf_size_bytes", "pdf_sha256",
    "summary", "summary_model", "summary_at",
)
TRANSLATION_COLUMNS = (
    "translation_id", "seq_id", "source_field", "target_locale", "source_fingerprint",
    "model_version", "prompt_version", "state", "translation_text", "attempts",
    "last_error_code", "created_at", "updated_at", "claimed_at", "completed_at",
)

# (PG 테이블, 컬럼, SQLite 테이블, seq_id 컬럼(자식 테이블 범위 제한용), 정렬 컬럼)
#
# seq_col 이 있는 테이블은 `--limit` 리허설에서 documents 와 같은 seq_id 경계로 잘라야
# 한다. 그러지 않으면 자식 행이 아직 적재되지 않은 문서를 참조해 FK 위반이 난다.
TABLES = (
    ("sites", ("site_id", "site_name", "site_url", "sheet", "created_at"),
     "sites", None, "site_id"),
    ("documents", DOCUMENT_COLUMNS,
     "documents", "seq_id", "seq_id"),
    ("document_lang", ("seq_id", "lang"),
     "document_lang", "seq_id", "seq_id"),
    ("document_translations", TRANSLATION_COLUMNS,
     "document_translations", "seq_id", "translation_id"),
    ("document_anomaly", ("seq_id", "verdict", "model_version", "created_at"),
     "document_anomaly", "seq_id", "seq_id"),
)


def source_query(columns, table, seq_col, order_by, boundary: int | None) -> str:
    """SQLite 원본 조회 SQL. boundary 가 주어지면 seq_id <= boundary 로 제한한다."""
    where = f" WHERE {seq_col} <= {boundary}" if (boundary and seq_col) else ""
    return f"SELECT {', '.join(columns)} FROM {table}{where} ORDER BY {order_by}"

# 이관 후 다음 번호를 맞춰야 하는 아이덴티티 컬럼.
SEQUENCES = (("documents", "seq_id"), ("document_translations", "translation_id"))


# ---------------------------------------------------------------------------
# psql 실행기
# ---------------------------------------------------------------------------
class Psql:
    """`psql`을 감싼 실행기. 기본은 delivery compose 의 postgres 컨테이너."""

    def __init__(self, argv: list[str]):
        self.argv = argv

    def run(self, sql: str) -> str:
        """SQL 실행 후 stdout 반환."""
        proc = subprocess.run(self.argv, input=sql, capture_output=True,
                              text=True, cwd=str(COMPOSE_DIR))
        if proc.returncode != 0:
            raise RuntimeError(f"psql 실패:\n{proc.stderr.strip()}")
        return proc.stdout

    def scalar(self, sql: str) -> str:
        out = subprocess.run(self.argv + ["-tA"], input=sql, capture_output=True,
                             text=True, cwd=str(COMPOSE_DIR))
        if out.returncode != 0:
            raise RuntimeError(f"psql 실패:\n{out.stderr.strip()}")
        return out.stdout.strip()

    def copy_in(self, table: str, columns: tuple[str, ...], rows, progress_every: int = 50_000) -> int:
        """COPY ... FROM STDIN (text 포맷) 으로 rows 를 스트리밍 적재."""
        col_sql = ", ".join(columns)
        proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd=str(COMPOSE_DIR))
        n = 0
        try:
            assert proc.stdin is not None
            proc.stdin.write(f"COPY {table} ({col_sql}) FROM STDIN;\n")
            for row in rows:
                proc.stdin.write("\t".join(_esc(v) for v in row))
                proc.stdin.write("\n")
                n += 1
                if progress_every and n % progress_every == 0:
                    print(f"      {n:,}", flush=True)
            proc.stdin.write("\\.\n")
            proc.stdin.close()
        except BrokenPipeError:
            pass
        _, stderr = proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"COPY {table} 실패 ({n:,}행 시도):\n{stderr.strip()}")
        return n


NUL_STRIPPED = 0  # NUL 바이트를 제거한 값의 개수(적재 후 보고)


def _esc(v) -> str:
    """COPY text 포맷 이스케이프. NULL 은 \\N.

    NUL 바이트(0x00)는 제거한다. SQLite 는 BLOB/TEXT 에 NUL 을 담을 수 있지만
    Postgres `text` 는 담을 수 없다(저장 자체가 불가). 원본 abstract 일부에 PDF 텍스트
    추출 과정에서 섞여 들어간 NUL 이 있어, 제거하지 않으면 COPY 가
    "extra data after last expected column" 로 실패한다.
    """
    global NUL_STRIPPED
    if v is None:
        return r"\N"
    if not isinstance(v, str):
        v = str(v)
    if "\x00" in v:
        NUL_STRIPPED += 1
        v = v.replace("\x00", "")
    return (v.replace("\\", "\\\\")
             .replace("\t", "\\t")
             .replace("\n", "\\n")
             .replace("\r", "\\r"))


COMPOSE_DIR = Path(__file__).resolve().parent.parent  # delivery/


# ---------------------------------------------------------------------------
# 단계
# ---------------------------------------------------------------------------
def open_sqlite(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise SystemExit(f"SQLite 원본을 찾을 수 없음: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.text_factory = str
    return con


def seq_boundary(con: sqlite3.Connection, limit: int | None) -> int | None:
    """리허설용 seq_id 경계. documents 를 seq_id 순 limit 건으로 자를 때의 마지막 seq_id."""
    if not limit:
        return None
    row = con.execute(
        "SELECT seq_id FROM documents ORDER BY seq_id LIMIT 1 OFFSET ?", (limit - 1,)
    ).fetchone()
    if row is None:  # limit 이 전체 건수보다 큼 → 전량
        return None
    return row[0]


def load_tables(psql: Psql, con: sqlite3.Connection, boundary: int | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table, columns, src_table, seq_col, order_by in TABLES:
        sql = source_query(columns, src_table, seq_col, order_by, boundary)
        t0 = time.time()
        print(f"  [{table}] 적재 중…", flush=True)
        cur = con.execute(sql)
        n = psql.copy_in(table, columns, cur)
        counts[table] = n
        print(f"  [{table}] {n:,}행  ({time.time() - t0:.1f}초)", flush=True)
    if NUL_STRIPPED:
        print(f"  * NUL 바이트를 제거한 값 {NUL_STRIPPED:,}개 "
              f"(Postgres text 는 NUL 저장 불가 — 해당 문자만 제거, 나머지 본문은 보존)")
    return counts


def fix_sequences(psql: Psql) -> None:
    """다음 발급 번호를 max+1 로 맞춘다.

    이게 빠지면 클라이언트가 수집을 시작할 때 1번부터 재발급되어 이관된 행과 PK 충돌,
    나아가 같은 이름의 PDF 블롭을 덮어쓴다.
    """
    for table, column in SEQUENCES:
        nxt = psql.scalar(
            f"SELECT setval(pg_get_serial_sequence('{table}', '{column}'), "
            f"GREATEST(COALESCE((SELECT max({column}) FROM {table}), 0), 1), true) + 1;"
        )
        print(f"  {table}.{column} → 다음 번호 {int(nxt):,}", flush=True)


def verify(psql: Psql, con: sqlite3.Connection, blob_root: Path, boundary: int | None) -> bool:
    ok = True
    print("\n=== 검증 ===")

    print("[1] 건수 대조")
    for table, columns, src_table, seq_col, order_by in TABLES:
        where = f" WHERE {seq_col} <= {boundary}" if (boundary and seq_col) else ""
        src = con.execute(f"SELECT count(*) FROM {src_table}{where}").fetchone()[0]
        dst = int(psql.scalar(f"SELECT count(*) FROM {table};"))
        mark = "OK " if src == dst else "불일치"
        if src != dst:
            ok = False
        print(f"  {mark} {table:<24} sqlite={src:>9,}  pg={dst:>9,}")

    print("[2] seq_id 보존")
    where = f" WHERE seq_id <= {boundary}" if boundary else ""
    src_max = con.execute(f"SELECT max(seq_id) FROM documents{where}").fetchone()[0]
    dst_max = int(psql.scalar("SELECT max(seq_id) FROM documents;"))
    same = src_max == dst_max
    ok = ok and same
    print(f"  {'OK ' if same else '불일치'} max(seq_id) sqlite={src_max:,} pg={dst_max:,}")

    print("[3] 체크섬 대조 (documents seq_id 합)")
    src_sum = con.execute(f"SELECT coalesce(sum(seq_id),0) FROM documents{where}").fetchone()[0]
    dst_sum = int(psql.scalar("SELECT coalesce(sum(seq_id),0) FROM documents;"))
    same = src_sum == dst_sum
    ok = ok and same
    print(f"  {'OK ' if same else '불일치'} sum(seq_id) sqlite={src_sum:,} pg={dst_sum:,}")

    print("[4] seq_id ↔ PDF 파일 대응 (표본 200건)")
    rows = psql.scalar(
        "SELECT seq_id FROM documents WHERE pdf_downloaded=1 ORDER BY seq_id LIMIT 200;"
    ).split()
    missing = [s for s in rows if not (blob_root / f"{int(s):012d}"[:4] /
                                       f"{int(s):012d}"[4:8] / f"{int(s):012d}.pdf").is_file()]
    if rows:
        if missing:
            ok = False
            print(f"  불일치 {len(missing)}/{len(rows)} 건 파일 없음 (예: {missing[:3]})")
        else:
            print(f"  OK  {len(rows)}건 전부 파일 존재")
    else:
        print("  (pdf_downloaded=1 행이 없어 생략)")

    print("[5] 참조 무결성")
    for sql, label in (
        ("SELECT count(*) FROM documents d LEFT JOIN sites s USING (site_id) WHERE s.site_id IS NULL",
         "documents.site_id → sites"),
        ("SELECT count(*) FROM document_translations t LEFT JOIN documents d USING (seq_id) WHERE d.seq_id IS NULL",
         "translations.seq_id → documents"),
    ):
        n = int(psql.scalar(sql + ";"))
        if n:
            ok = False
        print(f"  {'OK ' if n == 0 else '불일치'} {label}: 고아 {n}건")

    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="SQLite → 납품 Postgres 이관")
    ap.add_argument("--sqlite", type=Path, default=DEFAULT_SQLITE)
    ap.add_argument("--blob-root", type=Path, default=DEFAULT_BLOB_ROOT)
    ap.add_argument("--pg-user", default=os.environ.get("POSTGRES_USER", "libertree"))
    ap.add_argument("--pg-db", default=os.environ.get("POSTGRES_DB", "libertree"))
    ap.add_argument("--limit", type=int, default=None,
                    help="문서 N건만 이관(리허설). 생략 시 전량.")
    ap.add_argument("--verify-only", action="store_true", help="적재 없이 검증만")
    ap.add_argument("--yes", action="store_true", help="확인 프롬프트 생략")
    args = ap.parse_args()

    psql = Psql([
        "docker", "compose", "exec", "-T", "postgres",
        "psql", "-U", args.pg_user, "-d", args.pg_db, "-v", "ON_ERROR_STOP=1", "-q",
    ])
    con = open_sqlite(args.sqlite)
    boundary = seq_boundary(con, args.limit)

    print(f"원본 : {args.sqlite}  (읽기 전용)")
    print(f"대상 : compose postgres / {args.pg_db}")
    if boundary:
        print(f"모드 : 리허설 (문서 {args.limit:,}건, seq_id <= {boundary:,})")

    if args.verify_only:
        return 0 if verify(psql, con, args.blob_root, boundary) else 1

    existing = int(psql.scalar(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name='documents';"))
    if existing:
        n = int(psql.scalar("SELECT count(*) FROM documents;"))
        if n and not args.yes:
            print(f"\n대상 documents 에 이미 {n:,}행이 있습니다. "
                  f"중복 적재를 막기 위해 중단합니다.\n"
                  f"다시 하려면: docker compose down -v  (PG 볼륨 삭제) 후 재실행")
            return 2

    t0 = time.time()
    print("\n=== 1. 스키마 생성 ===")
    psql.run(DDL_TABLES)
    print("  테이블 6종 생성(IF NOT EXISTS)")

    print("\n=== 2. 데이터 적재 ===")
    counts = load_tables(psql, con, boundary)

    print("\n=== 3. 인덱스 생성 ===")
    psql.run(DDL_INDEXES)
    print("  기본 인덱스 7종")

    print("\n=== 4. 전문검색 인덱스 ===")
    t1 = time.time()
    psql.run(DDL_FTS)
    print(f"  tsvector(simple) + pg_trgm(title)  ({time.time() - t1:.1f}초)")

    print("\n=== 5. 시퀀스 이어붙이기 ===")
    fix_sequences(psql)

    print("\n=== 6. ANALYZE ===")
    psql.run("ANALYZE;")

    ok = verify(psql, con, args.blob_root, boundary)
    total = sum(counts.values())
    print(f"\n{'=' * 52}")
    print(f"{'이관 완료' if ok else '이관 완료 — 검증 실패 항목 있음'}: "
          f"총 {total:,}행 / {time.time() - t0:.1f}초")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
