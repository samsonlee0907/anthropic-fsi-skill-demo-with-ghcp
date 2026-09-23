import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from azure.core.exceptions import AzureError, ResourceNotFoundError
from fastapi.testclient import TestClient

with patch.dict(os.environ, {
    "PROJECT_ENDPOINT": "https://project.invalid/api/projects/test",
    "STORAGE_BLOB_ENDPOINT": "https://storage.invalid",
    "APPLICATIONINSIGHTS_CONNECTION_STRING": "",
}):
    from api.app import main, orchestrator, telemetry, webiq

KEY = "test-secret-never-echo-this"


class RequestTests(unittest.TestCase):
    def test_health_reports_only_allowlisted_deployment_metadata(self):
        with patch.object(main, "ENVIRONMENT_NAME", "fsi-comparison-env"), \
             patch.object(main, "MODEL_DEPLOYMENT_NAME", "selected-model"), \
             patch.dict(os.environ, {"UNRELATED_SECRET": KEY}), TestClient(main.app) as client:
            response = client.get("/api/health", headers={"X-WebIQ-Key": KEY})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["environment_name"], "fsi-comparison-env")
        self.assertEqual(response.json()["model_deployment_name"], "selected-model")
        self.assertEqual(set(response.json()), {
            "status", "project_endpoint", "telemetry", "environment_name", "model_deployment_name",
        })
        self.assertNotIn(KEY, response.text)

    def test_health_without_optional_metadata_remains_healthy(self):
        with patch.object(main, "ENVIRONMENT_NAME", None), \
             patch.object(main, "MODEL_DEPLOYMENT_NAME", None), TestClient(main.app) as client:
            response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["environment_name"])
        self.assertIsNone(response.json()["model_deployment_name"])

    def test_header_never_appears_in_validation_errors(self):
        with TestClient(main.app, base_url="https://testserver") as client:
            response = client.post("/api/run", headers={"X-WebIQ-Key": KEY},
                                   json={"scenario": "ib-pitch", "message": {"bad": KEY}})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(KEY, response.text)

    def test_credential_requires_https_and_is_not_echoed(self):
        with TestClient(main.app) as client:
            response = client.post("/api/run", headers={"X-WebIQ-Key": KEY}, json={"scenario": "ib-pitch"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(KEY, response.text)

    def test_invalid_key_fails_in_stream_without_agent_invocation(self):
        with patch.object(main, "run_scenario") as runner, TestClient(main.app, base_url="https://testserver") as client:
            response = client.post("/api/run", headers={"X-WebIQ-Key": KEY + " bad"},
                                   json={"scenario": "ib-pitch", "webiq_query": "AAPL news"})
        runner.assert_not_called()
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn('"outcome": "error"', response.text)
        self.assertNotIn(KEY, response.text)

    def test_storage_unavailable_is_retryable_503(self):
        with patch.object(main, "resolve_artifact", side_effect=orchestrator.ArtifactStorageUnavailable()), TestClient(main.app) as client:
            response = client.get("/api/artifacts/example")
        self.assertEqual(response.status_code, 503)

    def test_https_route_passes_only_enrichment_to_agent(self):
        received = []
        async def runner(scenario, message, **kwargs):
            received.append((scenario, message, kwargs))
            yield 'data: {"type":"done","outcome":"complete"}\n\n'
        enrichment = webiq.Enrichment("AAPL news", "2026-09-17", (
            webiq.Source("webiq-1", "Apple news", "https://example.com/apple", None, "Public news"),
        ))
        with patch.object(webiq, "search_webiq", return_value=enrichment) as search, \
             patch.object(main, "run_scenario", side_effect=runner), \
             TestClient(main.app, base_url="https://testserver") as client:
            response = client.post("/api/run", headers={"X-WebIQ-Key": KEY},
                                   json={"scenario": "ib-pitch", "message": "Apple mandate",
                                         "webiq_query": "AAPL news"})
        search.assert_awaited_once_with(KEY, "AAPL news")
        self.assertEqual(received[0][:2], ("ib-pitch", "Apple mandate"))
        self.assertIn("UNTRUSTED", received[0][2]["enrichment_context"])
        self.assertNotIn(KEY, repr(received))
        self.assertNotIn(KEY, response.text)
        self.assertIn("https://example.com/apple", response.text)

    def test_no_key_route_does_not_call_webiq(self):
        async def runner(scenario, message):
            yield 'data: {"type":"done","outcome":"complete"}\n\n'
        with patch.object(webiq, "search_webiq") as search, \
             patch.object(main, "run_scenario", side_effect=runner), TestClient(main.app) as client:
            response = client.post("/api/run", json={"scenario": "ib-pitch"})
        search.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("enrichment", response.text)

    def test_telemetry_header_sanitization_preserves_existing_fields(self):
        setting = "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS"
        with patch.dict(os.environ, {setting: "authorization"}):
            telemetry.configure()
            fields = os.environ[setting].split(",")
        self.assertIn("authorization", fields)
        self.assertIn("x-webiq-key", fields)
        self.assertIn("x-apikey", fields)


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def test_errors_end_with_error_not_success(self):
        with patch.object(orchestrator, "_submit_background_sync", side_effect=RuntimeError("Agent failed")):
            frames = [frame async for frame in orchestrator.run_scenario("ib-pitch", "Apple")]
        self.assertEqual(json.loads(frames[-1][5:])["outcome"], "error")

    async def test_summary_fallback_is_partial_and_enrichment_reaches_both_turns(self):
        payload = {"id": "response1", "status": "completed", "output_text": "Narrative"}
        artifact = {"id": "durable", "url": "/api/artifacts/durable", "filename": "summary.pptx", "persisted": True}
        with patch.object(orchestrator, "_submit_background_sync", return_value=payload) as submit, \
             patch.object(orchestrator, "_register_artifact", return_value=artifact), \
             patch.object(orchestrator, "_build_fallback_artifact", return_value=("summary.pptx", b"PK")):
            frames = [frame async for frame in orchestrator.run_scenario("ib-pitch", "Apple", "UNTRUSTED sample source")]
        self.assertEqual(submit.call_count, 2)
        for call in submit.call_args_list:
            self.assertIn("UNTRUSTED sample source", call.args[1])
        parsed = [json.loads(frame[5:]) for frame in frames]
        self.assertEqual(parsed[-1]["outcome"], "partial")
        self.assertTrue(any(item["type"] == "warning" for item in parsed))
        self.assertEqual(next(item for item in parsed if item["type"] == "artifact")["kind"], "summary")

    async def test_valid_artifact_keeps_complete_outcome(self):
        payload = {"id": "response1", "status": "completed", "output_text": "Narrative"}
        artifact = {"id": "durable", "url": "/api/artifacts/durable", "filename": "pitch.pptx"}
        with patch.object(orchestrator, "_submit_background_sync", return_value=payload), \
             patch.object(orchestrator, "_harvest_from_text_sync", return_value=("Narrative", [artifact])):
            frames = [frame async for frame in orchestrator.run_scenario("ib-pitch", "Apple")]
        self.assertEqual(json.loads(frames[-1][5:])["outcome"], "complete")

    def test_storage_failure_never_publishes_ephemeral_id(self):
        with patch.object(orchestrator, "_blob_service") as service:
            service.return_value.get_blob_client.return_value.upload_blob.side_effect = AzureError("Unavailable")
            with self.assertRaises(orchestrator.ArtifactStorageUnavailable):
                orchestrator._register_artifact("test.xlsx", b"PK")

    def test_durable_download_after_cache_loss(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(orchestrator, "_ARTIFACT_DIR", Path(directory)), \
             patch.object(orchestrator, "ARTIFACTS", {}), \
             patch.object(orchestrator, "_download_blob_sync", return_value=b"PKdata") as download:
            result = orchestrator._register_artifact("test.xlsx", b"PKdata", "artifacts/run/test.xlsx")
            orchestrator.ARTIFACTS.clear()
            resolved = orchestrator.resolve_artifact(result["id"])
            self.assertEqual(Path(resolved["path"]).read_bytes(), b"PKdata")
            download.assert_called_once_with("artifacts/run/test.xlsx")
            self.assertTrue(result["persisted"])

    def test_missing_blob_differs_from_unavailable_storage(self):
        art_id = orchestrator._encode_artifact_id("test.xlsx", "artifacts/run/test.xlsx")
        with patch.object(orchestrator, "_download_blob_sync", side_effect=ResourceNotFoundError()):
            self.assertIsNone(orchestrator.resolve_artifact(art_id))
        with patch.object(orchestrator, "_download_blob_sync", side_effect=AzureError("Unavailable")):
            with self.assertRaises(orchestrator.ArtifactStorageUnavailable):
                orchestrator.resolve_artifact(art_id)


if __name__ == "__main__":
    unittest.main()
