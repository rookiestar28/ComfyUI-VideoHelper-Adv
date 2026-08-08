"""Minimal loopback-only ComfyUI prompt/history API client."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener


class ComfyApiError(RuntimeError):
    """Raised when the contained ComfyUI API violates the expected contract."""


class LoopbackComfyApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        opener=None,
        request_timeout: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        parsed = urlparse(base_url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ComfyApiError("invalid loopback API port") from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != ""
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ComfyApiError("API URL must be credential-free http://127.0.0.1:<port>")
        self._base_url = f"http://127.0.0.1:{port}"
        # SECURITY: a loopback-only harness must never inherit environment proxy routing.
        self._opener = opener if opener is not None else build_opener(ProxyHandler({}))
        self._request_timeout = request_timeout
        self._sleep = sleep
        self._clock = clock

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self._request_timeout) as response:
                body = response.read()
        except HTTPError as exc:
            # SECURITY: response bodies can reflect prompts or private host details.
            raise ComfyApiError(f"loopback API returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ComfyApiError("loopback API request failed") from exc
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ComfyApiError("loopback API returned invalid JSON") from exc

    def submit_prompt(self, fixture: Mapping[str, Any]) -> str:
        prompt = fixture.get("prompt")
        if not isinstance(prompt, dict) or not prompt:
            raise ComfyApiError("runtime fixture prompt must be a non-empty object")
        payload: dict[str, Any] = {"prompt": prompt}
        workflow = fixture.get("workflow")
        if workflow is not None:
            payload["extra_data"] = {"extra_pnginfo": {"workflow": workflow}}

        response = self._request_json("POST", "/prompt", payload)
        if not isinstance(response, dict):
            raise ComfyApiError("prompt response must be an object")
        if response.get("node_errors"):
            raise ComfyApiError("prompt validation reported node errors")
        prompt_id = response.get("prompt_id")
        try:
            parsed_id = uuid.UUID(prompt_id)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ComfyApiError("prompt response is missing a valid prompt_id") from exc
        canonical_id = str(parsed_id)
        if prompt_id != canonical_id:
            raise ComfyApiError("prompt response prompt_id is not canonical")
        return canonical_id

    def wait_until_ready(
        self,
        owned_process,
        *,
        timeout: float = 120.0,
        poll_interval: float = 0.25,
    ) -> None:
        deadline = self._clock() + timeout
        while self._clock() <= deadline:
            try:
                response = self._request_json("GET", "/prompt")
                if isinstance(response, dict):
                    return
            except ComfyApiError:
                if owned_process.poll() is not None:
                    raise ComfyApiError("owned ComfyUI process exited before becoming ready")
            self._sleep(poll_interval)
        raise ComfyApiError("owned ComfyUI process readiness timed out")

    def wait_for_history(
        self,
        prompt_id: str,
        *,
        timeout: float = 120.0,
        poll_interval: float = 0.25,
    ) -> dict[str, Any]:
        deadline = self._clock() + timeout
        while self._clock() <= deadline:
            response = self._request_json("GET", f"/history/{prompt_id}")
            if isinstance(response, dict):
                entry = response.get(prompt_id)
                if isinstance(entry, dict):
                    status = entry.get("status", {})
                    if isinstance(status, dict):
                        status_str = status.get("status_str")
                        if status.get("completed") is True or status_str == "error":
                            return entry
            self._sleep(poll_interval)
        raise ComfyApiError("prompt history wait timed out")
