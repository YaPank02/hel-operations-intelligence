"""Capture one Finavia XML response; do not assume its flight-record schema."""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import xml.etree.ElementTree as ET

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://apigw.finavia.fi/flights/public/v0/flights"


class CollectorError(Exception):
    """A concise, user-actionable error that does not expose credentials."""


@dataclass(frozen=True)
class Settings:
    api_key: str
    flight_type: str = "all"
    airport: str = "HEL"

    @classmethod
    def from_environment(cls) -> "Settings":
        key = os.getenv("FINAVIA_API_KEY", "").strip()
        if not key or key == "PASTE_YOUR_KEY_HERE":
            raise CollectorError(
                "API key missing. Copy .env.example to .env and enter your "
                "Finavia key locally."
            )
        flight_type = os.getenv("FINAVIA_FLIGHT_TYPE", "all").strip().lower()
        airport = os.getenv("FINAVIA_AIRPORT", "HEL").strip().upper()
        if flight_type not in {"all", "dep", "arr"}:
            raise CollectorError("FINAVIA_FLIGHT_TYPE must be all, dep or arr.")
        if re.fullmatch(r"[A-Z]{3}", airport) is None:
            raise CollectorError("FINAVIA_AIRPORT must be a three-letter IATA code.")
        return cls(key, flight_type, airport)

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.flight_type}/{self.airport}"


def local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def fetch_xml(settings: Settings) -> tuple[bytes, ET.Element, str]:
    """Perform exactly one request and validate XML before saving anything."""
    try:
        response = requests.get(
            settings.url,
            headers={
                "app_key": settings.api_key,
                "Accept": "application/xml",
                "Cache-Control": "no-cache",
            },
            timeout=(10, 30),
            allow_redirects=False,
        )
    except requests.exceptions.Timeout:
        raise CollectorError("Request timed out. Wait before trying again.") from None
    except requests.exceptions.RequestException:
        raise CollectorError(
            "Network request failed. Check your internet connection and endpoint."
        ) from None

    status = response.status_code
    if status != 200:
        advice = {
            401: "Check your subscription key and access in the Finavia portal.",
            403: "Check your API subscription permissions in the Finavia portal.",
            404: "Compare the endpoint and airport code with the portal.",
            429: "The request limit was reached. Wait before trying again.",
        }.get(status, "Check the response status in the Finavia portal.")
        if 300 <= status < 400:
            advice = (
                "The endpoint returned a redirect. It was not followed; "
                "verify the HTTPS endpoint in the portal."
            )
        raise CollectorError(f"HTTP {status}. {advice}")

    payload = response.content
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        raise CollectorError(
            "HTTP 200, but the body is not valid XML. Inspect it in the portal."
        ) from None
    if local_tag(root.tag).lower() != "flights":
        raise CollectorError(
            "HTTP 200, but the XML root is not flights. Check the response "
            "schema in the portal before changing the collector."
        )
    return payload, root, response.headers.get("Content-Type", "")


def collect(settings: Settings, output_dir: Path) -> tuple[Path, Path, dict]:
    payload, root, content_type = fetch_xml(settings)
    collected_at = datetime.now(timezone.utc)
    snapshot_id = (
        collected_at.strftime("%Y%m%dT%H%M%S_%fZ") + "_" + uuid4().hex[:8]
    )
    stem = f"finavia_{settings.airport}_{settings.flight_type}_{snapshot_id}"
    xml_path = output_dir / f"{stem}.xml"
    metadata_path = output_dir / f"{stem}.metadata.json"
    sections = dict(sorted(Counter(local_tag(child.tag) for child in root).items()))
    metadata = {
        "snapshot_id": snapshot_id,
        "collected_at_utc": collected_at.isoformat(),
        "source_url": settings.url,
        "airport": settings.airport,
        "flight_type": settings.flight_type,
        "http_status": 200,
        "content_type": content_type,
        "response_bytes": len(payload),
        "xml_root": local_tag(root.tag),
        "top_level_element_counts": sections,
        "note": "XML element counts are structural, not verified flight counts.",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    xml_path.write_bytes(payload)
    metadata_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return xml_path, metadata_path, metadata


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    try:
        settings = Settings.from_environment()
        print(f"Requesting {settings.flight_type} flights for {settings.airport}.")
        xml_path, metadata_path, metadata = collect(
            settings, PROJECT_ROOT / "data" / "raw"
        )
    except CollectorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print(
            "ERROR: Could not save the response. Check folder permissions and space.",
            file=sys.stderr,
        )
        return 1
    print("HTTP 200; valid flights XML saved.")
    print(f"XML: {xml_path}")
    print(f"Metadata: {metadata_path}")
    print(f"Top-level XML sections: {metadata['top_level_element_counts']}")
    print("Next: inspect the schema before treating any elements as flight records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
