#!/usr/bin/env python3
"""Shared Splunkd REST path safety helpers for mcquery and mcpost."""

from __future__ import annotations

from typing import Tuple


def to_absolute_path(endpoint: str) -> str:
    """Ensure a Splunkd REST path keeps its leading slash.

    splunklib treats a path with no leading slash as *relative* to the
    service's own default namespace and silently re-prefixes it (e.g.
    "servicesNS/nobody/x/y" becomes
    "/servicesNS/<search's own owner>/<search's own app>/servicesNS/nobody/x/y"),
    producing a garbled path that 404s regardless of how correct the
    configured endpoint is. Always pass an absolute path through unchanged
    instead of stripping the leading slash.
    """
    return endpoint if endpoint.startswith("/") else "/" + endpoint


def reject_unsafe_endpoint(endpoint: str) -> Tuple[bool, str]:
    """Baseline safety checks shared by every Splunkd REST path.

    Rejects empty values, full URLs, path traversal, and Splunk Web
    browser-proxy paths. Callers still need their own allowlist check
    (app namespace, exact path, etc.) on top of this -- these checks
    alone are not sufficient to trust an endpoint.
    """
    if not endpoint:
        return False, "Endpoint is empty."
    if "://" in endpoint:
        return False, "Endpoint must be a local Splunkd path, not a URL."
    if ".." in endpoint or "\\" in endpoint:
        return False, "Endpoint contains an unsafe path segment."
    if "/splunkd/__raw/" in endpoint:
        return False, (
            "Endpoint is a Splunk Web browser-proxy path, not a Splunkd REST path. "
            "Strip the leading /<locale>/splunkd/__raw prefix, e.g. "
            "'/en-US/splunkd/__raw/services/x' -> '/services/x'."
        )
    return True, "ok"
