# -*- coding: utf-8 -*-
"""libertree 문서 제목/초록 한국어 배치 번역 (로컬 qwen3, 양쪽 GPU).

- 대상: 외국어(제목에 한글 없음) 문서, **etera 제외**.
  · title : 위 조건 전부
  · description(초록): 산문일 때만 (길이>=20, 템플릿/CSS/mojibake 아님)
- 저장: document_translations (기존 스키마). documents 원문 불변.
- 멱등/재개: (seq_id, source_field, locale, fingerprint, model, prompt) UNIQUE.
  이미 completed면 건너뜀 → 10일 배치가 끊겨도 이어서.
- OOM 안전: 입력 cap, num_ctx, GPU당 1요청.

Usage:
    python scripts/translate/run_translation.py --count          # 대상 집계만
    python scripts/translate/run_translation.py --limit 20       # 소량 검증
    python scripts/translate/run_translation.py                  # 전량 배치
"""
from __future__ import annotations
import argparse, hashlib, json, sqlite3, sys, time, threading, urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/data_raid/ruci_workspace/frwaler_job")
from crawler.translation_schema import migrate_translation_schema, canonical_db_path

ENDPOINTS = ["http://127.0.0.1:11435/api/generate", "http://127.0.0.1:11436/api/generate"]
MODEL_VERSION = "qwen3:8b"
PROMPT_VERSION = "translate-ko-v1"
LOCALE = "ko-KR"
SRC_CAP = 8000          # 입력 문자 상한(긴 초록 보존, OOM 안전)
NUM_CTX = 16384

# 산문 초록 조건 (etera 및 템플릿/CSS/mojibake 제외)
DESC_OK = (
    "LENGTH(TRIM(COALESCE(abstract,''))) >= 20 "
    "AND abstract NOT LIKE 'Title:%Publication date:%' "
    "AND abstract NOT LIKE '%Type: Ajaleht%' "
    "AND abstract NOT LIKE '%gform_wrapper%' AND abstract NOT LIKE '%<div%' AND abstract NOT LIKE '%<style%' "
    "AND abstract NOT LIKE '%'||char(65533)||'%'"
)
FOREIGN = "site_id <> 'etera-ee-browse' AND title NOT GLOB '*[가-힣]*' AND TRIM(COALESCE(title,'')) <> ''"

# qwen 이 오역이 잦은 저자원 언어 (그리스어 등) → qwen 제외, GPT 품질 담당
LANG_BLOCKLIST = ("el", "et", "fi", "vi")


