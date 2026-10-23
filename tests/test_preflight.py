"""Phase 1 — Pre-flight rejection (Sprint 141 / v31.4.0)."""

import io
import os
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

from powerbi_import.preflight import (
    ADVISORY,
    BLOCKER,
    LARGE_VISUAL_COUNT,
    LARGE_WORKBOOK_BYTES,
    PreflightIssue,
    PreflightResult,
    SUPPORTED_EXTENSIONS,
    UNSUPPORTED_CONNECTORS,
    WARNING,
    run_preflight,
)


# ────────────────────────────────────────────────────────────────────
#  Fixture helpers
# ────────────────────────────────────────────────────────────────────

_MINIMAL_TWB = """<?xml version='1.0' encoding='utf-8'?>
<workbook source-build='2024.3.0' version='18.1'>
  <datasources>
    <datasource name='ds1'>
      <connection class='sqlserver' server='localhost'/>
    </datasource>
  </datasources>
  <worksheets>
    <worksheet name='Sheet1'/>
  </worksheets>
</workbook>
""".encode('utf-8')


def _write_twb(tmp: str, name: str = 'wb.twb',
               body: bytes = _MINIMAL_TWB) -> str:
    p = os.path.join(tmp, name)
    with open(p, 'wb') as f:
        f.write(body)
    return p


def _write_twbx(tmp: str, twb_body: bytes = _MINIMAL_TWB,
                  extra_files: dict | None = None,
                  twb_name: str = 'wb.twb',
                  encrypt: bool = False) -> str:
    p = os.path.join(tmp, 'wb.twbx')
    with zipfile.ZipFile(p, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(twb_name, twb_body)
        for name, body in (extra_files or {}).items():
            zf.writestr(name, body)
    if encrypt:
        # Forge the encryption flag bit on the twb entry. Easiest way:
        # rewrite the central directory manually.
        with open(p, 'rb') as f:
            raw = f.read()
        # zipfile doesn't write encryption from stdlib. Patch flag bit
        # of every local file header (offset 6) to set bit 0.
        out = bytearray(raw)
        sig = b'PK\x03\x04'
        i = 0
        while True:
            j = out.find(sig, i)
            if j < 0:
                break
            out[j + 6] |= 0x01
            i = j + 4
        # Same for central directory headers (offset 8 from PK\x01\x02)
        cd_sig = b'PK\x01\x02'
        i = 0
        while True:
            j = out.find(cd_sig, i)
            if j < 0:
                break
            out[j + 8] |= 0x01
            i = j + 4
        with open(p, 'wb') as f:
            f.write(bytes(out))
    return p


# ════════════════════════════════════════════════════════════════════
#  Result dataclass plumbing
# ════════════════════════════════════════════════════════════════════

class TestResultObject(unittest.TestCase):
    def test_empty_result_ok(self):
        r = PreflightResult(path='x')
        self.assertTrue(r.ok)
        self.assertEqual(r.blockers, [])
        self.assertEqual(r.warnings, [])
        self.assertEqual(r.advisories, [])

    def test_add_records_severity(self):
        r = PreflightResult(path='x')
        r.add(BLOCKER, 'c1', 'm1')
        r.add(WARNING, 'c2', 'm2', suggestion='do x')
        r.add(ADVISORY, 'c3', 'm3')
        self.assertFalse(r.ok)
        self.assertEqual(len(r.blockers), 1)
        self.assertEqual(len(r.warnings), 1)
        self.assertEqual(len(r.advisories), 1)
        self.assertEqual(r.warnings[0].suggestion, 'do x')

    def test_as_dict_serialises_all(self):
        r = PreflightResult(path='wb.twbx')
        r.add(BLOCKER, 'c', 'm')
        d = r.as_dict()
        self.assertEqual(d['path'], 'wb.twbx')
        self.assertFalse(d['ok'])
        self.assertEqual(len(d['blockers']), 1)
        self.assertEqual(d['blockers'][0]['code'], 'c')

    def test_format_console_handles_no_issues(self):
        r = PreflightResult(path='x')
        out = r.format_console()
        self.assertIn('OK', out)

    def test_format_console_renders_each_severity(self):
        r = PreflightResult(path='x')
        r.add(BLOCKER, 'cb', 'mb', suggestion='sb')
        r.add(WARNING, 'cw', 'mw')
        r.add(ADVISORY, 'ca', 'ma')
        out = r.format_console()
        self.assertIn('BLOCKER', out)
        self.assertIn('WARNING', out)
        self.assertIn('ADVISORY', out)
        self.assertIn('sb', out)


# ════════════════════════════════════════════════════════════════════
#  Path checks
# ════════════════════════════════════════════════════════════════════

class TestPathChecks(unittest.TestCase):
    def test_empty_path_blocked(self):
        r = run_preflight('')
        self.assertFalse(r.ok)
        self.assertEqual(r.blockers[0].code, 'empty_path')

    def test_null_byte_blocked(self):
        r = run_preflight('foo\x00bar.twbx')
        self.assertFalse(r.ok)
        self.assertEqual(r.blockers[0].code, 'null_byte_path')

    def test_missing_file_blocked(self):
        r = run_preflight('/nonexistent/path/wb.twbx')
        self.assertFalse(r.ok)
        self.assertEqual(r.blockers[0].code, 'missing_file')

    def test_unsupported_extension_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, 'wb.pbix')
            open(p, 'wb').close()
            r = run_preflight(p)
            self.assertFalse(r.ok)
            self.assertEqual(r.blockers[0].code, 'unsupported_extension')


