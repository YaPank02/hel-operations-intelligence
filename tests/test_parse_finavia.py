"""Synthetic parser fixtures; no real Finavia data bundled."""
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "parse_finavia.py"
SPEC = importlib.util.spec_from_file_location("parse_finavia", SCRIPT)
parser = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parser)


def fixture(record, namespace=True):
    namespace_attr = ' xmlns="http://www.finavia.fi/FlightsService.xsd"' if namespace else ""
    return f'<flights{namespace_attr}><dep><header><timestamp>2026-11-03T13:37:00Z</timestamp></header><body>{record}</body></dep></flights>'


class ParserChecks(unittest.TestCase):
    def parse(self, xml):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"test.xml"
            path.write_text(xml, encoding="utf-8")
            return parser.parse_snapshot(path)

    def test_codeshares_do_not_create_extra_rows_and_fields_are_preserved(self):
        record="<flight><h_apt>HEL</h_apt><fltnr>TEST001</fltnr><sdt>2026-11-03T03:45:00Z</sdt><act_d>2026-11-03T04:01:00Z</act_d><ablk_d>2026-11-03T03:52:05Z</ablk_d><cflight_1>SHARE1</cflight_1><cflight_2>SHARE2</cflight_2><future_field>new</future_field></flight>"
        rows, report=self.parse(fixture(record))
        self.assertEqual(len(rows),1)
        self.assertEqual(json.loads(rows[0]["codeshares_json"]),["SHARE1","SHARE2"])
        self.assertEqual(rows[0]["source_future_field"],"new")
        self.assertEqual(rows[0]["act_d_helsinki"],"2026-11-03T06:01:00+02:00")
        self.assertEqual(rows[0]["ablk_d_helsinki"],"2026-11-03T05:52:05+02:00")
        self.assertEqual(report["rows_with_quality_flags"],0)
        self.assertNotIn("otp15",report)

    def test_missing_actual_times_are_not_automatically_bad_data(self):
        rows, report=self.parse(fixture("<flight><h_apt>HEL</h_apt><fltnr>TEST002</fltnr><sdt>2026-11-04T04:00:00Z</sdt><act_d/><ablk_d/></flight>",False))
        self.assertEqual(rows[0]["quality_flags"],"")
        self.assertEqual(rows[0]["act_d_utc"],"")
        self.assertEqual(report["total_records"],1)

    def test_bad_or_timezone_free_values_are_flagged_and_raw_retained(self):
        for value in ("bad-date","2026-11-03T10:00:00"):
            rows, report=self.parse(fixture(f"<flight><h_apt>HEL</h_apt><fltnr>X</fltnr><sdt>{value}</sdt></flight>"))
            self.assertIn("invalid_sdt",rows[0]["quality_flags"])
            self.assertEqual(rows[0]["source_sdt"],value)
            self.assertEqual(rows[0]["sdt_utc"],"")
            self.assertEqual(report["rows_with_quality_flags"],1)

    def test_midnight_and_summer_offsets(self):
        for source, expected in (
            ("2026-11-03T23:30:00Z","2026-11-04T01:30:00+02:00"),
            ("2026-07-03T23:30:00Z","2026-07-04T02:30:00+03:00"),
        ):
            rows,_=self.parse(fixture(f"<flight><h_apt>HEL</h_apt><fltnr>X</fltnr><sdt>{source}</sdt></flight>"))
            self.assertEqual(rows[0]["sdt_helsinki"],expected)

    def test_broken_structure_and_duplicate_fields_are_rejected(self):
        for xml in ("not xml","<wrong/>","<flights/>","<flights><dep/></flights>",
                    fixture("<flight><sdt>a</sdt><sdt>b</sdt></flight>"),
                    fixture("<flight><nested><value>x</value></nested></flight>")):
            with self.subTest(xml=xml), self.assertRaises(parser.ParseError):
                self.parse(xml)

    def test_duplicate_records_are_preserved_for_later_identity_design(self):
        record="<flight><h_apt>HEL</h_apt><fltnr>X</fltnr><sdt>2026-11-03T00:00:00Z</sdt></flight>"
        rows, report=self.parse(fixture(record*2))
        self.assertEqual(report["total_records"],2)
        self.assertEqual([row["source_row_number"] for row in rows],[1,2])

    def test_output_roundtrip_preserves_unicode_and_original_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            source=root/"test.xml"
            raw=fixture("<flight><h_apt>HEL</h_apt><fltnr>TEST</fltnr><sdt>2026-11-03T03:45:00Z</sdt><route_n_1>Kööpenhamina</route_n_1></flight>").encode()
            source.write_bytes(raw)
            output, profile, report=parser.write_outputs(source,root/"derived")
            with output.open(encoding="utf-8-sig",newline="") as handle:
                rows=list(csv.DictReader(handle))
            self.assertEqual(rows[0]["source_route_n_1"],"Kööpenhamina")
            self.assertEqual(source.read_bytes(),raw)
            self.assertEqual(json.loads(profile.read_text()),report)


if __name__=="__main__":
    unittest.main()
