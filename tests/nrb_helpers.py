"""NR-B regression helpers (memory only): the unchanged crawl() and the enumeration plan
must store the same documents for the same synthetic source.

native(): crawl() with its own network helpers replaced by ``serve(url) -> body|None``,
papers captured at _save_paper. pipeline(): the real SiteRunner with a fake transport over
the same ``serve``. No real network, process or customer DB (disposable SQLite only).
"""
import contextlib
import copy
import io
import json
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

from crawler.enumeration.adapters import clean_paper, paper_identity
from crawler.enumeration.http import Response
from tests.collector_split_helpers import load

ROOT = Path(__file__).resolve().parents[1]


def fixture(site):
    return json.loads((ROOT / "tests/fixtures/nrb" / (site + ".json")).read_text(encoding="utf-8"))


def plan_of(site):
    return json.loads((ROOT / "docs/examples/nrb" / (site + ".json")).read_text(encoding="utf-8"))["sites"][site]


def query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}


def as_text(body):
    if isinstance(body, (dict, list)):
        return json.dumps(body, ensure_ascii=False)
    return body


def _instance(site, conn):
    _, module = load(site)
    cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, "site_id", None) == site)
    try:
        return cls(conn, 0), module
    except TypeError:
        return cls(conn), module


def native(site, serve, net_methods=(), module_funcs=(), limit=None, init=None):
    """Run the unchanged crawl(); every name in net_methods (crawler attributes) and
    module_funcs (module-level functions) answers from serve(url). Papers are captured
    at _save_paper (nothing stored)."""
    from tests.enumeration_fakes import Env
    env = Env(site)
    try:
        crawler, module = _instance(site, env.conn)
        if init:
            init(crawler)
        captured = []

        def transport(url, *a, **k):
            body = serve(url)
            return None if body is None else as_text(body)

        crawler._delay = 0
        crawler._detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        for name in net_methods:
            setattr(crawler, name, transport)
        with contextlib.ExitStack() as stack:
            for name in module_funcs:
                stack.enter_context(patch.object(module, name, transport))
            stack.enter_context(patch("time.sleep", lambda *a: None))
            with contextlib.redirect_stdout(io.StringIO()):
                crawler.crawl(limit=limit)
        return captured
    finally:
        env.close()


def pipeline(site, serve, plan=None, ctype="text/html; charset=utf-8", types=None):
    from tests.enumeration_fakes import Env
    plan = copy.deepcopy(plan or plan_of(site))
    plan["request_interval_s"] = 0

    def handler(url):
        if urlsplit(url).path == "/robots.txt":
            return Response(404, {}, b"")
        body = serve(url)
        if body is None:
            return Response(404, {"Content-Type": "text/html"}, b"<html>not found</html>", url)
        t = (types(url) if types else None) or ("application/json" if isinstance(body, (dict, list)) else ctype)
        body = as_text(body)
        return Response(200, {"Content-Type": t}, body.encode("utf-8") if isinstance(body, str) else body, url)

    env = Env(site)
    try:
        _, module = load(site)
        cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, "site_id", None) == site)
        try:
            crawler = cls(env.conn, 0)
        except TypeError:
            crawler = cls(env.conn)
        with contextlib.redirect_stdout(io.StringIO()):
            result = env.run(plan, handler, crawler=crawler)
        return result, docs_of(env)
    finally:
        env.close()


def identities(papers):
    out = []
    for p in papers:
        c = clean_paper(dict(p))
        pn, mu = paper_identity(c)
        out.append((str(pn), mu, c.get("title"), c.get("published_date") or None, c.get("pdf_url") or None))
    return sorted(out)


def docs_of(env):
    return [dict(r) for r in env.conn.execute(
        "SELECT post_number, meta_url, title, published_date, pdf_url, abstract FROM documents ORDER BY seq_id")]


def doc_identities(docs):
    return sorted((str(d["post_number"]), d["meta_url"], d["title"], d["published_date"] or None, d["pdf_url"] or None,
                   d.get("abstract"))
                  for d in docs)


def native_docs(site, serve, net_methods=(), module_funcs=(), limit=None, init=None):
    """Like native(), but crawl() saves through its real _save_paper into a disposable
    SQLite DB, so both sides go through the same storage normalisation."""
    from tests.enumeration_fakes import Env
    env = Env(site)
    try:
        _, module = load(site)
        cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, "site_id", None) == site)
        try:
            crawler = cls(env.conn, 0)
        except TypeError:
            crawler = cls(env.conn)
        if init:
            init(crawler)

        def transport(url, *a, **k):
            body = serve(url)
            return None if body is None else as_text(body)

        crawler._delay = 0
        crawler._detail_delay = 0
        for name in net_methods:
            setattr(crawler, name, transport)
        with contextlib.ExitStack() as stack:
            for name in module_funcs:
                stack.enter_context(patch.object(module, name, transport))
            stack.enter_context(patch("time.sleep", lambda *a: None))
            with contextlib.redirect_stdout(io.StringIO()):
                crawler.crawl(limit=limit)
        return docs_of(env)
    finally:
        env.close()