# ════════════════════════════════════════════════════════════════════
#  Happy paths
# ════════════════════════════════════════════════════════════════════

class TestHappyPath(unittest.TestCase):
    def test_minimal_twb_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_twb(tmp)
            r = run_preflight(p)
            self.assertTrue(r.ok, msg=r.format_console())

    def test_minimal_twbx_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_twbx(tmp)
            r = run_preflight(p)
            self.assertTrue(r.ok, msg=r.format_console())

    def test_xml_workbook_with_twbx_extension_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, 'workbook.twbx')
            with open(p, 'wb') as stream:
                stream.write(_MINIMAL_TWB)
            r = run_preflight(p)
            self.assertTrue(r.ok, msg=r.format_console())

    def test_duplicate_twb_members_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_twbx(tmp, extra_files={'nested/second.twb': _MINIMAL_TWB})
            r = run_preflight(p)
            self.assertFalse(r.ok)
            self.assertTrue(any(i.code == 'duplicate_twb' for i in r.blockers))

    def test_xxe_xml_blocked_by_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = b'''<!DOCTYPE workbook [<!ENTITY xxe SYSTEM "file:///secret">]>
<workbook><description>&xxe;</description></workbook>'''
            r = run_preflight(_write_twb(tmp, body=body))
            self.assertFalse(r.ok)
            self.assertTrue(any(i.code == 'corrupt_xml' for i in r.blockers))

    def test_missing_datasource_dependency_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = b'''<workbook><datasources><datasource name="ds1"/></datasources>
<worksheets><worksheet name="Sheet1"><datasource-dependencies datasource="missing"/></worksheet>
</worksheets></workbook>'''
            r = run_preflight(_write_twb(tmp, body=body))
            self.assertFalse(r.ok)
            self.assertTrue(any(i.code == 'missing_datasource_reference' for i in r.blockers))

    def test_missing_dashboard_worksheet_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = b'''<workbook><worksheets><worksheet name="Sheet1"/></worksheets>
<dashboards><dashboard name="Dashboard"><zone-contains>Worksheet: Missing</zone-contains>
</dashboard></dashboards></workbook>'''
            r = run_preflight(_write_twb(tmp, body=body))
            self.assertFalse(r.ok)
            self.assertTrue(any(i.code == 'missing_worksheet_reference' for i in r.blockers))

    def test_duplicate_referenced_worksheet_names_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = b'''<workbook><worksheets><worksheet name="Sheet1"/>
<worksheet name="Sheet1"/></worksheets><dashboards><dashboard name="Dashboard">
<zone-contains>Worksheet: Sheet1</zone-contains></dashboard></dashboards></workbook>'''
            r = run_preflight(_write_twb(tmp, body=body))
            self.assertFalse(r.ok)
            self.assertTrue(any(i.code == 'duplicate_worksheet_name' for i in r.blockers))

    def test_duplicate_unreferenced_worksheet_names_warned(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = b'''<workbook><worksheets><worksheet name="Sheet1"/>
<worksheet name="Sheet1"/></worksheets></workbook>'''
            r = run_preflight(_write_twb(tmp, body=body))
            self.assertTrue(r.ok)
            self.assertTrue(any(i.code == 'duplicate_worksheet_name' for i in r.warnings))

    def test_supported_extensions_constant(self):
        # Defensive: don't accidentally drop one
        self.assertIn('.twb', SUPPORTED_EXTENSIONS)
        self.assertIn('.twbx', SUPPORTED_EXTENSIONS)
        self.assertIn('.tfl', SUPPORTED_EXTENSIONS)


# ════════════════════════════════════════════════════════════════════
#  Inline virtual Parameters dependencies
# ════════════════════════════════════════════════════════════════════

class TestInlineVirtualParameters(unittest.TestCase):
    def _run_dependencies(self, dependencies, declared=(), packaged=False):
        root = ET.fromstring(_MINIMAL_TWB)
        for name in declared:
            ET.SubElement(root.find('datasources'), 'datasource', name=name)
        worksheet = root.find('worksheets/worksheet')
        for reference, columns in dependencies:
            dependency = ET.SubElement(
                worksheet, 'datasource-dependencies', datasource=reference,
            )
            for attributes in columns:
                ET.SubElement(dependency, 'column', attributes)
        body = ET.tostring(root, encoding='utf-8')
        with tempfile.TemporaryDirectory() as tmp:
            path = (_write_twbx(tmp, twb_body=body) if packaged
                    else _write_twb(tmp, body=body))
            return run_preflight(path)

    def _assert_missing_dependency(self, result, reference):
        self.assertFalse(result.ok, msg=result.format_console())
        self.assertEqual(
            [issue.code for issue in result.blockers],
            ['missing_datasource_reference'],
        )
        self.assertIn(repr(reference), result.blockers[0].message)

    def test_named_inline_parameter_columns_with_nonempty_domains_are_valid(self):
        columns = [
            {'name': f'[Parameter {index}]', 'param-domain-type': domain}
            for index, domain in enumerate(('any', 'list', 'range'), start=1)
        ]
        # Exercise both one-column and multi-column virtual datasources.
        for declared_columns in [[column] for column in columns] + [columns]:
            for packaged in (False, True):
                with self.subTest(columns=declared_columns, packaged=packaged):
                    result = self._run_dependencies(
                        [('Parameters', declared_columns)], packaged=packaged,
                    )
                    self.assertTrue(result.ok, msg=result.format_console())
                    self.assertEqual(result.blockers, [])

    def test_bare_parameters_dependency_is_still_a_blocker(self):
        for packaged in (False, True):
            with self.subTest(packaged=packaged):
                result = self._run_dependencies(
                    [('Parameters', [])], packaged=packaged,
                )
                self._assert_missing_dependency(result, 'Parameters')

    def test_parameter_columns_require_nonempty_names_and_domain_markers(self):
        invalid_columns = (
            {'name': '[Parameter 1]'},
            {'name': '[Parameter 1]', 'param-domain-type': ''},
            {'param-domain-type': 'list'},
            {'name': '', 'param-domain-type': 'range'},
            {},
        )
        for column in invalid_columns:
            for packaged in (False, True):
                with self.subTest(column=column, packaged=packaged):
                    result = self._run_dependencies(
                        [('Parameters', [column])], packaged=packaged,
                    )
                    self._assert_missing_dependency(result, 'Parameters')

    def test_mixed_parameter_and_regular_columns_are_blockers_in_either_order(self):
        parameter = {'name': '[Parameter 1]', 'param-domain-type': 'list'}
        regular = {'name': '[Customer]', 'datatype': 'string'}
        for columns in ([parameter, regular], [regular, parameter]):
            for packaged in (False, True):
                with self.subTest(columns=columns, packaged=packaged):
                    result = self._run_dependencies(
                        [('Parameters', columns)], packaged=packaged,
                    )
                    self._assert_missing_dependency(result, 'Parameters')

    def test_only_exact_parameters_reference_gets_the_inline_exception(self):
        columns = [{'name': '[Parameter 1]', 'param-domain-type': 'any'}]
        for reference in ('MissingSource', 'parameters', 'Parameters ', 'Parameter'):
            with self.subTest(reference=reference):
                result = self._run_dependencies([(reference, columns)])
                self._assert_missing_dependency(result, reference)

    def test_valid_virtual_parameters_do_not_hide_real_missing_dependencies(self):
        parameter = ('Parameters', [
            {'name': '[Parameter 1]', 'param-domain-type': 'range'},
        ])
        missing = ('MissingSource', [{'name': '[Customer]'}])
        for dependencies in ([parameter, missing], [missing, parameter]):
            with self.subTest(dependencies=dependencies):
                result = self._run_dependencies(dependencies)
                self._assert_missing_dependency(result, 'MissingSource')

    def test_each_parameters_dependency_must_independently_prove_it_is_virtual(self):
        valid = ('Parameters', [
            {'name': '[Parameter 1]', 'param-domain-type': 'list'},
        ])
        for invalid_columns in ([], [{'name': '[Ordinary Column]'}]):
            invalid = ('Parameters', invalid_columns)
            for dependencies in ([valid, invalid], [invalid, valid]):
                with self.subTest(dependencies=dependencies):
                    result = self._run_dependencies(dependencies)
                    self._assert_missing_dependency(result, 'Parameters')

    def test_declared_datasources_do_not_need_inline_parameter_markers(self):
        for reference, columns in (
            ('Parameters', []),
            ('Parameters', [{'name': '[Physical Column]'}]),
            ('ds1', []),
            ('ds1', [{'name': '[Physical Column]'}]),
        ):
            with self.subTest(reference=reference, columns=columns):
                result = self._run_dependencies(
                    [(reference, columns)], declared=('Parameters',),
                )
                self.assertTrue(result.ok, msg=result.format_console())
                self.assertEqual(result.blockers, [])


# ════════════════════════════════════════════════════════════════════
#  Archive integrity
# ════════════════════════════════════════════════════════════════════

class TestArchiveIntegrity(unittest.TestCase):
    def test_corrupt_zip_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, 'broken.twbx')
            with open(p, 'wb') as f:
                f.write(b'not a real zip')
            r = run_preflight(p)
            self.assertFalse(r.ok)
            self.assertEqual(r.blockers[0].code, 'corrupt_archive')

    def test_zip_traversal_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, 'evil.twbx')
            with zipfile.ZipFile(p, 'w') as zf:
                zf.writestr('wb.twb', _MINIMAL_TWB)
                zf.writestr('../../etc/passwd', b'root:x:0:0::/:/bin/sh')
            r = run_preflight(p)
            self.assertFalse(r.ok)
            self.assertEqual(r.blockers[0].code, 'zip_traversal')

    def test_absolute_path_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, 'evil.twbx')
            with zipfile.ZipFile(p, 'w') as zf:
                zf.writestr('wb.twb', _MINIMAL_TWB)
                # absolute path inside zip
                zi = zipfile.ZipInfo('/abs/file.txt')
                zf.writestr(zi, b'x')
            r = run_preflight(p)
            self.assertFalse(r.ok)
            self.assertEqual(r.blockers[0].code, 'zip_traversal')

    def test_encrypted_archive_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_twbx(tmp, encrypt=True)
            r = run_preflight(p)
            self.assertFalse(r.ok)
            codes = {i.code for i in r.blockers}
            self.assertIn('encrypted_workbook', codes)


