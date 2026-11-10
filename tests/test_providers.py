from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from sylp_harness import providers


class FakeResponse:
    def __init__(self, events: list[dict]):
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def __iter__(self):
        for event in self.events:
            yield ("data: " + json.dumps(event) + "\n\n").encode()
        yield b"data: [DONE]\n\n"


class ProviderTests(unittest.TestCase):
    def test_openai_function_tools_are_sent_in_responses_request(self):
        request_body: dict = {}
        events = [
            {"type": "response.output_item.done", "output_index": 0,
             "item": {"type": "function_call", "id": "fc1", "call_id": "call1",
                      "name": "list_files", "arguments": '{"path":"."}'}},
            {"type": "response.completed", "response": {"id": "resp1", "status": "completed",
             "output": [{"type": "function_call", "id": "fc1", "call_id": "call1",
                         "name": "list_files", "arguments": '{"path":"."}'}]}},
        ]

        def urlopen(request, timeout):
            request_body.update(json.loads(request.data))
            return FakeResponse(events)

        tool = {"name": "list_files", "description": "List files",
                "parameters": {"type": "object", "properties": {}}}
        with patch.object(providers, "_credential", return_value="test-key"), \
                patch.object(providers.urllib.request, "urlopen", side_effect=urlopen):
            result = providers._openai_responses_complete(
                "test-model", [{"role": "user", "content": "inspect"}], [tool], "system",
                stream_output=False,
            )

        self.assertEqual(request_body["tools"][0]["name"], "list_files")
        self.assertEqual(request_body["tool_choice"], "auto")
        self.assertNotIn("additional_tools", json.dumps(request_body["input"]))
        self.assertEqual(result["tool_calls"][0]["arguments"], {"path": "."})

    def test_openai_empty_response_reports_provider_diagnostics(self):
        events = [{"type": "response.completed", "response": {
            "id": "resp-empty", "status": "completed", "output": [], "usage": {"input_tokens": 3},
        }}]
        with patch.object(providers, "_credential", return_value="test-key"), \
                patch.object(providers.urllib.request, "urlopen", return_value=FakeResponse(events)), \
                self.assertRaises(providers.EmptyModelResponse) as raised:
            providers._openai_responses_complete("test-model", [], [], "system", stream_output=False)

        self.assertEqual(raised.exception.response_id, "resp-empty")
        self.assertIn("Status: completed", str(raised.exception))
        self.assertIn("output item types: none", str(raised.exception))
        self.assertEqual(raised.exception.usage, {"input_tokens": 3})

    def test_compatible_provider_omits_empty_tools_and_surfaces_error_events(self):
        bodies: list[dict] = []

        def urlopen(request, timeout):
            bodies.append(json.loads(request.data))
            return FakeResponse([{"choices": [{"delta": {"content": "checkpoint"},
                                                  "finish_reason": "stop"}]}])

        with patch.object(providers, "_base_url", return_value="https://provider.invalid/v1"), \
                patch.object(providers, "_credential", return_value="test-key"), \
                patch.object(providers.urllib.request, "urlopen", side_effect=urlopen):
            result = providers.complete("openai-compatible", "test-model", [], [], "system",
                                        stream_output=False)

        self.assertEqual(result["text"], "checkpoint")
        self.assertNotIn("tools", bodies[0])
        self.assertNotIn("tool_choice", bodies[0])

        for error_event in ({"error": {"message": "invalid model"}},
                            {"type": "error", "message": "invalid model"}):
            with patch.object(providers, "_base_url", return_value="https://provider.invalid/v1"), \
                    patch.object(providers, "_credential", return_value="test-key"), \
                    patch.object(providers.urllib.request, "urlopen",
                                 return_value=FakeResponse([error_event])), \
                    self.assertRaisesRegex(RuntimeError, "invalid model"):
                providers.complete("openai-compatible", "test-model", [], [], "system", stream_output=False)

    def test_anthropic_streams_text_tool_calls_and_usage(self):
        request_body: dict = {}
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 7}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "hello"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "tool_use", "id": "tool1", "name": "list_files", "input": {}}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta", "partial_json": '{"path":"."}'}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
             "usage": {"output_tokens": 3}},
            {"type": "message_stop"},
        ]

        def urlopen(request, timeout):
            request_body.update(json.loads(request.data))
            return FakeResponse(events)

        tool = {"name": "list_files", "description": "List files",
                "parameters": {"type": "object", "properties": {}}}
        with patch.object(providers, "_base_url", return_value="https://anthropic.invalid/v1"), \
                patch.object(providers, "_credential", return_value="test-key"), \
                patch.object(providers.urllib.request, "urlopen", side_effect=urlopen):
            result = providers.complete("anthropic", "test-model", [], [tool], "system",
                                        stream_output=False)

        self.assertEqual(result["text"], "hello")
        self.assertEqual(result["tool_calls"][0]["arguments"], {"path": "."})
        self.assertEqual(result["usage"], {"input_tokens": 7, "output_tokens": 3})
        self.assertEqual(request_body["tools"][0]["name"], "list_files")

    def test_openrouter_streams_tool_arguments_and_usage(self):
        request_body: dict = {}
        events = [
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call1",
             "function": {"name": "list_files", "arguments": '{"path":'}}]},
             "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0,
             "function": {"arguments": '"."}'}}]}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 2}},
        ]

        def urlopen(request, timeout):
            request_body.update(json.loads(request.data))
            return FakeResponse(events)

        tool = {"name": "list_files", "description": "List files",
                "parameters": {"type": "object", "properties": {}}}
        with patch.object(providers, "_base_url", return_value="https://openrouter.invalid/api/v1"), \
                patch.object(providers, "_credential", return_value="test-key"), \
                patch.object(providers.urllib.request, "urlopen", side_effect=urlopen):
            result = providers.complete("openrouter", "test-model", [], [tool], "system",
                                        stream_output=False)

        self.assertEqual(result["tool_calls"][0]["arguments"], {"path": "."})
        self.assertEqual(result["usage"], {"prompt_tokens": 8, "completion_tokens": 2})
        self.assertTrue(request_body["stream_options"]["include_usage"])

    def test_gemini_and_antigravity_credentials_and_endpoints(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key"}, clear=True):
            self.assertTrue(providers.is_connected("gemini"))
            self.assertTrue(providers.is_connected("antigravity"))
            self.assertEqual(providers._credential("gemini"), "gemini-key")
            self.assertEqual(providers._credential("antigravity"), "gemini-key")

        with patch.dict(os.environ, {"GOOGLE_API_KEY": "google-key"}, clear=True):
            self.assertTrue(providers.is_connected("gemini"))
            self.assertTrue(providers.is_connected("antigravity"))
            self.assertEqual(providers._credential("gemini"), "google-key")
            self.assertEqual(providers._credential("antigravity"), "google-key")

        with patch.dict(os.environ, {"ANTIGRAVITY_API_KEY": "ag-key"}, clear=True):
            self.assertTrue(providers.is_connected("antigravity"))
            self.assertEqual(providers._credential("antigravity"), "ag-key")

        with patch.dict(os.environ, {}, clear=True), \
                patch("sylp_harness.providers.load_auth", return_value={}), \
                patch("sylp_harness.auth._antigravity_token_file") as mock_token_file:
            mock_token_file.return_value.exists.return_value = True
            self.assertTrue(providers.is_connected("antigravity"))

        self.assertIn("generativelanguage.googleapis.com", providers._base_url("gemini"))
        self.assertIn("generativelanguage.googleapis.com", providers._base_url("antigravity"))

    def test_gemini_and_antigravity_list_models_fallbacks_to_curated(self):
        with patch.object(providers, "_credential", side_effect=RuntimeError("no key")):
            gemini_models = providers.list_models("gemini")
            self.assertTrue(any(m["id"] == "gemini-2.5-pro" for m in gemini_models))

            ag_models = providers.list_models("antigravity")
            self.assertTrue(any(m["id"] == "gemini-2.5-pro" for m in ag_models))
            self.assertTrue(any("Antigravity" in m["name"] for m in ag_models))

    def test_gemini_and_antigravity_complete_streams_tool_calls_and_headers(self):
        headers_captured: dict = {}
        request_body: dict = {}
        events = [
            {"choices": [{"delta": {"content": "Inspecting... "}, "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_gemini",
             "function": {"name": "read_file", "arguments": '{"path":"main.py"}'}}]},
             "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        ]

        def urlopen(request, timeout):
            headers_captured.update(dict(request.header_items()))
            request_body.update(json.loads(request.data))
            return FakeResponse(events)

        tool = {"name": "read_file", "description": "Read file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}

        for prov in ("gemini", "antigravity"):
            headers_captured.clear()
            request_body.clear()
            with patch.object(providers, "_credential", return_value="test-goog-key"), \
                    patch.object(providers.urllib.request, "urlopen", side_effect=urlopen):
                result = providers.complete(prov, "gemini-2.5-flash", [], [tool], "system instructions",
                                            stream_output=False)

            self.assertEqual(result["text"], "Inspecting... ")
            self.assertEqual(len(result["tool_calls"]), 1)
            self.assertEqual(result["tool_calls"][0]["name"], "read_file")
            self.assertEqual(result["tool_calls"][0]["arguments"], {"path": "main.py"})
            self.assertEqual(headers_captured.get("X-goog-api-key"), "test-goog-key")
            self.assertEqual(request_body["tools"][0]["function"]["name"], "read_file")


if __name__ == "__main__":
    unittest.main()
