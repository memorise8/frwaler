# -*- coding: utf-8 -*-
"""초록(description) 한국어 번역 — gpt-5.4-mini (OpenAI 호환 엔드포인트 2개).

- 엔드포인트: localhost:10531 + 192.168.0.4:10532 (라운드로빈)
- 대상: FOREIGN + 산문초록(DESC_OK) 중 gpt-5.4-mini로 아직 완료 안 된 것
- 저장: document_translations, model_version='gpt-5.4-mini' (qwen과 별개 행)
- 멱등/재개: 완료분 skip → 한도로 멈춰도 재실행하면 이어서
- **한도 감지**: 429/auth expired/5xx 가 연속 N회면 깔끔히 중단(재실행 대기).
- 로그: scripts/translate/gpt_translate.log (진행 + 오류 타임스탬프)

Usage:
    python scripts/translate/gpt_translate_desc.py --count
    python scripts/translate/gpt_translate_desc.py --limit 5
    python scripts/translate/gpt_translate_desc.py            # 전량(한도까지)
"""
from __future__ import annotations
import argparse, hashlib, json, sqlite3, sys, time, threading, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

REPO = "/data_raid/ruci_workspace/frwaler_job"
sys.path.insert(0, REPO)
from scripts.translate.run_translation import FOREIGN, DESC_OK, LOCALE, LANG_BLOCKLIST  # noqa

DB = f"{REPO}/libertree-app/data/libertree.db"
LOG = f"{REPO}/scripts/translate/gpt_translate.log"
ENDPOINTS = ["http://localhost:10531/v1/chat/completions",
             "http://192.168.0.4:10532/v1/chat/completions"]
MODEL = "gpt-5.4-mini"
MODEL_VERSION = "gpt-5.4-mini"
PROMPT_VERSION = "translate-ko-v1"
SRC_CAP = 8000
QUOTA_STOP_STREAK = 6          # 연속 한도오류 이만큼이면 중단

stop_flag = threading.Event()
write_lock = threading.Lock()
_stat = {"ok": 0, "err": 0, "quota": 0, "streak": 0, "t0": 0.0}


def logline(msg):
    with write_lock:
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
            f.flush()


def fp(text): return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def call_gpt(text, url):
    """returns ('OK', translation) / ('QUOTA', code) / ('ERR', code)"""
    body = json.dumps({"model": MODEL, "messages": [{"role": "user",
        "content": "다음 텍스트를 자연스러운 한국어로 번역만 하라. 고유명사·기관명·숫자·날짜·URL은 원문 보존, 설명 없이 번역문만 출력하라.\n\n" + text[:SRC_CAP]}]}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            out = json.loads(r.read())["choices"][0]["message"]["content"].strip()
            return ("OK", out) if out else ("ERR", "empty")
    except urllib.error.HTTPError as e:
        code = e.code
        if code in (401, 403, 429) or code >= 500:
            return ("QUOTA", code)
        return ("ERR", code)
    except Exception as e:
        return ("QUOTA", str(e)[:40])   # 네트워크/타임아웃도 일시중단성으로 취급


def worker(conn, item):
    if stop_flag.is_set():
        return
    idx, (seq, text) = item
    url = ENDPOINTS[idx % len(ENDPOINTS)]
    status, payload = call_gpt(text or "", url)
    with write_lock:
        if status == "OK":
            conn.execute(
                "INSERT OR IGNORE INTO document_translations "
                "(seq_id, source_field, target_locale, source_fingerprint, model_version, prompt_version, "
                " state, translation_text, attempts, completed_at) "
                "VALUES (?,?,?,?,?,?, 'completed', ?, 1, CURRENT_TIMESTAMP)",
                (seq, "description", LOCALE, fp(text), MODEL_VERSION, PROMPT_VERSION, payload))
            _stat["ok"] += 1
            _stat["streak"] = 0
        elif status == "QUOTA":
            _stat["quota"] += 1
            _stat["streak"] += 1
            logline(f"[quota/err] seq={seq} code={payload} streak={_stat['streak']}")
            if _stat["streak"] >= QUOTA_STOP_STREAK and not stop_flag.is_set():
                stop_flag.set()
                logline(f"[STOP] 연속 {_stat['streak']}회 한도/오류 → 중단. 재실행하면 이어서.")
            return
        else:
            _stat["err"] += 1
            _stat["streak"] = 0
        n = _stat["ok"] + _stat["err"]
        if n % 50 == 0:
            conn.commit()
            el = time.time() - _stat["t0"]
            logline(f"[진행] ok={_stat['ok']:,} err={_stat['err']:,} quota={_stat['quota']}  {n/el:.2f}/s")


def build_desc_targets(conn, limit, unsafe_langs=False):
    q = conn.cursor().execute
    lang_clause = ""
    if unsafe_langs:
        block = ",".join(f"'{l}'" for l in LANG_BLOCKLIST)
        # 저자원 블록리스트(그리스어 등)만 → GPT가 품질 담당
        lang_clause = f" AND seq_id IN (SELECT seq_id FROM document_lang WHERE lang IN ({block}))"
    rows = [(s, ab) for s, ab in q(f"SELECT seq_id, abstract FROM documents WHERE {FOREIGN} AND {DESC_OK}{lang_clause}")]
    # 중복 방지: 어떤 모델(qwen 포함)이든 이미 완료된 초록 seq 는 제외
    done = {r[0] for r in q(
        "SELECT DISTINCT seq_id FROM document_translations "
        "WHERE source_field='description' AND state='completed' AND target_locale=?", (LOCALE,))}
    out = [(s, ab) for (s, ab) in rows if s not in done]
    return out[:limit] if limit else out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--unsafe-langs", action="store_true",
                    help="qwen allowlist 밖(그리스어 등 저자원)만 번역 — GPT 품질 담당")
    args = ap.parse_args()

    conn = sqlite3.connect(DB, timeout=60, check_same_thread=False, isolation_level=None)  # autocommit: 짧은 락
    conn.execute("PRAGMA busy_timeout=60000")

    if args.count:
        rem = len(build_desc_targets(conn, 0, args.unsafe_langs))
        gc = conn.execute("SELECT COUNT(*) FROM document_translations WHERE source_field='description' "
                          "AND state='completed' AND model_version=?", (MODEL_VERSION,)).fetchone()[0]
        scope = "저자원(unsafe)" if args.unsafe_langs else "전체"
        print(f"  gpt 완료 {gc:,} / {scope} 남음 {rem:,}")
        return

    targets = build_desc_targets(conn, args.limit, args.unsafe_langs)
    _stat["t0"] = time.time()
    logline(f"[start] gpt-5.4-mini 초록 번역 시작 — 대상 {len(targets):,}건, workers={args.workers}")
    print(f"[start] 대상 {len(targets):,}건 (로그: {LOG})", flush=True)
    items = list(enumerate(targets))
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(lambda it: worker(conn, it), items))
    conn.commit()
    el = time.time() - _stat["t0"]
    msg = (f"[done] ok={_stat['ok']:,} err={_stat['err']:,} quota={_stat['quota']} "
           f"/ {el/60:.1f}분 {'(한도중단)' if stop_flag.is_set() else '(완주)'}")
    logline(msg); print(msg, flush=True)
    conn.close()


if __name__ == "__main__":
    main()
