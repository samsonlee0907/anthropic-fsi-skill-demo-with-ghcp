"""Read-only ingress CORS validation; no Azure resources or model calls."""
import argparse
import contextlib
import io
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError

from scripts import validate


class CorsValidationTests(unittest.TestCase):
    def setUp(self):
        self.base = "https://api.example.test"
        self.origin = "https://portal.example.test"
        self.headers = {
            "Access-Control-Allow-Origin": self.origin,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-WebIQ-Key",
        }

    def run_check(self, *, status=200):
        with patch.object(validate.urllib.request, "urlopen") as opened, contextlib.redirect_stdout(io.StringIO()):
            opened.return_value.__enter__.return_value = SimpleNamespace(status=status, headers=self.headers)
            result = validate.validate_cors(self.base, self.origin)
        for call in opened.call_args_list:
            request = call.args[0]
            self.assertEqual(request.get_method(), "OPTIONS")
            self.assertIsNone(request.data)
            self.assertIsNone(request.get_header("X-webiq-key"))
            self.assertEqual(request.get_header("Origin"), self.origin)
        self.assertEqual(len(opened.call_args_list), 2)
        paths = [urllib.parse.urlsplit(call.args[0].full_url).path for call in opened.call_args_list]
        self.assertEqual(paths, ["/api/health", "/api/run"])
        return result

    def test_permitted_origin_methods_and_headers(self):
        self.assertTrue(self.run_check())

    def test_http_200_does_not_hide_missing_post_permission(self):
        self.headers["Access-Control-Allow-Methods"] = "GET,OPTIONS"
        self.assertFalse(self.run_check())

    def test_http_methods_are_case_sensitive(self):
        self.headers["Access-Control-Allow-Methods"] = "get,post,options"
        self.assertFalse(self.run_check())

    def test_missing_or_wrong_origin_and_headers_are_rejected(self):
        for header, value in (
            ("Access-Control-Allow-Origin", ""), ("Access-Control-Allow-Origin", "https://other.example.test"),
            ("Access-Control-Allow-Headers", "content-type"), ("Access-Control-Allow-Methods", ""),
        ):
            with self.subTest(header=header, value=value), patch.dict(self.headers, {header: value}):
                self.assertFalse(self.run_check())

    def test_wildcards_are_valid_for_noncredentialed_capability_header_requests(self):
        self.headers = {key: "*" for key in self.headers}
        self.assertTrue(self.run_check())
        self.assertFalse(self.run_check(status=400))

    def test_transport_failure_is_explicit_and_does_not_print_error_body(self):
        with patch.object(validate.urllib.request, "urlopen", side_effect=URLError("PRIVATE-CANARY")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(validate.validate_cors(self.base, self.origin))
        self.assertEqual(output.getvalue().count("[FAIL]"), 2)
        self.assertNotIn("PRIVATE-CANARY", output.getvalue())

    def test_preflight_only_never_invokes_scenarios(self):
        with patch("sys.argv", ["validate.py", "--api-base", self.base, "--portal-origin", self.origin,
                                "--preflight-only"]), \
                patch.object(validate, "validate_cors", return_value=True) as cors, \
                patch.object(validate, "validate") as scenario:
            with self.assertRaises(SystemExit) as exit_code:
                validate.main()
        self.assertEqual(exit_code.exception.code, 0)
        cors.assert_called_once_with(self.base, self.origin)
        scenario.assert_not_called()

    def test_failed_preflight_stops_deployment_validation_before_paid_scenarios(self):
        with patch("sys.argv", ["validate.py", "--api-base", self.base, "--portal-origin", self.origin]), \
                patch.object(validate, "validate_cors", return_value=False), \
                patch.object(validate, "validate") as scenario:
            with self.assertRaises(SystemExit) as exit_code:
                validate.main()
        self.assertEqual(exit_code.exception.code, 1)
        scenario.assert_not_called()

    def test_origin_validation_and_preflight_only_requirements(self):
        self.assertEqual(validate.portal_origin(self.origin + "/"), self.origin)
        for value in ("file:///tmp/test", self.origin + "/path", "https://user@portal.example.test",
                      self.origin + "?query=1", self.origin + "#fragment"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                validate.portal_origin(value)
        with patch("sys.argv", ["validate.py", "--api-base", self.base, "--preflight-only"]), \
                contextlib.redirect_stderr(io.StringIO()), patch.object(validate, "validate") as scenario:
            with self.assertRaises(SystemExit) as exit_code:
                validate.main()
        self.assertEqual(exit_code.exception.code, 2)
        scenario.assert_not_called()


if __name__ == "__main__":
    unittest.main()
