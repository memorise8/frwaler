"""Extract text from a DOCX package without executing or fetching its contents."""
from pathlib import PurePosixPath
import posixpath
from urllib.parse import urlsplit
import zipfile

from lxml import etree


_WORD = {
    'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'http://purl.oclc.org/ooxml/wordprocessingml/main',
}
_RELS = 'http://schemas.openxmlformats.org/package/2006/relationships'
_TYPES = 'http://schemas.openxmlformats.org/package/2006/content-types'
_REL_PREFIXES = (
    'http://schemas.openxmlformats.org/officeDocument/2006/relationships/',
    'http://purl.oclc.org/ooxml/officeDocument/relationships/',
)
_MAIN_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
MAX_XML_BYTES = 32 * 1024 * 1024
MAX_PACKAGE_ENTRIES = 10000


def _xml(package, name, budget, limit=None):
    info = package.getinfo(name)
    if info.file_size > min(budget[0], limit or MAX_XML_BYTES):
        raise ValueError('DOCX XML size limit')
    budget[0] -= info.file_size
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False,
                             no_network=True, recover=False, huge_tree=False)
    root = etree.fromstring(package.read(info), parser)
    if root.getroottree().docinfo.doctype:
        raise ValueError('DOCX XML DTD')
    return root


def _target(base, value):
    parsed = urlsplit(value)
    if (not value or parsed.scheme or parsed.netloc or parsed.query
            or parsed.fragment or '\\' in value):
        raise ValueError('invalid DOCX part target')
    path = posixpath.normpath(posixpath.join(base, value.lstrip('/'))
                             if not value.startswith('/') else value[1:])
    if path in ('.', '..') or path.startswith('../'):
        raise ValueError('DOCX part escapes package')
    return path


def _relationships(package, name, budget):
    root = _xml(package, name, budget, 2 * 1024 * 1024)
    if root.tag != '{' + _RELS + '}Relationships':
        raise ValueError('invalid DOCX relationships')
    rows = list(root)
    ids = [r.get('Id') for r in rows]
    if None in ids or len(ids) != len(set(ids)):
        raise ValueError('ambiguous DOCX relationships')
    return rows


def _kind(row):
    value = row.get('Type', '')
    return next((value[len(p):] for p in _REL_PREFIXES if value.startswith(p)), '')


def _text(root, expected):
    qname = etree.QName(root)
    if qname.namespace not in _WORD or qname.localname != expected:
        raise ValueError('invalid Word text part')
    parts = []

    def walk(node):
        if not isinstance(node.tag, str):
            return
        q = etree.QName(node)
        word = q.namespace in _WORD
        if word and q.localname in ('del', 'moveFrom'):
            return  # Deleted revisions are not the current document text.
        if word and q.localname == 't':
            parts.append(node.text or '')
        elif word and q.localname == 'tab':
            parts.append('\t')
        elif word and q.localname in ('br', 'cr'):
            parts.append('\n')
        for child in node:
            walk(child)
        if word and q.localname in ('p', 'tr'):
            parts.append('\n')
        elif word and q.localname == 'tc':
            parts.append('\t')

    walk(root)
    return ''.join(parts).strip()


def extract_text_docx(filepath):
    """Read the document and linked headers, footers, footnotes and endnotes.

    Images, embedded objects, comments and field instructions are not text.
    Invalid/oversized packages return None, matching the converter contract.
    External relationship targets are never opened.
    """
    try:
        with zipfile.ZipFile(filepath) as package:
            names = package.namelist()
            if len(names) > MAX_PACKAGE_ENTRIES or len(names) != len(set(names)):
                return None
            budget = [MAX_XML_BYTES]
            types = _xml(package, '[Content_Types].xml', budget, 2 * 1024 * 1024)
            if types.tag != '{' + _TYPES + '}Types':
                return None
            main = [r for r in _relationships(package, '_rels/.rels', budget)
                    if _kind(r) == 'officeDocument']
            if len(main) != 1 or main[0].get('TargetMode', 'Internal') != 'Internal':
                return None
            name = _target('', main[0].get('Target', ''))
            matching = [r for r in types if r.get('PartName') == '/' + name]
            if len(matching) != 1 or matching[0].get('ContentType') != _MAIN_TYPE:
                return None
            parts = [_text(_xml(package, name, budget), 'document')]
            base = posixpath.dirname(name)
            rels = posixpath.join(base, '_rels', PurePosixPath(name).name + '.rels')
            seen = {name}
            roots = {'header': 'hdr', 'footer': 'ftr', 'footnotes': 'footnotes', 'endnotes': 'endnotes'}
            if rels in names:
                for row in _relationships(package, rels, budget):
                    kind = _kind(row)
                    if kind not in roots:
                        continue
                    if row.get('TargetMode', 'Internal') != 'Internal':
                        return None
                    target = _target(base, row.get('Target', ''))
                    if target not in seen:
                        parts.append(_text(_xml(package, target, budget), roots[kind]))
                        seen.add(target)
            return '\n\n'.join(p for p in parts if p) or None
    except (OSError, ValueError, KeyError, RuntimeError, RecursionError,
            zipfile.BadZipFile, etree.LxmlError):
        return None


def is_docx_package(filepath):
    """Identify Word packages by their contents, including extensionless files."""
    try:
        with zipfile.ZipFile(filepath) as package:
            if 'word/document.xml' in package.namelist():
                return True
            root = _xml(package, '[Content_Types].xml', [2 * 1024 * 1024])
            return any(r.get('ContentType') == _MAIN_TYPE for r in root)
    except (OSError, ValueError, KeyError, RuntimeError, zipfile.BadZipFile, etree.LxmlError):
        return False
