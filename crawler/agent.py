# -*- coding: utf-8 -*-
"""Autonomous GPT agent that uses tools to analyze sites and create crawlers."""

import json
import os
import re
from urllib.parse import urlparse

from dotenv import load_dotenv
from openai import OpenAI

from .agent_tools import TOOL_FUNCTIONS

_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(_ENV_PATH)

# ---- Tool Definitions (OpenAI function calling format) ----

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": "Fetch a web page and return its HTML. Returns error details if fetch fails. Use this first to understand a site. If it fails with SSL error, retry with verify_ssl=false.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch"},
                    "verify_ssl": {"type": "boolean", "description": "Whether to verify SSL. Try false if true fails.", "default": True}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "curl_fetch",
            "description": "Fetch via curl (bypasses Python SSL issues). Use when fetch_page fails. Can make POST requests for API discovery.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string", "enum": ["GET", "POST"], "default": "GET"},
                    "data": {"type": "string", "description": "POST body data"},
                    "headers": {"type": "object", "description": "Additional HTTP headers"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "clean_html",
            "description": "Clean HTML by removing scripts/styles/comments. Returns compact version for analysis. Use before analyzing structure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "html": {"type": "string", "description": "Raw HTML to clean"},
                    "max_chars": {"type": "integer", "default": 12000}
                },
                "required": ["html"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "test_selector",
            "description": "Test a CSS selector against HTML. Returns match count and sample text from first matches. Always test selectors before using them in config.",
            "parameters": {
                "type": "object",
                "properties": {
                    "html": {"type": "string"},
                    "css_selector": {"type": "string"}
                },
                "required": ["html", "css_selector"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "extract_text",
            "description": "Extract all text content matching a CSS selector. Returns list of text strings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "html": {"type": "string"},
                    "css_selector": {"type": "string"},
                    "limit": {"type": "integer", "default": 10}
                },
                "required": ["html", "css_selector"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_config",
            "description": "Save a crawler JSON config. Only call when confident the config is correct. Required fields: site_id, site_name, base_url, crawl_type, list_page, detail_page, options.",
            "parameters": {
                "type": "object",
                "properties": {
                    "config": {"type": "object", "description": "The full crawler config object"}
                },
                "required": ["config"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "test_crawl",
            "description": "Run a test crawl for a site_id. Tests against in-memory DB. Call AFTER save_config to verify end-to-end.",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 3}
                },
                "required": ["site_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cloudscraper_fetch",
            "description": "Fetch a page using cloudscraper (bypasses Cloudflare/WAF protection). Use when fetch_page returns 403 Forbidden. Lighter and faster than browser_fetch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_fetch",
            "description": "Fetch a page using headless browser (Playwright). Use when fetch_page and curl_fetch return very small HTML (<5000 chars) or 0 links - this means the site is a SPA that needs JavaScript rendering. This is slower but renders the full page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch"},
                    "wait_seconds": {"type": "number", "description": "Seconds to wait for JS rendering", "default": 3}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_crawler_file",
            "description": "Write a custom Python crawler for sites needing API calls, curl, or special logic. Last resort when JSON config fails. Code must extend BaseCrawler.",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_id": {"type": "string"},
                    "code": {"type": "string", "description": "Complete Python source code"}
                },
                "required": ["site_id", "code"]
            }
        }
    },
]

# ---- System Prompt ----

