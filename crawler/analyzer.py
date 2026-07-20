# -*- coding: utf-8 -*-
"""Site analyzer that uses an OpenAI model to generate crawler configs.

Model selection (in priority order):
1. ANALYZER_MODEL env var (default: gpt-5.5)
2. Fallback chain: gpt-5.5 → gpt-5 → gpt-4o → gpt-4o-mini
"""

import json
import logging
import os
import re
import time
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup, Comment
from dotenv import load_dotenv
from openai import OpenAI

_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(_ENV_PATH)

CONFIGS_DIR = os.path.join(os.path.dirname(__file__), "sites", "configs")

# Model selection — override with ANALYZER_MODEL env var
ANALYZER_MODEL = os.environ.get("ANALYZER_MODEL", "gpt-5.5")
ANALYZER_FALLBACK_CHAIN = ["gpt-5.5", "gpt-5", "gpt-4o", "gpt-4o-mini"]

logger = logging.getLogger(__name__)


def _parse_json_response(content: str) -> dict:
    """Parse JSON from LLM response, handling markdown fences and leading text."""
    if not content or not content.strip():
        raise ValueError("Empty response content from model")
    # Strip markdown code fences: ```json ... ``` or ``` ... ```
    stripped = re.sub(r"```(?:json)?\s*", "", content).replace("```", "").strip()
    # Try direct parse first
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Extract first {...} block
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise json.JSONDecodeError("No JSON object found in response", content, 0)


def _chat_with_fallback(client, *, messages, response_format, max_tokens, temperature):
    """Call client.chat.completions.create with fallback on model-not-found errors.

    Returns (response_obj, model_used, fallback_used).
    Falls back at most once: tries ANALYZER_MODEL first, then walks
    ANALYZER_FALLBACK_CHAIN until a different model succeeds.

    Newer models (gpt-5.x) require ``max_completion_tokens`` instead of
    ``max_tokens``.  We attempt the call with the appropriate parameter and
    also handle the unsupported-parameter error by retrying with the other
    parameter name before moving to a different model.
    """
    primary = ANALYZER_MODEL
    last_exc = None

    # Models known to use max_completion_tokens; all others use max_tokens.
    # We detect dynamically too via the error message.
    _COMPLETION_TOKENS_MODELS = {"gpt-5.5", "gpt-5", "gpt-4o", "o1", "o3"}

    def _call(model, use_completion_tokens, include_temperature=True, include_response_format=True):
        kwargs = dict(
            model=model,
            messages=messages,
        )
        if include_response_format and response_format:
            kwargs["response_format"] = response_format
        if include_temperature:
            kwargs["temperature"] = temperature
        if use_completion_tokens:
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens
        return client.chat.completions.create(**kwargs)

    for attempt, model in enumerate([primary] + [m for m in ANALYZER_FALLBACK_CHAIN if m != primary]):
        # Pick the right token param based on model name heuristic
        use_ct = any(model.startswith(p) for p in ("gpt-5", "o1", "o3"))
        # gpt-5.x does not support custom temperature; use default only
        use_temp = not any(model.startswith(p) for p in ("gpt-5", "o1", "o3"))
        try:
            resp = _call(model, use_ct, include_temperature=use_temp)
            # gpt-5.x may return empty content when response_format is set;
            # retry without response_format constraint.
            content = resp.choices[0].message.content if resp.choices else None
            if not content or not content.strip():
                print(f"  [analyzer] model={model} returned empty content, retrying without response_format")
                resp = _call(model, use_ct, include_temperature=use_temp, include_response_format=False)
            if attempt == 0:
                logger.debug("analyzer: using model %s (primary)", model)
            else:
                logger.warning("analyzer: primary model %s unavailable, fell back to %s", primary, model)
            print(f"  [analyzer] model={model} fallback={'yes' if attempt > 0 else 'no'}")
            return resp, model, (attempt > 0)
        except Exception as exc:
            err_str = str(exc).lower()
            # Wrong token param? Retry once with the other variant for this model.
            if ("max_tokens" in err_str or "max_completion_tokens" in err_str) and "not supported" in err_str:
                try:
                    resp = _call(model, not use_ct, include_temperature=use_temp)
                    print(f"  [analyzer] model={model} retried with {'max_completion_tokens' if not use_ct else 'max_tokens'}")
                    return resp, model, (attempt > 0)
                except Exception as exc2:
                    err_str = str(exc2).lower()
                    exc = exc2
            # Unsupported temperature? Retry without it.
            if "temperature" in err_str and ("not supported" in err_str or "unsupported" in err_str):
                try:
                    resp = _call(model, use_ct, include_temperature=False)
                    print(f"  [analyzer] model={model} retried without temperature")
                    return resp, model, (attempt > 0)
                except Exception as exc2:
                    err_str = str(exc2).lower()
                    exc = exc2
            # Unsupported response_format? Retry without it.
            if "response_format" in err_str and ("not supported" in err_str or "unsupported" in err_str):
                try:
                    resp = _call(model, use_ct, include_temperature=use_temp, include_response_format=False)
                    print(f"  [analyzer] model={model} retried without response_format")
                    return resp, model, (attempt > 0)
                except Exception as exc2:
                    err_str = str(exc2).lower()
                    exc = exc2
            # Model not found → try next in chain
            if "model_not_found" in err_str or "model not found" in err_str or "does not exist" in err_str:
                print(f"  [analyzer] model {model} not available: {str(exc)[:80]}")
                last_exc = exc
                continue
            raise
    raise RuntimeError(f"All models in fallback chain exhausted. Last error: {last_exc}") from last_exc


