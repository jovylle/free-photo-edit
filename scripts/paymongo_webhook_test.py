#!/usr/bin/env python3
"""Send PayMongo-shaped webhook deliveries at a dev endpoint and check replies.

Start a real sandbox checkout first (POST /api/checkout, pay with a test card
or the GCash simulator), then use its session id:

  PAYMONGO_WEBHOOK_SECRET=whsk_test_... \\
    scripts/paymongo_webhook_test.py --session cs_... --amount 4900

Modes:
  valid      one correctly signed delivery; expects 200 with credits granted
  duplicate  the same delivery twice; expects 200 both, second credits nothing
  forged     signed with a wrong secret; expects 401 and no credit
  stale      correctly signed, timestamp 10 minutes old; expects 401
  low        paid amount one centavo short; expects 400 and no credit

No third-party dependencies, no calls except the target URL.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8078/api/paymongo/webhook"


def event(session_id: str, amount: int,
          kind: str = "checkout_session.payment.paid") -> dict:
    return {"data": {"attributes": {
        "type": kind,
        "data": {"id": session_id, "attributes": {"payments": [
            {"attributes": {"amount": amount, "status": "paid"}}]}}}}}


def sign(secret: str, t: int, raw: bytes) -> str:
    return hmac.new(secret.encode(), str(t).encode() + b"." + raw,
                    hashlib.sha256).hexdigest()


def post(url: str, raw: bytes, header: str) -> tuple[int, str]:
    req = urllib.request.Request(url, data=raw, method="POST", headers={
        "Content-Type": "application/json",
        "Paymongo-Signature": header,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _check(label: str, ok: bool, detail: str) -> bool:
    print(f"{'PASS' if ok else 'FAIL'} {label}: {detail}")
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--session", required=True, help="PayMongo cs_... id")
    ap.add_argument("--amount", type=int, default=4900,
                    help="recorded pack price in centavos (default Starter 4900)")
    ap.add_argument("--mode", default="valid",
                    choices=["valid", "duplicate", "forged", "stale", "low"])
    ap.add_argument("--secret", default=os.environ.get("PAYMONGO_WEBHOOK_SECRET", ""))
    ap.add_argument("--live", action="store_true", help="sign as li, not te")
    args = ap.parse_args(argv)

    if args.mode != "forged" and not args.secret:
        ap.error("--secret or PAYMONGO_WEBHOOK_SECRET is required")
    secret = "wrong-secret" if args.mode == "forged" else args.secret
    paid = args.amount - 1 if args.mode == "low" else args.amount
    raw = json.dumps(event(args.session, paid)).encode()
    t = int(time.time()) - (600 if args.mode == "stale" else 0)
    header = f"t={t},{'li' if args.live else 'te'}={sign(secret, t, raw)}"

    if args.mode == "duplicate":
        s1, b1 = post(args.url, raw, header)
        s2, b2 = post(args.url, raw, header)
        ok = _check("first delivery", s1 == 200, f"HTTP {s1} {b1.strip()}")
        ok &= _check("duplicate delivery", s2 == 200, f"HTTP {s2} {b2.strip()}")
        try:
            ok &= _check("duplicate granted nothing",
                         json.loads(b2).get("credited") == 0, b2.strip())
        except ValueError:
            ok = False
        return 0 if ok else 1

    status, body = post(args.url, raw, header)
    if args.mode == "valid":
        ok = _check("signed delivery", status == 200, f"HTTP {status} {body.strip()}")
        try:
            ok &= _check("credits granted", json.loads(body).get("credited", 0) > 0,
                         body.strip())
        except ValueError:
            ok = False
        return 0 if ok else 1
    expected = {"forged": 401, "stale": 401, "low": 400}[args.mode]
    return 0 if _check(args.mode, status == expected,
                       f"HTTP {status} (expected {expected}) {body.strip()}") else 1


if __name__ == "__main__":
    sys.exit(main())
