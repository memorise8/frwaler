import json
import time
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
import requests as req_lib
from bs4 import BeautifulSoup

router = APIRouter(prefix="/api", tags=["crawl-preview"])

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class PreviewRequest(BaseModel):
    url: str
    selectors: dict  # {item_container, item_link, title, date, ...}
    fetch_method: Optional[str] = None  # null, "browser", "cloudscraper"
    timeout: int = 15


class AutoSelectorsRequest(BaseModel):
    url: str
    fetch_method: Optional[str] = None
    timeout: int = 15


@router.post("/auto-selectors")
async def auto_selectors(req: AutoSelectorsRequest):
    import asyncio
    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _find_best_selectors(req.url, req.fetch_method, req.timeout)
    )
    return result


@router.post("/crawl-preview")
async def crawl_preview(req: PreviewRequest):
    import asyncio
    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _run_preview(req.url, req.selectors, req.fetch_method, req.timeout)
    )
    return result


def _fetch_html(url, method, timeout):
    """Fetch HTML using specified method."""
    if method == "browser":
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
            html = page.content()
            page.close()
            browser.close()
            pw.stop()
            return html, "browser"
        except Exception as e:
            return None, f"browser error: {str(e)[:100]}"
    elif method == "cloudscraper":
        try:
            import cloudscraper
            scraper = cloudscraper.create_scraper()
            resp = scraper.get(url, timeout=timeout)
            return resp.text, "cloudscraper"
        except Exception as e:
            return None, f"cloudscraper error: {str(e)[:100]}"
    else:
        try:
            session = req_lib.Session()
            session.headers.update({"User-Agent": USER_AGENT})
            resp = session.get(url, timeout=timeout)
            return resp.text, "requests"
        except Exception as e:
            return None, f"requests error: {str(e)[:100]}"


def _run_preview(url, selectors, fetch_method, timeout):
    html, method_used = _fetch_html(url, fetch_method, timeout)

    if html is None:
        return {
            "success": False,
            "error": method_used,
            "items": [],
            "html_size": 0,
            "method_used": fetch_method or "requests",
        }

    soup = BeautifulSoup(html, "html.parser")

    # Test item_container selector
    container_sel = selectors.get("item_container", "")
    items_found = soup.select(container_sel) if container_sel else []

    # Extract preview data from each item
    preview_items = []
    for item in items_found[:20]:  # Max 20 preview items
        # Title
        title = ""
        title_sel = selectors.get("title")
        link_sel = selectors.get("item_link")
        if title_sel:
            tag = item.select_one(title_sel)
            title = tag.get_text(strip=True) if tag else ""
        elif link_sel:
            tag = item.select_one(link_sel)
            title = tag.get_text(strip=True) if tag else ""

        # Link
        link = ""
        if link_sel:
            tag = item.select_one(link_sel)
            if tag:
                link_attr = selectors.get("item_link_attr", "href")
                link = tag.get(link_attr, "")

        # Date
        date = ""
        date_sel = selectors.get("date")
        if date_sel:
            tag = item.select_one(date_sel)
            date = tag.get_text(strip=True) if tag else ""

        preview_items.append({
            "title": title[:100],
            "link": link[:200],
            "date": date[:30],
        })

    # Also test some individual selectors for debugging
    selector_results = {}
    for key, sel in selectors.items():
        if sel and isinstance(sel, str):
            found = soup.select(sel)
            selector_results[key] = {
                "count": len(found),
                "sample": found[0].get_text(strip=True)[:80] if found else None,
            }

    return {
        "success": True,
        "method_used": method_used,
        "html_size": len(html),
        "total_items": len(items_found),
        "items": preview_items,
        "selector_results": selector_results,
        "diagnosis": _diagnose(html, soup, items_found, selectors),
    }


