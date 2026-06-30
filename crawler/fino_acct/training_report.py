from __future__ import annotations

from html import escape

from .training_models import SummaryValue


def build_report_html(summary: dict[str, SummaryValue]) -> str:
    lines = [
        "<!doctype html>",
        '<html lang="ko">',
        "<head>",
        '<meta charset="utf-8">',
        "<title>FSC/FSS/KICPA 학습 데이터 파이프라인 리포트</title>",
        "<style>",
        "body{font-family:system-ui,sans-serif;margin:32px;line-height:1.55;color:#1f2937}",
        "h1,h2{color:#111827}table{border-collapse:collapse;width:100%;margin:12px 0 28px}",
        "th,td{border:1px solid #d1d5db;padding:8px 10px;text-align:left;vertical-align:top}",
        "th{background:#f3f4f6}code{background:#f3f4f6;padding:2px 4px;border-radius:4px}",
        "</style>",
        "</head>",
        "<body>",
        "<h1>FSC/FSS/KICPA 학습 데이터 파이프라인 리포트</h1>",
        "<p>로컬에 수집된 공공 회계자료, 비조치의견서 DB, KICPA 회원전용 수집물을 학습 후보 JSONL로 통합한 결과입니다.</p>",
        "<h2>요약</h2>",
        "<table>",
        f"<tr><th>문서</th><td>{summary['documents']}</td></tr>",
        f"<tr><th>로드 이벤트</th><td>{summary['load_events']}</td></tr>",
        f"<tr><th>Chunks</th><td>{summary['chunks']}</td></tr>",
        f"<tr><th>Train pairs</th><td>{summary['train_pairs']}</td></tr>",
        "</table>",
        "<h2>요청 소스 커버리지</h2>",
        source_coverage_table(summary),
        "<h2>소스별 문서</h2>",
        mapping_table(summary["documents_by_source"]),
        "<h2>소스별 학습쌍</h2>",
        mapping_table(summary["pairs_by_source"]),
        "<h2>로드 상태</h2>",
        mapping_table(summary["events_by_status"]),
        "<h2>판단</h2>",
        (
            "<p><code>train_pairs.jsonl</code>은 embedding fine-tuning 후보로 사용할 수 있습니다. "
            "현재 anchor는 템플릿 기반 synthetic 질문이므로, 최종 학습 전 LLM 질문 다양화와 judge validation을 추가하는 것이 좋습니다.</p>"
        ),
        "</body>",
        "</html>",
    ]
    return "\n".join(lines)


def source_coverage_table(summary: dict[str, SummaryValue]) -> str:
    pairs = summary["pairs_by_source"]
    if not isinstance(pairs, dict):
        pairs = {}
    rows = [
        ("1", "금융위 행정지도", "https://better.fsc.go.kr/fsc_new/status/adminMap/OpertnList.do?stNo=11&muNo=145&muGpNo=60", "전용 상세 크롤러 추가 필요", 0),
        ("2", "금감원 행정지도", "https://www.fss.or.kr/fss/job/admnstgudc/list.do?menuNo=200492", "전용 상세 크롤러 추가 필요", 0),
        ("3", "비조치의견서", "https://better.fsc.go.kr/fsc_new/replyCase/PastReplyList.do?stNo=11&muNo=171&muGpNo=75", "로컬 DB 반영", pairs.get("public:better-fsc-go-kr-fsc_new", 0)),
        ("4", "감독행정", "https://www.fss.or.kr/fss/job/admnstgudcDtls/list.do?menuNo=200494", "전용 상세 크롤러 추가 필요", 0),
        ("5", "K-IFRS 실무사례와 해설 시리즈", "https://www.kicpa.or.kr/", "KICPA 인증 수집 반영", pairs.get("kicpa:kifrs_series", 0)),
        ("6", "세무자료", "https://www.kicpa.or.kr/", "KICPA 인증 수집 반영", pairs.get("kicpa:tax_materials", 0)),
        ("7", "세무단행본", "https://www.kicpa.or.kr/", "KICPA 인증 수집 반영", pairs.get("kicpa:tax_books", 0)),
        ("8", "IFRS실무사례", "https://www.kicpa.or.kr/", "KICPA 인증 수집 반영", pairs.get("kicpa:ifrs_cases", 0)),
        ("9", "심사·감리지적사례", "https://www.kicpa.or.kr/", "KICPA 인증 수집 반영", pairs.get("kicpa:audit_review_cases", 0)),
    ]
    body = "\n".join(
        f"<tr><td>{no}</td><td>{escape(name)}</td><td>{escape(status)}</td><td>{count}</td><td>{escape(url)}</td></tr>"
        for no, name, url, status, count in rows
    )
    return f"<table><tr><th>No</th><th>소스</th><th>현재 상태</th><th>학습쌍</th><th>URL</th></tr>{body}</table>"


def mapping_table(value: SummaryValue) -> str:
    if not isinstance(value, dict):
        return f"<p>{escape(str(value))}</p>"
    rows = "\n".join(f"<tr><td>{escape(key)}</td><td>{count}</td></tr>" for key, count in sorted(value.items()))
    return f"<table><tr><th>항목</th><th>건수</th></tr>{rows}</table>"
