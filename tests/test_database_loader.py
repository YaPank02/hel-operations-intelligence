"""Opt-in database tests; use an empty disposable test database only."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/"scripts"
sys.path.insert(0,str(SCRIPTS))
import load_postgres as loader
import psycopg

DSN=os.getenv("HEL_TEST_DSN")


def xml(generation="2099-01-01T10:00:00Z", flight="TEST1", status="BOR", dep_empty=False):
    record=f"""<flight><h_apt>HEL</h_apt><fltnr>{flight}</fltnr>
    <sdt>2099-01-01T11:00:00Z</sdt><sdate>20990101</sdate>
    <act_d>2099-01-01T11:20:00Z</act_d><ablk_d>2099-01-01T11:05:00Z</ablk_d>
    <prm>{status}</prm><prt>Fixture status</prt><cflight_1>SHARE1</cflight_1></flight>"""
    return f"""<flights xmlns="http://www.finavia.fi/FlightsService.xsd">
    <dep><header><timestamp>{generation}</timestamp></header><body>{'' if dep_empty else record}</body></dep>
    <arr><header><timestamp>{generation}</timestamp></header><body>{record}</body></arr></flights>"""


class SettingsChecks(unittest.TestCase):
    def test_password_is_not_in_repr_and_prompt_is_used(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(loader,"getpass",return_value="a#b'c"):
            settings=loader.DatabaseSettings.from_environment()
        self.assertNotIn("a#b'c",repr(settings))
        self.assertEqual(settings.dbname,"hel_operations")
        self.assertEqual(settings.password,"a#b'c")

    def test_invalid_port_is_rejected_before_password_prompt(self):
        with patch.dict(os.environ,{"HEL_DB_PORT":"invalid"},clear=True), patch.object(loader,"getpass") as prompt:
            with self.assertRaises(loader.LoadError):
                loader.DatabaseSettings.from_environment()
        prompt.assert_not_called()


@unittest.skipUnless(DSN,"Set HEL_TEST_DSN to an empty disposable test database.")
class DatabaseChecks(unittest.TestCase):
    def setUp(self):
        self.conn=psycopg.connect(DSN,autocommit=True)
        if self.conn.execute("SELECT to_regnamespace('hel')").fetchone()[0] is not None:
            self.conn.close()
            raise RuntimeError("Test database already contains hel; use a disposable empty database.")
        self.tx=self.conn.transaction(force_rollback=True)
        self.tx.__enter__()
        loader.initialise_schema(self.conn)
        self.temp=tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tx.__exit__(None,None,None)
        self.conn.close()
        self.temp.cleanup()

    def sample(self,name="test.xml",**kwargs):
        path=Path(self.temp.name)/name
        path.write_text(xml(**kwargs),encoding="utf-8")
        return path

    def count(self,table):
        # table is selected only from literal constants below.
        return self.conn.execute("SELECT COUNT(*) FROM hel."+table).fetchone()[0]

    def test_same_snapshot_is_loaded_once_and_schema_reapplies(self):
        path=self.sample()
        first=loader.load_snapshot(self.conn,path,initialise=True)
        again=loader.load_snapshot(self.conn,path,initialise=True)
        self.assertEqual(first["inserted_records"],2)
        self.assertEqual(again["status"],"already_loaded")
        self.assertEqual(self.count("snapshot"),1)
        self.assertEqual(self.count("flight_observation"),2)
        self.assertEqual(self.count("v_latest_flights"),2)

    def test_newer_source_wins_even_when_old_file_is_loaded_last(self):
        new=self.sample("new.xml",generation="2099-01-01T12:00:00Z",status="DEP")
        old=self.sample("old.xml",generation="2099-01-01T10:00:00Z",status="BOR")
        loader.load_snapshot(self.conn,new)
        loader.load_snapshot(self.conn,old)
        self.assertEqual(self.count("flight_observation"),4)
        self.assertEqual(self.conn.execute("SELECT DISTINCT status_code FROM hel.v_latest_flights").fetchall(),[("DEP",)])

    def test_zero_row_direction_does_not_reuse_previous_rows(self):
        loader.load_snapshot(self.conn,self.sample("old.xml"))
        loader.load_snapshot(self.conn,self.sample("new.xml",generation="2099-01-01T12:00:00Z",dep_empty=True))
        self.assertEqual(self.conn.execute("SELECT direction FROM hel.v_latest_flights").fetchall(),[("arr",)])
        self.assertEqual(self.count("snapshot_section"),4)

    def test_failed_insert_rolls_back_the_entire_new_snapshot(self):
        path=self.sample()
        rows,profile=loader.prepare_snapshot(path)
        rows[-1]["source_row_number"]=0
        with patch.object(loader,"prepare_snapshot",return_value=(rows,profile)):
            with self.assertRaises(psycopg.errors.CheckViolation):
                loader.load_snapshot(self.conn,path)
        self.assertEqual(self.count("snapshot"),0)
        self.assertEqual(self.count("snapshot_section"),0)
        self.assertEqual(self.count("flight_observation"),0)

    def test_actual_block_codeshares_and_original_fields_are_distinct(self):
        loader.load_snapshot(self.conn,self.sample())
        actual,block,codeshares,raw=self.conn.execute(
            "SELECT actual_event_at,block_event_at,codeshares,source_fields FROM hel.v_latest_flights WHERE direction='dep'"
        ).fetchone()
        self.assertNotEqual(actual,block)
        self.assertEqual(codeshares,["SHARE1"])
        self.assertEqual(raw["fltnr"],"TEST1")
        self.assertNotIn("sha256",raw)
        self.assertEqual(self.conn.execute("SELECT SUM(record_count) FROM hel.v_traffic_hourly").fetchone()[0],2)

    def test_uploaded_snapshot_if_available(self):
        name="finavia_HEL_all_20261103T133813_618605Z_5c48cd02.xml"
        candidates=[loader.ROOT/"data"/"raw"/name,loader.ROOT.parent/"upload"/name]
        source=next((p for p in candidates if p.exists()),None)
        if source is None:
            self.skipTest("Original uploaded XML is not present in this test checkout.")
        result=loader.load_snapshot(self.conn,source)
        self.assertEqual(result["inserted_records"],625)
        self.assertEqual(self.conn.execute(
            "SELECT direction,COUNT(*) FROM hel.v_latest_flights GROUP BY direction ORDER BY direction"
        ).fetchall(),[("arr",318),("dep",307)])
        self.assertEqual(self.conn.execute(
            "SELECT SUM(stored_record_count) FROM hel.v_snapshot_quality"
        ).fetchone()[0],625)
        self.assertEqual(loader.load_snapshot(self.conn,source)["inserted_records"],0)


if __name__=="__main__":
    unittest.main()