SYSTEM_PROMPT = """You are an expert web scraping agent. Your job is to analyze a website and create a working crawler configuration.

## Your Goal
Given a URL (typically a list/board page), create either:
1. A JSON config file for the GenericCrawler (preferred), OR
2. A custom Python crawler file (for API-based or specially challenging sites)

## Strategy (follow this order)

### Phase 1: Reconnaissance
1. fetch_page(url) to get the HTML
2. If fetch fails → retry with verify_ssl=false
3. If still fails or returns 403 → try cloudscraper_fetch(url) (bypasses Cloudflare/WAF)
4. If still fails → try curl_fetch(url)
5. If HTML is very small (<5000 chars) or has 0 links → this is likely a SPA site → try browser_fetch(url) which uses a headless browser to render JavaScript
6. ALSO check for SPA indicators even in large HTML: if you see <div id="root">, <div id="app">, <app-root>, __NEXT_DATA__, ng-version, or the HTML is mostly <script> tags with very little visible text content → this is a SPA that needs browser_fetch(url). Don't be fooled by large HTML size — SPA shells can be 50-100KB of scripts with no actual content.
7. If response looks like JSON, this might be an API endpoint
5. clean_html(html="__last__") to get a compact version (IMPORTANT: pass "__last__" as html to use the stored full HTML from the last fetch, do NOT paste the HTML inline)
6. *** MOST IMPORTANT ***: clean_html returns "selector_hints" - a dict of CSS selectors that ALREADY MATCHED real elements in the full HTML. Each hint has a count and sample text. YOU MUST USE THESE HINTS FIRST before trying your own selectors. Pick the hint with samples that look like article titles as your item_container and item_link. For example, if hints show {"tbody tr": {"count": 11, "sample": "article title..."}, "td.left a": {"count": 11, "sample": "article title..."}}, then use "tbody tr" as item_container and "td.left a" (or just "a") as item_link within the container.
7. Similarly, test_selector(html="__last__", ...) and extract_text(html="__last__", ...) use the last fetched HTML
8. Check for encoding issues (look for <meta charset> in HTML, check if text looks garbled)

### Phase 1.5: Detect Site Type
After reconnaissance, determine which type of site this is:

**Type A: "list-detail"** (most common) — A board/list page with links to individual detail pages.
Signs: repeating rows/cards, each with a link to a detail URL, pagination controls.
→ Use crawl_type: "html", proceed to Phase 2.

**Type B: "single-page"** — A single page with direct document links (PDF, DOC, etc.) listed inline.
Signs: no pagination, document links (*.pdf, *.doc, *.xlsx) scattered across the page, often grouped under headings (h2/h3). No separate detail pages.
→ Use crawl_type: "single-page", skip to Phase 2B.

Test: count links with `test_selector(html="__last__", css_selector="a[href$='.pdf']")`. If many PDF/DOC links exist directly on the page without a list→detail pattern, this is a single-page site.

### Phase 2A: Analyze List Page (for list-detail sites)
7. Look at the cleaned HTML carefully for repeating patterns (table rows, div items, li elements)
8. Formulate CSS selector hypotheses for: item_container, item_link, title, date
9. Call MULTIPLE test_selector() in PARALLEL (one per selector) to save iterations
10. If selectors fail, look at the clean_html output more carefully and try different selectors
11. Use extract_text to verify content quality (e.g., are titles actually article titles?)
12. Find a detail page link from the matches using extract_text on the link selector

### Phase 2B: Analyze Single Page (for single-page sites)
*** CRITICAL: For single-page sites, the "link" selector must capture ALL document links, not just a few. ***
7. FIRST test broad document link selectors and compare counts:
   - test_selector("a[href$='.pdf']") — count PDF links
   - test_selector("a[href$='.doc'], a[href$='.docx']") — count DOC links
   - test_selector("a[href$='.xlsx']") — count Excel links
   - test_selector("a[href$='.pdf'], a[href$='.doc'], a[href$='.docx'], a[href$='.xlsx']") — count ALL document links
8. ALWAYS prefer the broadest selector that captures the most document links. If "a[href$='.pdf']" finds 40 links but ".resourcebox a" finds only 2, USE "a[href$='.pdf']".
9. Check link text quality with extract_text - are link texts meaningful titles or generic ("PDF", "Download")?
10. If generic, the crawler will auto-extract title from surrounding context
11. Identify category grouping: test heading selectors (h2, h3) to see section structure
12. Skip to Phase 4 (no detail page needed)

### Phase 3: Analyze Detail Page (list-detail only)
12. fetch_page(detail_url) to get a detail page
13. clean_html and analyze structure
14. Formulate and test selectors for: title, abstract/content, authors, date, pdf_link
15. Verify with test_selector

### Phase 4: Build and Test
16. save_config(config) with validated selectors
17. test_crawl(site_id, limit=3) to verify end-to-end
18. If test_crawl shows empty titles/content, adjust selectors and retry

### Phase 5: Fallback
19. If HTML scraping fails, check for API endpoints (look for ajax/fetch URLs in the HTML source, try common patterns like adding /api/ or changing .do to JSON endpoints)
20. If API found: write_crawler_file with API-based crawler using curl subprocess
21. If nothing works: explain why clearly

## Important Rules
- ALWAYS test selectors with test_selector before saving config
- ALWAYS run test_crawl after saving config
- If test_crawl samples show garbled text, set options.encoding (e.g., "euc-kr")
- Check pagination: look for page params in URLs, next buttons
- When title has prefix like "Title:" or "제목:", note it - GenericCrawler auto-strips common prefixes
- When writing custom crawler code, import from ..base_crawler import BaseCrawler
- Keep responses concise

## Config Schema

### For list-detail sites (crawl_type: "html"):
{
  "site_id": "string",
  "site_name": "string",
  "base_url": "string (scheme://host)",
  "crawl_type": "html",
  "list_page": {
    "url": "full list page URL",
    "params": {},
    "pagination": {"type": "query_param|none", "param": "page", "start": 1, "step": 1},
    "selectors": {
      "item_container": "CSS selector",
      "item_link": "CSS selector",
      "item_link_attr": "href",
      "title": "CSS selector or null",
      "date": "CSS selector or null",
      "category": "CSS selector or null"
    }
  },
  "detail_page": {
    "selectors": {
      "title": "CSS selector or null",
      "abstract": "CSS selector or null",
      "authors": "CSS selector or null",
      "date": "CSS selector or null",
      "pdf_link": "CSS selector or null",
      "keywords": "CSS selector or null",
      "department": "CSS selector or null",
      "doi": "CSS selector or null"
    }
  },
  "options": {"delay": 1.5, "encoding": null, "verify_ssl": true, "id_regex": null, "fetch_method": null}
}

### For single-page sites (crawl_type: "single-page"):
{
  "site_id": "string",
  "site_name": "string",
  "base_url": "string (scheme://host)",
  "crawl_type": "single-page",
  "list_page": {
    "url": "the page URL",
    "selectors": {
      "link": "CSS selector for document links (e.g. a[href$='.pdf'])",
      "link_attr": "href",
      "category_heading": "CSS selector for section headings (e.g. h2, h3) or null"
    }
  },
  "options": {"delay": 1.5, "encoding": null, "verify_ssl": true, "id_regex": null, "fetch_method": null}
}

### IMPORTANT: fetch_method option
- Set "fetch_method": "browser" when you had to use browser_fetch() to get the HTML content
  (i.e., when fetch_page/curl_fetch returned very small or empty HTML but browser_fetch worked).
- Set "fetch_method": "cloudscraper" when you had to use cloudscraper_fetch() to bypass 403/WAF.
- Leave null or omit for normal sites where fetch_page/curl_fetch worked fine.
- This tells the crawler which method to use when actually crawling.

When finished, respond with text (no tool call) containing a JSON block:
```json
{"success": true/false, "site_id": "...", "method": "config|custom_crawler", "items_found": N, "reason": "..."}
```"""


