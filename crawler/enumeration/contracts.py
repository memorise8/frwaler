"""Opt-in, declarative bindings for existing crawler parsers. No I/O or ID synthesis."""
from __future__ import annotations

from copy import deepcopy
import inspect
import json
import re
from urllib.parse import parse_qs, urlsplit

from .adapters import AdapterConfigError


class ContractError(AdapterConfigError):
    """A declared adapter contract cannot be satisfied; never an empty listing."""


def fail(code):
    raise ContractError("adapter_contract:" + code)


TOKENS = {"html", "raw", "soup", "json", "url", "item", "resp", "detail"}
NEW_KEYS = {"contract_version", "list_source", "list_result", "row_path", "list_row",
            "list_kwargs", "detail_kwargs", "paper_args", "paper_kwargs"}


def _path(path):
    if not isinstance(path, list) or any(type(p) not in (str, int) or
                                        (isinstance(p, int) and p < 0) for p in path):
        fail("invalid_path")


def _binding(binding, stage):
    allowed = TOKENS - ({"item", "detail"} if stage == "list" else {"detail"} if stage == "detail" else set())
    if isinstance(binding, str):
        if binding not in allowed:
            fail("invalid_argument_token:" + binding)
        return
    if not isinstance(binding, dict) or set(binding) - {"source", "path", "type"}:
        fail("invalid_binding")
    source = binding.get("source")
    if source not in {"item", "detail", "json", "query"} or (source != "query" and source not in allowed):
        fail("invalid_binding_source")
    if "path" not in binding:
        fail("binding_path_required")
    _path(binding["path"])
    if "type" in binding and (source != "query" or binding["type"] != "int"):
        fail("invalid_binding_conversion")


def validate_contract(cfg):
    """Called by plan validation and Adapter construction, including malformed opt-ins."""
    if "contract_version" not in cfg:
        if NEW_KEYS.intersection(cfg):
            fail("contract_version_required")
        return False
    if type(cfg["contract_version"]) is not int or cfg["contract_version"] != 1:
        fail("unsupported_version")
    if cfg.get("type") != "crawler":
        fail("registered_crawler_required")
    if cfg.get("id_regex"):
        fail("identity_override_forbidden")
    source = cfg.get("list_source", "method")
    if source not in ("method", "json"):
        fail("invalid_list_source")
    if source == "method" and not cfg.get("list_method"):
        fail("list_method_required")
    if source == "json":
        if cfg.get("list_method") or cfg.get("list_args") or cfg.get("list_kwargs"):
            fail("json_source_with_list_method")
        if not cfg.get("paper_method"):
            fail("json_source_requires_registered_paper_method")
    if not (cfg.get("detail_method") or cfg.get("paper_method") or cfg.get("list_returns_paper") is True):
        fail("paper_conversion_required")
    if "list_returns_paper" in cfg and type(cfg["list_returns_paper"]) is not bool:
        fail("invalid_list_returns_paper")
    if cfg.get("list_returns_paper") and (cfg.get("detail_method") or cfg.get("paper_method")):
        fail("ambiguous_paper_conversion")
    for key in ("list_result", "row_path"):
        _path(cfg.get(key, []))
    row = cfg.get("list_row", {"type": "dict"})
    if not isinstance(row, dict) or set(row) - {"type", "fields"} or row.get("type") not in ("dict", "tuple", "url"):
        fail("invalid_row_schema")
    if row["type"] == "tuple":
        fields = row.get("fields")
        if (not isinstance(fields, list) or not fields or
                any(not isinstance(f, str) or not f for f in fields) or len(set(fields)) != len(fields)):
            fail("invalid_tuple_fields")
    elif "fields" in row:
        fail("fields_require_tuple_row")
    for stage in ("list", "detail", "paper"):
        args = cfg.get(stage + "_args", default_args(stage))
        kwargs = cfg.get(stage + "_kwargs", {})
        if not isinstance(args, list) or not isinstance(kwargs, dict) or any(not isinstance(k, str) for k in kwargs):
            fail("invalid_arguments:" + stage)
        if stage != "list" and not cfg.get(stage + "_method") and (stage + "_args" in cfg or stage + "_kwargs" in cfg):
            fail("arguments_without_method:" + stage)
        for binding in [*args, *kwargs.values()]:
            _binding(binding, stage)
            if (stage == "paper" and cfg.get("paper_method") and not cfg.get("detail_method")
                    and not (binding == "item" or (isinstance(binding, dict) and binding.get("source") == "item"))):
                fail("paper_response_without_detail")
    return True


