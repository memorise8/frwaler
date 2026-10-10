"""Fingerprint selected implementation files and their local static imports.

External packages and dynamically constructed imports are not resolved here;
those need environment/runtime evidence in addition to this source fingerprint.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path


class SourceProvenance:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._bodies = {}
        self._imports = {}

    def _owned(self, path):
        path = Path(path).resolve()
        return path if path.is_relative_to(self.root) and path.is_file() else None

    def _module_files(self, module):
        parts = module.split('.') if module else []
        if not parts or any(not part.isidentifier() for part in parts):
            return set()
        base = self.root.joinpath(*parts)
        target = base.with_suffix('.py')
        if not target.is_file():
            target = base/'__init__.py'
        if not target.is_file():
            return set()
        files = {target.resolve()}
        for i in range(1, len(parts)):
            initializer = self.root.joinpath(*parts[:i], '__init__.py')
            if initializer.is_file():
                files.add(initializer.resolve())
        return files

    def _body(self, path):
        if path not in self._bodies:
            self._bodies[path] = path.read_bytes()
        return self._bodies[path]

    def _dependencies(self, path):
        if path in self._imports:
            return self._imports[path]
        found = set()
        if path.suffix == '.py':
            package = list(path.parent.relative_to(self.root).parts)
            tree = ast.parse(self._body(path).decode('utf-8-sig'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        found.update(self._module_files(alias.name))
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        if node.level > len(package):
                            continue
                        prefix = package[:len(package)-node.level+1]
                    else:
                        prefix = []
                    module = prefix + (node.module.split('.') if node.module else [])
                    found.update(self._module_files('.'.join(module)))
                    for alias in node.names:
                        if alias.name != '*':
                            found.update(self._module_files('.'.join(module + [alias.name])))
        self._imports[path] = found
        return found

    def implementation_files(self, cls):
        files = set()
        for base in cls.__mro__:
            module = base.__module__
            if module.startswith('custom_'):
                # The registry loads custom modules with this explicit name;
                # spec_from_file_location does not add them to sys.modules.
                path = self._owned(self.root/'crawler/sites/custom'/(module[7:]+'.py'))
                if path:
                    files.add(path)
            else:
                files.update(self._module_files(module))
            for member in vars(base).values():
                functions = (member.fget, member.fset, member.fdel) if isinstance(member, property) else (
                    member.__func__ if isinstance(member, (staticmethod, classmethod)) else member,
                )
                for function in functions:
                    code = getattr(function, '__code__', None)
                    if code:
                        path = self._owned(code.co_filename)
                        if path:
                            files.add(path)
        return files

    def fingerprint(self, cls, extra_files=()):
        pending = self.implementation_files(cls)
        for filename in extra_files:
            path = self._owned(self.root/filename)
            if path is None:
                raise FileNotFoundError(str(filename))
            pending.add(path)
        visited = set()
        while pending:
            path = pending.pop()
            if path in visited:
                continue
            visited.add(path)
            pending.update(self._dependencies(path)-visited)
        digest = hashlib.sha256(b'local-static-import-closure-v2\0')
        names = []
        for path in sorted(visited):
            name = path.relative_to(self.root).as_posix()
            body = self._body(path)
            digest.update(name.encode('utf-8') + b'\0')
            digest.update(len(body).to_bytes(8, 'big') + body)
            names.append(name)
        return digest.hexdigest(), names
