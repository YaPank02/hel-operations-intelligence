"""Flatten one saved snapshot without merging movements or calculating OTP."""
from __future__ import annotations
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
TIME_FIELDS = ("sdt", "est_d", "pest_d", "act_d", "ablk_d")


class ParseError(ValueError):
    pass


def tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def child(element: ET.Element, name: str):
    return next((item for item in element if tag(item) == name), None)


def text(element, name: str) -> str:
    if element is None:
        return ""
    item = child(element, name)
    return "" if item is None else (item.text or "").strip()


def parse_time(value: str):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ParseError("Invalid timestamp") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ParseError("Timestamp has no timezone")
    return result.astimezone(timezone.utc)


def parse_snapshot(path: Path):
    try:
        helsinki = ZoneInfo("Europe/Helsinki")
    except ZoneInfoNotFoundError:
        raise ParseError("Timezone data missing. Install requirements-parser.txt.") from None
    content = path.read_bytes()
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        raise ParseError("Input is not valid XML.") from None
    if tag(root) != "flights":
        raise ParseError("Expected XML root flights.")
    digest = hashlib.sha256(content).hexdigest()
    rows, section_report = [], {}
    for section in root:
        direction = tag(section)
        if direction not in {"arr", "dep"}:
            raise ParseError("Unexpected top-level section: " + direction)
        if direction in section_report:
            raise ParseError("Repeated direction section: " + direction)
        body = child(section, "body")
        if body is None:
            raise ParseError("Missing body in section " + direction)
        source_time = text(child(section, "header"), "timestamp")
        flights = list(body)
        section_report[direction] = {"records": len(flights), "source_timestamp_raw": source_time}
        for number, flight in enumerate(flights, 1):
            if tag(flight) != "flight":
                raise ParseError("Unexpected record in body: " + tag(flight))
            fields = {}
            for item in flight:
                name = tag(item)
                if name in fields or len(item):
                    raise ParseError("Duplicate or nested flight field: " + name)
                fields[name] = (item.text or "").strip()
            # Prefix source columns so schema evolution cannot overwrite provenance.
            row = {"source_file": path.name, "source_sha256": digest,
                   "direction": direction, "source_row_number": number,
                   "source_generated_at_raw": source_time}
            row.update({"source_" + key: value for key, value in fields.items()})
            flags = []
            for key in ("h_apt", "fltnr", "sdt"):
                if not fields.get(key):
                    flags.append("missing_" + key)
            for key in TIME_FIELDS:
                value = fields.get(key, "")
                try:
                    parsed = parse_time(value)
                except ParseError:
                    parsed = None
                    flags.append("invalid_" + key)
                row[key + "_utc"] = parsed.isoformat() if parsed else ""
                row[key + "_helsinki"] = parsed.astimezone(helsinki).isoformat() if parsed else ""
                if key == "sdt":
                    row["scheduled_date_helsinki"] = (
                        parsed.astimezone(helsinki).date().isoformat() if parsed else ""
                    )
            # Codeshares stay within the same source record; never explode them.
            row["codeshares_json"] = json.dumps(
                [fields["cflight_" + str(i)] for i in range(1, 7)
                 if fields.get("cflight_" + str(i))], ensure_ascii=False
            )
            row["quality_flags"] = "|".join(flags)
            rows.append(row)
    if not section_report:
        raise ParseError("No arrivals or departures sections found.")
    dates = Counter(row["scheduled_date_helsinki"] for row in rows if row["scheduled_date_helsinki"])
    report = {
        "source_file": path.name, "source_sha256": digest,
        "source_bytes": len(content), "total_records": len(rows),
        "sections": section_report, "scheduled_dates_helsinki": dict(sorted(dates.items())),
        "rows_with_quality_flags": sum(bool(row["quality_flags"]) for row in rows),
        "records_with_codeshares": sum(row["codeshares_json"] != "[]" for row in rows),
        "statuses": {
            direction: dict(Counter(row.get("source_prm", "") or "(blank)"
                                    for row in rows if row["direction"] == direction))
            for direction in section_report
        },
        "nonempty_actual_fields": {
            direction: {
                key: sum(bool(row.get("source_" + key)) for row in rows
                         if row["direction"] == direction)
                for key in ("act_d", "ablk_d")
            } for direction in section_report
        },
        "note": "One row per source flight record, not a deduplicated history. No OTP calculated."
    }
    return rows, report


def write_outputs(source: Path, destination: Path):
    rows, report = parse_snapshot(source)
    leading = ["source_file", "source_sha256", "direction", "source_row_number",
               "source_generated_at_raw", "source_h_apt", "source_fltnr",
               "source_sdt", "sdt_utc", "sdt_helsinki", "scheduled_date_helsinki",
               "source_act_d", "act_d_utc", "act_d_helsinki",
               "source_ablk_d", "ablk_d_utc", "ablk_d_helsinki",
               "source_prm", "source_prt", "codeshares_json", "quality_flags"]
    columns = leading + sorted({key for row in rows for key in row} - set(leading))
    destination.mkdir(parents=True, exist_ok=True)
    csv_path = destination / (source.stem + ".flights.csv")
    report_path = destination / (source.stem + ".profile.json")
    # UTF-8 BOM helps Excel recognise accented airport names. Timestamp values
    # remain ISO strings with offsets; database/DAX types are a later step.
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return csv_path, report_path, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Saved XML file; default: latest HEL/all collector filename.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "processed")
    args = parser.parse_args()
    source = args.input
    if source is None:
        matches = sorted((ROOT / "data" / "raw").glob("finavia_HEL_all_*.xml"))
        if not matches:
            print("ERROR: No HEL/all XML found in data/raw. Run the collector first.", file=sys.stderr)
            return 1
        source = matches[-1]
    try:
        csv_path, profile_path, report = write_outputs(source, args.output_dir)
    except (OSError, ParseError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1
    counts = ", ".join(f"{key}: {value['records']}" for key, value in report["sections"].items())
    print(f"Parsed {report['total_records']} source flight records ({counts}).")
    print(f"Rows with quality flags: {report['rows_with_quality_flags']}")
    print(f"CSV: {csv_path}")
    print(f"Profile: {profile_path}")
    print("No records merged and no punctuality measures calculated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