def default_args(stage):
    return {"list": ["html"], "detail": ["html", "item"], "paper": ["item"]}[stage]


def uses_item(cfg):
    if cfg.get("list_returns_paper"):
        return True
    for stage in ("detail", "paper"):
        if not cfg.get(stage + "_method"):
            continue
        bindings = cfg.get(stage + "_args", default_args(stage)) + list(cfg.get(stage + "_kwargs", {}).values())
        if any(b == "item" or (isinstance(b, dict) and b.get("source") == "item") for b in bindings):
            return True
    return False


def select(value, path):
    for key in path:
        if isinstance(key, str) and isinstance(value, dict) and key in value:
            value = value[key]
        elif type(key) is int and isinstance(value, (list, tuple)) and key < len(value):
            value = value[key]
        else:
            fail("missing_path:" + json.dumps(path, ensure_ascii=False))
    return value


class Contract:
    def __init__(self, cfg, crawler):
        validate_contract(cfg)
        self.cfg = cfg
        self.crawler = crawler
        for stage in ("list", "detail", "paper"):
            name = cfg.get(stage + "_method")
            if not name:
                continue
            if not isinstance(name, str) or not callable(getattr(crawler, name, None)):
                fail("method_missing:" + stage)
            try:
                inspect.signature(getattr(crawler, name)).bind(
                    *[None] * len(cfg.get(stage + "_args", default_args(stage))),
                    **dict.fromkeys(cfg.get(stage + "_kwargs", {})))
            except (TypeError, ValueError):
                fail("method_signature:" + stage)

    def value(self, binding, resp, item, detail):
        if isinstance(binding, dict):
            source = binding["source"]
            if source == "query":
                if resp is None:
                    fail("response_required:query")
                query = parse_qs(urlsplit(resp.url).query, keep_blank_values=True)
                if any(len(v) != 1 for v in query.values()):
                    fail("ambiguous_query")
                data = {k: v[0] for k, v in query.items()}
            else:
                data = self.value(source, resp, item, detail)
            value = select(data, binding["path"])
            if binding.get("type") == "int":
                if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
                    fail("query_not_integer")
                value = int(value)
            return deepcopy(value)
        if binding == "item":
            if item is None:
                fail("item_required")
            return deepcopy(item.payload)
        if binding == "detail":
            return deepcopy(detail)
        if resp is None:
            fail("response_required:" + binding)
        if binding == "html":
            return resp.text()
        if binding == "raw":
            return resp.body
        if binding == "json":
            return resp.json()
        if binding == "url":
            return resp.url
        if binding == "resp":
            return resp
        if binding == "soup":
            from bs4 import BeautifulSoup
            return BeautifulSoup(resp.text(), "html.parser")
        fail("invalid_argument_token:" + str(binding))

    def call(self, stage, resp=None, item=None, detail=None):
        from .guard import guarded
        args = [self.value(b, resp, item, detail) for b in self.cfg.get(stage + "_args", default_args(stage))]
        kwargs = {k: self.value(b, resp, item, detail) for k, b in self.cfg.get(stage + "_kwargs", {}).items()}
        with guarded():
            return getattr(self.crawler, self.cfg[stage + "_method"])(*args, **kwargs)

    def rows(self, resp):
        raw = resp.json() if self.cfg.get("list_source") == "json" else self.call("list", resp)
        rows = select(raw, self.cfg.get("list_result", []))
        if not isinstance(rows, list):
            fail("list_result_not_list")
        schema = self.cfg.get("list_row", {"type": "dict"})
        out = []
        for raw_row in rows:
            row = select(raw_row, self.cfg.get("row_path", []))
            if schema["type"] == "dict":
                if not isinstance(row, dict):
                    fail("row_not_dict")
                row = deepcopy(row)
            elif schema["type"] == "tuple":
                if not isinstance(row, tuple) or len(row) != len(schema["fields"]):
                    fail("tuple_row_shape")
                row = dict(zip(schema["fields"], deepcopy(row)))
            else:
                if not isinstance(row, str) or not row:
                    fail("row_not_url")
                row = {"url": row}
            try:
                json.dumps(row, allow_nan=False)
            except (TypeError, ValueError):
                fail("row_not_json_serializable")
            out.append(row)
        return out
