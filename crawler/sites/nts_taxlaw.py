# -*- coding: utf-8 -*-
"""국세법령정보시스템 crawler - 세법해석례 and 판례 (curl-based due to SSL issues)."""

import json
import re
import subprocess
import time
from pathlib import Path

from ..base_crawler import BaseCrawler

_ROOT = Path(__file__).resolve().parent.parent.parent
_HTML_EXPORT_ROOT = _ROOT / "data" / "exports"


class _NTSTaxlawBase(BaseCrawler):
    """Shared base for NTS taxlaw crawlers."""

    base_url = "https://taxlaw.nts.go.kr"

    _API_URL = "https://taxlaw.nts.go.kr/action.do"
    _REFERER = "https://taxlaw.nts.go.kr"
    _PAGE_SIZE = 50

    # Subclasses must define these
    _COLLECTION: str
    _DCM_CODES: list

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl_post(self, action_id: str, param_data: dict) -> str | None:
        """POST via curl; returns raw response text or None on failure."""
        param_json = json.dumps(param_data, ensure_ascii=False)
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-X", "POST",
            "-H", "Content-Type: application/x-www-form-urlencoded",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", f"Referer: {self._REFERER}",
            "--data-urlencode", f"paramData={param_json}",
            "-d", f"actionId={action_id}",
            self._API_URL,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
                if result.stdout.strip():
                    return result.stdout
                if attempt < 2:
                    wait = (attempt + 1) * 3
                    print(f"[{self.site_id}] Empty response, retrying in {wait}s...")
                    time.sleep(wait)
            except Exception as e:
                if attempt < 2:
                    wait = (attempt + 1) * 3
                    print(f"[{self.site_id}] curl error: {e}, retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {e}")
        return None

    def _fetch_list(self, page: int, search: str | None = None,
                    doc_type: str | None = None,
                    date_from: str | None = None,
                    date_to: str | None = None) -> str | None:
        dcm_codes = self._DCM_CODES
        if doc_type:
            dcm_codes = [doc_type]

        param_data = {
            "collectionName": self._COLLECTION,
            "sortField": "DCM_RGT_DTM/DESC",
            "startCount": page,
            "viewCount": self._PAGE_SIZE,
            "dcmClCdCtl": dcm_codes,
            "qstnPrdcOrgnClCtl": [],
            "rltnStttCtl": [],
            "schDtBase": "DCM_RGT_DTM",
        }
        if search:
            param_data["icldVcbCtl"] = [search]
        if date_from:
            param_data["bltnStrtDt"] = date_from.replace("-", "")
        if date_to:
            param_data["bltnEndDt"] = date_to.replace("-", "")
        return self._curl_post("ASIPDI002PR01", param_data)

    def _fetch_detail(self, doc_id: str) -> dict | None:
        param_data = {"dcmDVO": {"ntstDcmId": doc_id}}
        raw = self._curl_post("ASIQTB002PR01", param_data)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data.get("data", {}).get("ASIQTB002PR01")
        except (json.JSONDecodeError, AttributeError):
            return None

    # ------------------------------------------------------------------
    # Document-number search (document mode, accurate)
    # ------------------------------------------------------------------

    # Collection code used by document mode (not the list API's
    # "<collection>,<collection>_gr" — ASEISA001MR01 expects a single
    # flat name like "question" or "precedent").
    _DOC_MODE_COLLECTION: str = ""

    def _doc_mode_search(self, doc_number: str,
                         view_count: int = 10) -> list[dict]:
        """Search by document number via ASEISA001MR01 (searchType=document).

        The server restricts matching to doc-number indexes
        (NTST_DCM_DSCM_CNTN, DOCU_NO_STR1/2/3) with automatic 0-prefix
        padding variants — unlike the regular keyword search which also
        matches titles and bodies. Returns a flat list of item dicts.
        """
        if not self._DOC_MODE_COLLECTION:
            return []
        params = {
            "schVcb": doc_number,
            "startCount": 1,
            "collection": self._DOC_MODE_COLLECTION,
            "wnKey": "",
            "searchType": "document",
            "sortField": "SCORE/DESC",
            "ntstTlawClCdList": [],
            "icldVcbCtl": [],
            "exclVcbCtl": [],
            "rltnStttCtl": [],
            "schDtBase": "DCM_RGT_DTM",
            "viewCount": str(view_count),
            "prtsSprcChiefJdgmYn": "",
            "prtsAttrYrCtl": [],
            "prtsPrgrStatCtl": [],
            "prtsLwsDfntYn": "",
            "infrOpClCtl": [],
            "mainIdCtl": [],
            "useSynonymYn": "N",
        }
        param_json = json.dumps(params, ensure_ascii=False)
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-X", "POST",
            "-H", "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Origin: https://taxlaw.nts.go.kr",
            "-H", "Referer: https://taxlaw.nts.go.kr/is/USEISA001M.do",
            "-H", "X-Requested-With: XMLHttpRequest",
            "--data-urlencode", f"paramData={param_json}",
            "-d", "actionId=ASEISA001MR01",
            self._API_URL,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
            data = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            return []
        items: list[dict] = []
        coll_list = (data.get("data", {}).get("ASEISA001MR01", {})
                     .get("searchResultVO", {}).get("collectionList", []) or [])
        for c in coll_list:
            if c and c.get("resultList"):
                items.extend(c["resultList"])
        return items

    def lookup_by_doc_number(self, doc_number: str) -> dict | None:
        """Find the exact document matching ``doc_number``.

        Returns a dict with ``doc_id``, ``title``, ``type``, ``category``,
        ``published_date`` or ``None`` if not found.
        """
        items = self._doc_mode_search(doc_number)
        for item in items:
            no = re.sub(r"<!H[SE]>", "", item.get("NTST_DCM_DSCM_CNTN", "") or "")
            if no == doc_number:
                rgt = item.get("DCM_RGT_DTM", "") or ""
                published = self._parse_date(rgt)
                return {
                    "doc_number": doc_number,
                    "doc_id": item.get("DOC_ID", ""),
                    "title": re.sub(r"<!H[SE]>", "", item.get("TTL", "") or ""),
                    "type": item.get("NTST_DCM_CL_NM", "") or "",
                    "category": item.get("NTST_TLAW_CL_NM", "") or "",
                    "published_date": published,
                }
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(raw: str) -> str:
        """Convert '20260409000000' -> '2026-04-09'."""
        if not raw or len(raw) < 8:
            return ""
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"

    @staticmethod
    def _strip_tags(html: str) -> str:
        """Strip HTML tags and normalize whitespace."""
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&[a-zA-Z]+;", "", text)
        return re.sub(r"\s+", " ", text).strip()

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None, search=None, doc_type=None,
              date_from=None, date_to=None, incremental=False):
        """Crawl via JSON API.

        Parameters
        ----------
        limit:
            Maximum number of papers to save. None means unlimited.
        search:
            Optional keyword search string (added as icldVcbCtl).
        doc_type:
            Optional document type code filter (e.g. '001_02' for 질의회신).
        date_from:
            Optional start date filter (YYYY-MM-DD or YYYYMMDD).
        date_to:
            Optional end date filter (YYYY-MM-DD or YYYYMMDD).
        """
        page = 1
        saved = 0

        while True:
            if limit is not None and saved >= limit:
                break

            time.sleep(self._delay)

            # JSON 디코드 실패 / fetch 실패 시 최대 5회 재시도 (exponential backoff)
            data = None
            for retry in range(5):
                raw = self._fetch_list(page, search, doc_type, date_from, date_to)
                if raw:
                    try:
                        data = json.loads(raw)
                        break
                    except json.JSONDecodeError:
                        wait = (retry + 1) * 10
                        print(f"[{self.site_id}] Invalid JSON at page {page}, "
                              f"retry {retry + 1}/5 in {wait}s...")
                        time.sleep(wait)
                else:
                    wait = (retry + 1) * 10
                    print(f"[{self.site_id}] Empty response at page {page}, "
                          f"retry {retry + 1}/5 in {wait}s...")
                    time.sleep(wait)

            if data is None:
                print(f"[{self.site_id}] Failed after 5 retries at page {page}. "
                      f"Skipping page but continuing.")
                page += 1
                continue

            try:
                items = data["data"]["ASIPDI002PR01"]["body"]
            except (KeyError, TypeError):
                print(f"[{self.site_id}] Unexpected response structure at page {page}. Stopping.")
                break

            if not items:
                print(f"[{self.site_id}] No more items at page {page}. Done.")
                break

            if page == 1:
                try:
                    counts = data["data"]["ASIPDI002PR01"]["top"][0]["categoryMap"]["SUB_ID_CATEGORY"]
                    print(f"[{self.site_id}] Category counts: {counts}")
                except (KeyError, TypeError, IndexError):
                    pass
                total = data.get("data", {}).get("ASIPDI002PR01", {}).get("totalCount")
                if total is not None:
                    print(f"[{self.site_id}] Total records: {total}")

            # Incremental mode: skip pages where all items already exist
            if incremental:
                doc_ids = [str(it.get("dcm", {}).get("DOC_ID", "")) for it in items]
                doc_ids = [d for d in doc_ids if d]
                if doc_ids:
                    existing = self._conn.execute(
                        "SELECT external_id FROM papers WHERE site_id = ? AND external_id IN ({})".format(
                            ",".join("?" * len(doc_ids))
                        ),
                        [self.site_id] + doc_ids
                    ).fetchall()
                    existing_ids = {r[0] for r in existing}
                    new_count = len([d for d in doc_ids if d not in existing_ids])
                    if new_count == 0:
                        print(f"[{self.site_id}] Incremental: page {page} has no new items. Stopping.")
                        break
                    print(f"[{self.site_id}] Incremental: {new_count} new / {len(doc_ids)} on page {page}")

            for item in items:
                if limit is not None and saved >= limit:
                    break

                dcm = item.get("dcm", {})
                doc_id = str(dcm.get("DOC_ID", ""))

                # Skip already-existing documents in incremental mode
                if incremental and doc_id:
                    check = self._conn.execute(
                        "SELECT 1 FROM papers WHERE site_id = ? AND external_id = ?",
                        [self.site_id, doc_id]
                    ).fetchone()
                    if check:
                        continue

                title = dcm.get("TTL", "")
                content = dcm.get("CNTN", "") or ""
                gist = dcm.get("GIST_CNTN", "") or ""
                doc_number = dcm.get("NTST_DCM_DSCM_CNTN", "") or ""
                tax_category = dcm.get("NTST_TLAW_CL_NM", "") or ""
                doc_type_name = dcm.get("NTST_DCM_CL_NM", "") or ""
                raw_date = dcm.get("DCM_RGT_DTM", "") or ""
                file_id = dcm.get("NTST_FLE_ID", "") or ""
                src_org_cd = dcm.get("NTST_DCM_SRCS_ORGN_CL_CD", "") or ""
                reply_ref = dcm.get("NTST_DCM_RPLY_CNTN", "") or ""

                published_date = self._parse_date(raw_date)

                # Fetch detail
                html_body = ""
                keyword_list = []
                related_laws: list[str] = []
                trial_history: list[str] = []
                detail_gist = ""
                detail_content = ""
                referenced_cases: list[str] = []  # dcmRfrnPrtsList — 참조판례
                cited_cases: list[str] = []      # dcmQutPrtsList  — 인용판례
                related_topics: list[str] = []   # dcmRltnStttMatrList — 관련법령 주제어
                attached_files: list[dict] = []  # fleDVOList
                detail_extra: dict = {}

                time.sleep(self._delay)
                detail = self._fetch_detail(doc_id) if doc_id else None

                if detail:
                    dvo = detail.get("dcmDVO") or {}
                    detail_gist = dvo.get("ntstDcmGistCntn", "") or ""
                    detail_content = dvo.get("ntstDcmCntn", "") or ""
                    keywords_raw = dvo.get("ntstDcmMatrCntn", "") or ""
                    if keywords_raw:
                        keyword_list = [k.strip() for k in keywords_raw.split(",") if k.strip()]

                    # Additional dvo fields (extend metadata)
                    detail_extra = {
                        "attrYr": dvo.get("attrYr"),
                        "decisionClassCd": dvo.get("ntstDcmDcsClCd"),
                        "reviewResultCd": dvo.get("ntstDcmInveRsltCd"),
                        "reviewReason": dvo.get("ntstDcmInveRsn"),
                        "supremeCourtAllAgmt": dvo.get("sprcJdgmAllAgmtYn"),
                        "caseNumber": dvo.get("dsbdHpnnNo"),
                        "attachedFileId": dvo.get("ntstWpFleId"),
                        "firstRegDtm": dvo.get("frsRgtDtm"),
                        "lastAltDtm": dvo.get("lstAltDtm"),
                        "inputOrgCd": dvo.get("inptOptrTxhfOgzCd"),
                    }
                    # Drop meaningless placeholder values
                    detail_extra = {k: v for k, v in detail_extra.items()
                                    if v not in (None, "", "ZZ", "ZZZ", "ZZZZ",
                                                 "ZZZZZ", "ZZZZZZ", "ZZZZZZZ")}

                    # HTML body from editor list
                    raw_html_content = ""
                    for editor_item in (detail.get("dcmHwpEditorDVOList") or []):
                        if editor_item.get("dcmFleTy") == "html":
                            raw_html = editor_item.get("dcmFleByte", "") or ""
                            if raw_html:
                                raw_html_content = raw_html
                                html_body = self._strip_tags(raw_html)
                                break

                    # Persist raw HTML to file (preserve tables/images) — wiki use
                    raw_html_path = ""
                    if raw_html_content and doc_id:
                        html_dir = _HTML_EXPORT_ROOT / self.site_id / "_html"
                        html_dir.mkdir(parents=True, exist_ok=True)
                        html_file = html_dir / f"{doc_id}.html"
                        try:
                            html_file.write_text(raw_html_content, encoding="utf-8")
                            raw_html_path = str(html_file.relative_to(_ROOT))
                        except Exception as e:
                            print(f"[{self.site_id}] raw HTML 저장 실패 ({doc_id}): {e}")

                    # Related laws (이름 + 조문 ID)
                    for law in (detail.get("dcmRltnStttList") or []):
                        name = law.get("ntstTextNm", "") or ""
                        if name:
                            related_laws.append(name)

                    # Trial history
                    for trial in (detail.get("trilPsagList") or []):
                        case_num = trial.get("ntstDcmDscmCntn", "") or ""
                        if case_num:
                            trial_history.append(case_num)

                    # Referenced cases (참조판례)
                    for prts in (detail.get("dcmRfrnPrtsList") or []):
                        no = prts.get("ntstDcmDscmCntn", "") or ""
                        if no:
                            referenced_cases.append(no)

                    # Cited cases (인용판례)
                    for prts in (detail.get("dcmQutPrtsList") or []):
                        no = prts.get("ntstDcmDscmCntn", "") or ""
                        if no:
                            cited_cases.append(no)

                    # Related-law topics (주제어)
                    for matr in (detail.get("dcmRltnStttMatrList") or []):
                        name = matr.get("ntstTextNm") or matr.get("matrCntn") or ""
                        if name:
                            related_topics.append(name)

                    # Attached files
                    for f in (detail.get("fleDVOList") or []):
                        nm = f.get("fleOrgNm") or f.get("fleNm") or ""
                        fid = f.get("fleId") or f.get("ntstFleId") or ""
                        if nm or fid:
                            attached_files.append({"name": nm, "fileId": fid})

                # Build abstract from best available content
                abstract_parts = []
                for part in [detail_gist or gist, detail_content or content, html_body]:
                    if part and part not in abstract_parts:
                        abstract_parts.append(part)
                abstract = "\n\n".join(abstract_parts)

                paper = {
                    "id": None,
                    "site_id": self.site_id,
                    "external_id": doc_id,
                    "title": title,
                    "authors": json.dumps([], ensure_ascii=False),
                    "abstract": abstract,
                    "category": tax_category,
                    "keywords": json.dumps(keyword_list, ensure_ascii=False),
                    "published_date": published_date,
                    "url": f"https://taxlaw.nts.go.kr/pd/USEPDA002P.do?ntstDcmId={doc_id}",
                    "pdf_url": "",
                    "doi": "",
                    "department": "",
                    "metadata": json.dumps({
                        "documentNumber": doc_number,
                        "documentTypeName": doc_type_name,
                        "replyReference": reply_ref,
                        "fileId": file_id,
                        "sourceOrgCode": src_org_cd,
                        "relatedLaws": related_laws,
                        "trialHistory": trial_history,
                        "referencedCases": referenced_cases,
                        "citedCases": cited_cases,
                        "relatedTopics": related_topics,
                        "attachedFiles": attached_files,
                        "rawHtmlPath": raw_html_path,
                        **detail_extra,
                    }, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                counter = f"{saved}/{limit}" if limit else str(saved)
                print(f"[{self.site_id}] Saved {counter}: {title[:60]}")

                # 불완전 수집 로그 (별도 JSONL — 사후 분석용)
                issues: list[str] = []
                if not abstract.strip():
                    issues.append("empty_abstract")
                elif len(abstract) < 200:
                    issues.append("shallow_abstract")
                if not raw_html_path:
                    issues.append("missing_html")
                if not related_laws:
                    issues.append("missing_laws")
                if not doc_number:
                    issues.append("missing_doc_no")
                if issues:
                    incomplete_dir = _HTML_EXPORT_ROOT / self.site_id
                    incomplete_dir.mkdir(parents=True, exist_ok=True)
                    rec = {
                        "external_id": doc_id,
                        "doc_number": doc_number,
                        "title": title,
                        "abstract_len": len(abstract),
                        "category": tax_category,
                        "published_date": published_date,
                        "issues": issues,
                    }
                    with (incomplete_dir / "_incomplete.jsonl").open(
                            "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    def scan_index(self, doc_type=None, mark_deleted=True):
        """Scan list API pages to build/refresh ``doc_index`` for this site.

        Only uses the cheap list endpoint — no per-document detail fetches.
        Upserts every observed ``DOC_ID`` and, once the scan completes
        successfully, marks rows not observed in this scan as deleted.

        Returns the number of DOC_IDs observed.
        """
        import json as _json
        from datetime import datetime

        from .. import db as _dbm

        scan_started_at = datetime.utcnow().isoformat(sep=" ", timespec="seconds")
        print(f"[{self.site_id}] scan_index: start ({scan_started_at})")

        observed = 0
        page = 1
        empty_streak = 0  # consecutive pages that failed to fetch
        total_on_server: int | None = None

        while True:
            time.sleep(0.5)  # lighter than normal crawl delay (list API only)

            raw = None
            data = None
            for retry in range(5):
                raw = self._fetch_list(page, doc_type=doc_type)
                if not raw:
                    wait = (retry + 1) * 5
                    print(f"[{self.site_id}] scan_index: empty response at page {page}, "
                          f"retry {retry + 1}/5 in {wait}s...")
                    time.sleep(wait)
                    continue
                try:
                    data = _json.loads(raw)
                    break
                except json.JSONDecodeError:
                    wait = (retry + 1) * 5
                    print(f"[{self.site_id}] scan_index: bad JSON at page {page}, "
                          f"retry {retry + 1}/5 in {wait}s...")
                    time.sleep(wait)

            if data is None:
                empty_streak += 1
                if empty_streak >= 3:
                    print(f"[{self.site_id}] scan_index: {empty_streak} consecutive "
                          f"failures at page {page}. Aborting (NOT marking deleted).")
                    return observed
                page += 1
                continue
            empty_streak = 0

            try:
                items = data["data"]["ASIPDI002PR01"]["body"]
            except (KeyError, TypeError):
                print(f"[{self.site_id}] scan_index: unexpected response at page {page}. Stopping.")
                break

            if page == 1:
                total_on_server = (data.get("data", {})
                                       .get("ASIPDI002PR01", {})
                                       .get("totalCount"))
                if total_on_server is not None:
                    print(f"[{self.site_id}] scan_index: total on server = {total_on_server}")

            if not items:
                print(f"[{self.site_id}] scan_index: no more items at page {page}. Done.")
                break

            for item in items:
                dcm = item.get("dcm", {}) or {}
                doc_id = str(dcm.get("DOC_ID", "") or "")
                if not doc_id:
                    continue

                doc_number = dcm.get("NTST_DCM_DSCM_CNTN", "") or ""
                title = dcm.get("TTL", "") or ""
                doc_type_name = dcm.get("NTST_DCM_CL_NM", "") or ""
                category = dcm.get("NTST_TLAW_CL_NM", "") or ""
                raw_date = dcm.get("DCM_RGT_DTM", "") or ""
                last_alt = (dcm.get("LST_ALT_DTM")
                            or dcm.get("LAST_ALT_DTM")
                            or dcm.get("DCM_LAST_ALT_DTM")
                            or "")
                published_date = self._parse_date(raw_date)

                # Strip API highlight markers from indexed text
                doc_number = re.sub(r"<!H[SE]>", "", doc_number)
                title = re.sub(r"<!H[SE]>", "", title)

                # Keep full raw dcm dict for future analysis (small, text-only)
                extra = _json.dumps({k: v for k, v in dcm.items()
                                     if k not in {"CNTN", "GIST_CNTN"}},
                                    ensure_ascii=False)

                _dbm.upsert_doc_index(
                    self._conn, self.site_id, doc_id,
                    doc_number=doc_number or None,
                    title=title or None,
                    doc_type=doc_type_name or None,
                    category=category or None,
                    published_date=published_date or None,
                    last_altered_at=last_alt or None,
                    extra=extra,
                )
                observed += 1

            # Commit once per page (batch) — avoid per-row commit overhead
            self._conn.commit()

            if page % 20 == 0:
                print(f"[{self.site_id}] scan_index: page {page}, observed {observed} so far")

            page += 1

        if mark_deleted and observed > 0:
            n_deleted = _dbm.mark_doc_index_deleted(self._conn, self.site_id, scan_started_at)
            print(f"[{self.site_id}] scan_index: marked {n_deleted} row(s) as deleted (unseen this scan)")

        # Print final summary
        stats = _dbm.get_doc_index_stats(self._conn, self.site_id)
        for r in stats:
            print(f"[{self.site_id}] doc_index: total={r['total']} active={r['active']} deleted={r['deleted']}")
        print(f"[{self.site_id}] scan_index: observed {observed} DOC_IDs across {page - 1} pages")
        return observed

    def gap_fill(self, doc_type=None):
        """Fast gap fill: scan list pages for DOC_IDs, then fetch only missing ones."""
        import json as _json

        # Phase 1: Scan all list pages to collect DOC_IDs (no detail fetch)
        print(f"[{self.site_id}] Gap fill Phase 1: Scanning list pages...")
        all_doc_ids = []
        page = 1
        while True:
            time.sleep(0.5)  # faster than normal crawl delay
            raw = self._fetch_list(page, doc_type=doc_type)
            if not raw:
                page += 1
                if page > 3:  # 3 consecutive failures = stop
                    break
                continue

            try:
                data = _json.loads(raw)
                items = data["data"]["ASIPDI002PR01"]["body"]
            except (json.JSONDecodeError, KeyError, TypeError):
                page += 1
                continue

            if not items:
                print(f"[{self.site_id}] Phase 1: No more items at page {page}. Scan complete.")
                break

            if page == 1:
                total = data.get("data", {}).get("ASIPDI002PR01", {}).get("totalCount")
                if total is not None:
                    print(f"[{self.site_id}] Total records on server: {total}")

            for item in items:
                dcm = item.get("dcm", {})
                doc_id = str(dcm.get("DOC_ID", ""))
                if doc_id:
                    all_doc_ids.append(doc_id)

            if page % 100 == 0:
                print(f"[{self.site_id}] Phase 1: Scanned {page} pages, {len(all_doc_ids)} DOC_IDs...")

            page += 1

        print(f"[{self.site_id}] Phase 1 done: {len(all_doc_ids)} DOC_IDs from {page - 1} pages.")

        # Phase 2: Find missing DOC_IDs
        print(f"[{self.site_id}] Phase 2: Finding missing DOC_IDs...")
        existing = set()
        # Query in batches of 500
        for i in range(0, len(all_doc_ids), 500):
            batch = all_doc_ids[i:i + 500]
            rows = self._conn.execute(
                "SELECT external_id FROM papers WHERE site_id = ? AND external_id IN ({})".format(
                    ",".join("?" * len(batch))
                ),
                [self.site_id] + batch
            ).fetchall()
            existing.update(r[0] for r in rows)

        missing = [d for d in all_doc_ids if d not in existing]
        # Remove duplicates while preserving order
        seen = set()
        unique_missing = []
        for d in missing:
            if d not in seen:
                seen.add(d)
                unique_missing.append(d)
        missing = unique_missing

        print(f"[{self.site_id}] Phase 2 done: {len(existing)} existing, {len(missing)} missing.")

        if not missing:
            print(f"[{self.site_id}] No missing documents. Done.")
            return 0

        # Phase 3: Fetch detail and save for missing DOC_IDs only
        print(f"[{self.site_id}] Phase 3: Fetching {len(missing)} missing documents...")
        saved = 0
        for i, doc_id in enumerate(missing, 1):
            time.sleep(self._delay)
            detail = self._fetch_detail(doc_id)
            if not detail:
                print(f"[{self.site_id}] Failed to fetch detail for {doc_id}, skipping.")
                continue

            dvo = detail.get("dcmDVO") or {}
            title = dvo.get("ntstDcmTtl") or dvo.get("TTL") or dvo.get("ttl") or ""
            doc_number = dvo.get("ntstDcmDscmCntn") or ""
            tax_category = dvo.get("ntstTlawClNm") or ""
            # DVO has code only (ntstDcmClCd), not name — map it
            _DCM_CL_NAMES = {
                "01": "사전", "02": "질의", "03": "기준", "04": "고시",
                "05": "적부", "06": "이의", "07": "심사", "08": "심판",
                "09": "판례", "10": "헌재",
            }
            doc_type_code = dvo.get("ntstDcmClCd") or ""
            doc_type_name = _DCM_CL_NAMES.get(doc_type_code, "")
            raw_date = dvo.get("dcmRgtDtm") or ""
            file_id = dvo.get("ntstFleId") or ""
            src_org_cd = dvo.get("ntstDcmSrcsOrgnClCd") or ""
            reply_ref = dvo.get("ntstDcmRplyCntn") or ""
            detail_gist = dvo.get("ntstDcmGistCntn") or ""
            detail_content = dvo.get("ntstDcmCntn") or ""
            keywords_raw = dvo.get("ntstDcmMatrCntn") or ""
            keyword_list = [k.strip() for k in keywords_raw.split(",") if k.strip()] if keywords_raw else []

            published_date = self._parse_date(raw_date)

            # Additional dvo fields
            detail_extra = {
                "attrYr": dvo.get("attrYr"),
                "decisionClassCd": dvo.get("ntstDcmDcsClCd"),
                "reviewResultCd": dvo.get("ntstDcmInveRsltCd"),
                "reviewReason": dvo.get("ntstDcmInveRsn"),
                "supremeCourtAllAgmt": dvo.get("sprcJdgmAllAgmtYn"),
                "caseNumber": dvo.get("dsbdHpnnNo"),
                "attachedFileId": dvo.get("ntstWpFleId"),
                "firstRegDtm": dvo.get("frsRgtDtm"),
                "lastAltDtm": dvo.get("lstAltDtm"),
                "inputOrgCd": dvo.get("inptOptrTxhfOgzCd"),
            }
            detail_extra = {k: v for k, v in detail_extra.items()
                            if v not in (None, "", "ZZ", "ZZZ", "ZZZZ",
                                         "ZZZZZ", "ZZZZZZ", "ZZZZZZZ")}

            # HTML body
            html_body = ""
            raw_html_content = ""
            for editor_item in (detail.get("dcmHwpEditorDVOList") or []):
                if editor_item.get("dcmFleTy") == "html":
                    raw_html = editor_item.get("dcmFleByte", "") or ""
                    if raw_html:
                        raw_html_content = raw_html
                        html_body = self._strip_tags(raw_html)
                        break

            # Save raw HTML
            raw_html_path = ""
            if raw_html_content and doc_id:
                html_dir = _HTML_EXPORT_ROOT / self.site_id / "_html"
                html_dir.mkdir(parents=True, exist_ok=True)
                html_file = html_dir / f"{doc_id}.html"
                try:
                    html_file.write_text(raw_html_content, encoding="utf-8")
                    raw_html_path = str(html_file.relative_to(_ROOT))
                except Exception as e:
                    print(f"[{self.site_id}] raw HTML save failed ({doc_id}): {e}")

            # Related laws
            related_laws = [law.get("ntstTextNm", "") for law in (detail.get("dcmRltnStttList") or []) if law.get("ntstTextNm")]
            # Trial history
            trial_history = [t.get("ntstDcmDscmCntn", "") for t in (detail.get("trilPsagList") or []) if t.get("ntstDcmDscmCntn")]
            # Referenced cases
            referenced_cases = [p.get("ntstDcmDscmCntn", "") for p in (detail.get("dcmRfrnPrtsList") or []) if p.get("ntstDcmDscmCntn")]
            # Cited cases
            cited_cases = [p.get("ntstDcmDscmCntn", "") for p in (detail.get("dcmQutPrtsList") or []) if p.get("ntstDcmDscmCntn")]
            # Related topics
            related_topics = [(m.get("ntstTextNm") or m.get("matrCntn") or "") for m in (detail.get("dcmRltnStttMatrList") or []) if (m.get("ntstTextNm") or m.get("matrCntn"))]
            # Attached files
            attached_files = [{"name": f.get("fleOrgNm") or f.get("fleNm") or "", "fileId": f.get("fleId") or f.get("ntstFleId") or ""} for f in (detail.get("fleDVOList") or []) if (f.get("fleOrgNm") or f.get("fleNm") or f.get("fleId") or f.get("ntstFleId"))]

            # Build abstract
            abstract_parts = []
            for part in [detail_gist, detail_content, html_body]:
                if part and part not in abstract_parts:
                    abstract_parts.append(part)
            abstract = "\n\n".join(abstract_parts)

            paper = {
                "id": None,
                "site_id": self.site_id,
                "external_id": doc_id,
                "title": title,
                "authors": json.dumps([], ensure_ascii=False),
                "abstract": abstract,
                "category": tax_category,
                "keywords": json.dumps(keyword_list, ensure_ascii=False),
                "published_date": published_date,
                "url": f"https://taxlaw.nts.go.kr/pd/USEPDA002P.do?ntstDcmId={doc_id}",
                "pdf_url": "",
                "doi": "",
                "department": "",
                "metadata": json.dumps({
                    "documentNumber": doc_number,
                    "documentTypeName": doc_type_name,
                    "replyReference": reply_ref,
                    "fileId": file_id,
                    "sourceOrgCode": src_org_cd,
                    "relatedLaws": related_laws,
                    "trialHistory": trial_history,
                    "referencedCases": referenced_cases,
                    "citedCases": cited_cases,
                    "relatedTopics": related_topics,
                    "attachedFiles": attached_files,
                    "rawHtmlPath": raw_html_path,
                    **detail_extra,
                }, ensure_ascii=False),
            }

            self._save_paper(paper)
            saved += 1
            print(f"[{self.site_id}] Gap fill {saved}/{len(missing)}: {title[:60]}")

        print(f"[{self.site_id}] Gap fill done. {saved} new documents saved.")
        return saved


class NTSTaxlawQtCrawler(_NTSTaxlawBase):
    """Crawler for 국세법령 세법해석례."""

    site_id = "nts-taxlaw-qt"
    site_name = "국세법령 세법해석례"

    _COLLECTION = "question,question_gr"
    _DCM_CODES = ["001_01", "001_02", "001_03", "001_04"]
    _DOC_MODE_COLLECTION = "question"


class NTSTaxlawPdCrawler(_NTSTaxlawBase):
    """Crawler for 국세법령 판례."""

    site_id = "nts-taxlaw-pd"
    site_name = "국세법령 판례"

    _COLLECTION = "precedent,precedent_gr"
    _DCM_CODES = ["001_05", "001_06", "001_07", "001_08", "001_09", "001_10"]
    _DOC_MODE_COLLECTION = "precedent"


# Backward-compat alias (old code referenced NTSTaxlawCrawler)
NTSTaxlawCrawler = NTSTaxlawQtCrawler
