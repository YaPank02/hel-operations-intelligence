"""Export checks against an empty disposable database (HEL_TEST_DSN)."""
import csv
from collections import Counter
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import export_excel as exporter
import load_postgres as loader
import psycopg
from test_database_loader import xml

DSN = os.getenv("HEL_TEST_DSN")


@unittest.skipUnless(DSN, "Set HEL_TEST_DSN to an empty disposable test database.")
class ExcelExportChecks(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(DSN, autocommit=True)
        if self.conn.execute("SELECT to_regnamespace('hel')").fetchone()[0] is not None:
            self.conn.close()
            raise RuntimeError("Database already contains hel; use an empty disposable database.")
        loader.initialise_schema(self.conn)
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.output = self.folder / "output"

    def tearDown(self):
        self.conn.execute("DROP SCHEMA hel CASCADE")
        self.conn.close()
        self.temp.cleanup()

    def load(self, name="fixture.xml", **kwargs):
        path = self.folder / name
        path.write_text(xml(**kwargs), encoding="utf-8")
        loader.load_snapshot(self.conn, path)

    def rows(self, name):
        with (self.output / name).open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def test_real_snapshot_details_match_independent_sql_controls(self):
        source = loader.ROOT.parent / "upload" / "finavia_HEL_all_20261103T133813_618605Z_5c48cd02.xml"
        if not source.exists():
            self.skipTest("Original uploaded snapshot is not available.")
        loader.load_snapshot(self.conn, source)
        result = exporter.export_bundle(self.conn, self.output)
        self.assertEqual(exporter.verify_bundle(self.output), result)
        flights = self.rows("flights_current.csv")
        self.assertEqual(len(flights), 625)
        counts = Counter((r["scheduled_date_helsinki"], r["direction"]) for r in flights)
        for control in self.rows("sql_control_totals.csv"):
            key = (control["scheduled_date_helsinki"], control["direction"])
            detail = [r for r in flights if (r["scheduled_date_helsinki"], r["direction"]) == key]
            self.assertEqual(int(control["flight_records"]), counts[key])
            self.assertEqual(int(control["flagged_records"]), sum(int(r["quality_issue_count"]) > 0 for r in detail))
            self.assertEqual(int(control["records_without_status"]), sum(not r["status_text"].strip() for r in detail))
        self.assertEqual({r["export_id"] for r in flights}, {result["export_id"]})
        self.assertEqual(len(self.rows("source_freshness.csv")), 2)

    def test_empty_newer_direction_is_in_freshness_without_old_flights(self):
        self.load("old.xml")
        self.load("new.xml", generation="2099-01-01T12:00:00Z", dep_empty=True)
        exporter.export_bundle(self.conn, self.output)
        self.assertEqual([r["direction"] for r in self.rows("flights_current.csv")], ["arr"])
        freshness = {r["direction"]: r for r in self.rows("source_freshness.csv")}
        self.assertEqual(freshness["dep"]["source_records"], "0")
        self.assertEqual(freshness["dep"]["source_updated_helsinki"], "2099-01-01T14:00:00")

    def test_failed_sql_keeps_previous_completed_export(self):
        self.load()
        exporter.export_bundle(self.conn, self.output)
        before = {p.name: p.read_bytes() for p in self.output.iterdir() if p.is_file()}
        fake_root = self.folder / "broken"
        (fake_root / "sql").mkdir(parents=True)
        for _, name in exporter.EXPORTS:
            original = (exporter.ROOT / "sql" / name).read_text()
            (fake_root / "sql" / name).write_text(original)
        (fake_root / "sql" / "005_excel_controls.sql").write_text("SELECT * FROM missing_export_table")
        with patch.object(exporter, "ROOT", fake_root), self.assertRaises(psycopg.Error):
            exporter.export_bundle(self.conn, self.output)
        after = {p.name: p.read_bytes() for p in self.output.iterdir() if p.is_file()}
        self.assertEqual(before, after)
        exporter.verify_bundle(self.output)

    def test_mixed_exports_are_detected_and_repeat_export_replaces_files(self):
        self.load()
        first = exporter.export_bundle(self.conn, self.output)
        self.load("new.xml", generation="2099-01-01T12:00:00Z", flight="NEW1")
        second = exporter.export_bundle(self.conn, self.output)
        self.assertNotEqual(first["export_id"], second["export_id"])
        self.assertEqual({r["flight_number"] for r in self.rows("flights_current.csv")}, {"NEW1"})
        path = self.output / "flights_current.csv"
        path.write_bytes(path.read_bytes() + b"unexpected")
        with self.assertRaises(exporter.ExportError):
            exporter.verify_bundle(self.output)

    def test_missing_times_and_blank_status_remain_visible(self):
        path = self.folder / "quality.xml"
        content = xml(flight="=TEST", status="").replace(
            "<sdt>2099-01-01T11:00:00Z</sdt>", "<sdt></sdt>"
        ).replace("Fixture status", "   ")
        path.write_text(content, encoding="utf-8")
        loader.load_snapshot(self.conn, path)
        exporter.export_bundle(self.conn, self.output)
        flights = self.rows("flights_current.csv")
        self.assertTrue(all(r["scheduled_date_helsinki"] == "" for r in flights))
        self.assertTrue(all(r["flight_number"] == "'=TEST" for r in flights))
        self.assertTrue(all("missing_sdt" in r["quality_flags"] for r in flights))
        for control in self.rows("sql_control_totals.csv"):
            self.assertEqual(control["flagged_records"], "1")
            self.assertEqual(control["records_without_status"], "1")


if __name__ == "__main__":
    unittest.main()
