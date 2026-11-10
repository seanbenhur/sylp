from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


PROVIDERS = ("openai", "anthropic", "gemini", "antigravity", "openrouter", "openai-compatible")
ENV_KEYS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "antigravity": "ANTIGRAVITY_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openai-compatible": "EVERUN_API_KEY",
}
DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "antigravity": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openrouter": "https://openrouter.ai/api/v1",
}
GEMINI_MODELS = [
    {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
    {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
    {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash"},
    {"id": "gemini-1.5-pro", "name": "Gemini 1.5 Pro"},
    {"id": "gemini-1.5-flash", "name": "Gemini 1.5 Flash"},
]
ANTIGRAVITY_MODELS = [
    {"id": "gemini-2.5-pro", "name": "Antigravity Gemini 2.5 Pro"},
    {"id": "gemini-2.5-flash", "name": "Antigravity Gemini 2.5 Flash"},
    {"id": "gemini-2.0-flash", "name": "Antigravity Gemini 2.0 Flash"},
    {"id": "gemini-1.5-pro", "name": "Antigravity Gemini 1.5 Pro"},
    {"id": "gemini-1.5-flash", "name": "Antigravity Gemini 1.5 Flash"},
]


class EmptyModelResponse(RuntimeError):
    def __init__(self, message: str, *, response_id: str | None = None, usage: dict[str, Any] | None = None):
        super().__init__(message)
        self.response_id = response_id
        self.usage = usage or {}


def config_home() -> Path:
    return Path(os.environ.get("EVERUN_HOME", Path.home() / ".everrun"))


def _auth_path() -> Path:
    return config_home() / "auth.json"


def load_auth() -> dict[str, Any]:
    path = _auth_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_key(provider: str, api_key: str) -> None:
    if provider not in PROVIDERS:
        raise ValueError(f"Unsupported provider {provider!r}; choose from {', '.join(PROVIDERS)}")
    if not api_key.strip():
        raise ValueError("API key cannot be empty")
    auth = load_auth()
    auth[provider] = {"auth_type": "api_key", "api_key": api_key.strip()}
    save_auth(auth)


def configure_provider(provider: str, *, api_key: str | None = None, base_url: str | None = None) -> None:
    if provider not in PROVIDERS:
        raise ValueError(f"Unsupported provider {provider!r}; choose from {', '.join(PROVIDERS)}")
    auth = load_auth()
    if api_key:
        entry = {"auth_type": "api_key", "api_key": api_key.strip()}
    else:
        entry = auth.setdefault(provider, {})
    if base_url:
        if provider != "openai-compatible":
            raise ValueError("A custom base URL is currently supported only for openai-compatible")
        entry["base_url"] = base_url.rstrip("/")
    auth[provider] = entry
    save_auth(auth)


def is_connected(provider: str) -> bool:
    entry = load_auth().get(provider, {})
    key = os.environ.get(ENV_KEYS[provider]) or entry.get("api_key")
    if not key and provider in ("gemini", "antigravity"):
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key and provider == "antigravity":
        key = load_auth().get("gemini", {}).get("api_key")
    if provider == "openai-compatible":
        return bool(entry.get("base_url") or os.environ.get("EVERUN_BASE_URL"))
    if provider == "openai" and entry.get("auth_type") == "oauth":
        return bool(entry.get("refresh_token") and "chatgpt.tokens.use.direct" in entry.get("scopes", []))
    if provider == "antigravity":
        if entry.get("auth_type") == "oauth" and entry.get("refresh_token"):
            return True
        from .auth import _antigravity_token_file
        try:
            if _antigravity_token_file().exists():
                return True
        except Exception:
            pass
    return bool(key)


def save_auth(auth: dict[str, Any]) -> None:
    path = _auth_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(auth, indent=2) + "\n", encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def default_model(provider: str) -> str | None:
    value = load_auth().get("_defaults", {}).get(provider)
    return str(value) if value else None


def default_provider() -> str | None:
    value = load_auth().get("_defaults", {}).get("provider")
    return str(value) if value else None


def save_default_model(provider: str, model: str) -> None:
    auth = load_auth()
    defaults = auth.setdefault("_defaults", {})
    defaults[provider] = model
    auth["_defaults"] = defaults
    save_auth(auth)


def save_default_provider(provider: str) -> None:
    auth = load_auth()
    defaults = auth.setdefault("_defaults", {})
    defaults["provider"] = provider
    auth["_defaults"] = defaults
    save_auth(auth)


def remove_key(provider: str) -> None:
    auth = load_auth()
    auth.pop(provider, None)
    path = _auth_path()
    if auth:
        save_auth(auth)
    elif path.exists():
        path.unlink()


def _credential(provider: str) -> str:
    env_key = ENV_KEYS[provider]
    key = os.environ.get(env_key)
    if not key and provider in ("gemini", "antigravity"):
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if key:
        return key
    entry = load_auth().get(provider, {})
    if provider == "openai" and entry.get("auth_type") == "oauth":
        from .auth import openai_access_token
        return openai_access_token()
    if provider == "antigravity" and (entry.get("auth_type") == "oauth" or not entry.get("api_key")):
        from .auth import antigravity_access_token
        try:
            return antigravity_access_token()
        except Exception:
            pass
    key = entry.get("api_key")
    if not key and provider == "antigravity":
        key = load_auth().get("gemini", {}).get("api_key")
    if not key:
        if provider == "openai-compatible":
            if os.environ.get("EVERUN_BASE_URL") or entry.get("base_url"):
                return ""
            raise RuntimeError(f"Configure a base URL with `everrun auth set {provider}`")
        if provider == "antigravity":
            raise RuntimeError("Connect Antigravity with `everrun auth login antigravity` or set GEMINI_API_KEY")
        raise RuntimeError(f"Connect {provider} with `everrun auth set {provider}` or set {env_key}")
    return str(key)


def _base_url(provider: str) -> str:
    if provider == "openai-compatible":
        value = os.environ.get("EVERUN_BASE_URL") or load_auth().get(provider, {}).get("base_url")
        if not value:
            raise RuntimeError("Set EVERUN_BASE_URL or configure a base URL with `everrun auth set openai-compatible`")
        return str(value).rstrip("/")
    return DEFAULT_BASE_URLS[provider]


def _request_timeout() -> int:
    try:
        value = int(os.environ.get("EVERUN_REQUEST_TIMEOUT", "900"))
    except ValueError:
        value = 900
    return max(60, min(3600, value))


def _request(url: str, *, headers: dict[str, str], data: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=_request_timeout()) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{url} returned HTTP {exc.code}: {details[:1500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach model provider at {url}: {exc.reason}") from exc


def list_models(provider: str) -> list[dict[str, str]]:
    if provider not in PROVIDERS:
        raise ValueError(f"Unsupported provider {provider!r}; choose from {', '.join(PROVIDERS)}")
    if provider in ("gemini", "antigravity"):
        curated = list(ANTIGRAVITY_MODELS if provider == "antigravity" else GEMINI_MODELS)
        base = _base_url(provider)
        try:
            key = _credential(provider)
        except Exception:
            return curated
        if provider == "antigravity" and key.startswith("ya29."):
            return curated  # OAuth tokens cannot list models on the Gemini Developer API
        headers = {"Authorization": f"Bearer {key}", "x-goog-api-key": key}
        try:
            response = _request(f"{base}/models", headers=headers)
            raw = response.get("data", response.get("models", []))
            remote: list[dict[str, str]] = []
            for item in raw if isinstance(raw, list) else []:
                if not isinstance(item, dict):
                    continue
                model_id = item.get("id") or item.get("slug")
                if not isinstance(model_id, str):
                    continue
                name = item.get("display_name") or item.get("name") or model_id
                remote.append({"id": model_id, "name": str(name)})
            if remote:
                seen = set()
                combined = []
                for item in remote + curated:
                    if item["id"] not in seen:
                        seen.add(item["id"])
                        combined.append(item)
                return sorted(combined, key=lambda item: item["name"].lower())
        except Exception:
            pass
        return curated
    base = _base_url(provider)
    key = _credential(provider)
    headers = {"Authorization": f"Bearer {key}"}
    if not key:
        headers = {}
    if provider == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://github.com/seanbenhur/everrun"
        headers["X-Title"] = "Everrun"
    response = _request(f"{base}/models", headers=headers)
    raw = response.get("data", response.get("models", []))
    chatgpt_plan = provider == "openai" and load_auth().get("openai", {}).get("auth_type") == "oauth" and not os.environ.get("OPENAI_API_KEY")
    models: list[dict[str, str]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        if chatgpt_plan and item.get("visibility") != "list":
            continue
        model_id = item.get("id") or item.get("slug")
        if not isinstance(model_id, str):
            continue
        name = item.get("display_name") or item.get("name") or model_id
        models.append({"id": model_id, "name": str(name)})
    return models if chatgpt_plan else sorted(models, key=lambda item: item["name"].lower())


def _openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": tool} for tool in tools]


# Antigravity OAuth tokens (ya29.*) are issued for Google's Code Assist backend,
# not the Gemini Developer API. Sending them to generativelanguage.googleapis.com
# returns 401 UNAUTHENTICATED, so OAuth sign-ins must go through cloudcode-pa.
ANTIGRAVITY_OAUTH_ENDPOINTS = (
    "https://daily-cloudcode-pa.sandbox.googleapis.com",
    "https://cloudcode-pa.googleapis.com",
)
_antigravity_project_cache: dict[str, str] = {}


def _antigravity_oauth_token() -> str | None:
    """Return the Antigravity OAuth access token when OAuth is the active credential, else None."""
    if os.environ.get("ANTIGRAVITY_API_KEY") or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return None
    entry = load_auth().get("antigravity", {})
    if entry.get("api_key") or entry.get("auth_type") != "oauth" or not entry.get("refresh_token"):
        return None
    from .auth import antigravity_access_token
    try:
        return antigravity_access_token()
    except Exception:
        return None


def _antigravity_backend(token: str) -> tuple[str, str | None]:
    """Resolve (base_url, cloud project) on the Code Assist backend, onboarding the user if needed."""
    metadata = {"ideType": "ANTIGRAVITY", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}
    headers = {"Authorization": f"Bearer {token}", "content-type": "application/json",
               "User-Agent": "antigravity/1.15.8 darwin/arm64"}
    last_error: Exception | None = None
    for base in ANTIGRAVITY_OAUTH_ENDPOINTS:
        try:
            if base in _antigravity_project_cache:
                return base, _antigravity_project_cache[base] or None
            info = _request(f"{base}/v1internal:loadCodeAssist", headers=headers, data={"metadata": metadata})
            project = info.get("cloudaicompanionProject")
            if isinstance(project, dict):
                project = project.get("id")
            if not project:
                tier = None
                for candidate in info.get("allowedTiers") or []:
                    if isinstance(candidate, dict) and candidate.get("isDefault"):
                        tier = candidate.get("id")
                        break
                if not tier and isinstance(info.get("currentTier"), dict):
                    tier = info["currentTier"].get("id")
                operation = _request(f"{base}/v1internal:onboardUser", headers=headers,
                                     data={"tierId": tier or "legacy-tier", "metadata": metadata})
                for _ in range(10):
                    if operation.get("done"):
                        break
                    name = operation.get("name")
                    if not name:
                        break
                    time.sleep(2)
                    operation = _request(f"{base}/v1internal/{name}", headers=headers)
                response = operation.get("response") or {}
                project = response.get("cloudaicompanionProject")
                if isinstance(project, dict):
                    project = project.get("id")
            _antigravity_project_cache[base] = str(project or "")
            return base, str(project) if project else None
        except Exception as exc:  # try the next endpoint
            last_error = exc
    raise RuntimeError(f"Antigravity backend onboarding failed: {last_error}")


def _gemini_contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Everrun's OpenAI-style history into Gemini contents."""
    call_names: dict[str, str] = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            if call.get("id") and function.get("name"):
                call_names[str(call["id"])] = str(function["name"])
    contents: list[dict[str, Any]] = []

    def append(role: str, parts: list[dict[str, Any]]) -> None:
        if not parts:
            return
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})

    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role == "user":
            append("user", [{"text": str(content)}])
        elif role == "assistant":
            parts: list[dict[str, Any]] = []
            if isinstance(content, str) and content:
                parts.append({"text": content})
            elif isinstance(content, list):  # Anthropic-style blocks, defensively
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                        parts.append({"text": str(block["text"])})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                if not function.get("name"):
                    continue
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    arguments = {}
                part: dict[str, Any] = {"functionCall": {"name": function["name"], "args": arguments}}
                # The Antigravity backend requires replaying the thought signature it
                # originally returned alongside each function call.
                if function.get("thought_signature"):
                    part["thoughtSignature"] = function["thought_signature"]
                parts.append(part)
            append("model", parts)
        elif role == "tool":
            name = call_names.get(str(message.get("tool_call_id")), "unknown")
            append("user", [{"functionResponse": {"name": name, "response": {"result": str(content)}}}])
    return contents


def _gemini_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    declarations = [{"name": tool["name"], "description": tool.get("description", ""),
                     "parameters": tool.get("parameters") or {"type": "object", "properties": {}}}
                    for tool in tools]
    return [{"functionDeclarations": declarations}] if declarations else []


def _antigravity_oauth_complete(model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                                system: str, *, stream_output: bool = True) -> dict[str, Any]:
    token = _antigravity_oauth_token()
    if not token:
        raise RuntimeError("Connect Antigravity with `everrun auth login antigravity` or set GEMINI_API_KEY")
    base, project = _antigravity_backend(token)
    request_body: dict[str, Any] = {"contents": _gemini_contents(messages)}
    if system:
        request_body["systemInstruction"] = {"parts": [{"text": system}]}
    if tools:
        request_body["tools"] = _gemini_tools(tools)
    body: dict[str, Any] = {"model": model, "request": request_body}
    if project:
        body["project"] = project
    data = _request(f"{base}/v1internal:generateContent",
                    headers={"Authorization": f"Bearer {token}", "content-type": "application/json",
                             "User-Agent": "antigravity/1.15.8 darwin/arm64"},
                    data=body)
    response = data.get("response") if isinstance(data.get("response"), dict) else data
    candidates = response.get("candidates") or []
    if not candidates:
        raise EmptyModelResponse(f"Antigravity returned no candidates: {json.dumps(response)[:600]}")
    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    text_parts: list[str] = []
    calls: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            text_parts.append(part["text"])
        function_call = part.get("functionCall")
        if isinstance(function_call, dict) and function_call.get("name"):
            calls.append({"id": f"call_{len(calls)}_{int(time.time() * 1000) % 10**8}",
                          "name": str(function_call["name"]),
                          "arguments": function_call.get("args") or {},
                          "thought_signature": part.get("thoughtSignature")})
    text = "".join(text_parts)
    if not text and not calls:
        raise EmptyModelResponse(
            f"Antigravity response ended without text or tool calls. Finish reason: "
            f"{candidates[0].get('finishReason', '<unknown>')}; response: {json.dumps(response)[:600]}",
            usage=response.get("usageMetadata"),
        )
    if text and stream_output:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    history_tool_calls = []
    for call in calls:
        function: dict[str, Any] = {"name": call["name"], "arguments": json.dumps(call["arguments"])}
        if call.get("thought_signature"):
            function["thought_signature"] = call["thought_signature"]
        history_tool_calls.append({"id": call["id"], "type": "function", "function": function})
    assistant_message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if history_tool_calls:
        assistant_message["tool_calls"] = history_tool_calls
    metadata = response.get("usageMetadata") or {}
    usage = {"input_tokens": metadata.get("promptTokenCount"),
             "output_tokens": metadata.get("candidatesTokenCount"), "raw": metadata} if metadata else {}
    return {"text": text, "tool_calls": calls, "raw": response,
            "assistant_message": assistant_message,
            "finish_reason": "tool_calls" if calls else "stop",
            "streamed": bool(text and stream_output), "usage": usage}


def _anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]


def _sse_events(request: urllib.request.Request, provider: str):
    try:
        with urllib.request.urlopen(request, timeout=_request_timeout()) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                if payload:
                    yield json.loads(payload)
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{provider} returned HTTP {exc.code}: {details[:1500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach {provider}: {exc.reason}") from exc


def _openai_responses_complete(model: str, history: list[dict[str, Any]], tools: list[dict[str, Any]], system: str,
                               *, stream_output: bool = True) -> dict[str, Any]:
    api_tools = [{"type": "function", "name": tool["name"], "description": tool["description"],
                  "parameters": tool["parameters"], "strict": False} for tool in tools]
    # Responses API function definitions belong in the top-level `tools`
    # argument. Putting them in `input` as a made-up `additional_tools` item
    # can produce responses that finish without invoking Everrun's tools.
    body = {"model": model, "instructions": system, "input": list(history),
            "store": False, "stream": True}
    if api_tools:
        body["tools"] = api_tools
        body["tool_choice"] = "auto"
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_credential('openai')}", "Content-Type": "application/json"},
        method="POST",
    )
    completed = None
    text_deltas: list[str] = []
    finalized_text: dict[tuple[int, int], str] = {}
    streamed_items: dict[int, dict[str, Any]] = {}
    streamed_text = False
    try:
        with urllib.request.urlopen(request, timeout=_request_timeout()) as response:
            data = ""
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    continue
                event = json.loads(data)
                if event.get("type") == "response.output_text.delta":
                    delta = event.get("delta", "")
                    if delta:
                        text_deltas.append(str(delta))
                        if stream_output:
                            sys.stdout.write(str(delta))
                            sys.stdout.flush()
                            streamed_text = True
                elif event.get("type") == "response.refusal.delta":
                    delta = event.get("delta", "")
                    if delta:
                        text_deltas.append(str(delta))
                        if stream_output:
                            sys.stdout.write(str(delta))
                            sys.stdout.flush()
                            streamed_text = True
                elif event.get("type") == "response.output_text.done":
                    key = (int(event.get("output_index", 0)), int(event.get("content_index", 0)))
                    finalized_text[key] = str(event.get("text") or "")
                elif event.get("type") == "response.output_item.added":
                    item = event.get("item")
                    if isinstance(item, dict):
                        streamed_items[int(event.get("output_index", len(streamed_items)))] = item
                elif event.get("type") == "response.output_item.done":
                    item = event.get("item")
                    if isinstance(item, dict):
                        streamed_items[int(event.get("output_index", len(streamed_items)))] = item
                elif event.get("type") == "response.function_call_arguments.done":
                    index = int(event.get("output_index", -1))
                    if index in streamed_items:
                        streamed_items[index]["arguments"] = event.get("arguments", "")
                elif event.get("type") in {"response.completed", "response.incomplete"}:
                    completed = event.get("response")
                elif event.get("type") == "response.failed":
                    error = event.get("response", {}).get("error") or {}
                    raise RuntimeError(f"OpenAI model request failed: {error.get('code') or error.get('message') or event}")
                elif event.get("type") == "error":
                    raise RuntimeError(f"OpenAI model request failed: {event.get('message') or event}")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI Responses API returned HTTP {exc.code}: {details[:1500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach OpenAI Responses API: {exc.reason}") from exc
    if not completed:
        raise RuntimeError("OpenAI Responses stream ended without response.completed")
    if streamed_text:
        sys.stdout.write("\n")
        sys.stdout.flush()
    output = completed.get("output") or [streamed_items[index] for index in sorted(streamed_items)]
    text = "".join(
        str(part.get("text") or part.get("refusal") or "")
        for item in output if item.get("type") == "message"
        for part in item.get("content", []) if part.get("type") in {"output_text", "refusal"}
    )
    if not text and finalized_text:
        text = "".join(finalized_text[index] for index in sorted(finalized_text))
    if not text:
        text = str(completed.get("output_text") or "".join(text_deltas))
    calls = []
    for item in output:
        if item.get("type") != "function_call":
            continue
        try:
            arguments = json.loads(item.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {"_invalid_json": item.get("arguments", "")}
        calls.append({"id": item.get("call_id") or item.get("id"), "name": item.get("name"),
                      "arguments": arguments})
    history_items = output
    if text and not history_items:
        history_items = [{"type": "message", "role": "assistant",
                          "content": [{"type": "output_text", "text": text}]}]
    if not text and not calls:
        item_types = sorted({str(item.get("type", "unknown")) for item in history_items if isinstance(item, dict)})
        incomplete = completed.get("incomplete_details")
        error = completed.get("error")
        raise EmptyModelResponse(
            f"OpenAI response {completed.get('id', '<unknown>')} ended without user-visible text or tool calls. "
            f"Status: {completed.get('status', '<unknown>')}; output item types: {item_types or 'none'}; "
            f"captured {len(streamed_items)} stream items and {len(text_deltas)} text deltas; "
            f"incomplete details: {incomplete or 'none'}; error: {error or 'none'}.",
            response_id=completed.get("id"),
            usage=completed.get("usage"),
        )
    return {"text": text, "tool_calls": calls, "raw": output, "history_items": history_items, "streamed": streamed_text,
            "finish_reason": "tool_calls" if calls else ("length" if completed.get("status") == "incomplete" else "stop"),
            "response_id": completed.get("id"), "usage": completed.get("usage")}


def complete(provider: str, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], system: str,
             *, stream_output: bool = True) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise ValueError(f"Unsupported provider {provider!r}; choose from {', '.join(PROVIDERS)}")
    if provider == "openai":
        return _openai_responses_complete(model, messages, tools, system, stream_output=stream_output)
    if provider == "antigravity" and _antigravity_oauth_token() is not None:
        return _antigravity_oauth_complete(model, messages, tools, system, stream_output=stream_output)
    base = _base_url(provider)
    key = _credential(provider)
    if provider == "anthropic":
        data = {"model": model, "max_tokens": 8192, "system": system, "messages": messages, "stream": True}
        if tools:
            data["tools"] = _anthropic_tools(tools)
        request = urllib.request.Request(
            f"{base}/messages", data=json.dumps(data).encode("utf-8"),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            method="POST",
        )
        blocks_by_index: dict[int, dict[str, Any]] = {}
        stop_reason = None
        completed = False
        streamed = False
        usage: dict[str, Any] = {}
        for event in _sse_events(request, "Anthropic Messages API"):
            kind = event.get("type")
            if kind == "message_start":
                usage.update((event.get("message") or {}).get("usage") or {})
            elif kind == "content_block_start":
                blocks_by_index[int(event["index"])] = dict(event.get("content_block") or {})
                block = blocks_by_index[int(event["index"])]
                if block.get("type") == "tool_use":
                    block["partial_json"] = ""
            elif kind == "content_block_delta":
                index = int(event["index"])
                block = blocks_by_index.setdefault(index, {"type": "text", "text": ""})
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text_delta = str(delta.get("text") or "")
                    block["text"] = str(block.get("text") or "") + text_delta
                    if text_delta and stream_output:
                        sys.stdout.write(text_delta)
                        sys.stdout.flush()
                        streamed = True
                elif delta.get("type") == "input_json_delta":
                    block["partial_json"] = str(block.get("partial_json") or "") + str(delta.get("partial_json") or "")
            elif kind == "content_block_stop":
                block = blocks_by_index.get(int(event["index"]), {})
                if block.get("type") == "tool_use":
                    try:
                        block["input"] = json.loads(block.pop("partial_json", "") or "{}")
                    except json.JSONDecodeError:
                        block["input"] = {"_invalid_json": block.pop("partial_json", "")}
            elif kind == "message_delta":
                stop_reason = (event.get("delta") or {}).get("stop_reason")
                usage.update(event.get("usage") or {})
            elif kind == "message_stop":
                completed = True
            elif kind == "error":
                raise RuntimeError(f"Anthropic model request failed: {event.get('error') or event}")
        if not completed:
            raise RuntimeError("Anthropic stream ended without message_stop")
        if streamed:
            sys.stdout.write("\n")
            sys.stdout.flush()
        blocks = [blocks_by_index[index] for index in sorted(blocks_by_index)]
        text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
        calls = [{"id": block["id"], "name": block["name"], "arguments": block.get("input", {})}
                 for block in blocks if block.get("type") == "tool_use"]
        return {"text": text, "tool_calls": calls, "raw": blocks,
                "assistant_message": {"role": "assistant", "content": blocks},
                "finish_reason": stop_reason, "streamed": streamed, "usage": usage}

    headers = {"Authorization": f"Bearer {key}", "content-type": "application/json"}
    if not key:
        headers.pop("Authorization")
    if provider in ("gemini", "antigravity"):
        if not key.startswith("ya29."):
            headers["x-goog-api-key"] = key
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://github.com/seanbenhur/everrun"
        headers["X-Title"] = "Everrun"
    request_body = {"model": model, "messages": [{"role": "system", "content": system}, *messages],
                    "stream": True}
    if tools:
        request_body["tools"] = _openai_tools(tools)
        request_body["tool_choice"] = "auto"
    if provider == "openrouter":
        request_body["stream_options"] = {"include_usage": True}
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(request_body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    content_parts: list[str] = []
    tool_parts: dict[int, dict[str, Any]] = {}
    finish_reason = None
    usage: dict[str, Any] = {}
    for event in _sse_events(request, provider):
        if event.get("error") or event.get("type") == "error":
            detail = event.get("error") or event.get("message") or event
            raise RuntimeError(f"{provider} model request failed: {detail}")
        if isinstance(event.get("usage"), dict):
            usage.update(event["usage"])
        choices = event.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str) and content:
            content_parts.append(content)
            if stream_output:
                sys.stdout.write(content)
                sys.stdout.flush()
        for part in delta.get("tool_calls", []):
            index = int(part.get("index", 0))
            current = tool_parts.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            if part.get("id"):
                current["id"] = part["id"]
            function = part.get("function") or {}
            current["function"]["name"] += str(function.get("name") or "")
            current["function"]["arguments"] += str(function.get("arguments") or "")
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
    streamed = bool(content_parts and stream_output)
    if streamed:
        sys.stdout.write("\n")
        sys.stdout.flush()
    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if tool_parts:
        message["tool_calls"] = [tool_parts[index] for index in sorted(tool_parts)]
    calls = []
    for call in message.get("tool_calls", []):
        fn = call.get("function", {})
        try:
            arguments = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {"_invalid_json": fn.get("arguments", "")}
        calls.append({"id": call["id"], "name": fn.get("name", ""), "arguments": arguments})
    return {"text": message.get("content") or "", "tool_calls": calls, "raw": message,
            "assistant_message": message, "finish_reason": finish_reason,
            "streamed": streamed, "usage": usage}


def has_antigravity_sdk() -> bool:
    try:
        import google.antigravity  # noqa: F401
        return True
    except ImportError:
        return False