def _find_best_selectors(url, fetch_method, timeout):
    """Automatically find the best CSS selectors for a page."""
    html, method_used = _fetch_html(url, fetch_method, timeout)
    if html is None:
        return {"success": False, "error": method_used}

    soup = BeautifulSoup(html, "html.parser")

    # Strategy: Find repeating structures that look like list items
    candidates = []

    # Common Korean government board selectors
    test_selectors = [
        # Table-based boards
        ("table tbody tr", "td a", "a", "td:last-child"),
        ("table.board_list tbody tr", "td.subject a", "td.subject a", "td.date"),
        ("table.board_table_list tbody tr", "td.subject a", "td.subject a", "td.date"),
        ("table.boardList tbody tr", "td a", "td a", "td:nth-child(4)"),
        # List-based boards
        ("ul.board_list li", "a", "a", "span.date"),
        ("ul.list li", "a", "a", "span.date"),
        ("div.board-list li", "a", "a", "span.date"),
        ("li.bbsRowCls", "a.title", "a.title", "span.date"),
        # Div-based
        ("div.list-item", "a", "a", "span.date"),
        ("div.news-item", "a", "a", "span.date"),
        ("div.item", "a", "a", "span.date"),
        ("article", "a", "a", "time"),
        # Generic
        ("div.bbs-list li", "a", "a", "span.date"),
        ("div.bd-list li", "a", "a", "span.date"),
        ("div.listWrap li", "a", "a", "span.date"),
    ]

    for container, link, title, date in test_selectors:
        items = soup.select(container)
        if len(items) >= 3:
            # Check if items have links
            has_links = sum(1 for i in items if i.select_one(link))
            if has_links >= 3:
                # Score based on number of items and link coverage
                score = len(items) * 10 + (has_links / len(items)) * 50

                # Check date selector
                has_dates = sum(1 for i in items if i.select_one(date))
                if has_dates > 0:
                    score += 20

                # Preview first item
                first = items[0]
                sample_title = ""
                link_tag = first.select_one(link)
                if link_tag:
                    sample_title = link_tag.get_text(strip=True)[:60]
                sample_link = link_tag.get("href", "") if link_tag else ""

                candidates.append({
                    "selectors": {
                        "item_container": container,
                        "item_link": link,
                        "item_link_attr": "href",
                        "title": title,
                        "date": date if has_dates > 0 else None,
                    },
                    "score": score,
                    "items_found": len(items),
                    "links_found": has_links,
                    "sample_title": sample_title,
                    "sample_link": sample_link[:100],
                })

    # Also try auto-detection: find the largest repeating structure
    for tag_name in ["tr", "li", "div", "article"]:
        parent_counts = {}
        for tag in soup.select(tag_name):
            parent = tag.parent
            if parent:
                parent_id = id(parent)
                if parent_id not in parent_counts:
                    parent_counts[parent_id] = {"parent": parent, "children": []}
                parent_counts[parent_id]["children"].append(tag)

        for pid, info in parent_counts.items():
            children = info["children"]
            if 5 <= len(children) <= 100:
                # Build selector from parent
                parent = info["parent"]
                parent_classes = parent.get("class", [])
                if parent_classes:
                    parent_sel = f"{parent.name}.{'.'.join(parent_classes)}"
                else:
                    parent_sel = parent.name
                container = f"{parent_sel} > {tag_name}"

                # Check for links
                has_links = sum(1 for c in children if c.select_one("a"))
                if has_links >= 3:
                    score = len(children) * 8 + (has_links / len(children)) * 40
                    first = children[0]
                    first_link = first.select_one("a")
                    sample_title = first_link.get_text(strip=True)[:60] if first_link else ""

                    candidates.append({
                        "selectors": {
                            "item_container": container,
                            "item_link": "a",
                            "item_link_attr": "href",
                            "title": "a",
                            "date": None,
                        },
                        "score": score,
                        "items_found": len(children),
                        "links_found": has_links,
                        "sample_title": sample_title,
                        "sample_link": first_link.get("href", "")[:100] if first_link else "",
                    })

    if not candidates:
        return {
            "success": True,
            "recommendations": [],
            "method_used": method_used,
            "hint": "자동 감지 실패. fetch_method를 'browser'로 변경하거나, 미리보기에서 직접 셀렉터를 입력해보세요.",
        }

    # Sort by score, deduplicate by container
    seen = set()
    unique = []
    for c in sorted(candidates, key=lambda x: x["score"], reverse=True):
        if c["selectors"]["item_container"] not in seen:
            seen.add(c["selectors"]["item_container"])
            unique.append(c)

    return {
        "success": True,
        "recommendations": unique[:5],  # Top 5
        "method_used": method_used,
    }


def _diagnose(html, soup, items, selectors):
    """Provide diagnostic hints when items are empty."""
    hints = []

    if not items:
        text_len = len(soup.get_text(strip=True))
        if text_len < 500:
            hints.append("페이지 콘텐츠가 매우 적습니다 (JS 렌더링 필요할 수 있음). fetch_method를 'browser'로 변경해보세요.")

        # Check for iframes
        iframes = soup.select("iframe[src]")
        if iframes:
            hints.append(f"iframe {len(iframes)}개 발견. 콘텐츠가 iframe 안에 있을 수 있습니다.")

        # Suggest alternative selectors
        for sel in ["table tbody tr", "ul li", "div.list-item", "div.board-list li", "article"]:
            found = soup.select(sel)
            if len(found) >= 3:
                hints.append(f"대체 셀렉터 '{sel}'에서 {len(found)}개 항목 발견")

    return hints