def _get_client():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set in environment or .env file.")
    return OpenAI(api_key=api_key)


def _fetch_page(url, verify_ssl=True):
    """Fetch a page with browser-like headers."""
    import urllib3
    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=30, verify=verify_ssl)
        resp.raise_for_status()
        return resp.text
    except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
        if verify_ssl:
            print("  Connection/SSL error. Retrying with verify=False...")
            return _fetch_page(url, verify_ssl=False)
        raise


def _clean_html(html, max_chars=10000):
    """Strip scripts, styles, comments and truncate HTML for LLM analysis."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove non-content elements
    for tag in soup.find_all(["script", "style", "noscript", "svg", "iframe", "link", "meta"]):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    # Remove empty attributes to reduce noise
    for tag in soup.find_all(True):
        # Keep only class, id, href, src attributes
        attrs_to_keep = {}
        for attr in ["class", "id", "href", "src", "action", "name", "type"]:
            if tag.has_attr(attr):
                attrs_to_keep[attr] = tag[attr]
        tag.attrs = attrs_to_keep

    cleaned = str(soup)
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "\n<!-- truncated -->"
    return cleaned


def _analyze_list_page(client, html, url):
    """Analyze a list page structure. Returns (result_dict, model_used, usage)."""
    prompt = f"""당신은 웹 스크래핑 전문가입니다. 아래 HTML은 게시판/목록 페이지입니다.
이 페이지에서 반복되는 아이템(게시글, 논문, 보고서 등)의 구조를 분석해주세요.

URL: {url}

HTML:
{html}

다음 JSON 형식으로 응답해주세요:
{{
  "item_container": "반복 아이템의 CSS 셀렉터 (예: table tbody tr, div.list-item)",
  "item_link": "아이템 내 상세 페이지 링크의 CSS 셀렉터 (예: a.title, td a[href])",
  "item_link_attr": "링크 URL이 있는 속성 (보통 href)",
  "title": "아이템 내 제목 CSS 셀렉터 (예: a.title, td.subject a)",
  "date": "아이템 내 날짜 CSS 셀렉터 (예: td.date, span.date) 또는 null",
  "category": "아이템 내 카테고리 CSS 셀렉터 또는 null",
  "pagination_type": "query_param 또는 none",
  "pagination_param": "페이지네이션 파라미터명 (예: page, nPage, pageNo) 또는 null",
  "pagination_start": 1,
  "items_found": "페이지에서 발견된 아이템 수 (숫자)"
}}

