"""Source fingerprints must follow the selected parser as well as inherited loops."""
import tempfile
import unittest
from pathlib import Path

from delivery.health.provenance import SourceProvenance


class SourceProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.write('pkg/__init__.py', '')
        parent_code = 'class Parent:\n    def crawl(self):\n        return 1\n'
        self.write('pkg/parent.py', parent_code)
        env = {'__name__': 'pkg.parent'}
        exec(compile(parent_code, str(self.root/'pkg/parent.py'), 'exec'), env)
        self.parent = env['Parent']
        self.write('pkg/child.py', 'from . import helper\nclass Child:\n    FILTER = "first"\n')
        self.write('pkg/helper.py', 'from .deep import VALUE\n')
        self.write('pkg/deep.py', 'VALUE = 1\n')
        self.child = type('Child', (self.parent,), {'__module__': 'pkg.child'})

    def write(self, name, text):
        path = self.root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def fingerprint(self, cls=None, extras=()):
        return SourceProvenance(self.root).fingerprint(cls or self.child, extras)

    def test_inherited_crawl_does_not_hide_subclass_source_change(self):
        before, files = self.fingerprint()
        self.assertIn('pkg/parent.py', files)
        self.assertIn('pkg/child.py', files)
        self.write('pkg/child.py', 'from . import helper\nclass Child:\n    FILTER = "second"\n')
        self.assertNotEqual(before, self.fingerprint()[0])

    def test_local_transitive_relative_import_is_fingerprinted(self):
        before, files = self.fingerprint()
        self.assertIn('pkg/deep.py', files)
        self.write('pkg/deep.py', 'VALUE = 2\n')
        self.assertNotEqual(before, self.fingerprint()[0])

    def test_parent_package_initializer_is_fingerprinted(self):
        before, files = self.fingerprint()
        self.assertIn('pkg/__init__.py', files)
        self.write('pkg/__init__.py', 'SETTING = "changed"\n')
        self.assertNotEqual(before, self.fingerprint()[0])

    def test_registry_custom_module_without_sys_modules_entry_is_included(self):
        self.write('crawler/sites/custom/example.py', 'FILTER = "first"\n')
        cls = type('Custom', (self.parent,), {'__module__': 'custom_example'})
        before, files = self.fingerprint(cls)
        self.assertIn('crawler/sites/custom/example.py', files)
        self.write('crawler/sites/custom/example.py', 'FILTER = "second"\n')
        self.assertNotEqual(before, self.fingerprint(cls)[0])

    def test_configuration_change_invalidates_selected_implementation(self):
        self.write('config.json', '{"filter": 1}')
        before, files = self.fingerprint(extras=['config.json'])
        self.assertIn('config.json', files)
        self.write('config.json', '{"filter": 2}')
        self.assertNotEqual(before, self.fingerprint(extras=['config.json'])[0])

    def test_unselected_source_does_not_change_selected_fingerprint(self):
        self.write('pkg/unselected.py', 'FILTER = "old"\n')
        before, files = self.fingerprint()
        self.assertNotIn('pkg/unselected.py', files)
        self.write('pkg/unselected.py', 'FILTER = "new"\n')
        self.assertEqual(before, self.fingerprint()[0])

    def test_circular_imports_terminate_and_extra_order_is_irrelevant(self):
        self.write('pkg/deep.py', 'from .child import Child\nVALUE = 1\n')
        one = self.fingerprint(extras=['pkg/deep.py', 'pkg/helper.py'])
        two = self.fingerprint(extras=['pkg/helper.py', 'pkg/deep.py', 'pkg/deep.py'])
        self.assertEqual(one, two)
        self.assertEqual(len(one[1]), len(set(one[1])))

    def test_external_and_dynamic_imports_are_not_executed(self):
        self.write('pkg/helper.py', 'import nonexistent_external_package\nraise RuntimeError("must not execute")\n')
        _, files = self.fingerprint()
        self.assertIn('pkg/helper.py', files)

    def test_missing_explicit_dependency_cannot_produce_a_fingerprint(self):
        with self.assertRaises(FileNotFoundError):
            self.fingerprint(extras=['missing.json'])
