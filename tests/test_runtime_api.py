import json
import unittest
from unittest import mock
from urllib.error import URLError

from scripts.runtime.api import ComfyApiError, LoopbackComfyApiClient


class _FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class _FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return _FakeResponse(response)


class _ExitedProcess:
    def poll(self):
        return 7


class LoopbackClientTests(unittest.TestCase):
    def test_default_opener_disables_environment_proxy_routing(self):
        with mock.patch("scripts.runtime.api.build_opener") as build:
            LoopbackComfyApiClient("http://127.0.0.1:18288")

        handler = build.call_args.args[0]
        self.assertEqual(handler.proxies, {})

    def test_client_rejects_non_loopback_or_credentialed_urls(self):
        credentialed_url = "http://" + ":".join(("fixture-user", "fixture-pass")) + "@127.0.0.1:8188"
        invalid_urls = [
            "http://0.0.0.0:8188",
            "http://localhost:8188",
            "https://127.0.0.1:8188",
            credentialed_url,
            "http://127.0.0.1:8188/path",
            "http://127.0.0.1:8188?token=value",
        ]
        for url in invalid_urls:
            with self.subTest(url=url), self.assertRaises(ComfyApiError):
                LoopbackComfyApiClient(url)

    def test_submit_prompt_sends_expected_prompt_and_workflow_metadata(self):
        opener = _FakeOpener([{"prompt_id": "11111111-1111-4111-8111-111111111111", "number": 1, "node_errors": {}}])
        client = LoopbackComfyApiClient("http://127.0.0.1:18288", opener=opener)
        fixture = {
            "prompt": {"1": {"class_type": "EmptyImage", "inputs": {}}},
            "workflow": {"version": 0.4, "nodes": [], "links": []},
        }

        prompt_id = client.submit_prompt(fixture)

        self.assertEqual(prompt_id, "11111111-1111-4111-8111-111111111111")
        request, timeout = opener.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.full_url, "http://127.0.0.1:18288/prompt")
        self.assertGreater(timeout, 0)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["prompt"], fixture["prompt"])
        self.assertEqual(body["extra_data"]["extra_pnginfo"]["workflow"], fixture["workflow"])
        self.assertNotIn("client_id", body)

    def test_submit_prompt_rejects_node_errors_and_missing_prompt_id(self):
        for response in (
            {"prompt_id": "11111111-1111-4111-8111-111111111111", "node_errors": {"2": {"errors": []}}},
            {"node_errors": {}},
        ):
            with self.subTest(response=response):
                client = LoopbackComfyApiClient(
                    "http://127.0.0.1:18288",
                    opener=_FakeOpener([response]),
                )
                with self.assertRaises(ComfyApiError):
                    client.submit_prompt({"prompt": {"1": {"class_type": "EmptyImage", "inputs": {}}}})

    def test_wait_for_history_returns_only_requested_completed_entry(self):
        prompt_id = "11111111-1111-4111-8111-111111111111"
        entry = {"status": {"completed": True, "status_str": "success"}, "outputs": {"2": {}}}
        opener = _FakeOpener([{}, {prompt_id: entry}])
        client = LoopbackComfyApiClient(
            "http://127.0.0.1:18288",
            opener=opener,
            sleep=lambda _seconds: None,
        )

        result = client.wait_for_history(prompt_id, timeout=1, poll_interval=0)

        self.assertEqual(result, entry)
        self.assertEqual([request.full_url for request, _ in opener.requests], [
            f"http://127.0.0.1:18288/history/{prompt_id}",
            f"http://127.0.0.1:18288/history/{prompt_id}",
        ])

    def test_wait_for_history_returns_current_host_terminal_error_entry(self):
        prompt_id = "11111111-1111-4111-8111-111111111111"
        entry = {"status": {"completed": False, "status_str": "error"}, "outputs": {}}
        clock_values = iter([0.0, 0.0, 2.0])
        client = LoopbackComfyApiClient(
            "http://127.0.0.1:18288",
            opener=_FakeOpener([{prompt_id: entry}]),
            sleep=lambda _seconds: None,
            clock=lambda: next(clock_values),
        )

        self.assertEqual(
            client.wait_for_history(prompt_id, timeout=1, poll_interval=0),
            entry,
        )

    def test_wait_until_ready_fails_if_owned_process_exits(self):
        opener = _FakeOpener([URLError("connection refused")])
        client = LoopbackComfyApiClient(
            "http://127.0.0.1:18288",
            opener=opener,
            sleep=lambda _seconds: None,
        )

        with self.assertRaisesRegex(ComfyApiError, "exited"):
            client.wait_until_ready(_ExitedProcess(), timeout=1, poll_interval=0)


if __name__ == "__main__":
    unittest.main()
