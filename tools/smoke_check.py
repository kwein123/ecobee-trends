"""Smoke-test a running dashboard against the demo database.

Checks that the page, stylesheet and API all respond, that the API returns
the expected demo thermostats, and — a security regression test — that raw
thermostat identifiers never appear in the API response.

Usage:
    python tools/smoke_check.py [--base http://127.0.0.1:8321]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request

EXPECTED_UNITS = ["Lake House", "Main Floor", "Upstairs"]
# The demo thermostats' real identifiers; none may appear in API output.
RAW_ID_PATTERN = re.compile(r"10000000000\d")


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as resp:
        if resp.status != 200:
            raise AssertionError(f"{url} returned HTTP {resp.status}")
        return resp.read()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://127.0.0.1:8321")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    checks = []

    try:
        page = fetch(f"{base}/").decode("utf-8", "replace")
        assert "Ecobee Trends" in page, "page did not contain its own title"
        checks.append("page loads")

        css = fetch(f"{base}/static/theme.css").decode("utf-8", "replace")
        assert "--surface" in css, "theme.css missing its design tokens"
        for asset, marker in (("dashboard.css", ".panel"), ("dashboard-core.js", "EcobeeCore"),
                              ("dashboard.js", "renderPanels")):
            body = fetch(f"{base}/static/{asset}").decode("utf-8", "replace")
            assert marker in body, f"{asset} did not look right"
        checks.append("stylesheets and scripts load")

        body = fetch(f"{base}/api/data?hours=6")
        data = json.loads(body)
        names = sorted(t["display_name"] for t in data["thermostats"])
        assert names == EXPECTED_UNITS, f"unexpected units: {names}"
        checks.append(f"API returns {names}")

        assert all(t["ts"] for t in data["thermostats"]), "a thermostat has no readings"
        checks.append("all thermostats have readings")

        assert not RAW_ID_PATTERN.search(body.decode("utf-8", "replace")), \
            "SECURITY: a raw thermostat identifier appeared in the API response"
        checks.append("identifiers are masked")

    except (AssertionError, urllib.error.URLError, KeyError, ValueError) as err:
        for c in checks:
            print(f"  ok   {c}")
        print(f"  FAIL {err}", file=sys.stderr)
        return 1

    for c in checks:
        print(f"  ok   {c}")
    print(f"{len(checks)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
