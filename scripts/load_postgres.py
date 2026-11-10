"""Load a saved HEL/all snapshot transactionally into local PostgreSQL."""
from __future__ import annotations
import argparse
from dataclasses import dataclass, field
from datetime import datetime
from getpass import getpass
import json
import os
from pathlib import Path
import sys

import psycopg
from psycopg.types.json import Jsonb
from dotenv import load_dotenv
from parse_finavia import ROOT, ParseError, parse_snapshot, parse_time

PROVENANCE = {"source_file", "source_sha256", "source_row_number", "source_generated_at_raw"}
INSERT_OBSERVATION = """
INSERT INTO hel.flight_observation (
 snapshot_id, direction, source_row_number, home_airport, flight_number,
 source_flight_date, scheduled_at, estimated_at, previous_estimated_at,
 actual_event_at, block_event_at, status_code, status_text,
 aircraft_registration, aircraft_type, route_1, route_2, route_3, route_4,
 gate, stand, codeshares, quality_flags, source_fields
) VALUES (
 %(snapshot_id)s, %(direction)s, %(source_row_number)s, %(home_airport)s,
 %(flight_number)s, %(source_flight_date)s, %(scheduled_at)s, %(estimated_at)s,
 %(previous_estimated_at)s, %(actual_event_at)s, %(block_event_at)s,
 %(status_code)s, %(status_text)s, %(aircraft_registration)s, %(aircraft_type)s,
 %(route_1)s, %(route_2)s, %(route_3)s, %(route_4)s, %(gate)s, %(stand)s,
 %(codeshares)s, %(quality_flags)s, %(source_fields)s)
"""


class LoadError(ValueError):
    pass


@dataclass(frozen=True)
class DatabaseSettings:
    host: str = "127.0.0.1"
    port: int = 5432
    dbname: str = "hel_operations"
    user: str = "postgres"
    password: str = field(default="", repr=False)

    @classmethod
    def from_environment(cls):
        try:
            port = int(os.getenv("HEL_DB_PORT", "5432"))
        except ValueError:
            raise LoadError("HEL_DB_PORT must be an integer.") from None
        if not 1 <= port <= 65535:
            raise LoadError("HEL_DB_PORT must be between 1 and 65535.")
        password = os.getenv("HEL_DB_PASSWORD", "")
        if not password:
            try:
                password = getpass("PostgreSQL password (hidden): ")
            except (EOFError, OSError):
                raise LoadError("Run in an interactive terminal to enter the database password.") from None
        if not password:
            raise LoadError("No database password supplied.")
        return cls(
            host=os.getenv("HEL_DB_HOST", "127.0.0.1"),
            port=port, dbname=os.getenv("HEL_DB_NAME", "hel_operations"),
            user=os.getenv("HEL_DB_USER", "postgres"), password=password,
        )

    def connect(self):
        # Keyword parameters safely handle spaces, quotes and # in passwords.
        return psycopg.connect(
            host=self.host, port=self.port, dbname=self.dbname, user=self.user,
            password=self.password, connect_timeout=10, autocommit=True,
            application_name="HEL Operations Intelligence",
        )


def initialise_schema(conn):
    for name in ("001_snapshot_tables.sql", "002_reporting_views.sql"):
        conn.execute((ROOT / "sql" / name).read_text(encoding="utf-8"), prepare=False)


def source_capture_time(source: Path):
    metadata_path = source.with_suffix(".metadata.json")
    if not metadata_path.exists():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        value = metadata.get("collected_at_utc")
        return parse_time(value) if value else None
    except (json.JSONDecodeError, AttributeError, TypeError, ParseError):
        raise LoadError("The snapshot metadata has invalid JSON or collection time.") from None


def prepare_snapshot(source: Path):
    rows, profile = parse_snapshot(source)
    if not rows:
        raise LoadError("Empty snapshot: refusing to replace the latest reporting set.")
    if set(profile["sections"]) != {"dep", "arr"}:
        raise LoadError("This loader expects a complete HEL/all snapshot with both sections.")
    if any(row.get("source_h_apt") not in {"HEL", "", None} for row in rows):
        raise LoadError("This first loader supports HEL snapshots only.")
    for section in profile["sections"].values():
        stamp = parse_time(section["source_timestamp_raw"])
        if stamp is None:
            raise LoadError("Source generation timestamps are required for latest reporting.")
        section["source_generated_at"] = stamp
    return rows, profile


