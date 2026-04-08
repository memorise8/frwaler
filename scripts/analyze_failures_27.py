#!/usr/bin/env python3
"""Analyze the 27 still-failed sites from the retry run using GPT."""
import json, os, sys, time
sys.path.insert(0, ".")

# Load API key from crawler/.env
with open("crawler/.env") as f:
    for line in f:
        if "=" in line:
            k, v = line.strip().split("=", 1)
            os.environ[k] = v

from openai import OpenAI
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup


def main():
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    with open("reports/kr_retry_smartfind_20260407_011956.json") as f:
        data = json.load(f)

    still_failed = data["still_failed"]
    print(f"분석 대상: {len(still_failed)}개\n")

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"
    )

    results = []
    for i, site in enumerate(still_failed):
        url = site["url"]
        name = site["name"]
        print(f"[{i+1}/{len(still_failed)}] {name} ...")

        # Get rendered HTML context
        try:
            page.goto(url, timeout=20000, wait_until="networkidle")
            html = page.content()
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup.select("script, style, noscript, svg"):
                tag.decompose()

            all_links = soup.select("a[href]")
            file_links = [
                a for a in all_links
                if any(x in (a.get("href", "").lower()) for x in
                       [".pdf", ".hwp", ".xlsx", ".xls", ".doc", ".docx",
                        "download", "attach", "filesrc", "filedown"])
            ]
            iframes = soup.select("iframe[src]")
            onclick_tags = soup.select("[onclick]")

            file_samples = "\n".join(
                f"  {a.get_text(strip=True)[:30]} -> {a.get('href', '')[:60]}"
                for a in file_links[:5]
            )
            onclick_samples = "\n".join(
                f"  {t.get('onclick', '')[:80]}" for t in onclick_tags[:5]
            )

            context = (
                f"렌더링된 HTML 분석:\n"
                f"- 전체 링크: {len(all_links)}개\n"
                f"- 파일 관련 링크: {len(file_links)}개\n"
                f"- iframe: {len(iframes)}개\n"
                f"- onclick 핸들러: {len(onclick_tags)}개\n"
                f"- 텍스트 길이: {len(soup.get_text(strip=True))}자\n\n"
                f"파일 관련 링크 샘플:\n{file_samples}\n\n"
                f"onclick 샘플:\n{onclick_samples}\n\n"
                f"페이지 텍스트 일부:\n{soup.get_text(strip=True)[:1000]}"
            )
        except Exception as e:
            context = f"페이지 접근 실패: {str(e)[:200]}"

        # Call GPT
        gpt_analysis = {}
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "system",
                    "content": "웹 크롤링 전문가. 한국어로 답변. 반드시 JSON만 출력."
                }, {
                    "role": "user",
                    "content": (
                        f"URL: {url}\n{context}\n\n"
                        "이 사이트에서 다운로드 파일을 못 찾은 정확한 원인을 분석하세요.\n\n"
                        "가능한 원인:\n"
                        "A. JavaScript onclick으로만 다운로드 (fn_download 등)\n"
                        "B. 첨부파일이 팝업/새창으로 제공\n"
                        "C. 파일이 AJAX API로 동적 로딩\n"
                        "D. 파일 다운로드에 로그인/세션 필요\n"
                        "E. 실제 다운로드 파일이 없는 페이지 (뉴스 텍스트만)\n"
                        "F. 파일 링크가 있지만 크롤러가 감지 못함 (특수 패턴)\n"
                        "G. 페이지 로딩 실패/타임아웃\n"
                        "H. 첨부파일이 게시글 하위에만 존재 (목록에서 안 보임)\n\n"
                        'JSON: {"cause": "A-H 중 하나", "cause_ko": "원인", '
                        '"detail": "구체적 설명 1-2문장", "suggestion": "해결방안 1문장"}'
                    )
                }],
                temperature=0,
                max_completion_tokens=200,
            )

            text = response.choices[0].message.content.strip()
            if "```" in text:
                text = text.split("```")[1].strip()
                if text.startswith("json"):
                    text = text[4:].strip()
            gpt_analysis = json.loads(text)
            print(f"  {gpt_analysis.get('cause', '?')}. {gpt_analysis.get('cause_ko', '')}")
        except Exception as e:
            gpt_analysis = {"cause": "?", "cause_ko": "분석 실패", "detail": str(e)[:100], "suggestion": ""}
            print(f"  Error: {str(e)[:60]}")

        results.append({
            "name": name,
            "url": url,
            "gpt_analysis": gpt_analysis,
        })

        time.sleep(0.5)

    page.close()
    browser.close()
    pw.stop()

    # Build cause summary
    cause_summary = {}
    for r in results:
        cause = r["gpt_analysis"].get("cause", "?")
        cause_ko = r["gpt_analysis"].get("cause_ko", "기타")
        key = f"{cause}. {cause_ko}"
        if key not in cause_summary:
            cause_summary[key] = []
        cause_summary[key].append(r["name"])

    output = {
        "total": len(results),
        "analyzed_at": "2026-04-07",
        "cause_summary": cause_summary,
        "results": results,
    }

    with open("reports/kr_failure_analysis.json", "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n완료: {len(results)}개 저장 → reports/kr_failure_analysis.json")
    print("\n원인 분류:")
    for cause, sites in sorted(cause_summary.items()):
        print(f"  {cause}: {len(sites)}개 — {', '.join(sites)}")


if __name__ == "__main__":
    main()
