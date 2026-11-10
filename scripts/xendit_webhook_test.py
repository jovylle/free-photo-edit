#!/usr/bin/env python3
"""Send a Xendit payment_session.completed webhook to a local or dev API.

  XENDIT_WEBHOOK_TOKEN=... scripts/xendit_webhook_test.py \
      --session ps_... --amount 49 --mode duplicate

Modes: valid (default), duplicate, forged, low, unknown.
Exits non-zero when the API does not answer as expected, so it is safe
as a release check.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8078/api/xendit/webhook"


def event(session: str, amount: int) -> dict:
    return {
        "event": "payment_session.completed",
        "business_id": "test-business",
        "created": "2026-01-01T00:00:00Z",
        "data": {
            "payment_session_id": session,
            "reference_id": "test-reference",
            "session_type": "PAY",
            "mode": "PAYMENT_LINK",
            "currency": "PHP",
            "amount": amount,
            "status": "COMPLETED",
        },
    }


def post(url: str, body: dict, token: str) -> tuple[int, dict]:
    raw = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=raw, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "free-photo-edit-webhook-test/1.0",
                 "x-callback-token": token})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--session", required=True)
    ap.add_argument("--amount", type=int, required=True)
    ap.add_argument("--mode", default="valid",
                    choices=["valid", "duplicate", "forged", "low", "unknown"])
    ap.add_argument("--token",
                    default=os.environ.get("XENDIT_WEBHOOK_TOKEN", ""))
    args = ap.parse_args()
    if not args.token:
        ap.error("--token or XENDIT_WEBHOOK_TOKEN is required")

    session = "ps_unknown_session" if args.mode == "unknown" else args.session
    amount = args.amount - 1 if args.mode == "low" else args.amount
    token = "wrong-token" if args.mode == "forged" else args.token
    body = event(session, amount)

    if args.mode == "duplicate":
        first, _ = post(args.url, body, token)
        status, data = post(args.url, body, token)
        ok = first == 200 and status == 200 and data.get("credited") == 0
        print(f"duplicate: first={first} second={status} "
              f"credited={data.get('credited')} -> {'ok' if ok else 'FAIL'}")
        return 0 if ok else 1

    expected = {"valid": 200, "forged": 401, "low": 400, "unknown": 200}
    status, data = post(args.url, body, token)
    ok = status == expected[args.mode]
    if args.mode == "valid":
        ok = ok and data.get("credited", 0) > 0
    if args.mode == "unknown":
        ok = ok and data.get("credited", 0) == 0
    print(f"{args.mode}: {status} {data} -> {'ok' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