# ════════════════════════════════════════════════════════════════════
#  XML well-formedness
# ════════════════════════════════════════════════════════════════════

class TestXmlChecks(unittest.TestCase):
    def test_corrupt_xml_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_twb(tmp, body=b'<workbook><not closed')
            r = run_preflight(p)
            self.assertFalse(r.ok)
            self.assertEqual(r.blockers[0].code, 'corrupt_xml')

    def test_empty_twb_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_twb(tmp, body=b'')
            r = run_preflight(p)
            self.assertFalse(r.ok)
            self.assertEqual(r.blockers[0].code, 'empty_xml')

    def test_twbx_without_twb_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, 'bad.twbx')
            with zipfile.ZipFile(p, 'w') as zf:
                zf.writestr('readme.txt', b'no twb here')
            r = run_preflight(p)
            self.assertFalse(r.ok)
            codes = {i.code for i in r.blockers}
            self.assertIn('missing_twb', codes)


# ════════════════════════════════════════════════════════════════════
#  Tableau version
# ════════════════════════════════════════════════════════════════════

class TestVersionChecks(unittest.TestCase):
    def test_supported_version_no_warning(self):
        body = _MINIMAL_TWB.replace(b"source-build='2024.3.0'",
                                      b"source-build='2024.3.0'")
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_twb(tmp, body=body)
            r = run_preflight(p)
            codes = {i.code for i in r.warnings}
            self.assertNotIn('newer_tableau_version', codes)

    def test_newer_version_warns(self):
        body = _MINIMAL_TWB.replace(b"source-build='2024.3.0'",
                                      b"source-build='2026.1.0'")
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_twb(tmp, body=body)
            r = run_preflight(p)
            codes = {i.code for i in r.warnings}
            self.assertIn('newer_tableau_version', codes)
            # Still ok (no blocker)
            self.assertTrue(r.ok)

    def test_no_source_build_silent(self):
        body = _MINIMAL_TWB.replace(b" source-build='2024.3.0'", b"")
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_twb(tmp, body=body)
            r = run_preflight(p)
            codes = {i.code for i in r.warnings}
            self.assertNotIn('newer_tableau_version', codes)


