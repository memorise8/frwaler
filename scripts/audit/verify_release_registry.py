"""Read-only registry and source audit, intended for an isolated worker image.

Usage: python verify_release_registry.py --output /audit/current_registry.json
No crawl is executed. Imports are observed so silent loader failures are visible.
"""
import argparse
import ast
import hashlib
import importlib.machinery
import inspect
import json
import os
from pathlib import Path
import sys
from collections import defaultdict


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path("/app/crawler")
    errors = []
    declarations = defaultdict(list)
    syntax_errors = []
    hashes = {}
    files = list(root.rglob("*.py"))
    for path in files:
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(root))
        content = path.read_bytes()
        hashes[rel] = hashlib.sha256(content).hexdigest()
        try:
            tree = ast.parse(content, filename=str(path))
        except SyntaxError as exc:
            syntax_errors.append({"file": rel, "error": str(exc)})
            continue
        if "/sites/" in str(path):
            constants = {target.id: statement.value.value for statement in tree.body
                         if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Constant)
                         for target in statement.targets if isinstance(target, ast.Name)}
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for field in node.body:
                        if isinstance(field, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "site_id" for t in field.targets):
                            if isinstance(field.value, ast.Constant) and isinstance(field.value.value, str) and field.value.value:
                                declarations[field.value.value].append(rel)
                        elif isinstance(field, ast.FunctionDef) and field.name == "site_id":
                            for statement in field.body:
                                if isinstance(statement, ast.Return):
                                    value = statement.value
                                    sid = value.value if isinstance(value, ast.Constant) else constants.get(value.id) if isinstance(value, ast.Name) else None
                                    if isinstance(sid, str) and sid:
                                        declarations[sid].append(rel)
    for path in sorted((root / "sites/configs").glob("*.json")):
        rel = str(path.relative_to(root))
        hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        try:
            cfg = json.loads(path.read_text())
            if cfg.get("site_id"):
                declarations[cfg["site_id"]].append(rel)
        except Exception as exc:
            syntax_errors.append({"file": rel, "error": str(exc)})

    original = importlib.machinery.SourceFileLoader.exec_module
    def watched(loader, module):
        try:
            return original(loader, module)
        except Exception as exc:
            if "/crawler/sites/custom/" in loader.path:
                errors.append({"file": loader.path, "error": f"{type(exc).__name__}: {exc}"})
            raise
    importlib.machinery.SourceFileLoader.exec_module = watched
    from crawler.sites import CRAWLERS
    registry = []
    for sid, cls in sorted(CRAWLERS.items()):
        source = getattr(cls, "_config_path", None) or getattr(getattr(cls.crawl, "__code__", None), "co_filename", "")
        registry.append({"id": sid, "class": cls.__name__, "source": source,
                         "name": str(getattr(cls, "site_name", "")), "url": str(getattr(cls, "base_url", "")),
                         "signature": str(inspect.signature(cls.crawl)), "abstract": inspect.isabstract(cls)})
    from playwright.sync_api import sync_playwright
    browser = {}
    with sync_playwright() as playwright:
        path = playwright.chromium.executable_path
        browser = {"chromium_path": path, "exists": os.path.exists(path)}
        try:
            instance = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            instance.close()
            browser["launch"] = "ok"
        except Exception as exc:
            browser["launch"] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
    result = {"python": sys.version, "python_files": len(files),
              "custom_files": len(list((root / "sites/custom").glob("*.py"))) - int((root / "sites/custom/__init__.py").exists()),
              "config_files": len(list((root / "sites/configs").glob("*.json"))),
              "syntax_errors": syntax_errors, "import_errors": errors, "registry": registry,
              "missing_ids": sorted(set(declarations) - set(CRAWLERS)),
              "duplicate_ids": {sid: paths for sid, paths in declarations.items() if len(paths) > 1},
              "hashes": hashes, "browser": browser}
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({key: len(value) if isinstance(value, list) else value for key, value in result.items()
                      if key not in ("registry", "duplicate_ids", "hashes")}, ensure_ascii=False))
    print(f"registry={len(registry)}, duplicate_ids={len(result['duplicate_ids'])}, output={args.output}")


if __name__ == "__main__":
    main()
