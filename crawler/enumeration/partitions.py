"""Opt-in, source-verified explicit partitions; never invent source filter syntax."""


def validate_partition_tree(seg, listing, raw):
    if seg.get("server_verified") is not True:
        raise ValueError("partition_tree_requires_server_verification")
    if raw.get("overall_total_check") is not True or seg.get("allow_overlap"):
        raise ValueError("partition_tree_requires_all_reconciliation_checks")
    cap = seg.get("hidden_cap")
    if type(cap) is not int or cap < 2 or not listing.get("total"):
        raise ValueError("partition_tree_requires_cap_and_total")
    size = seg.get("count_size_param")
    if not isinstance(size, str) or not size or size == listing.get("page_param", "page"):
        raise ValueError("partition_tree_requires_count_size_param")
    nodes = seg.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("partition_tree_requires_nodes")
    keys = [n.get("key") for n in nodes if isinstance(n, dict)]
    if len(keys) != len(nodes) or any(not isinstance(k, str) or not k for k in keys) or len(set(keys)) != len(keys):
        raise ValueError("partition_tree_invalid_keys")
    by_key = {n["key"]: n for n in nodes}
    root = seg.get("root")
    if root not in by_key or by_key[root].get("params") != {}:
        raise ValueError("partition_tree_requires_unfiltered_root")
    protected = set(listing.get("params") or {}) | {listing.get("page_param", "page"), size}
    from urllib.parse import parse_qs, urlsplit
    protected.update(parse_qs(urlsplit(listing["url"]).query))
    seen = set()

    def visit(key, ancestors):
        if key in seen or key in ancestors or key not in by_key:
            raise ValueError("partition_tree_cycle_or_shared_child")
        seen.add(key)
        node = by_key[key]
        params = node.get("params")
        if not isinstance(params, dict) or any(not isinstance(k, str) or k in protected for k in params):
            raise ValueError("partition_tree_invalid_or_protected_params")
        if any(not isinstance(v, (str, int, float, bool)) for v in params.values()):
            raise ValueError("partition_tree_requires_literal_scalar_params")
        children = node.get("children", [])
        if not isinstance(children, list) or any(not isinstance(k, str) for k in children):
            raise ValueError("partition_tree_invalid_children")
        if children and len(children) < 2:
            raise ValueError("partition_tree_requires_two_children")
        for child in children:
            visit(child, ancestors | {key})
    visit(root, set())
    if seen != set(keys):
        raise ValueError("partition_tree_unreachable_node")
