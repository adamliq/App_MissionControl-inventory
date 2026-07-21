#!/usr/bin/env python3
"""
mcparser - restricted Splunkd REST POST command for SPL syntax validation.

Designed for a single Splunk Cloud stack. POSTs to a fixed allowlist of
local Splunkd REST endpoints using the running Splunk search session. It
does not store credentials and it refuses arbitrary URLs, browser-proxy
paths, or endpoints outside the allowlist.

Unlike mcquery (read-only GET against an app-namespace allowlist), this
command performs POST requests, so its allowlist is an exact set of full
paths rather than a namespace prefix -- POST semantics are more
consequential than a read-only GET, so the safety boundary here is
deliberately tighter and is not lookup-editable.

The only endpoint currently allowed is Splunkd's search parser, always
called with parse_only=true: this validates SPL syntax without running
the search. parse_only is intentionally not user-configurable -- this
command is a syntax validator, not a general search-dispatch proxy.

Example:
    | mcparser query="index=main | stats count"
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterable

from splunklib.binding import HTTPError
from splunklib.searchcommands import Configuration, GeneratingCommand, Option, dispatch

import mc_common

DEFAULT_ENDPOINT = "/services/search/v2/parser"

# Exact-path allowlist (not a namespace prefix, unlike mcquery). Extending
# this to a new endpoint is a code change, not a lookup-editable setting.
ALLOWED_POST_ENDPOINTS = (DEFAULT_ENDPOINT,)


@Configuration(distributed=False)
class MCParserCommand(GeneratingCommand):
    """POST an SPL query to Splunkd's parser endpoint to validate its syntax."""

    query = Option(
        doc='SPL query text to validate. Example: query="index=main | stats count"',
        require=True,
    )
    endpoint = Option(
        doc="Splunkd REST path to POST to. Restricted to an explicit allowlist.",
        require=False,
        default=DEFAULT_ENDPOINT,
    )

    def generate(self) -> Iterable[Dict[str, Any]]:
        query_time = int(time.time())
        endpoint = str(self.endpoint).strip()
        query_text = str(self.query)

        valid, reason = self._validate_endpoint(endpoint)
        if not valid:
            yield self._error_event("endpoint_rejected", reason, endpoint=endpoint, query_time=query_time)
            return

        path = mc_common.to_absolute_path(endpoint)
        post_params = {
            "q": query_text,
            "parse_only": "true",
            "output_mode": "json",
        }

        self.logger.info("mcparser endpoint=%s params=%s", path, post_params)

        try:
            response = self.service.post(path, **post_params)
            payload = self._read_json_response(response)
        except HTTPError as exc:
            event = self._error_event(
                "parse_failed",
                f"HTTP {exc.status} {exc.reason}",
                endpoint=endpoint,
                query=query_text,
                status=exc.status,
                query_time=query_time,
            )
            # HTTPError only pretty-prints XML detail; the parser endpoint
            # returns JSON, so parse exc.body ourselves for a readable
            # messages field instead of a raw bytes repr.
            detail = exc.body.decode("utf-8", errors="replace") if isinstance(exc.body, bytes) else exc.body
            try:
                detail_payload = json.loads(detail) if detail else None
            except ValueError:
                detail_payload = None
            if isinstance(detail_payload, dict) and "messages" in detail_payload:
                event["messages"] = json.dumps(detail_payload["messages"], default=str)
            elif detail:
                event["detail"] = detail
            yield event
            return
        except Exception as exc:  # pragma: no cover - defensive for Splunk runtime
            yield self._error_event(
                "endpoint_query_failed", str(exc), endpoint=endpoint, query=query_text, query_time=query_time
            )
            return

        event: Dict[str, Any] = {
            "_time": query_time,
            "endpoint": endpoint,
            "query": query_text,
        }
        if isinstance(payload, dict) and "messages" in payload:
            event["messages"] = json.dumps(payload["messages"], default=str)
        event["raw_json"] = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
        yield event

    @staticmethod
    def _validate_endpoint(endpoint: str) -> tuple[bool, str]:
        ok, reason = mc_common.reject_unsafe_endpoint(endpoint)
        if not ok:
            return False, reason
        if endpoint not in ALLOWED_POST_ENDPOINTS:
            return False, (
                f"Endpoint '{endpoint}' is not allowed for mcparser. "
                f"Allowed endpoints: {', '.join(ALLOWED_POST_ENDPOINTS)}."
            )
        return True, "ok"

    @staticmethod
    def _read_json_response(response: Any) -> Any:
        body = response.body.read()
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        if not body:
            return {}
        return json.loads(body)

    @staticmethod
    def _error_event(code: str, message: str, **kwargs: Any) -> Dict[str, Any]:
        event = {
            "_time": kwargs.pop("query_time", int(time.time())),
            "error": code,
            "message": message,
        }
        event.update(kwargs)
        return event


if __name__ == "__main__":
    dispatch(MCParserCommand)
