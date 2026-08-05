from typing import Final

from .models import Target, TargetKind


TARGETS: Final[tuple[Target, ...]] = (
    Target(1, "회계", "한국회계기준원(KASB)", "질의회신 요약 전체", "https://www.kasb.or.kr/front/board/allReplySummaryList.do", "게시판 크롤링 + 첨부파일 다운로드", "fino-qna-kasb-index-v1", "qna", "K_IFRS_QNA|GAAP_QNA|IFRS_IC_KR|FAST_QNA|TF_SUPPORT", "IFRS IC 원문 영어 대신 KASB의 IFRS 해석위원회 논의 결과를 우선 수집", TargetKind.LIST),
    Target(2, "회계", "금융감독원(FSS)", "K-IFRS 질의회신요약", "https://www.fss.or.kr/fss/bbs/B0000132/list.do?menuNo=200442", "게시판 크롤링 + 첨부파일 다운로드", "fino-fss-accounting-index-v1", "qna", "K_IFRS_QNA", "금감원 회계질의 공개자료", TargetKind.LIST),
    Target(3, "회계", "금융감독원(FSS)", "일반기업회계기준 질의회신요약", "https://www.fss.or.kr/fss/bbs/B0000133/list.do?menuNo=200443", "게시판 크롤링 + 첨부파일 다운로드", "fino-fss-accounting-index-v1", "qna", "GAAP_QNA", "GAAP 질의회신 보강", TargetKind.LIST),
    Target(4, "회계", "금융감독원(FSS)", "과거 국제회계기준 Q&A", "https://www.fss.or.kr/fss/bbs/B0000134/list.do?menuNo=200445", "게시판 크롤링", "fino-fss-accounting-index-v1", "qna", "OLD_IFRS_QNA", "과거 K-IFRS 도입·운영 관련 Q&A", TargetKind.LIST),
    Target(5, "회계", "금융감독원(FSS)", "심사·감리지적사례", "https://www.fss.or.kr/fss/bbs/B0000135/list.do?menuNo=200448", "게시판 크롤링 + PDF/HWP 첨부 다운로드", "fino-fss-accounting-index-v1", "supervisory_case", "AUDIT_REVIEW_CASE", "답변 1차 근거보다는 감독상 유의사항·유사사례로 활용", TargetKind.LIST),
    Target(6, "회계", "금융감독원(FSS)", "감사인 감리결과 개선권고사항 등", "https://www.fss.or.kr/fss/bbs/B0000291/list.do?cl1Cd=01&menuNo=200620&pageIndex=1", "게시판 크롤링 + 첨부파일 다운로드", "fino-fss-accounting-index-v1", "audit_quality_review", "AUDITOR_REVIEW_RECOMMENDATION", "회계법인 품질관리·감사인 감리 관련 보조자료", TargetKind.LIST),
    Target(7, "회계", "금융감독원(FSS)", "보도자료 API", "https://www.fss.or.kr/fss/api/apiInquiryBodoInfo/view.do?menuNo=200281", "Open API", "fino-fss-accounting-index-v1", "press_release_api", "FSS_BODO_API", "회계 관련 보도자료성 문서의 보조 수집 경로", TargetKind.META),
    Target(8, "회계", "금융위원회(FSC)", "금융위 보도자료 전체", "https://www.fsc.go.kr/no010101", "게시판 크롤링 + 첨부파일 다운로드", "fino-fsc-accounting-policy-index-v1", "policy", "FSC_PRESS_RELEASE", "회계정책·외감제도·감리 조치·감독지침 후보를 키워드/부서로 필터링", TargetKind.LIST),
    Target(9, "회계", "금융위원회(FSC)", "회계제도팀 보도자료 검색", "https://www.fsc.go.kr/no010101?curPage=&srchBeginDt=&srchCtgry=&srchEndDt=&srchKey=dept&srchText=%ED%9A%8C%EA%B3%84%EC%A0%9C%EB%8F%84%ED%8C%80", "게시판 크롤링 + 첨부파일 다운로드", "fino-fsc-accounting-policy-index-v1", "policy", "ACCOUNTING_POLICY_TEAM", "최신 회계제도·외감·감리 정책자료 우선 수집", TargetKind.LIST),
    Target(10, "회계", "금융위원회(FSC)", "기업회계팀 과거 자료 검색", "https://www.fsc.go.kr/no010101?curPage=&srchBeginDt=&srchCtgry=&srchEndDt=&srchKey=dept&srchText=%EA%B8%B0%EC%97%85%ED%9A%8C%EA%B3%84%ED%8C%80", "게시판 크롤링 + 첨부파일 다운로드", "fino-fsc-accounting-policy-index-v1", "policy", "CORPORATE_ACCOUNTING_TEAM", "과거 부서명 기준 회계정책 자료 보강", TargetKind.LIST),
    Target(11, "회계", "금융위원회(FSC)", "금융위 공공데이터 설명", "https://www.fsc.go.kr/in060101", "페이지 확인 + API/공공데이터 경로 탐색", "fino-fsc-accounting-policy-index-v1", "public_data", "FSC_PUBLIC_DATA", "금융공공데이터 개방 체계 확인용", TargetKind.META),
    Target(12, "회계", "공공데이터포털(data.go.kr)", "금융위원회 보도자료 데이터", "https://www.data.go.kr/data/3036476/fileData.do?recommendDataYn=Y", "파일데이터 다운로드 또는 메타데이터 수집", "fino-fsc-accounting-policy-index-v1", "public_data", "FSC_PRESS_FILEDATA", "보도자료 메타·첨부 링크 확인용 보조 경로", TargetKind.META),
    Target(13, "회계", "금융위원회(FSC)", "질의회신제도 개선 정책", "https://www.fsc.go.kr/no010101/74339", "개별 게시글 크롤링 + 첨부파일 다운로드", "fino-fsc-accounting-policy-index-v1", "policy", "ACCOUNTING_QNA_POLICY", "K-IFRS 질의회신제도 구조·개선방향 태깅", TargetKind.DETAIL),
    Target(14, "회계", "금융위원회(FSC)", "회계기준 적용 감독지침", "https://www.fsc.go.kr/no010101/78584", "개별 게시글 크롤링 + 첨부파일 다운로드", "fino-fsc-accounting-policy-index-v1", "supervisory_guidance", "ACCOUNTING_SUPERVISORY_GUIDANCE", "신산업·특정 거래 회계처리 불확실성 해소 자료", TargetKind.DETAIL),
    Target(15, "회계", "금융위원회(FSC)", "회계법인 품질관리 감리 결과 개선권고", "https://www.fsc.go.kr/no010101/84720", "개별 게시글 크롤링 + 첨부파일 다운로드", "fino-fsc-accounting-policy-index-v1", "audit_quality_review", "AUDIT_FIRM_QUALITY_REVIEW", "감사품질·품질관리 감리 사례 보조자료", TargetKind.DETAIL),
    Target(16, "회계", "금융위원회(FSC)", "사업보고서 조사·감리결과 조치", "https://www.fsc.go.kr/no010101/85695", "개별 게시글 크롤링 + 첨부파일 다운로드", "fino-fsc-accounting-policy-index-v1", "enforcement", "FINANCIAL_STATEMENT_REVIEW", "감리 조치·제재성 자료로 유사사례 보강", TargetKind.DETAIL),
    Target(17, "회계", "금융위원회(FSC)", "IFRS/K-IFRS 제도 도입·개정 자료", "https://www.fsc.go.kr/no010101/82451", "개별 게시글 크롤링 + 첨부파일 다운로드", "fino-fsc-accounting-policy-index-v1", "policy", "IFRS_ADOPTION_OR_REVISION", "K-IFRS 제도·기준 개정 정책자료", TargetKind.DETAIL),
    # 18~23: acct_data.xlsx 대상(교육·해설 계열). 목록 페이지가 첨부를 직접 노출하므로
    # 전용 수집기 scripts/collect_acct_data.py 가 처리한다(기존 LIST 페이징과 구조가 다름).
    Target(18, "회계", "한국회계기준원(KASB)", "행사·교육자료", "https://www.kasb.or.kr/front/board/List2003.do", "게시판 크롤링 + 첨부파일 다운로드", "fino-acct-edu-index-v1", "education", "KASB_EVENT_EDU", "KAI Forum·세미나·교육교재. 기준서 실무이슈 해설 비중이 높음", TargetKind.LIST),
    Target(19, "회계", "한국회계기준원(KASB)", "기고자료", "https://www.kasb.or.kr/front/board/List2005.do", "게시판 크롤링 + 첨부파일 다운로드", "fino-acct-edu-index-v1", "commentary", "KASB_ARTICLE", "월간공인회계사·협회지 기고문. 기준서 쟁점 해설", TargetKind.LIST),
    Target(20, "회계", "한국회계기준원(KASB)", "교육자료", "https://www.kasb.or.kr/front/board/eduAccstdList.do", "게시판 크롤링 + 첨부파일 다운로드", "fino-acct-edu-index-v1", "education", "KASB_EDU_MATERIAL", "질의회신 교육자료 PDF. 스마트강의(YouTube)는 수집 대상 제외", TargetKind.LIST),
    Target(21, "회계", "한국회계기준원(KASB)", "회계기준적용의견서", "https://www.kasb.or.kr/front/board/opinionList.do", "개별 게시글 크롤링 + 첨부파일 다운로드", "fino-qna-kasb-index-v1", "opinion", "KASB_APPLICATION_OPINION", "기준원이 발표한 적용의견서. 준규범적 성격", TargetKind.LIST),
    Target(22, "회계", "한국회계기준원(KASB)", "정착지원TF 질의회신요약", "https://www.kasb.or.kr/front/board/List016008.do", "게시판 크롤링 + 첨부파일 다운로드", "fino-qna-kasb-index-v1", "qna", "TF_SUPPORT", "우선순위 1(질의회신 요약 전체)과 중복 가능 — 적재 시 해시 대조", TargetKind.LIST),
    Target(23, "회계", "한국공인회계사회(KICPA)", "IFRS 실무사례", "https://www.kicpa.or.kr/board/list.brd?boardId=accstd02", "게시판 크롤링 + 첨부파일 다운로드", "fino-acct-edu-index-v1", "commentary", "KICPA_IFRS_CASE", "월간공인회계사 IFRS 실무사례. 발행년월·K-IFRS 번호 메타 보유", TargetKind.LIST),
)
