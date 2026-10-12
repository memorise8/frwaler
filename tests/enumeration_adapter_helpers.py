"""Offline helpers: actual registered classes without constructors or registry auto-imports."""
import importlib.util
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/enumeration_adapter'


def load_crawler(site_id):
    from crawler.generic_crawler import GenericCrawler
    from crawler.enumeration.guard import guarded
    path = ROOT / 'crawler/sites/configs' / (site_id + '.json')
    if path.exists():
        c = object.__new__(GenericCrawler)
        c._config = json.loads(path.read_text())
        c._fetch_method = c._config.get('options', {}).get('fetch_method')
        return c
    path = ROOT / 'crawler/sites/custom' / (site_id + '.py')
    spec = importlib.util.spec_from_file_location('contract_test_' + site_id.replace('-', '_'), path)
    mod = importlib.util.module_from_spec(spec)
    previous = sys.path[:]
    try:
        with guarded():
            spec.loader.exec_module(mod)
    finally:
        sys.path[:] = previous
    cls = next(x for x in vars(mod).values() if isinstance(x, type) and getattr(x, 'site_id', None) == site_id)
    return object.__new__(cls)


def baseline(name):
    key = 'crawler.enumeration._baseline_' + name
    if key not in sys.modules:
        module = types.ModuleType(key)
        module.__package__ = 'crawler.enumeration'
        sys.modules[key] = module
        source = (FIXTURES / (name + '-e2ebfc9.txt')).read_text()
        exec(compile(source, name + '-e2ebfc9', 'exec'), module.__dict__)
    return sys.modules[key]