# ---- Agent Class ----

class AutoAddAgent:
    """Autonomous agent that uses GPT with tools to create crawlers."""

    MAX_ITERATIONS = 15

    def __init__(self, max_iterations=None, verbose=False, force_browser=False):
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set in environment or .env file.")
        self._client = OpenAI(api_key=api_key)
        self._max_iterations = max_iterations or self.MAX_ITERATIONS
        self._verbose = verbose
        self._force_browser = force_browser
        # Store full HTML separately (not sent to GPT to save tokens)
        self._html_store = {}

    def run(self, url, site_id=None, site_name=None):
        """Run the agent loop. Returns dict with success, site_id, method, reason."""
        if not site_id:
            site_id = self._generate_site_id(url)
        if not site_name:
            parsed = urlparse(url)
            site_name = parsed.hostname or site_id

        print(f"Agent: Analyzing {url} ...")
        print(f"  Site ID: {site_id}, Name: {site_name}")

        if self._force_browser:
            user_msg = (
                f"Analyze this website and create a working crawler.\n"
                f"URL: {url}\n"
                f"Site ID: {site_id}\n"
                f"Site Name: {site_name}\n\n"
                f"IMPORTANT: This is a known SPA (Single Page Application) site. "
                f"Skip fetch_page entirely and use browser_fetch(url) FIRST to get the "
                f"fully rendered HTML. The site requires JavaScript rendering. "
                f"Set fetch_method: \"browser\" in the final config.\n\n"
                f"Start by calling browser_fetch on the URL."
            )
        else:
            user_msg = (
                f"Analyze this website and create a working crawler.\n"
                f"URL: {url}\n"
                f"Site ID: {site_id}\n"
                f"Site Name: {site_name}\n\n"
                f"Start by fetching the page."
            )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg}
        ]

        for iteration in range(self._max_iterations):
            model = self._select_model(iteration)

            try:
                response = self._client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=TOOL_DEFINITIONS,
                    tool_choice="auto",
                    parallel_tool_calls=True,
                    max_tokens=4000,
                )
            except Exception as e:
                print(f"  [ERROR] GPT API call failed: {e}")
                return {"success": False, "site_id": site_id, "method": "none", "reason": str(e)}

            message = response.choices[0].message
            messages.append(message)

            # If GPT wants to call tools
            if message.tool_calls:
                for tool_call in message.tool_calls:
                    name = tool_call.function.name
                    try:
                        args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        args = {}

                    # Auto-fill site_id/site_name in save_config if missing
                    if name == "save_config":
                        # GPT sometimes sends config as a nested object or string
                        if "config" not in args:
                            # Treat entire args as config
                            args = {"config": args}
                        cfg = args["config"]
                        if isinstance(cfg, str):
                            try:
                                cfg = json.loads(cfg)
                                args["config"] = cfg
                            except json.JSONDecodeError:
                                pass
                        if isinstance(cfg, dict):
                            if not cfg.get("site_id"):
                                cfg["site_id"] = site_id
                            if not cfg.get("site_name"):
                                cfg["site_name"] = site_name
                            if not cfg.get("base_url"):
                                parsed_url = urlparse(url)
                                cfg["base_url"] = f"{parsed_url.scheme}://{parsed_url.hostname}"
                            if not cfg.get("crawl_type"):
                                cfg["crawl_type"] = "single-page"

                    # Execute tool
                    result = self._execute_tool(name, args)
                    self._report_progress(name, args, result)

                    # Prepare result for GPT (strip full_html/full_body to save tokens)
                    result_for_gpt = {k: v for k, v in result.items()
                                      if k not in ("full_html", "full_body")}

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result_for_gpt, ensure_ascii=False, default=str)[:8000]
                    })
            else:
                # GPT responded with text = done
                content = message.content or ""
                if self._verbose:
                    print(f"\n  Agent response: {content[:500]}")
                return self._parse_final_response(content, site_id)

        print(f"  [LIMIT] Max iterations ({self._max_iterations}) reached.")
        # Check if config was saved despite running out of iterations
        config_path = os.path.join(os.path.dirname(__file__), "sites", "configs", f"{site_id}.json")
        custom_path = os.path.join(os.path.dirname(__file__), "sites", "custom", f"{site_id}.py")
        if os.path.exists(config_path) or os.path.exists(custom_path):
            return {"success": True, "site_id": site_id, "method": "config", "reason": "Completed at iteration limit"}
        return {"success": False, "site_id": site_id, "method": "none", "reason": "Max iterations reached"}

    def _execute_tool(self, name, args):
        """Execute a tool function by name."""
        func = TOOL_FUNCTIONS.get(name)
        if not func:
            return {"error": f"Unknown tool: {name}"}

        # For tools that take html, auto-substitute stored HTML
        # GPT often sends truncated/empty html - use stored full version instead
        if "html" in args:
            html_arg = args["html"]
            if not html_arg or html_arg == "__last__" or len(html_arg) < 50:
                args["html"] = self._html_store.get("last_html", "")

        try:
            result = func(**args)
        except Exception as e:
            result = {"error": str(e)[:200]}

        # Store full HTML for later use
        if name == "fetch_page" and result.get("success"):
            self._html_store["last_html"] = result.get("full_html", "")
            self._html_store["last_url"] = args.get("url", "")
        elif name == "curl_fetch" and result.get("success"):
            self._html_store["last_html"] = result.get("full_body", "")
            self._html_store["last_url"] = args.get("url", "")
        elif name == "cloudscraper_fetch" and result.get("success"):
            self._html_store["last_html"] = result.get("full_html", "")
            self._html_store["last_url"] = args.get("url", "")
        elif name == "browser_fetch" and result.get("success"):
            self._html_store["last_html"] = result.get("full_html", "")
            self._html_store["last_url"] = args.get("url", "")

        return result

    def _select_model(self, iteration):
        """Use gpt-4o-mini for first 6 iterations, escalate to gpt-4o after."""
        if iteration >= 6:
            return "gpt-4o"
        return "gpt-4o-mini"

    def _report_progress(self, name, args, result):
        """Print progress to stdout."""
        icons = {
            "fetch_page": "GET",
            "curl_fetch": "CURL",
            "cloudscraper_fetch": "SCRAPER",
            "browser_fetch": "BROWSER",
            "clean_html": "CLEAN",
            "test_selector": "TEST",
            "extract_text": "EXTRACT",
            "save_config": "SAVE",
            "test_crawl": "CRAWL",
            "write_crawler_file": "WRITE",
        }
        icon = icons.get(name, "TOOL")

        if name == "fetch_page":
            url = args.get("url", "")[:60]
            if result.get("success"):
                length = result.get("length", 0)
                print(f"  [{icon}] {url} -> OK ({length} chars)")
            else:
                err = result.get("error", "")[:50]
                print(f"  [{icon}] {url} -> FAIL: {err}")
        elif name == "curl_fetch":
            url = args.get("url", "")[:60]
            method = args.get("method", "GET")
            if result.get("success"):
                is_json = " (JSON)" if result.get("is_json") else ""
                print(f"  [{icon}] {method} {url} -> OK{is_json}")
            else:
                print(f"  [{icon}] {method} {url} -> FAIL")
        elif name == "cloudscraper_fetch":
            url = args.get("url", "")[:60]
            if result.get("success"):
                length = result.get("length", 0)
                print(f"  [{icon}] {url} -> OK ({length} chars)")
            else:
                code = result.get("status_code", "?")
                print(f"  [{icon}] {url} -> FAIL (HTTP {code})")
        elif name == "browser_fetch":
            url = args.get("url", "")[:60]
            if result.get("success"):
                length = result.get("length", 0)
                print(f"  [{icon}] {url} -> OK ({length} chars)")
            else:
                err = result.get("error", "")[:50]
                print(f"  [{icon}] {url} -> FAIL: {err}")
        elif name == "clean_html":
            length = result.get("length", 0)
            print(f"  [{icon}] {length} chars")
        elif name == "test_selector":
            sel = args.get("css_selector", "")[:40]
            matches = result.get("matches", 0)
            ok = "OK" if result.get("ok") else "FAIL"
            print(f"  [{icon}] '{sel}' -> {matches} matches [{ok}]")
        elif name == "extract_text":
            sel = args.get("css_selector", "")[:40]
            count = result.get("count", 0)
            print(f"  [{icon}] '{sel}' -> {count} texts")
        elif name == "save_config":
            sid = args.get("config", {}).get("site_id", "?")
            ok = "OK" if result.get("success") else "FAIL"
            print(f"  [{icon}] Config for '{sid}' [{ok}]")
        elif name == "test_crawl":
            count = result.get("count", 0)
            ok = "PASS" if result.get("success") else "FAIL"
            print(f"  [{icon}] {ok} - {count} items crawled")
        elif name == "write_crawler_file":
            sid = args.get("site_id", "?")
            ok = "OK" if result.get("success") else "FAIL"
            print(f"  [{icon}] Custom crawler for '{sid}' [{ok}]")

    def _parse_final_response(self, content, site_id):
        """Parse the agent's final text response for the result JSON."""
        # Try to extract JSON from the response
        try:
            # Look for JSON block in markdown code fence
            json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(1))
            # Try to find bare JSON
            json_match = re.search(r'\{[^{}]*"success"[^{}]*\}', content)
            if json_match:
                return json.loads(json_match.group(0))
        except (json.JSONDecodeError, AttributeError):
            pass
        # Fallback - check if config was actually saved
        import os
        config_path = os.path.join(os.path.dirname(__file__), "sites", "configs", f"{site_id}.json")
        custom_path = os.path.join(os.path.dirname(__file__), "sites", "custom", f"{site_id}.py")
        if os.path.exists(config_path) or os.path.exists(custom_path):
            return {"success": True, "site_id": site_id, "method": "config", "reason": content[:200]}
        return {"success": False, "site_id": site_id, "method": "none", "reason": content[:200]}

    @staticmethod
    def _generate_site_id(url):
        """Generate a site_id from URL."""
        parsed = urlparse(url)
        domain = parsed.hostname or "unknown"
        domain = re.sub(r"^(www\.|m\.)", "", domain)
        site_id = re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")
        # Add path hint for uniqueness
        path = parsed.path.strip("/").split("/")
        if path and path[-1]:
            hint = re.sub(r"[^a-z0-9]+", "-", path[-1].lower().split(".")[0]).strip("-")
            if hint and hint != site_id:
                site_id = f"{site_id}-{hint}"[:30]
        return site_id
