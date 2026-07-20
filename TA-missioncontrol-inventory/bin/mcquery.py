#!/usr/bin/env python3
"""
mcquery - lookup-driven Mission Control / Enterprise Security inventory query command.

Designed for a single Splunk Cloud stack. The command calls local Splunkd
REST endpoints under an allowlisted set of app namespaces (Mission Control,
Enterprise Security) using the running Splunk search session. It does not
store credentials and it refuses arbitrary URLs, browser-proxy paths, or
paths outside the allowed app namespaces.

Example:
    | mcquery collection=soar_apps
    | mcquery all=true include_raw=false
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode

from splunklib.searchcommands import Configuration, GeneratingCommand, Option, dispatch, validators


APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
DEFAULT_LOOKUP = "missioncontrol_endpoints.csv"

# Splunkd REST app namespaces this command is allowed to query. The owner
# segment in /servicesNS/<owner>/<app>/... is intentionally unconstrained
# (it varies: "-", "nobody", or a real username) -- what's actually
# security-relevant is the app namespace, since that determines which
# app's REST handler answers the request.
ALLOWED_APP_NAMESPACES = (
    "missioncontrol",
    "SplunkEnterpriseSecuritySuite",
)


@Configuration(distributed=False)
class MCQueryCommand(GeneratingCommand):
    """Query one or more local Mission Control endpoints configured in a lookup."""

    collection = Option(
        doc="Collection name from missioncontrol_endpoints.csv. Example: collection=soar_apps",
        require=False,
        default=None,
    )
    all_endpoints = Option(
        name="all",
        doc="Query every endpoint where enabled=true. Example: all=true",
        require=False,
        default=False,
        validate=validators.Boolean(),
    )
    config_lookup = Option(
        doc="Endpoint registry lookup file under the app lookups directory.",
        require=False,
        default=DEFAULT_LOOKUP,
    )
    include_raw = Option(
        doc="Include raw_json for each returned object.",
        require=False,
        default=True,
        validate=validators.Boolean(),
    )
    max_pages = Option(
        doc="Safety limit for pages fetched per endpoint.",
        require=False,
        default=100,
        validate=validators.Integer(1),
    )
    limit = Option(
        doc="Optional maximum number of objects returned across all endpoints.",
        require=False,
        default=0,
        validate=validators.Integer(0),
    )

    def generate(self) -> Iterable[Dict[str, Any]]:
        emitted = 0
        query_time = int(time.time())

        try:
            rows = self._load_endpoint_rows(str(self.config_lookup))
        except Exception as exc:  # pragma: no cover - defensive for Splunk runtime
            yield self._error_event("lookup_load_failed", str(exc), query_time=query_time)
            return

        selected_rows = self._select_rows(rows)
        if not selected_rows:
            yield self._error_event(
                "no_matching_endpoints",
                "No endpoint rows matched. Use collection=<name>, all=true, or enable rows in missioncontrol_endpoints.csv.",
                query_time=query_time,
            )
            return

        for row in selected_rows:
            collection_name = (row.get("collection") or "").strip()
            endpoint = (row.get("endpoint") or "").strip()

            valid, reason = self._validate_endpoint(endpoint)
            if not valid:
                yield self._error_event(
                    "endpoint_rejected",
                    reason,
                    collection=collection_name,
                    endpoint=endpoint,
                    query_time=query_time,
                )
                continue

            try:
                for event in self._query_endpoint(row, query_time=query_time):
                    yield event
                    emitted += 1
                    if int(self.limit or 0) > 0 and emitted >= int(self.limit):
                        return
            except Exception as exc:  # pragma: no cover - defensive for Splunk runtime
                yield self._error_event(
                    "endpoint_query_failed",
                    str(exc),
                    collection=collection_name,
                    endpoint=endpoint,
                    query_time=query_time,
                )

    def _load_endpoint_rows(self, lookup_name: str) -> List[Dict[str, str]]:
        lookup_basename = os.path.basename(lookup_name)
        lookup_path = os.path.join(APP_DIR, "lookups", lookup_basename)
        if not os.path.exists(lookup_path):
            raise FileNotFoundError("Lookup not found: {0}".format(lookup_path))

        with open(lookup_path, "r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def _select_rows(self, rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
        requested_collection = (str(self.collection).strip() if self.collection else "")

        if requested_collection:
            requested = {x.strip() for x in requested_collection.split(",") if x.strip()}
            return [row for row in rows if (row.get("collection") or "").strip() in requested]

        if bool(self.all_endpoints):
            return [row for row in rows if self._as_bool(row.get("enabled"))]

        # Neither collection= nor all=true was given -- nothing selected.
        # generate() reports this via the no_matching_endpoints error event
        # rather than silently guessing "all enabled rows" was intended.
        return []

    def _query_endpoint(self, row: Dict[str, str], query_time: int) -> Iterable[Dict[str, Any]]:
        collection_name = (row.get("collection") or "").strip()
        endpoint = (row.get("endpoint") or "").strip()

        # splunklib treats a path with no leading slash as *relative* to the
        # service's own default namespace and re-prefixes it (e.g.
        # "servicesNS/nobody/x/y" becomes
        # "/servicesNS/<search's own owner>/<search's own app>/servicesNS/nobody/x/y").
        # These endpoints are always absolute Splunkd paths, so the leading
        # slash must be preserved -- stripping it here silently 404s every
        # request regardless of how correct the configured path is.
        path = endpoint if endpoint.startswith("/") else "/" + endpoint

        page_size = self._bounded_int(row.get("page_size"), default=100, minimum=1, maximum=500)
        max_pages = int(self.max_pages or 100)

        base_params = self._parse_params(row.get("default_params"))
        sort_value = (row.get("sort") or "").strip()
        order_value = (row.get("order") or "").strip()
        if sort_value:
            base_params["sort"] = sort_value
        if order_value:
            base_params["order"] = order_value

        for page in range(0, max_pages):
            request_params = dict(base_params)
            request_params["page_size"] = str(page_size)
            request_params["page"] = str(page)

            self.logger.info(
                "mcquery collection=%s url=%s?%s",
                collection_name,
                path,
                urlencode(request_params),
            )
            response = self.service.get(path, **request_params)
            payload = self._read_json_response(response)
            items = self._extract_items(payload, row.get("result_path"))

            if not items:
                if page == 0:
                    yield {
                        "_time": query_time,
                        "collection": collection_name,
                        "endpoint": endpoint,
                        "page": page,
                        "result_count": 0,
                        "warning": "no_results",
                    }
                return

            for item in items:
                yield self._normalise_item(row, item, page=page, query_time=query_time, endpoint=endpoint)

            # Common REST pagination behaviour: a short page means we reached the end.
            if len(items) < page_size:
                return

    def _normalise_item(
        self,
        row: Dict[str, str],
        item: Any,
        page: int,
        query_time: int,
        endpoint: str,
    ) -> Dict[str, Any]:
        collection_name = (row.get("collection") or "").strip()
        id_field = (row.get("id_field") or "id").strip()
        name_field = (row.get("name_field") or "name").strip()

        event: Dict[str, Any] = {
            "_time": query_time,
            "collection": collection_name,
            "endpoint": endpoint,
            "page": page,
            "object_id": self._first_non_empty(
                self._get_path(item, id_field),
                self._get_path(item, "id"),
                self._get_path(item, "_key"),
                self._get_path(item, "guid"),
                self._get_path(item, "uuid"),
            ),
            "object_name": self._first_non_empty(
                self._get_path(item, name_field),
                self._get_path(item, "name"),
                self._get_path(item, "title"),
                self._get_path(item, "label"),
            ),
            "configured": self._first_non_empty(
                self._get_path(item, "configured"),
                self._get_path(item, "is_configured"),
            ),
            "version": self._first_non_empty(
                self._get_path(item, "version"),
                self._get_path(item, "app_version"),
            ),
            "description": self._first_non_empty(
                self._get_path(item, "description"),
                self._get_path(item, "summary"),
            ),
        }

        if isinstance(item, dict):
            for key, value in item.items():
                if self._is_scalar(value):
                    field_name = self._safe_field_name(str(key))
                    if field_name not in event:
                        event[field_name] = value

        if bool(self.include_raw):
            event["raw_json"] = json.dumps(item, sort_keys=True, default=str, separators=(",", ":"))

        return event

    @staticmethod
    def _validate_endpoint(endpoint: str) -> Tuple[bool, str]:
        if not endpoint:
            return False, "Endpoint is empty."
        if "://" in endpoint:
            return False, "Endpoint must be a local Splunkd path, not a URL."
        if ".." in endpoint or "\\" in endpoint:
            return False, "Endpoint contains an unsafe path segment."
        if "/splunkd/__raw/" in endpoint:
            return False, (
                "Endpoint is a Splunk Web browser-proxy path, not a Splunkd REST path. "
                "Strip the leading /<locale>/splunkd/__raw prefix, e.g. use "
                "/servicesNS/nobody/missioncontrol/v1/automation_rule instead of "
                "/en-US/splunkd/__raw/servicesNS/nobody/missioncontrol/v1/automation_rule."
            )

        parts = [part for part in endpoint.split("/") if part]
        if parts[:1] == ["servicesNS"] and len(parts) >= 3:
            app_namespace = parts[2]
        elif parts[:1] == ["services"] and len(parts) >= 2:
            app_namespace = parts[1]
        else:
            return False, "Endpoint must start with /servicesNS/<owner>/<app>/ or /services/<app>/."

        if app_namespace not in ALLOWED_APP_NAMESPACES:
            return False, (
                f"Endpoint app namespace '{app_namespace}' is not allowed. "
                f"Allowed namespaces: {', '.join(ALLOWED_APP_NAMESPACES)}."
            )
        return True, "ok"

    @staticmethod
    def _parse_params(param_string: Optional[str]) -> Dict[str, str]:
        if not param_string:
            return {}
        return {str(k): str(v) for k, v in parse_qsl(str(param_string), keep_blank_values=True)}

    @staticmethod
    def _read_json_response(response: Any) -> Any:
        body = response.body.read()
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        if not body:
            return {}
        return json.loads(body)

    def _extract_items(self, payload: Any, result_path: Optional[str]) -> List[Any]:
        if isinstance(payload, list):
            return payload

        if result_path:
            found = self._get_path(payload, result_path)
            if isinstance(found, list):
                return found
            if isinstance(found, dict):
                return [found]

        if isinstance(payload, dict):
            for key in ("items", "results", "data", "entry", "apps", "objects"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
            return [payload]

        return []

    @staticmethod
    def _get_path(obj: Any, dotted_path: Optional[str]) -> Any:
        if not dotted_path:
            return None
        current = obj
        for part in str(dotted_path).split("."):
            part = part.strip()
            if part == "":
                continue
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        return current

    @staticmethod
    def _first_non_empty(*values: Any) -> Any:
        for value in values:
            if value is not None and value != "":
                return value
        return ""

    @staticmethod
    def _safe_field_name(name: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_")
        if not safe:
            return "field"
        if not re.match(r"^[A-Za-z_]", safe):
            safe = "field_" + safe
        return safe

    @staticmethod
    def _is_scalar(value: Any) -> bool:
        return value is None or isinstance(value, (str, int, float, bool))

    @staticmethod
    def _as_bool(value: Any) -> bool:
        return str(value).strip().lower() in ("1", "true", "t", "yes", "y", "enabled")

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            number = int(str(value).strip())
        except Exception:
            number = default
        return max(minimum, min(maximum, number))

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
    dispatch(MCQueryCommand)
