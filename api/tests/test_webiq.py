import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from api.app import webiq

KEY = "test-secret-must-not-leak-abc123"
RESULT = json.loads((Path(__file__).parent / "fixtures" / "webiq_search_web.json").read_text(encoding="utf-8"))


def events(frames):
    return [json.loads(frame.removeprefix("data:").strip()) for frame in frames]


class WebIQTests(unittest.IsolatedAsyncioTestCase):
    async def search(self, handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.object(webiq.httpx, "AsyncClient", return_value=client):
            return await webiq.search_webiq(KEY, "NVDA recent company news")

    async def test_unsupported_contract_never_submits_agent(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"results": []})
        ))
        with patch.object(webiq.httpx, "AsyncClient", return_value=client):
            runner = AsyncMock()
            frames = [frame async for frame in webiq.enriched_run(
                "ib-pitch", "Apple mandate", KEY, "AAPL news", runner
            )]
        runner.assert_not_called()
        self.assertIn("unsupported response", "".join(frames))
        self.assertNotIn(KEY, "".join(frames))
        self.assertEqual(events(frames)[-1], {"type": "done", "outcome": "error"})

    async def test_exact_request_and_normalized_sources(self):
        def handler(request):
            self.assertEqual(str(request.url), webiq.WEBIQ_ENDPOINT)
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.headers["x-apikey"], KEY)
            self.assertEqual(request.headers["content-type"], "application/json")
            body = json.loads(request.content)
            self.assertEqual(body.pop("query"), "NVDA recent company news")
            self.assertEqual(body, {"maxResults": 10, "contentFormat": "passage", "maxLength": 5000})
            self.assertNotIn(KEY, request.content.decode())
            return httpx.Response(200, json=RESULT)
        with self.assertLogs("httpx", level="INFO") as logs:
            result = await self.search(handler)
        self.assertNotIn(KEY, "\n".join(logs.output))
        self.assertEqual(len(result.sources), 2)
        self.assertIsNone(result.sources[0].published_at)
        self.assertEqual(result.sources[0].updated_at, "2026-09-16T10:20:30.1234567Z")
        self.assertEqual(result.sources[0].crawled_at, "2026-09-17T01:02:03.1234567Z")
        self.assertIsNone(result.sources[1].updated_at)
        self.assertNotIn("content", result.sources[0].citation())
        self.assertIn("UNTRUSTED", result.context())
        self.assertIn("Keep SEC filings as the primary source", result.context())
        self.assertNotIn(KEY, result.context())
        self.assertNotIn("tracking.example", result.context())
        self.assertNotIn("synthetic-provider-trace-id", result.context())
        self.assertEqual(result.sources[0].citation()["url"], "https://example.com/news/product")

    async def test_no_key_is_exact_baseline_without_search(self):
        calls = []
        async def runner(*args, **kwargs):
            calls.append((args, kwargs))
            yield 'data: {"type":"done","outcome":"complete"}\n\n'
        with patch.object(webiq, "search_webiq", new_callable=AsyncMock) as search:
            frames = [frame async for frame in webiq.enriched_run("ib-pitch", "Apple mandate", "  ", None, runner)]
        search.assert_not_called()
        self.assertEqual(calls, [(("ib-pitch", "Apple mandate"), {})])
        self.assertEqual(events(frames), [{"type": "done", "outcome": "complete"}])

    async def test_provider_failures_are_sanitized_and_never_submit_agent(self):
        for status in [302, 400, 401, 403, 429, 500, 503]:
            with self.subTest(status=status):
                with self.assertRaises(webiq.WebIQError) as error:
                    await self.search(lambda request: httpx.Response(status, text=KEY, headers={"Location": "https://other.invalid"}))
                self.assertNotIn(KEY, str(error.exception))
                runner = AsyncMock()
                with patch.object(webiq, "search_webiq", side_effect=error.exception):
                    frames = [frame async for frame in webiq.enriched_run("pe-lbo", "Tesla", KEY, "TSLA news", runner)]
                runner.assert_not_called()
                self.assertEqual(events(frames)[-1], {"type": "done", "outcome": "error"})
                self.assertNotIn(KEY, "".join(frames))

    async def test_transport_exception_is_not_echoed(self):
        def handler(request):
            raise httpx.ConnectError(KEY, request=request)
        with self.assertRaises(webiq.WebIQError) as error:
            await self.search(handler)
        self.assertNotIn(KEY, str(error.exception))

    async def test_overall_deadline_and_response_size(self):
        async def slow(request):
            await asyncio.sleep(0.05)
            return httpx.Response(200, json=RESULT)
        with patch.object(webiq, "TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(webiq.WebIQError, "timed out"):
                await self.search(slow)
        with patch.object(webiq, "MAX_RESPONSE_BYTES", 20):
            with self.assertRaisesRegex(webiq.WebIQError, "too much data"):
                await self.search(lambda request: httpx.Response(200, json=RESULT))

    async def test_bad_json_and_reflected_key_are_rejected(self):
        for response in [httpx.Response(200, text=KEY), httpx.Response(200, json={"webResults": [{"content": KEY}]})]:
            with self.assertRaises(webiq.WebIQError) as error:
                await self.search(lambda request: response)
            self.assertNotIn(KEY, str(error.exception))

    def test_bounds_unknown_schema_and_source_validation(self):
        items = [{"title": "T" * 400, "url": f"https://example.com/{i}", "content": "<p>" + "X" * 10_000 + "</p>"}
                 for i in range(20)]
        result = webiq.normalize_response({"webResults": items}, "NVDA", KEY)
        self.assertEqual(len(result.sources), 10)
        self.assertEqual(sum(len(source.content) for source in result.sources), 20_000)
        self.assertTrue(all(len(source.title) == 300 for source in result.sources))
        self.assertTrue(all(source.published_at is None for source in result.sources))
        for payload in [{"unexpected": []}, {"webResults": "not-list"}, {"webResults": [], "error": "failed"},
                        {"results": []}, {"webPages": {"value": []}},
                        {"webResults": [{"title": "x", "url": "javascript:alert(1)", "content": "x"}]},
                        {"webResults": [{"title": "x", "url": "https://example.com", "content": "x", "crawledAt": "yesterday"}]},
                        {"webResults": [{"title": "x", "url": "https://example.com", "snippet": "classic-only content"}]}]:
            with self.subTest(payload=payload), self.assertRaises(webiq.WebIQError):
                webiq.normalize_response(payload, "NVDA", KEY)

    def test_html_passages_become_plain_text_without_script_style_or_comments(self):
        markup = (
            "<style>hidden style text</style><article><h2>Example &amp; Co</h2>"
            "<p>Revenue <b>rose</b> &lt; 5%.</p><script>hidden script text</script>"
            "<template><p>hidden template text</p></template>"
            "<p>Next<br>line.</p><!-- hidden comment --></article>"
        )
        item = {"title": "Synthetic", "url": "https://example.com/news", "content": markup}
        result = webiq.normalize_response({"webResults": [item]}, "NVDA", KEY)
        self.assertEqual(result.sources[0].content, "Example & Co Revenue rose < 5%. Next line.")
        self.assertNotIn("hidden", result.context())
        self.assertNotIn("<article>", result.context())
        self.assertNotIn("<script>", result.context())

    def test_html_decoded_credential_is_never_forwarded(self):
        encoded = "".join(f"&#{ord(char)};" for char in KEY)
        item = {"title": "Synthetic", "url": "https://example.com/news", "content": f"<p>{encoded}</p>"}
        with self.assertRaises(webiq.WebIQError) as error:
            webiq.normalize_response({"webResults": [item]}, "NVDA", KEY)
        self.assertNotIn(KEY, str(error.exception))

    def test_source_dates_and_url_deduplication(self):
        item = RESULT["webResults"][0]
        result = webiq.normalize_response({"webResults": [item, item]}, "NVDA", KEY)
        self.assertEqual(len(result.sources), 1)
        self.assertIsNone(result.sources[0].published_at)

    def test_provider_error_code_is_never_an_empty_success(self):
        for code in ["AuthUserThrottled", "UnknownProviderFailure"]:
            with self.subTest(code=code), self.assertRaises(webiq.WebIQError) as error:
                webiq.normalize_response(
                    {"webResults": [], "errorCode": code, "userMessage": "Untrusted provider detail"},
                    "NVDA", KEY,
                )
            self.assertNotIn("Untrusted provider detail", str(error.exception))

    async def test_empty_search_is_visible_and_partial(self):
        async def runner(*args, **kwargs):
            self.assertEqual(kwargs, {"enrichment_context": ""})
            yield 'data: {"type":"done","outcome":"complete"}\n\n'
        result = webiq.normalize_response({"webResults": []}, "NVDA", KEY)
        with patch.object(webiq, "search_webiq", return_value=result):
            frames = [frame async for frame in webiq.enriched_run("pe-lbo", "Nvidia", KEY, "NVDA", runner)]
        parsed = events(frames)
        self.assertEqual(parsed[1]["status"], "empty")
        self.assertTrue(any(item["type"] == "warning" for item in parsed))
        self.assertEqual(parsed[-1]["outcome"], "partial")

    async def test_concurrent_requests_do_not_share_keys(self):
        calls = []
        async def search(key, query):
            calls.append((key, query))
            await asyncio.sleep(0)
            return webiq.normalize_response(RESULT, query, key)
        async def runner(scenario, message, **kwargs):
            self.assertNotIn(KEY, kwargs["enrichment_context"])
            self.assertNotIn("second-secret", kwargs["enrichment_context"])
            yield 'data: {"type":"done","outcome":"complete"}\n\n'
        async def collect(key, query):
            return [frame async for frame in webiq.enriched_run("ib-pitch", query, key, query, runner)]
        with patch.object(webiq, "search_webiq", side_effect=search):
            runs = await asyncio.gather(collect(KEY, "NVDA"), collect("second-secret", "AAPL"))
        self.assertCountEqual(calls, [(KEY, "NVDA"), ("second-secret", "AAPL")])
        for run in runs:
            self.assertNotIn(KEY, "".join(run))
            self.assertNotIn("second-secret", "".join(run))

    def test_invalid_input_errors_do_not_echo_key(self):
        for key, query, message in [(KEY + "\n" + KEY, "NVDA", "mandate"), (KEY, None, "mandate"),
                                    (KEY, KEY, "mandate"), (KEY, "NVDA", KEY)]:
            with self.subTest(query=query), self.assertRaises(webiq.WebIQError) as error:
                webiq.validate_request(key, query, message)
            self.assertNotIn(KEY, str(error.exception))


if __name__ == "__main__":
    unittest.main()
