"""
Fire-and-forget notification to Home Assistant's webhook trigger.

Set HA_WEBHOOK_URL (e.g. http://homeassistant.local:8123/api/webhook/hibid-alerts)
as an environment variable once you've set up the HA automation side. Until
then, this just logs what it would have sent, so the rest of the app works
fine without HA configured yet.
"""

from __future__ import annotations

import os
from typing import Any

import requests

HA_WEBHOOK_URL = os.environ.get("HA_WEBHOOK_URL", "").strip()
WEBHOOK_TIMEOUT = 5


def notify_new_match(profile_name: str, lot: dict[str, Any], term: str, score: float) -> None:
    payload = {
        "profile": profile_name,
        "term": term,
        "title": lot.get("title"),
        "url": lot.get("url"),
        "auction_name": lot.get("auction_name"),
        "closing_date": lot.get("closing_date"),
        "score": score,
    }

    if not HA_WEBHOOK_URL:
        print(f"[HA webhook not configured] would notify {profile_name}: {payload['title']} (score {score})")
        return

    try:
        requests.post(HA_WEBHOOK_URL, json=payload, timeout=WEBHOOK_TIMEOUT)
    except requests.RequestException as exc:
        # Never let a notification failure break the scrape run.
        print(f"[HA webhook failed] {exc}")
