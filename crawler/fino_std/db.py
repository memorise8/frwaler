from pathlib import Path
import sqlite3

from .board import PublishedFile
from .models import ParagraphRecord


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            std_num INTEGER NOT NULL UNIQUE,
            std_type TEXT NOT NULL,
            title TEXT NOT NULL,
            source_url TEXT NOT NULL,
            collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS paragraphs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            para_num TEXT NOT NULL,
            section_path TEXT NOT NULL,
            body_html TEXT NOT NULL,
            body_text TEXT NOT NULL,
            source_url TEXT NOT NULL,
            seq INTEGER NOT NULL,
            UNIQUE(document_id, seq)
        );

        -- 공표 게시판의 현재 판본 대장. 첨부 파일명에 판본이 박혀 있어
        -- (`..._수정목록_26-1_...`) 본문을 받지 않고도 최신 여부를 가릴 수 있다.
        CREATE TABLE IF NOT EXISTS published_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            board TEXT NOT NULL,
            entry_title TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_no TEXT NOT NULL,
            file_seq TEXT NOT NULL,
            collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(board, file_no, file_seq)
        );

        CREATE INDEX IF NOT EXISTS idx_std_published_board ON published_files(board);
        CREATE INDEX IF NOT EXISTS idx_std_documents_type ON documents(std_type);
        CREATE INDEX IF NOT EXISTS idx_std_paragraphs_doc ON paragraphs(document_id);
        CREATE INDEX IF NOT EXISTS idx_std_paragraphs_num ON paragraphs(para_num);
        """
    )
    conn.commit()


def upsert_document(
    conn: sqlite3.Connection, *, std_num: int, std_type: str, title: str, source_url: str
) -> int:
    conn.execute(
        """
        INSERT INTO documents (std_num, std_type, title, source_url)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(std_num) DO UPDATE SET
            std_type = excluded.std_type,
            title = excluded.title,
            source_url = excluded.source_url,
            collected_at = CURRENT_TIMESTAMP
        """,
        (std_num, std_type, title, source_url),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM documents WHERE std_num = ?", (std_num,)).fetchone()
    return int(row["id"])


def replace_paragraphs(
    conn: sqlite3.Connection, document_id: int, records: list[ParagraphRecord]
) -> int:
    with conn:  # 단일 트랜잭션: 삭제+삽입
        conn.execute("DELETE FROM paragraphs WHERE document_id = ?", (document_id,))
        conn.executemany(
            """
            INSERT INTO paragraphs (
                document_id, para_num, section_path, body_html, body_text, source_url, seq
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [(document_id, r.para_num, r.section_path, r.body_html, r.body_text,
              r.source_url, r.seq) for r in records],
        )
    return len(records)


def replace_published_files(
    conn: sqlite3.Connection, board: str, items: list[PublishedFile]
) -> int:
    """한 게시판의 판본 대장을 통째로 갈아끼운다.

    게시판에서 내려간 파일은 대장에도 남으면 안 된다. 낡은 판이 남아 있으면
    "무엇이 바뀌었나" 대조에서 사라진 판을 현행으로 착각한다.
    """
    with conn:  # 단일 트랜잭션: 삭제+삽입
        conn.execute("DELETE FROM published_files WHERE board = ?", (board,))
        conn.executemany(
            """
            INSERT INTO published_files (board, entry_title, file_name, file_no, file_seq)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(i.board, i.entry_title, i.file_name, i.file_no, i.file_seq) for i in items],
        )
    return len(items)