규칙:
- CSS 셀렉터는 구체적으로 작성하세요 (table.boardList tbody tr 처럼)
- 클래스명이 있으면 사용하세요
- 아이템이 없으면 items_found를 0으로 설정하세요
- JSON만 응답하세요"""

    response, model_used, fallback_used = _chat_with_fallback(
        client,
        messages=[
            {"role": "system", "content": "웹 스크래핑 전문가. JSON만 응답합니다."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        max_tokens=800,
        temperature=0.1,
    )
    usage = response.usage
    return _parse_json_response(response.choices[0].message.content), model_used, usage


def _analyze_detail_page(client, html, url):
    """Analyze a detail page structure. Returns (result_dict, model_used, usage)."""
    prompt = f"""당신은 웹 스크래핑 전문가입니다. 아래 HTML은 게시글/논문/보고서의 상세 페이지입니다.
이 페이지에서 주요 정보의 CSS 셀렉터를 찾아주세요.

URL: {url}

HTML:
{html}

다음 JSON 형식으로 응답해주세요:
{{
  "title": "제목 CSS 셀렉터 (예: h1.title, h2.view-title)",
  "abstract": "본문/내용 CSS 셀렉터 (예: div.content, div.view_con) 또는 null",
  "authors": "저자 CSS 셀렉터 (여러 개 선택 가능) 또는 null",
  "date": "작성일/등록일 CSS 셀렉터 또는 null",
  "pdf_link": "PDF 다운로드 링크 CSS 셀렉터 (예: a[href*=download], a[href$=.pdf]) 또는 null",
  "keywords": "키워드/태그 CSS 셀렉터 또는 null",
  "department": "담당부서/기관 CSS 셀렉터 또는 null",
  "doi": "DOI 링크 CSS 셀렉터 또는 null"
}}

