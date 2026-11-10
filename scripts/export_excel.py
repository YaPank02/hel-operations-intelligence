"""Export consistent SQL data to fixed CSV filenames for Excel Power Query."""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

import psycopg
from dotenv import load_dotenv
from load_postgres import DatabaseSettings, LoadError, ROOT

EXPORTS = (
    ("flights_current.csv", "004_excel_flights.sql"),
    ("sql_control_totals.csv", "005_excel_controls.sql"),
    ("source_freshness.csv", "006_excel_freshness.sql"),
)
MANIFEST = "export_manifest.json"


class ExportError(ValueError):
    pass


def csv_value(value):
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    # Preserve source strings as text, including formula-like values in CSVs.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_bundle(conn, output_dir: Path):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    export_id = str(uuid4())
    metadata = {
        "schema_version": 1,
        "export_id": export_id,
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "display_timezone": "Europe/Helsinki",
        "record_grain": "one source flight row in each latest direction section",
        "files": {},
    }
    # Read every query under one database snapshot, even if ingestion runs now.
    # Stage all files before replacing any existing successful export.
    with tempfile.TemporaryDirectory(prefix=".export-", dir=output_dir) as temp:
        stage = Path(temp)
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '30s'")
            for filename, query_name in EXPORTS:
                if query_name == "004_excel_flights.sql":
                    query_name = conn.execute("""
                        SELECT CASE
                            WHEN EXISTS (
                                SELECT 1 FROM pg_attribute
                                WHERE attrelid = to_regclass('hel.v_latest_flights_weather')
                                    AND attname = 'weather_level' AND NOT attisdropped
                            ) THEN '010_excel_flights_attention.sql'
                            WHEN to_regclass('hel.v_latest_flights_weather') IS NOT NULL
                                THEN '009_excel_flights_weather.sql'
                            ELSE '004_excel_flights.sql'
                        END
                    """).fetchone()[0]
                query = (ROOT / "sql" / query_name).read_text(encoding="utf-8")
                cursor = conn.execute(query)
                columns = [column.name for column in cursor.description]
                rows = cursor.fetchall()
                if filename == "source_freshness.csv" and len(rows) != 2:
                    raise ExportError("Expected one source section for arrivals and departures.")
                path = stage / filename
                with path.open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(["export_id", *columns])
                    writer.writerows(
                        [export_id, *(csv_value(value) for value in row)]
                        for row in rows
                    )
                metadata["files"][filename] = {
                    "rows": len(rows),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
        if not metadata["files"]["flights_current.csv"]["rows"]:
            raise ExportError("No latest flight records; previous export has been retained.")
        (stage / MANIFEST).write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        # Each replacement is atomic. The manifest is the final completion marker.
        # If a Windows file lock interrupts publication, verify_bundle fails closed.
        for filename, _ in EXPORTS:
            os.replace(stage / filename, output_dir / filename)
        os.replace(stage / MANIFEST, output_dir / MANIFEST)
    return metadata


def verify_bundle(output_dir: Path):
    """Reject missing/mixed files; used by later briefing automation too."""
    output_dir = Path(output_dir)
    try:
        metadata = json.loads((output_dir / MANIFEST).read_text(encoding="utf-8"))
        if metadata["schema_version"] != 1 or not metadata["export_id"]:
            raise ExportError("Unknown or incomplete export manifest.")
        for filename, _ in EXPORTS:
            content = (output_dir / filename).read_bytes()
            if hashlib.sha256(content).hexdigest() != metadata["files"][filename]["sha256"]:
                raise ExportError("Export files do not match the completed manifest; rerun the export.")
    except (KeyError, TypeError, ValueError, OSError) as exc:
        if isinstance(exc, ExportError):
            raise
        raise ExportError("Export files or manifest are missing or invalid; rerun the export.") from None
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "processed" / "excel")
    parser.add_argument("--verify-only", action="store_true", help="Check files without a database connection.")
    args = parser.parse_args()
    try:
        if args.verify_only:
            metadata = verify_bundle(args.output_dir)
        else:
            load_dotenv(ROOT / ".env", override=False)
            settings = DatabaseSettings.from_environment()
            with settings.connect() as conn:
                metadata = export_bundle(conn, args.output_dir)
            verify_bundle(args.output_dir)
    except psycopg.OperationalError:
        print("ERROR: Could not connect. Check PostgreSQL, database/user/port and password.", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        print(f"ERROR: SQL export failed (SQLSTATE {exc.sqlstate or 'unknown'}).", file=sys.stderr)
        return 1
    except (ExportError, LoadError, OSError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1
    print("Verified export: " + metadata["export_id"])
    for filename, info in metadata["files"].items():
        print(f"{filename}: {info['rows']} rows")
    print("Folder: " + str(args.output_dir.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
