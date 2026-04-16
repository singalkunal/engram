"""Staso API client for querying traces and conversations."""

import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from typing import Optional

STASO_BASE = os.environ.get("STASO_API_URL", "https://api.staso.ai")
STASO_ENV = os.environ.get("STASO_ENV", "default")

# Staso backend supports API key auth on all read endpoints (first-class, not a hack).
# Cloudflare's default bot protection blocks non-browser User-Agents on the CDN layer,
# so we include browser-like headers as a workaround until Cloudflare rules are updated.
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"


def _headers() -> dict:
    api_key = os.environ.get("STASO_API_KEY", "")
    h = {
        "accept": "*/*",
        "content-type": "application/json",
        "user-agent": _UA,
        "origin": "https://staso.ai",
        "referer": "https://staso.ai/",
    }
    if api_key:
        h["x-api-key"] = api_key
    else:
        print("[warn] No STASO_API_KEY set", file=sys.stderr)
    return h


def get(url: str, params: Optional[dict] = None) -> dict:
    if params:
        url = url + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        if e.code == 403:
            raise SystemExit(
                f"[error] Staso 403 — set STASO_API_KEY in .env\n"
                f"        cp .env.example .env && fill credentials\n"
                f"        {body}"
            )
        raise SystemExit(f"[error] Staso HTTP {e.code}: {body}")


def fetch_conversations(start: datetime, end: datetime, limit: int = 50) -> list[dict]:
    return get(f"{STASO_BASE}/v1/traces/conversations", {
        "environment": STASO_ENV,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "limit": limit,
    }).get("conversations", [])


def fetch_trace_detail(trace_id: str) -> dict:
    return get(f"{STASO_BASE}/v1/traces/{trace_id}")


def fetch_traces_for_session(session_id: str, start: datetime, end: datetime) -> list[dict]:
    return get(f"{STASO_BASE}/v1/traces", {
        "environment": STASO_ENV,
        "session_id": session_id,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "limit": 200,
        "sort_by": "timestamp",
        "sort_order": "asc",
    }).get("traces", [])