def fp(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def translate(text: str, url: str) -> str:
    prompt = ("다음 텍스트를 자연스러운 한국어로 번역만 하라. "
              "고유명사·기관명·숫자·날짜·URL은 원문 그대로 보존하고, 설명 없이 번역문만 출력하라.\n\n" + text[:SRC_CAP])
    body = json.dumps({"model": MODEL_VERSION, "prompt": prompt, "stream": False, "think": False,
                       "options": {"num_ctx": NUM_CTX, "num_predict": 4096, "temperature": 0.2}}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read()).get("response", "").strip()


def build_targets(conn, limit, fields=("title", "description"), desc_backfill=False, safe_langs=False):
    """(seq_id, source_field, source_text) 중 아직 completed 아닌 것.

    desc_backfill=True: 초록을 seq_id 내림차순으로, **어떤 모델이든** 이미 완료된 초록은 건너뜀
    (GPT가 오름차순으로 하는 동안 qwen이 반대쪽부터 백필 → 중복 최소화).
    safe_langs=True: document_lang.lang 이 LANG_ALLOWLIST 인 문서만 (qwen 고자원 언어 전용).
    """
    q = conn.cursor().execute
    lang_clause = ""
    if safe_langs:
        block = ",".join(f"'{l}'" for l in LANG_BLOCKLIST)
        # 저자원 블록리스트(그리스어 등) 제외 = qwen 담당
        lang_clause = f" AND seq_id NOT IN (SELECT seq_id FROM document_lang WHERE lang IN ({block}))"
    rows = []
    if "title" in fields:
        for seq, ti in q(f"SELECT seq_id, title FROM documents WHERE {FOREIGN}{lang_clause}"):
            rows.append((seq, "title", ti))
    if "description" in fields:
        order = "ORDER BY seq_id DESC" if desc_backfill else ""
        for seq, ab in q(f"SELECT seq_id, abstract FROM documents WHERE {FOREIGN} AND {DESC_OK}{lang_clause} {order}"):
            rows.append((seq, "description", ab))
    # 동일 모델 완료분 제외
    done = set()
    for seq, sf, sfp in q(
        "SELECT seq_id, source_field, source_fingerprint FROM document_translations "
        "WHERE state='completed' AND target_locale=? AND model_version=? AND prompt_version=?",
        (LOCALE, MODEL_VERSION, PROMPT_VERSION),
    ):
        done.add((seq, sf, sfp))
    # 백필: 어떤 모델이든 완료된 초록 seq 제외
    done_any_desc = set()
    if desc_backfill:
        done_any_desc = {r[0] for r in q(
            "SELECT DISTINCT seq_id FROM document_translations "
            "WHERE source_field='description' AND state='completed' AND target_locale=?", (LOCALE,))}
    out = []
    for (s, f, t) in rows:
        if (s, f, fp(t or "")) in done:
            continue
        if f == "description" and s in done_any_desc:
            continue
        out.append((s, f, t))
    return out[:limit] if limit else out


write_lock = threading.Lock()
_stat = {"ok": 0, "err": 0, "t0": 0.0}


def worker(conn, item):
    idx, (seq, field, text) = item
    url = ENDPOINTS[idx % len(ENDPOINTS)]
    f = fp(text or "")
    try:
        out = translate(text or "", url)
        if not out:
            raise ValueError("empty")
        state, tt, err = "completed", out, None
    except Exception:
        state, tt, err = "failed", None, "llm_error"
    with write_lock:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO document_translations "
                "(seq_id, source_field, target_locale, source_fingerprint, model_version, prompt_version, "
                " state, translation_text, attempts, last_error_code, completed_at) "
                "VALUES (?,?,?,?,?,?,?,?,1,?, CASE WHEN ?='completed' THEN CURRENT_TIMESTAMP END)",
                (seq, field, LOCALE, f, MODEL_VERSION, PROMPT_VERSION, state, tt, err, state),
            )
            if state == "completed":
                _stat["ok"] += 1
            else:
                _stat["err"] += 1
            n = _stat["ok"] + _stat["err"]
            if n % 200 == 0:
                conn.commit()
                el = time.time() - _stat["t0"]
                print(f"  {n:>7,}  ok={_stat['ok']:,} err={_stat['err']:,}  {n/el:.1f}/s  {el/60:.1f}m", flush=True)
        except sqlite3.Error as e:
            print(f"  [db] {e}", file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=len(ENDPOINTS),
                    help="동시 요청 수 (ollama 연속배칭 활용, GPU당 workers/2)")
    ap.add_argument("--fields", default="title,description",
                    help="번역 필드 (title / description / title,description)")
    ap.add_argument("--desc-backfill", action="store_true",
                    help="초록 백필: 내림차순 + 어떤 모델이든 완료된 초록은 건너뜀(GPT 하이브리드용)")
    ap.add_argument("--safe-langs", action="store_true",
                    help="qwen 고자원 언어(LANG_ALLOWLIST)만 번역 — 그리스어 등 저자원은 GPT로")
    args = ap.parse_args()
    fields = tuple(f.strip() for f in args.fields.split(","))

    db = str(canonical_db_path())
    conn = sqlite3.connect(db, timeout=60, check_same_thread=False, isolation_level=None)  # autocommit: 짧은 락
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    migrate_translation_schema(conn)   # 멱등
    print(f"[schema] document_translations 준비 완료 ({db})", flush=True)

    if args.count:
        q = conn.cursor().execute
        nt = q(f"SELECT COUNT(*) FROM documents WHERE {FOREIGN}").fetchone()[0]
        nd = q(f"SELECT COUNT(*) FROM documents WHERE {FOREIGN} AND {DESC_OK}").fetchone()[0]
        dc = q("SELECT COUNT(*) FROM document_translations WHERE state='completed'").fetchone()[0]
        print(f"  번역 대상 title      : {nt:,}")
        print(f"  번역 대상 description: {nd:,}")
        print(f"  합계 호출 예상       : {nt+nd:,}  (이미 완료 {dc:,})")
        return

    targets = build_targets(conn, args.limit, fields, args.desc_backfill, args.safe_langs)
    print(f"[targets] 이번 실행 대상 {len(targets):,}건 (fields={fields}, backfill={args.desc_backfill}, safe_langs={args.safe_langs})", flush=True)
    _stat["t0"] = time.time()
    items = list(enumerate(targets))
    print(f"[workers] 동시 요청 {args.workers}", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(lambda it: worker(conn, it), items))
    conn.commit()
    print(f"\n[done] ok={_stat['ok']:,} err={_stat['err']:,} / {(time.time()-_stat['t0'])/60:.1f}분", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
