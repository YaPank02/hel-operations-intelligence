"""Offline checks. XML below is synthetic, not Finavia flight evidence."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_finavia.py"
SPEC = importlib.util.spec_from_file_location("fetch_finavia", SCRIPT)
collector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = collector
SPEC.loader.exec_module(collector)


def response(status=200, content=b"<flights><dep/><arr/></flights>"):
    result = collector.requests.Response()
    result.status_code = status
    result._content = content
    result.headers["Content-Type"] = "application/xml"
    return result


class CollectorChecks(unittest.TestCase):
    def setUp(self):
        self.settings = collector.Settings("synthetic-key-for-tests")

    @patch.object(collector.requests, "get")
    def test_preserves_bytes_and_keeps_key_out_of_metadata(self, get):
        payload = b'<?xml version="1.0"?><flights><dep/><arr/></flights>'
        get.return_value = response(content=payload)
        with tempfile.TemporaryDirectory() as temp:
            xml_path, meta_path, metadata = collector.collect(
                self.settings, Path(temp)
            )
            self.assertEqual(xml_path.read_bytes(), payload)
            metadata_text = meta_path.read_text(encoding="utf-8")
            self.assertNotIn(self.settings.api_key, metadata_text)
            self.assertEqual(json.loads(metadata_text), metadata)
            self.assertEqual(metadata["top_level_element_counts"], {"arr": 1, "dep": 1})
        args, kwargs = get.call_args
        self.assertEqual(args[0], collector.BASE_URL + "/all/HEL")
        self.assertEqual(kwargs["headers"]["app_key"], self.settings.api_key)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], (10, 30))

    @patch.object(collector.requests, "get")
    def test_namespaced_xml_and_unique_capture_names(self, get):
        get.return_value = response(
            content=b'<flights xmlns="urn:synthetic"><dep/></flights>'
        )
        with tempfile.TemporaryDirectory() as temp:
            first = collector.collect(self.settings, Path(temp))
            second = collector.collect(self.settings, Path(temp))
            self.assertEqual(first[2]["xml_root"], "flights")
            self.assertEqual(first[2]["top_level_element_counts"], {"dep": 1})
            self.assertNotEqual(first[0], second[0])
            self.assertEqual(len(list(Path(temp).glob("*.xml"))), 2)

    @patch.object(collector.requests, "get")
    def test_auth_rate_limit_redirect_and_server_errors_save_nothing(self, get):
        for status in (401, 403, 404, 429, 302, 500):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                get.return_value = response(status=status)
                with self.assertRaisesRegex(collector.CollectorError, f"HTTP {status}"):
                    collector.collect(self.settings, Path(temp))
                self.assertEqual(list(Path(temp).iterdir()), [])

    @patch.object(collector.requests, "get")
    def test_invalid_xml_or_wrong_root_save_nothing(self, get):
        for payload in (b"not xml", b"<flights>", b"<error>denied</error>"):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temp:
                get.return_value = response(content=payload)
                with self.assertRaises(collector.CollectorError):
                    collector.collect(self.settings, Path(temp))
                self.assertEqual(list(Path(temp).iterdir()), [])

    @patch.object(collector.requests, "get")
    def test_empty_flights_is_retained_without_claiming_a_flight_count(self, get):
        get.return_value = response(content=b"<flights/>")
        with tempfile.TemporaryDirectory() as temp:
            _, _, metadata = collector.collect(self.settings, Path(temp))
            self.assertEqual(metadata["top_level_element_counts"], {})
            self.assertNotIn("flight_count", metadata)

    @patch.object(collector.requests, "get")
    def test_timeout_does_not_expose_exception_or_key(self, get):
        get.side_effect = collector.requests.exceptions.Timeout(
            "diagnostic containing " + self.settings.api_key
        )
        with self.assertRaises(collector.CollectorError) as error:
            collector.fetch_xml(self.settings)
        self.assertNotIn(self.settings.api_key, str(error.exception))

    def test_missing_or_placeholder_key_is_rejected(self):
        for value in ("", "PASTE_YOUR_KEY_HERE"):
            with self.subTest(value=value), patch.dict(
                os.environ, {"FINAVIA_API_KEY": value}, clear=True
            ):
                with self.assertRaises(collector.CollectorError):
                    collector.Settings.from_environment()

    def test_selector_validation_and_normalisation(self):
        with patch.dict(
            os.environ,
            {"FINAVIA_API_KEY": "synthetic", "FINAVIA_FLIGHT_TYPE": "DEP",
             "FINAVIA_AIRPORT": "hel"},
            clear=True,
        ):
            settings = collector.Settings.from_environment()
            self.assertEqual(settings.url, collector.BASE_URL + "/dep/HEL")
            os.environ["FINAVIA_AIRPORT"] = "../HEL"
            with self.assertRaises(collector.CollectorError):
                collector.Settings.from_environment()

    @patch.object(collector, "load_dotenv")
    @patch.object(collector.requests, "get")
    def test_main_missing_key_exits_before_any_request(self, get, load):
        with patch.dict(os.environ, {}, clear=True), patch(
            "sys.stderr", new_callable=io.StringIO
        ) as errors:
            self.assertEqual(collector.main(), 1)
            self.assertIn("API key missing", errors.getvalue())
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