규칙:
- CSS 셀렉터는 구체적으로 작성하세요
- 해당 정보가 없으면 null로 설정하세요
- JSON만 응답하세요"""

    response, model_used, fallback_used = _chat_with_fallback(
        client,
        messages=[
            {"role": "system", "content": "웹 스크래핑 전문가. JSON만 응답합니다."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        max_tokens=600,
        temperature=0.1,
    )
    usage = response.usage
    return _parse_json_response(response.choices[0].message.content), model_used, usage


def _validate_selectors(html, selectors):
    """Validate CSS selectors against actual HTML. Returns (valid_count, total_count, details)."""
    soup = BeautifulSoup(html, "html.parser")
    results = {}
    valid = 0
    total = 0

    for key, selector in selectors.items():
        if not selector:
            continue
        total += 1
        try:
            matches = soup.select(selector)
            count = len(matches)
            results[key] = {"selector": selector, "matches": count, "ok": count > 0}
            if count > 0:
                valid += 1
        except Exception as e:
            results[key] = {"selector": selector, "matches": 0, "ok": False, "error": str(e)}

    return valid, total, results


def _generate_site_id(url):
    """Generate a site_id from URL."""
    parsed = urlparse(url)
    domain = parsed.hostname or "unknown"
    # Remove common prefixes
    domain = re.sub(r"^(www\.|m\.)", "", domain)
    # Convert to slug
    site_id = re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")
    return site_id


def analyze_html(html: str, url: str, *, site_id=None, site_name=None, save_config=False):
    """Analyze a pre-fetched HTML page — skips the HTTP fetch step.

    Identical to ``analyze_site`` except ``html`` is supplied directly.
    The detail-page link is still fetched via plain requests.

    Parameters
    ----------
    html : str
        Raw HTML of the list/board page.
    url : str
        Canonical URL of the list page (used for base_url derivation and prompts).
    site_id, site_name, save_config : same as analyze_site.

    Returns
    -------
    dict or None  — same schema as analyze_site (includes ``_analyzer_meta``).
    """
    client = _get_client()
    parsed = urlparse(url)

    if not site_id:
        site_id = _generate_site_id(url)
    if not site_name:
        site_name = parsed.hostname or site_id

    base_url = f"{parsed.scheme}://{parsed.hostname}"

    total_input_tokens = 0
    total_output_tokens = 0
    model_used = ANALYZER_MODEL
    fallback_used = False

    # Step 1: analyze the supplied list-page HTML
    list_html = html
    cleaned_list = _clean_html(list_html)
    print("Analyzing list page structure with AI (prefetched HTML)...")
    list_analysis, list_model, list_usage = _analyze_list_page(client, cleaned_list, url)
    model_used = list_model
    if list_usage:
        total_input_tokens += getattr(list_usage, "prompt_tokens", 0)
        total_output_tokens += getattr(list_usage, "completion_tokens", 0)

    items_found = list_analysis.get("items_found", 0)
    print(f"Found {items_found} items on list page.")

    list_selectors = {
        "item_container": list_analysis.get("item_container"),
        "title": list_analysis.get("title"),
        "date": list_analysis.get("date"),
    }
    valid, total_sel, details = _validate_selectors(list_html, list_selectors)
    print(f"List selector validation: {valid}/{total_sel} OK")

    # Step 2: fetch a detail page
    detail_analysis = {}
    detail_url_example = None
    soup = BeautifulSoup(list_html, "html.parser")
    container_sel = list_analysis.get("item_container") or "tr"
    link_sel = list_analysis.get("item_link") or "a[href]"

    try:
        containers = soup.select(container_sel)
    except Exception:
        containers = []
    for container in containers[:3]:
        link = container.select_one(link_sel)
        if link:
            href = link.get(list_analysis.get("item_link_attr", "href"), "")
            if href:
                if not href.startswith("http"):
                    href = base_url + href if href.startswith("/") else url.rsplit("/", 1)[0] + "/" + href
                detail_url_example = href
                break

    if detail_url_example:
        print(f"Fetching detail page: {detail_url_example[:80]}...")
        time.sleep(1)
        try:
            detail_html = _fetch_page(detail_url_example)
            cleaned_detail = _clean_html(detail_html)
            print("Analyzing detail page structure with AI...")
            detail_analysis, detail_model, detail_usage = _analyze_detail_page(client, cleaned_detail, detail_url_example)
            model_used = detail_model
            if detail_usage:
                total_input_tokens += getattr(detail_usage, "prompt_tokens", 0)
                total_output_tokens += getattr(detail_usage, "completion_tokens", 0)
        except Exception as e:
            print(f"  Detail page fetch failed: {e}")

    # Step 3: build config
    url_params = {}
    if parsed.query:
        for param in parsed.query.split("&"):
            if "=" in param:
                k, v = param.split("=", 1)
                url_params[k] = v

    config = {
        "site_id": site_id,
        "site_name": site_name,
        "base_url": base_url,
        "crawl_type": "html",
        "list_page": {
            "url": url.split("?")[0] if url_params else url,
            "params": url_params,
            "pagination": {
                "type": list_analysis.get("pagination_type", "query_param"),
                "param": list_analysis.get("pagination_param", "page"),
                "start": list_analysis.get("pagination_start", 1),
                "step": 1,
            },
            "selectors": {
                "item_container": list_analysis.get("item_container", "tr"),
                "item_link": list_analysis.get("item_link", "a[href]"),
                "item_link_attr": list_analysis.get("item_link_attr", "href"),
                "title": list_analysis.get("title"),
                "date": list_analysis.get("date"),
                "category": list_analysis.get("category"),
            },
        },
        "detail_page": {
            "selectors": {
                "title": detail_analysis.get("title"),
                "abstract": detail_analysis.get("abstract"),
                "authors": detail_analysis.get("authors"),
                "date": detail_analysis.get("date"),
                "pdf_link": detail_analysis.get("pdf_link"),
                "keywords": detail_analysis.get("keywords"),
                "department": detail_analysis.get("department"),
                "doi": detail_analysis.get("doi"),
            }
        },
        "options": {"delay": 1.5, "id_regex": None},
        "metadata": {
            "analyzed_from": url,
            "detail_sample": detail_url_example,
            "confidence": round(valid / max(total_sel, 1), 2),
        },
    }

    _COST_PER_1M = {
        "gpt-5.5": (2.50, 10.00),
        "gpt-5":   (2.50, 10.00),
        "gpt-4o":  (2.50, 10.00),
        "gpt-4o-mini": (0.15, 0.60),
    }
    in_price, out_price = _COST_PER_1M.get(model_used, (2.50, 10.00))
    estimated_cost = (total_input_tokens / 1_000_000) * in_price + (total_output_tokens / 1_000_000) * out_price

    analyzer_meta = {
        "model_used": model_used,
        "fallback_used": fallback_used,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "estimated_cost_usd": round(estimated_cost, 6),
    }
    print(f"  [analyzer] tokens in={total_input_tokens} out={total_output_tokens} cost=${estimated_cost:.4f}")

    config_saved = False
    if save_config:
        os.makedirs(CONFIGS_DIR, exist_ok=True)
        config_path = os.path.join(CONFIGS_DIR, f"{site_id}.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        config_saved = True
        print(f"Config saved to {config_path}")

    config["_analyzer_meta"] = {**analyzer_meta, "config_saved": config_saved}
    return config


def analyze_site(url, site_id=None, site_name=None, save_config=True):
    """Analyze a website and generate a crawler config.

    Parameters
    ----------
    url : str
        The list/board page URL to analyze.
    site_id : str, optional
        Custom site identifier. Auto-generated from URL if omitted.
    site_name : str, optional
        Human-readable site name. Uses domain if omitted.
    save_config : bool
        If True (default), write the config JSON to configs/ directory.

    Returns
    -------
    dict or None
        The generated config dict with an extra ``_analyzer_meta`` key
        containing ``model_used``, ``input_tokens``, ``output_tokens``,
        ``estimated_cost_usd``, ``fallback_used``.
        Returns None if the site could not be fetched.
    """
    client = _get_client()
    parsed = urlparse(url)

    if not site_id:
        site_id = _generate_site_id(url)
    if not site_name:
        site_name = parsed.hostname or site_id

    base_url = f"{parsed.scheme}://{parsed.hostname}"
    print(f"Fetching {url} ...")

    # Accumulated token / model tracking across both LLM calls
    total_input_tokens = 0
    total_output_tokens = 0
    model_used = ANALYZER_MODEL
    fallback_used = False

    # Step 1: Fetch and analyze list page
    try:
        list_html = _fetch_page(url)
    except Exception as e:
        print(f"ERROR: Failed to fetch {url}: {e}")
        print("The site may be blocking requests or is unreachable.")
        return None
    cleaned_list = _clean_html(list_html)
    print("Analyzing list page structure with AI...")
    list_analysis, list_model, list_usage = _analyze_list_page(client, cleaned_list, url)
    model_used = list_model
    if list_usage:
        total_input_tokens += getattr(list_usage, "prompt_tokens", 0)
        total_output_tokens += getattr(list_usage, "completion_tokens", 0)

    items_found = list_analysis.get("items_found", 0)
    print(f"Found {items_found} items on list page.")

    # Validate list selectors
    list_selectors = {
        "item_container": list_analysis.get("item_container"),
        "title": list_analysis.get("title"),
        "date": list_analysis.get("date"),
    }
    valid, total, details = _validate_selectors(list_html, list_selectors)
    print(f"List selector validation: {valid}/{total} OK")
    for k, v in details.items():
        status = "OK" if v["ok"] else "FAIL"
        print(f"  {k}: {v['selector']} → {v['matches']} matches [{status}]")

    # Step 2: Find a detail page link and analyze it
    detail_analysis = {}
    detail_url_example = None
    soup = BeautifulSoup(list_html, "html.parser")
    container_sel = list_analysis.get("item_container") or "tr"
    link_sel = list_analysis.get("item_link") or "a[href]"

    try:
        containers = soup.select(container_sel)
    except Exception:
        containers = []
    for container in containers[:3]:  # Try first 3 items
        link = container.select_one(link_sel)
        if link:
            href = link.get(list_analysis.get("item_link_attr", "href"), "")
            if href:
                if not href.startswith("http"):
                    if href.startswith("/"):
                        href = base_url + href
                    else:
                        href = url.rsplit("/", 1)[0] + "/" + href
                detail_url_example = href
                break

    if detail_url_example:
        print(f"\nFetching detail page: {detail_url_example[:80]}...")
        time.sleep(1)
        try:
            detail_html = _fetch_page(detail_url_example)
            cleaned_detail = _clean_html(detail_html)
            print("Analyzing detail page structure with AI...")
            detail_analysis, detail_model, detail_usage = _analyze_detail_page(client, cleaned_detail, detail_url_example)
            model_used = detail_model  # last model wins (should be the same)
            if detail_usage:
                total_input_tokens += getattr(detail_usage, "prompt_tokens", 0)
                total_output_tokens += getattr(detail_usage, "completion_tokens", 0)

            # Validate detail selectors
            detail_sels = {k: v for k, v in detail_analysis.items() if v}
            dv, dt, dd = _validate_selectors(detail_html, detail_sels)
            print(f"Detail selector validation: {dv}/{dt} OK")
            for k, v in dd.items():
                status = "OK" if v["ok"] else "FAIL"
                print(f"  {k}: {v['selector']} → {v['matches']} matches [{status}]")
        except Exception as e:
            print(f"  Detail page fetch failed: {e}")
    else:
        print("  No detail page links found.")

    # Step 3: Build config
    # Determine URL params from the original URL
    url_params = {}
    if parsed.query:
        for param in parsed.query.split("&"):
            if "=" in param:
                k, v = param.split("=", 1)
                url_params[k] = v

    config = {
        "site_id": site_id,
        "site_name": site_name,
        "base_url": base_url,
        "crawl_type": "html",
        "list_page": {
            "url": url.split("?")[0] if url_params else url,
            "params": url_params,
            "pagination": {
                "type": list_analysis.get("pagination_type", "query_param"),
                "param": list_analysis.get("pagination_param", "page"),
                "start": list_analysis.get("pagination_start", 1),
                "step": 1,
            },
            "selectors": {
                "item_container": list_analysis.get("item_container", "tr"),
                "item_link": list_analysis.get("item_link", "a[href]"),
                "item_link_attr": list_analysis.get("item_link_attr", "href"),
                "title": list_analysis.get("title"),
                "date": list_analysis.get("date"),
                "category": list_analysis.get("category"),
            },
        },
        "detail_page": {
            "selectors": {
                "title": detail_analysis.get("title"),
                "abstract": detail_analysis.get("abstract"),
                "authors": detail_analysis.get("authors"),
                "date": detail_analysis.get("date"),
                "pdf_link": detail_analysis.get("pdf_link"),
                "keywords": detail_analysis.get("keywords"),
                "department": detail_analysis.get("department"),
                "doi": detail_analysis.get("doi"),
            }
        },
        "options": {
            "delay": 1.5,
            "id_regex": None,
        },
        "metadata": {
            "analyzed_from": url,
            "detail_sample": detail_url_example,
            "confidence": round(valid / max(total, 1), 2),
        },
    }

    # Cost estimate (prices per 1M tokens, conservative placeholders until actual pricing known)
    # gpt-5.5: unknown — use gpt-4o pricing as placeholder ($2.50/$10.00 per 1M in/out)
    _COST_PER_1M = {
        "gpt-5.5": (2.50, 10.00),
        "gpt-5":   (2.50, 10.00),
        "gpt-4o":  (2.50, 10.00),
        "gpt-4o-mini": (0.15, 0.60),
    }
    in_price, out_price = _COST_PER_1M.get(model_used, (2.50, 10.00))
    estimated_cost = (total_input_tokens / 1_000_000) * in_price + (total_output_tokens / 1_000_000) * out_price

    analyzer_meta = {
        "model_used": model_used,
        "fallback_used": fallback_used,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "estimated_cost_usd": round(estimated_cost, 6),
    }
    print(f"  [analyzer] tokens in={total_input_tokens} out={total_output_tokens} cost=${estimated_cost:.4f}")

    # Save config (without _analyzer_meta — keep JSON file clean)
    config_saved = False
    if save_config:
        os.makedirs(CONFIGS_DIR, exist_ok=True)
        config_path = os.path.join(CONFIGS_DIR, f"{site_id}.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        config_saved = True

    confidence = config["metadata"]["confidence"]
    if save_config:
        print(f"\nConfig saved to {config_path}")
    print(f"Confidence: {confidence}")
    if confidence < 0.5:
        print("Warning: Low confidence. Manual review recommended.")

    # Attach meta so callers (e.g. tier1_batch.py) can track usage
    config["_analyzer_meta"] = {**analyzer_meta, "config_saved": config_saved}
    return config
