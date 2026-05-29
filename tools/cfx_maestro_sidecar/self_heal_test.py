#!/usr/bin/env python3
"""Deterministic self-heal test for the CFX Maestro sidecar.

The CFX Maestro WCF reliable session has InactivityTimeout = 10 minutes per
the published WSDL policy. After 10+ minutes of no traffic, the cached client
channel faults; with the OLD sidecar binary, the next request 502s and the
sidecar needs a manual restart. With the SELF-HEALING binary (ClientManager),
the next request logs:

    [sidecar] channel faulted (...); recreating and retrying once.

and transparently recovers.

This script:

  1. registers, queries status, unregisters    (warm-up call)
  2. sleeps long enough to time out the WCF session (default 11 minutes)
  3. registers, queries status, unregisters    (the test call)

If step 3 succeeds, the channel was either still healthy OR self-heal kicked
in (watch the sidecar console for the "channel faulted" log line to confirm
which).

Usage:
    py tools\\cfx_maestro_sidecar\\self_heal_test.py --sidecar-url http://192.168.0.23:8080/xmlcommand
"""

import argparse
import asyncio
import os
import sys
import time

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
  sys.path.insert(0, _REPO_ROOT)

from pylabrobot.thermocycling.biorad.cfx_maestro import CFXMaestroSession


async def _one_round(url: str, label: str) -> None:
  t0 = time.monotonic()
  async with CFXMaestroSession(url) as session:
    blocks = await session.list_instruments()
  dt = time.monotonic() - t0
  print(
    f"[{label}] OK   register/query/unregister  in {dt:.2f}s  "
    f"({len(blocks)} instrument(s))"
  )


async def main(args) -> int:
  print(f"[self-heal] sidecar = {args.sidecar_url}")
  print(f"[self-heal] idle gap = {args.idle_seconds}s  (WCF InactivityTimeout = 600s)")

  print("\n--- Round 1: warm-up ---")
  await _one_round(args.sidecar_url, "round1")

  print(f"\n--- Sleeping {args.idle_seconds}s to time out the WCF session ---")
  print("    (watch the sidecar console — no traffic should appear here)")
  for remaining in range(args.idle_seconds, 0, -30):
    print(f"    {remaining}s remaining", flush=True)
    await asyncio.sleep(min(30, remaining))

  print("\n--- Round 2: post-idle call (the actual test) ---")
  try:
    await _one_round(args.sidecar_url, "round2")
  except Exception as e:  # noqa: BLE001
    print(f"[self-heal] round2 FAILED: {e}", file=sys.stderr)
    print(
      "[self-heal] Either the sidecar binary lacks ClientManager (rebuild!), "
      "or self-heal hit an error on the retry. Check the sidecar console.",
      file=sys.stderr,
    )
    return 1

  print("\n[self-heal] PASS")
  print(
    "[self-heal] To confirm self-heal actually fired, look at the sidecar "
    "console for a line like:\n"
    '             [sidecar] channel faulted (...); recreating and retrying once.\n'
    "           If you see it -> self-heal proven. If you don’t, the "
    "channel happened to still be healthy at the call time (try a longer --idle-seconds)."
  )
  return 0


if __name__ == "__main__":
  p = argparse.ArgumentParser(description="CFX Maestro sidecar self-heal test")
  p.add_argument("--sidecar-url", default="http://localhost:8080/xmlcommand")
  p.add_argument(
    "--idle-seconds",
    type=int,
    default=660,
    help="idle gap between the two rounds (default: 660s = 11 minutes; "
    "must exceed the WCF InactivityTimeout of 600s)",
  )
  args = p.parse_args()
  sys.exit(asyncio.run(main(args)))
