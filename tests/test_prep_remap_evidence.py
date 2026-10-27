"""Evidence-contract tests for Prep ``.v2018_3_3.Remap`` node structure.

Static structural evidence only: no production converter is exercised and no
runtime behaviour is claimed. Covers 1 dialect node type (Remap), not all
Prep dialects. Source: aloth/tableau-book-resources (CC-BY-4.0, Alexander Loth).
"""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "prep_dialect_samples" / "remap_evidence.json"
TFL = ROOT / "examples" / "real_world" / "tableau_prep_book.tfl"


class EvidenceError(ValueError):
    """Raised by the test-side evidence decoder for malformed/ambiguous maps."""


def _unquote(s):
    if not isinstance(s, str):
        raise EvidenceError("encoded value must be a string: %r" % (s,))
    try:
        v = json.loads(s)
    except json.JSONDecodeError as e:
        raise EvidenceError("not JSON-encoded: %r" % (s,)) from e
    if not isinstance(v, str):
        raise EvidenceError("decoded value must be a string: %r" % (s,))
    return v


def decode_remap(values):
    """Return {source_value: target_value}; fail on malformed or ambiguous maps."""
    if not isinstance(values, dict):
        raise EvidenceError("values must be an object")
    mapping = {}
    for enc_target, enc_sources in values.items():
        target = _unquote(enc_target)
        if not isinstance(enc_sources, list):
            raise EvidenceError("sources must be a list")
        for enc_src in enc_sources:
            src = _unquote(enc_src)
            if src in mapping and mapping[src] != target:
                raise EvidenceError("ambiguous source %r -> %r / %r" % (src, mapping[src], target))
            mapping[src] = target
    return mapping


def apply_remap(mapping, rows):
    return [None if r is None else mapping.get(r, r) for r in rows]


def _node(path):
    parts = path.split(".")[1:]  # drop "$"
    # path: nodes.<id>.loomContainer.nodes.<id>
    node = json.loads(TFL.read_text(encoding="utf-8"))
    cur = node
    for p in parts:
        cur = cur[p]
    return cur


class TestRemapEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_provenance_and_license(self):
        p = self.fx["provenance"]
        self.assertEqual(p["license"], "CC-BY-4.0")
        self.assertIn("Alexander Loth", p["attribution"])
        self.assertIn("Wiley", p["attribution"])
        self.assertEqual(p["source_file"], "examples/real_world/tableau_prep_book.tfl")
        self.assertRegex(p["upstream_commit"], r"^[0-9a-f]{40}$")
        self.assertIn(p["upstream_commit"], p["source_url"])
        self.assertIn(p["upstream_commit"], p["license_url"])
        self.assertEqual(p["upstream_member"], "flow")
        self.assertTrue(p["transformation_note"])
        self.assertIn("none", p["runtime_claim"])
        self.assertTrue(self.fx["generic_multigroup_remap"]["altered"])
        self.assertFalse(self.fx["discounts_remap"]["altered"])

    def test_discounts_payload_matches_producer_node(self):
        ev = self.fx["discounts_remap"]
        node = _node(ev["source_node_path"])
        for key, val in ev["payload"].items():
            self.assertEqual(node[key], val, key)
        self.assertEqual(node["nodeType"], ".v2018_3_3.Remap")

    def test_second_node_shape_matches_producer_node(self):
        ev = self.fx["generic_multigroup_remap"]
        node = _node(ev["source_node_path"])
        shape = ev["source_shape"]
        self.assertEqual(node["nodeType"], shape["nodeType"])
        self.assertEqual(node["groupMethodProps"], shape["groupMethodProps"])
        self.assertEqual([len(v) for v in node["values"].values()], shape["group_sizes"])
        self.assertEqual(
            all(k in v for k, v in node["values"].items()),
            shape["target_listed_in_own_sources"])
        # Sanity: the generic payload is not raw source data.
        self.assertNotEqual(node["values"], ev["payload"]["values"])
        self.assertNotEqual(node["columnName"], ev["payload"]["columnName"])
        # Generic payload keeps the same properties as the real shape.
        self.assertEqual(ev["payload"]["groupMethodProps"], shape["groupMethodProps"])

    def _check_scenario(self, key):
        ev = self.fx[key]
        sc = ev["scenario"]
        mapping = decode_remap(ev["payload"]["values"])
        self.assertEqual(sc["input_type"], "string")
        self.assertEqual(sc["expected_type"], "string")
        self.assertEqual(apply_remap(mapping, sc["input_rows"]), sc["expected_rows"])

    def test_discounts_scenario(self):
        self._check_scenario("discounts_remap")
        m = decode_remap(self.fx["discounts_remap"]["payload"]["values"])
        self.assertEqual(m, {"0": "0", "None": "0"})

    def test_generic_scenario(self):
        self._check_scenario("generic_multigroup_remap")

    def test_unmatched_and_null_preserved(self):
        for key in ("discounts_remap", "generic_multigroup_remap"):
            sc = self.fx[key]["scenario"]
            m = decode_remap(self.fx[key]["payload"]["values"])
            out = apply_remap(m, sc["input_rows"])
            self.assertIn(None, sc["input_rows"])
            self.assertEqual(out[sc["input_rows"].index(None)], None)
            self.assertEqual(len(out), len(sc["input_rows"]))
        self.assertEqual(apply_remap({"a": "b"}, ["zzz"]), ["zzz"])

    def test_decoder_rejects_malformed(self):
        bad = [
            [],
            {"\"a\"": "\"b\""},
            {"a": ["\"b\""]},
            {"\"a\"": ["b"]},
            {"\"a\"": [1]},
            {"\"a\"": ["[1]"]},
            {"1": ["\"x\""]},
        ]
        for values in bad:
            with self.subTest(values=values):
                with self.assertRaises(EvidenceError):
                    decode_remap(values)

    def test_decoder_rejects_ambiguous_duplicates(self):
        with self.assertRaises(EvidenceError):
            decode_remap({"\"A\"": ["\"x\""], "\"B\"": ["\"x\""]})

    def test_decoder_allows_idempotent_duplicate(self):
        self.assertEqual(decode_remap({"\"A\"": ["\"x\"", "\"x\""]}), {"x": "A"})


if __name__ == "__main__":
    unittest.main()
