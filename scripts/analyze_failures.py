#!/usr/bin/env python3
"""Analyze why smart finder failed on certain sites using GPT-5.4-mini."""
import json, glob, os, sys, time
from datetime import datetime
from urllib.parse import urlparse
sys.path.insert(0, ".")

def analyze_with_gpt(url, html_snippet, page_info):
    """Ask GPT-5.4-mini to analyze why no downloadable files were found."""
    from openai import OpenAI
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    response = client.chat.completions.create(
        model="gpt-5.4-mini",
        messages=[{
            "role": "system",
            "content": "You are a web crawling expert. Analyze why a crawler couldn't find downloadable files on a Korean government/research website. Respond in Korean. Be concise (2-3 sentences max)."
        }, {
            "role": "user",
            "content": f"""이 사이트에서 다운로드 가능한 파일을 찾지 못했습니다. 원인을 분석해주세요.

URL: {url}
방문한 상세 페이지 수: {page_info.get('details', 0)}
페이지 접근 방법: {page_info.get('method', 'unknown')}
기존 실패 원인: {page_info.get('reason', '')}

페이지 HTML 일부 (축약):
{html_snippet[:2000]}

다음 중 해당하는 원인을 선택하고 설명해주세요:
1. 파일이 JavaScript onclick/popup으로만 다운로드됨
2. 첨부파일이 별도 팝업 창이나 iframe에서 제공됨
3. 로그인/인증이 필요한 콘텐츠
4. 파일이 외부 CDN/별도 도메인에서 제공됨
5. 페이지에 실제 다운로드 가능한 파일이 없음 (뉴스/공지만 있음)
6. 동적 콘텐츠 로딩 (AJAX)으로 파일 목록이 별도 API에서 로딩됨
7. 사이트 접근 차단 (IP 차단, 인증서 오류 등)
8. 기타

JSON으로 응답: {{"cause_number": 1-8, "cause_ko": "원인 한글", "detail": "상세 설명", "suggestion": "해결 방안"}}"""
        }],
        temperature=0,
        max_completion_tokens=300,
    )

    text = response.choices[0].message.content.strip()
    if "```" in text:
        text = text.split("```")[1].strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except:
        return {"cause_number": 8, "cause_ko": "분석 실패", "detail": text[:200], "suggestion": "수동 확인 필요"}

def fetch_page_html(url):
    """Fetch page HTML with browser for analysis."""
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0")
        page.goto(url, timeout=15000, wait_until="networkidle")
        html = page.content()
        page.close()
        browser.close()
        pw.stop()

        # Clean and truncate
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.select("script, style, noscript, svg, path, meta, link"):
            tag.decompose()
        return soup.get_text(strip=True)[:3000]
    except Exception as e:
        return f"페이지 접근 실패: {str(e)[:200]}"

def main():
    report = sorted(glob.glob("reports/kr_smartfind_*.json"))[-1]
    with open(report) as f:
        data = json.load(f)

    failed = data["failed"]
    print(f"Analyzing {len(failed)} failed sites with GPT-5.4-mini...\n")

    results = []
    for i, site in enumerate(failed):
        name = site["name"]
        url = site["url"]
        print(f"[{i+1}/{len(failed)}] {name}...")

        html_snippet = fetch_page_html(url)
        analysis = analyze_with_gpt(url, html_snippet, site)

        results.append({
            "name": name,
            "url": url,
            "original_reason": site["reason"],
            "method": site["method"],
            "details_visited": site["details"],
            "gpt_analysis": analysis,
        })

        cause = analysis.get("cause_ko", "?")
        suggestion = analysis.get("suggestion", "")
        print(f"  원인: {cause}")
        print(f"  제안: {suggestion}")

        time.sleep(0.5)  # Rate limit

    # Save
    output = {
        "analyzed_at": datetime.now().isoformat(),
        "model": "gpt-5.4-mini",
        "total": len(results),
        "results": results,
        # Cause summary
        "cause_summary": {},
    }

    # Summarize causes
    for r in results:
        cn = r["gpt_analysis"].get("cause_number", 8)
        cause = r["gpt_analysis"].get("cause_ko", "기타")
        key = f"{cn}. {cause}"
        if key not in output["cause_summary"]:
            output["cause_summary"][key] = []
        output["cause_summary"][key].append(r["name"])

    out_path = "reports/kr_failure_analysis.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"분석 완료: {len(results)}개 사이트")
    print(f"저장: {out_path}")
    print(f"\n원인 분류:")
    for cause, sites in output["cause_summary"].items():
        print(f"  {cause}: {len(sites)}개")
        for s in sites:
            print(f"    - {s}")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
