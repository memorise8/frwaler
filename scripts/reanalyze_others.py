#!/usr/bin/env python3
"""Re-analyze 'other' category failures with more specific GPT prompt."""
import json, os, sys, time
sys.path.insert(0, ".")

from openai import OpenAI
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup


def main():
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    with open("reports/kr_failure_analysis.json") as f:
        data = json.load(f)

    others = [r for r in data["results"] if r["gpt_analysis"].get("cause_number") == 8]
    print(f"재분석 대상: {len(others)}개\n")

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0")

    updated = 0
    for i, r in enumerate(others):
        url = r["url"]
        name = r["name"]
        print(f"[{i+1}/{len(others)}] {name}...")

        # Get rendered HTML
        try:
            page.goto(url, timeout=15000, wait_until="networkidle")
            html = page.content()
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup.select("script, style, noscript, svg"):
                tag.decompose()

            all_links = soup.select("a[href]")
            file_links = [a for a in all_links if any(x in (a.get("href", "").lower()) for x in [".pdf", ".hwp", ".xlsx", "download", "attach", "filesrc"])]
            iframes = soup.select("iframe[src]")
            onclick_tags = soup.select("[onclick]")

            file_samples = "\n".join(
                f"  {a.get_text(strip=True)[:30]} -> {a.get('href', '')[:60]}" for a in file_links[:5]
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

        try:
            response = client.chat.completions.create(
                model="gpt-5.4-mini",
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
                        'JSON: {"cause": "A-H 중 하나", "cause_ko": "원인", "detail": "구체적 설명 1-2문장", "suggestion": "해결방안 1문장"}'
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
            analysis = json.loads(text)

            for orig in data["results"]:
                if orig["url"] == url:
                    orig["gpt_analysis"] = analysis
                    updated += 1

            print(f"  {analysis.get('cause', '?')}. {analysis.get('cause_ko', '')}")
        except Exception as e:
            print(f"  Error: {str(e)[:60]}")

        time.sleep(0.3)

    page.close()
    browser.close()
    pw.stop()

    # Rebuild cause summary
    data["cause_summary"] = {}
    for r in data["results"]:
        cause = r["gpt_analysis"].get("cause", r["gpt_analysis"].get("cause_number", "?"))
        cause_ko = r["gpt_analysis"].get("cause_ko", "기타")
        key = f"{cause}. {cause_ko}"
        if key not in data["cause_summary"]:
            data["cause_summary"][key] = []
        data["cause_summary"][key].append(r["name"])

    with open("reports/kr_failure_analysis.json", "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"\n업데이트: {updated}개")
    print(f"\n새 원인 분류:")
    for cause, sites in sorted(data["cause_summary"].items()):
        print(f"  {cause}: {len(sites)}개")


if __name__ == "__main__":
    main()
