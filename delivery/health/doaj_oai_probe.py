"""Bounded DOAJ ListRecords continuation review; never certifies a full harvest.

Only the public endpoint is used. Opaque pagination tokens remain in memory;
neither request URLs containing tokens nor record bodies are written to reports.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from pathlib import Path
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

from .results import atomic_json, utc_now
from .source_observer import HostGate, HostDeferred, fetch_once

ENDPOINT = "https://doaj.org/oai.article"
NS = {"o": "http://www.openarchives.org/OAI/2.0/",
      "dc": "http://purl.org/dc/elements/1.1/"}


def parse_page(body):
    # Reject declarations before handing public XML to the stdlib parser.
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ValueError("xml_declarations_not_allowed")
    root = ET.fromstring(body)
    if root.tag != "{" + NS["o"] + "}OAI-PMH":
        raise ValueError("unexpected_xml_root")
    errors = root.findall("o:error", NS)
    if errors:
        return {"errors": [e.get("code") for e in errors]}
    listing = root.find("o:ListRecords", NS)
    if listing is None:
        raise ValueError("missing_list_records")
    ids, deleted, coverage = [], 0, Counter()
    for record in listing.findall("o:record", NS):
        header = record.find("o:header", NS)
        if header is None:
            raise ValueError("missing_header")
        identifier = header.findtext("o:identifier", namespaces=NS)
        if not identifier or not identifier.startswith("oai:doaj.org/article:"):
            raise ValueError("invalid_article_identifier")
        ids.append(hashlib.sha256(identifier.encode()).hexdigest())
        if header.get("status") == "deleted":
            deleted += 1
            continue
        metadata = record.find("o:metadata", NS)
        if metadata is None:
            raise ValueError("active_record_missing_metadata")
        for field in ("title", "identifier", "description", "creator", "date", "publisher", "source"):
            if any((node.text or "").strip() for node in metadata.findall(".//dc:" + field, NS)):
                coverage[field] += 1
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate_ids_within_page")
    node = listing.find("o:resumptionToken", NS)
    token = (node.text or "").strip() if node is not None else ""
    total = node.get("completeListSize") if node is not None else None
    if total is not None:
        total = int(total)
        if total < len(ids):
            raise ValueError("invalid_total")
    if not ids and token:
        raise ValueError("empty_page_with_continuation")
    return {"id_hashes": ids, "deleted_headers": deleted, "field_presence": dict(coverage),
            "reported_complete_list_size": total, "token": token,
            "cursor": node.get("cursor") if node is not None else None}


def probe(*, pages=4, fetch=fetch_once, gate=None, on_progress=None):
    if not 1 <= pages <= 8:
        raise ValueError("pages must be 1..8")
    gate = gate or HostGate()
    result = {"site_id": "doaj-org-search", "started_at": utc_now(),
              "endpoint": ENDPOINT, "verb": "ListRecords", "metadata_prefix": "oai_dc",
              "state": "running", "pages": [], "unique_identifiers_observed": 0,
              "full_collection_verified": False, "enumerable_total": None,
              "save_eligible_total": None, "database_writes": False,
              "token_values_stored": False, "complete_list_sizes": [],
              "source_size_includes_deleted_headers": "not_assumed_equal_to_search_api"}
    seen, tokens, token = set(), set(), None
    for number in range(1, pages + 1):
        params = {"verb": "ListRecords"}
        if token is None:
            params["metadataPrefix"] = "oai_dc"
        else:
            params["resumptionToken"] = token
        row = {"page": number, "request_parameter_names": sorted(params)}
        result["pages"].append(row)
        try:
            with gate.enter("doaj.org"):
                response = fetch(ENDPOINT + "?" + urlencode(params), timeout=20)
                if response["status"] in (429, 503):
                    gate.defer("doaj.org", response["headers"].get("retry-after"))
            row.update(http_status=response["status"], curl_exit=response["curl_exit"])
            if response["status"] != 200 or response["curl_exit"] or not response["body_complete"]:
                result["state"] = "request_failed_or_incomplete"
                break
            row["body_sha256"] = hashlib.sha256(response["body"]).hexdigest()
            data = parse_page(response["body"])
            if "errors" in data:
                row["oai_error_codes"] = data["errors"]
                result["state"] = "oai_error"
                break
            token = data.pop("token")
            ids = set(data["id_hashes"])
            row.update(data, records=len(ids), overlap_with_previous=len(ids & seen),
                       continuation_present=bool(token))
            if ids & seen:
                result["state"] = "repeated_ids_between_pages"
                break
            seen.update(ids)
            result["unique_identifiers_observed"] = len(seen)
            total = data["reported_complete_list_size"]
            if total is not None and total not in result["complete_list_sizes"]:
                result["complete_list_sizes"].append(total)
            if not token:
                result["state"] = "source_end_signal_observed_requires_reconciliation"
                break
            if token in tokens:
                result["state"] = "repeated_continuation_token"
                break
            tokens.add(token)
            result["state"] = "bounded_continuation_observed_not_full_harvest"
        except HostDeferred:
            result["state"] = "host_cooldown_deferred"
            break
        except Exception as exc:
            row["exception_type"] = type(exc).__name__
            result["state"] = "parse_or_transport_error"
            break
        if on_progress:
            on_progress(result)
    result.update(finished_at=utc_now(), host_cooldowns=gate.cooldowns())
    if on_progress:
        on_progress(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pages", type=int, default=4)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False, mode=0o700)
    result = probe(pages=args.pages, on_progress=lambda data: atomic_json(args.out / "result.json", data))
    print(result["state"], result["unique_identifiers_observed"])


if __name__ == "__main__":
    main()
