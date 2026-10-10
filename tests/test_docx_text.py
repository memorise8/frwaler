"""Word ZIPs must reach the Word parser instead of disappearing as empty HWPX."""
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from crawler.converter import convert_file
from crawler.docx_text import extract_text_docx

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R='http://schemas.openxmlformats.org/package/2006/relationships'
O='http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
T='http://schemas.openxmlformats.org/package/2006/content-types'
M='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'

def rels(rows):
    return f'<Relationships xmlns="{R}">'+''.join(f'<Relationship Id="r{i}" Type="{O}{kind}" Target="{target}" {extra}/>' for i,(kind,target,extra) in enumerate(rows))+'</Relationships>'

def document(body, namespace=W):
    return f'<w:document xmlns:w="{namespace}"><w:body>{body}</w:body></w:document>'

def package(body=None, main='word/document.xml'):
    return {'[Content_Types].xml':f'<Types xmlns="{T}"><Override PartName="/{main}" ContentType="{M}"/></Types>',
            '_rels/.rels':rels([('officeDocument',main,'')]),
            main:document(body or '<w:p><w:r><w:t>Source title #1 Yield*Trait</w:t></w:r></w:p>')}

class DOCXTextTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'captured-blob'

    def write(self, parts):
        with zipfile.ZipFile(self.path,'w',zipfile.ZIP_DEFLATED) as z:
            for name,data in parts.items():z.writestr(name,data)
        return self.path

    def test_extensionless_document_preserves_source_symbols(self):
        self.assertEqual(convert_file(self.write(package())), 'Source title #1 Yield*Trait')

    def test_split_runs_tabs_breaks_hyperlinks_and_table_order(self):
        p=package('<w:p><w:r><w:t>First</w:t><w:tab/><w:t>second</w:t><w:br/></w:r><w:hyperlink><w:r><w:t>link</w:t></w:r></w:hyperlink></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>cell one</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>cell two</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
        text=convert_file(self.write(p));self.assertTrue(text.startswith('First\tsecond\nlink\n'));self.assertEqual(text.count('cell one'),1);self.assertLess(text.index('cell one'),text.index('cell two'))

    def test_deleted_revisions_and_field_instructions_are_excluded(self):
        p=package('<w:p><w:del><w:r><w:t>deleted</w:t></w:r></w:del><w:moveFrom><w:r><w:t>moved</w:t></w:r></w:moveFrom><w:r><w:instrText>FIELD CODE</w:instrText><w:t>visible</w:t></w:r></w:p>')
        self.assertEqual(convert_file(self.write(p)),'visible')

    def test_related_notes_headers_and_footers_are_read_once(self):
        p=package();relations=[]
        for kind,root in [('header','hdr'),('footer','ftr'),('footnotes','footnotes'),('endnotes','endnotes')]:
            relations.append((kind,kind+'.xml',''));p['word/'+kind+'.xml']=f'<w:{root} xmlns:w="{W}"><w:p><w:r><w:t>{kind} text</w:t></w:r></w:p></w:{root}>'
        relations.extend([('header','header.xml',''),('hyperlink','https://invalid.example/','TargetMode="External"')]);p['word/_rels/document.xml.rels']=rels(relations)
        text=convert_file(self.write(p))
        for kind in ('header','footer','footnotes','endnotes'):self.assertEqual(text.count(kind+' text'),1)

    def test_nondefault_main_part_and_strict_word_namespace(self):
        p=package(main='content/main.xml');p['content/main.xml']=document('<w:p><w:r><w:t>strict</w:t></w:r></w:p>','http://purl.oclc.org/ooxml/wordprocessingml/main')
        self.assertEqual(convert_file(self.write(p)),'strict')

    def test_external_or_escaping_text_part_is_not_opened(self):
        for target,extra in [('https://invalid.example/x','TargetMode="External"'),('../../outside.xml',''),('file:///etc/passwd','')]:
            with self.subTest(target=target):
                p=package();p['word/_rels/document.xml.rels']=rels([('header',target,extra)]);self.assertIsNone(convert_file(self.write(p)))

    def test_broken_linked_part_does_not_silently_yield_partial_body(self):
        p=package();p['word/_rels/document.xml.rels']=rels([('footnotes','absent.xml','')]);self.assertIsNone(convert_file(self.write(p)))

    def test_DTD_and_entity_references_are_not_expanded(self):
        for encoding in ('utf-8','utf-16'):
            p=package();p['word/document.xml']=('<?xml version="1.0" encoding="'+encoding+'"?><!DOCTYPE w:document [<!ENTITY x SYSTEM "file:///etc/passwd">]>'+document('<w:p><w:r><w:t>&x;</w:t></w:r></w:p>')).encode(encoding)
            self.assertIsNone(convert_file(self.write(p)))

    def test_duplicate_zip_members_are_ambiguous(self):
        self.write(package())
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with zipfile.ZipFile(self.path,'a') as z:z.writestr('word/document.xml',document(''))
        self.assertIsNone(convert_file(self.path))

    def test_XML_budget_and_member_count_are_enforced(self):
        self.write(package())
        with patch('crawler.docx_text.MAX_XML_BYTES',32):self.assertIsNone(extract_text_docx(self.path))
        with patch('crawler.docx_text.MAX_PACKAGE_ENTRIES',2):self.assertIsNone(extract_text_docx(self.path))

    def test_wrong_content_type_and_wrong_root_are_rejected(self):
        p=package();p['[Content_Types].xml']=p['[Content_Types].xml'].replace(M,'application/xml');self.assertIsNone(convert_file(self.write(p)))
        p=package();p['word/document.xml']='<document>not Word</document>';self.assertIsNone(convert_file(self.write(p)))

    def test_empty_or_invalid_Word_does_not_fall_back_to_HWPX(self):
        for value in (document('<w:p/>'),'<broken>'):
            p=package();p['word/document.xml']=value;p['Contents/section0.xml']='<root><t>wrong format</t></root>';self.assertIsNone(convert_file(self.write(p)))

    def test_HWPX_and_unrelated_ZIP_keep_existing_behavior(self):
        self.assertEqual(convert_file(self.write({'Contents/section0.xml':'<root><t>HWPX text</t></root>'})),'HWPX text')
        self.assertIsNone(convert_file(self.write({'readme.txt':'ordinary ZIP'})))

    def test_duplicate_main_relationship_is_rejected(self):
        p=package();p['_rels/.rels']=rels([('officeDocument','word/document.xml','')]*2);self.assertIsNone(convert_file(self.write(p)))

if __name__=='__main__':unittest.main()