# ════════════════════════════════════════════════════════════════════
#  Connector checks
# ════════════════════════════════════════════════════════════════════

class TestConnectorChecks(unittest.TestCase):
    def test_supported_connector_no_blocker(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_twb(tmp)
            r = run_preflight(p)
            codes = {i.code for i in r.blockers}
            self.assertNotIn('unsupported_connector', codes)

    def test_essbase_blocked(self):
        body = _MINIMAL_TWB.replace(b"class='sqlserver'",
                                      b"class='essbase'")
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_twb(tmp, body=body)
            r = run_preflight(p)
            self.assertFalse(r.ok)
            blocker = r.blockers[0]
            self.assertEqual(blocker.code, 'unsupported_connector')
            self.assertIn('Essbase', blocker.suggestion)

    def test_unsupported_connectors_constant_non_empty(self):
        self.assertGreater(len(UNSUPPORTED_CONNECTORS), 0)


# ════════════════════════════════════════════════════════════════════
#  Visual count advisory
# ════════════════════════════════════════════════════════════════════

class TestVisualCount(unittest.TestCase):
    def test_few_visuals_no_advisory(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_twb(tmp)
            r = run_preflight(p)
            codes = {i.code for i in r.advisories}
            self.assertNotIn('many_worksheets', codes)

    def test_many_visuals_advises(self):
        worksheets = b''.join(
            f"<worksheet name='S{i}'/>".encode()
            for i in range(LARGE_VISUAL_COUNT + 5)
        )
        body = _MINIMAL_TWB.replace(
            b"<worksheets>\n    <worksheet name='Sheet1'/>\n  </worksheets>",
            b'<worksheets>' + worksheets + b'</worksheets>',
        )
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_twb(tmp, body=body)
            r = run_preflight(p)
            codes = {i.code for i in r.advisories}
            self.assertIn('many_worksheets', codes)


# ════════════════════════════════════════════════════════════════════
#  Size advisory
# ════════════════════════════════════════════════════════════════════

class TestSizeAdvisory(unittest.TestCase):
    def test_small_file_no_advisory(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_twb(tmp)
            r = run_preflight(p)
            codes = {i.code for i in r.advisories}
            self.assertNotIn('large_workbook', codes)


# ════════════════════════════════════════════════════════════════════
#  Missing extracts
# ════════════════════════════════════════════════════════════════════

class TestMissingExtracts(unittest.TestCase):
    def test_extract_present_no_warning(self):
        body = _MINIMAL_TWB.replace(
            b"<connection class='sqlserver' server='localhost'/>",
            b"<connection class='sqlserver'><extract filename='Data/E1.hyper'/></connection>",
        )
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_twbx(tmp, twb_body=body, extra_files={
                'Data/E1.hyper': b'fake-hyper-bytes',
            })
            r = run_preflight(p)
            codes = {i.code for i in r.warnings}
            self.assertNotIn('missing_extract', codes)

    def test_extract_missing_warns(self):
        body = _MINIMAL_TWB.replace(
            b"<connection class='sqlserver' server='localhost'/>",
            b"<connection class='sqlserver'><extract filename='Data/Missing.hyper'/></connection>",
        )
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_twbx(tmp, twb_body=body)
            r = run_preflight(p)
            codes = {i.code for i in r.warnings}
            self.assertIn('missing_extract', codes)
            self.assertTrue(r.ok)  # warning only


if __name__ == '__main__':
    unittest.main()
