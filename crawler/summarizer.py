# -*- coding: utf-8 -*-
"""AI summarization module using OpenAI GPT-4o-mini."""

import io
import os
import urllib.request

from dotenv import load_dotenv

# Load .env from crawler directory
_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(_ENV_PATH)

import openai
import PyPDF2

from . import db as db_module


def extract_text_from_hwpx(file_path, max_chars=12000):
    """Extract text from an HWPX file (ZIP-based Hangul format)."""
    import zipfile
    from bs4 import BeautifulSoup
    try:
        if not zipfile.is_zipfile(file_path):
            return None
        texts = []
        with zipfile.ZipFile(file_path) as z:
            for name in sorted(z.namelist()):
                if 'section' in name.lower() and name.endswith('.xml'):
                    content = z.read(name).decode('utf-8')
                    soup = BeautifulSoup(content, 'xml')
                    text = soup.get_text(separator=' ', strip=True)
                    if text:
                        texts.append(text)
        result = '\n'.join(texts)
        return result[:max_chars] if result else None
    except Exception as e:
        print(f"  HWPX extraction error: {e}")
        return None


def download_file_curl(url, dest_path):
    """Download a file using curl (handles SSL issues)."""
    import subprocess, urllib.parse
    # URL-encode properly if needed
    parsed = urllib.parse.urlparse(url)
    if parsed.query:
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        encoded_query = urllib.parse.urlencode({k: v[0] for k, v in params.items()})
        url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{encoded_query}"

    try:
        result = subprocess.run([
            'curl', '-sk', '--tlsv1.2', '--max-time', '60',
            '-o', dest_path,
            '-H', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            url
        ], capture_output=True, text=True, timeout=60)
        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 100:
            return True
        return False
    except Exception as e:
        print(f"  Download error: {e}")
        return False


def extract_text_from_pdf_url(url, max_pages=10):
    """Download a PDF from url and extract text from up to max_pages pages."""
    try:
        import requests as req_lib
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "*/*",
            "Referer": "https://www.mohw.go.kr/",
        }
        resp = req_lib.get(url, headers=headers, timeout=60)
        resp.raise_for_status()
        pdf_bytes = resp.content
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        pages_to_read = min(max_pages, len(reader.pages))
        text_parts = []
        for i in range(pages_to_read):
            page_text = reader.pages[i].extract_text()
            if page_text:
                text_parts.append(page_text)
        return "\n".join(text_parts)
    except Exception as e:
        print(f"  [PDF extraction failed] {e}")
        return None


def summarize_text(client, text, title):
    """Call GPT-4o-mini to produce a 3-5 sentence Korean summary."""
    prompt = (
        f"다음 논문/보고서의 내용을 한국어로 3~5문장으로 요약해 주세요.\n"
        f"제목: {title}\n\n"
        f"내용:\n{text[:8000]}"
    )
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "당신은 학술 논문 요약 전문가입니다. 핵심 내용을 간결하게 한국어로 요약합니다."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=500,
    )
    return response.choices[0].message.content.strip()


def summarize_papers(conn, site_id=None, limit=None):
    """Summarize papers that have no summary yet.

    For MOHW papers: try PDF first, fall back to abstract.
    For NTRS and others: use abstract.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set in environment or .env file.")
        return

    client = openai.OpenAI(api_key=api_key)

    papers = db_module.get_papers_without_summary(conn, site_id=site_id, limit=limit)
    print(f"Found {len(papers)} paper(s) without summary.")

    for paper in papers:
        paper_id = paper["id"]
        title = paper["title"] or "(제목 없음)"
        s_id = paper["site_id"]
        print(f"\n[{s_id}] {title[:80]}")

        text = None
        pdf_url = paper["pdf_url"] or ""

        if pdf_url:
            # Determine file type
            is_hwp = '.hwp' in pdf_url.lower()
            is_pdf = '.pdf' in pdf_url.lower() or 'application/pdf' in pdf_url.lower()

            if is_hwp:
                # Download HWP and extract text
                downloads_dir = os.path.join(os.path.dirname(__file__), '..', 'downloads', s_id)
                os.makedirs(downloads_dir, exist_ok=True)
                ext = '.hwpx' if '.hwpx' in pdf_url.lower() else '.hwp'
                local_path = os.path.join(downloads_dir, f"{paper['external_id']}{ext}")

                if not os.path.exists(local_path):
                    print(f"  Downloading HWP: {pdf_url[:60]}...")
                    download_file_curl(pdf_url, local_path)

                if os.path.exists(local_path):
                    print(f"  Extracting text from HWPX...")
                    text = extract_text_from_hwpx(local_path)

            elif is_pdf:
                print(f"  Extracting text from PDF...")
                text = extract_text_from_pdf_url(pdf_url)

        if not text:
            text = paper["abstract"]
            if text and text.strip():
                print("  Using abstract.")

        if not text or not text.strip():
            doi = paper["doi"] or ""
            text = f"제목: {title}"
            if doi:
                text += f"\nDOI: {doi}"
            print("  Using title for summary.")

        if not text or not text.strip():
            print("  No text available, skipping.")
            continue

        print("  Summarizing with GPT-4o-mini...")
        try:
            summary = summarize_text(client, text, title)
            db_module.update_summary(conn, paper_id, summary)
            print(f"  Summary saved ({len(summary)} chars):")
            print(f"  {summary[:200]}...")
        except Exception as e:
            print(f"  ERROR during summarization: {e}")