def observation_values(row, snapshot_id):
    def value(name):
        return row.get("source_" + name) or None
    def timestamp(name):
        parsed = row.get(name + "_utc")
        return datetime.fromisoformat(parsed) if parsed else None
    original_fields = {
        key.removeprefix("source_"): value
        for key, value in row.items()
        if key.startswith("source_") and key not in PROVENANCE
    }
    result = {
        "snapshot_id": snapshot_id, "direction": row["direction"],
        "source_row_number": row["source_row_number"],
        "home_airport": value("h_apt"), "flight_number": value("fltnr"),
        "source_flight_date": value("sdate"),
        "scheduled_at": timestamp("sdt"), "estimated_at": timestamp("est_d"),
        "previous_estimated_at": timestamp("pest_d"),
        "actual_event_at": timestamp("act_d"), "block_event_at": timestamp("ablk_d"),
        "status_code": value("prm"), "status_text": value("prt"),
        "aircraft_registration": value("acreg"), "aircraft_type": value("actype"),
        "gate": value("gate"), "stand": value("park"),
        "codeshares": json.loads(row["codeshares_json"]),
        "quality_flags": row["quality_flags"].split("|") if row["quality_flags"] else [],
        "source_fields": Jsonb(original_fields),
    }
    result.update({"route_" + str(i): value("route_" + str(i)) for i in range(1, 5)})
    return result


def load_snapshot(conn, source: Path, initialise=False):
    rows, profile = prepare_snapshot(source)
    captured_at = source_capture_time(source)
    with conn.transaction():
        if initialise:
            initialise_schema(conn)
        inserted = conn.execute(
            """INSERT INTO hel.snapshot
              (source_sha256, source_file, source_bytes, airport_scope, request_scope,
               source_captured_at, expected_record_count)
              VALUES (%s, %s, %s, 'HEL', 'all', %s, %s)
              ON CONFLICT (source_sha256) DO NOTHING RETURNING snapshot_id""",
            (profile["source_sha256"], source.name, profile["source_bytes"],
             captured_at, len(rows)),
        ).fetchone()
        if inserted is None:
            snapshot_id = conn.execute(
                "SELECT snapshot_id FROM hel.snapshot WHERE source_sha256 = %s",
                (profile["source_sha256"],),
            ).fetchone()[0]
            return {"status": "already_loaded", "snapshot_id": snapshot_id,
                    "inserted_records": 0}
        snapshot_id = inserted[0]
        for direction, section in profile["sections"].items():
            conn.execute(
                """INSERT INTO hel.snapshot_section
                 (snapshot_id, direction, source_generated_at, expected_record_count)
                 VALUES (%s, %s, %s, %s)""",
                (snapshot_id, direction, section["source_generated_at"], section["records"]),
            )
        # One transaction covers every section and observation.
        with conn.cursor() as cursor:
            cursor.executemany(INSERT_OBSERVATION, [
                observation_values(row, snapshot_id) for row in rows
            ])
        stored = conn.execute(
            "SELECT COUNT(*) FROM hel.flight_observation WHERE snapshot_id = %s",
            (snapshot_id,),
        ).fetchone()[0]
        if stored != len(rows):
            raise LoadError("Stored row count does not match the parsed snapshot.")
    return {"status": "loaded", "snapshot_id": snapshot_id,
            "inserted_records": stored}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Default: latest saved HEL/all XML.")
    parser.add_argument("--init-db", action="store_true", help="Create the initial tables and views, then load.")
    args = parser.parse_args()
    source = args.input
    if source is None:
        matches = sorted((ROOT / "data" / "raw").glob("finavia_HEL_all_*.xml"))
        if not matches:
            print("ERROR: No saved HEL/all XML. Run the collector first.", file=sys.stderr)
            return 1
        source = matches[-1]
    load_dotenv(ROOT / ".env", override=False)
    try:
        # Check data before asking for a password or opening a DB connection.
        prepare_snapshot(source)
        settings = DatabaseSettings.from_environment()
        with settings.connect() as conn:
            if conn.execute("SELECT current_database()").fetchone()[0] != settings.dbname:
                raise LoadError("Connected database name differs from the configured name.")
            result = load_snapshot(conn, source, initialise=args.init_db)
            totals = conn.execute(
                """SELECT (SELECT COUNT(*) FROM hel.snapshot),
                          (SELECT COUNT(*) FROM hel.flight_observation),
                          (SELECT COUNT(*) FROM hel.v_latest_flights)"""
            ).fetchone()
    except psycopg.OperationalError:
        print("ERROR: Could not connect. Check PostgreSQL is running, database/user/port and password.", file=sys.stderr)
        return 1
    except psycopg.errors.UndefinedTable:
        print("ERROR: Tables missing. Run this script with --init-db first.", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        print(f"ERROR: Database operation failed (SQLSTATE {exc.sqlstate or 'unknown'}). The active transaction was rolled back.", file=sys.stderr)
        return 1
    except (LoadError, ParseError, OSError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1
    if result["status"] == "already_loaded":
        print(f"Snapshot {result['snapshot_id']} was already loaded; 0 records added.")
    else:
        print(f"Loaded snapshot {result['snapshot_id']}: {result['inserted_records']} flight observations.")
    print(f"Stored snapshots: {totals[0]}; historical observations: {totals[1]}; latest source records: {totals[2]}.")
    print("Next: inspect sql/003_verify_load.sql in pgAdmin.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
