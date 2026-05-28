#!/usr/bin/env python3
"""Drive multiple CFX systems concurrently through a single CFXMaestroSession.

CFX Maestro allows only one registered API client at a time, but that client
can address many instruments by serial number. This script opens a session
once, attaches a CFXMaestroBackend per discovered instrument, then exercises
every unit *concurrently* via ``asyncio.gather``:

  1. status snapshot across all units
  2. OpenLid on every unit at once
  3. CloseLid on every unit at once
  4. final status snapshot

By default it includes every connected instrument. Pass --serials to restrict
the set (useful to exclude units that are mid-run — opening their lid would
pause the protocol).

Usage:
    py tools\\cfx_maestro_sidecar\\concurrent_test.py --sidecar-url http://host:8080/xmlcommand
    py tools\\cfx_maestro_sidecar\\concurrent_test.py --serials CT045747,CT020727,CT041292
"""

import argparse
import asyncio
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
  sys.path.insert(0, _REPO_ROOT)

from pylabrobot.thermocycling.biorad.cfx_maestro import (
  CFXMaestroBackend,
  CFXMaestroSession,
)


async def _snapshot(label: str, backends: dict) -> None:
  results = await asyncio.gather(
    *(b._status() for b in backends.values()), return_exceptions=True
  )
  print(f"\n--- {label} ---")
  for serial, res in zip(backends.keys(), results):
    if isinstance(res, Exception):
      print(f"  {serial}: ERROR {res}")
    else:
      print(
        f"  {serial}: status={res.status!r}  block={res.block_temperature}C  "
        f"lid={res.lid_temperature}C  cycle={res.cycle}/{res.cycles}"
      )


async def _gather_labelled(label: str, coros: dict) -> None:
  """asyncio.gather across a dict {serial: coro}, reporting per-serial outcome."""
  print(f"\n=== {label} ===")
  results = await asyncio.gather(*coros.values(), return_exceptions=True)
  for serial, res in zip(coros.keys(), results):
    if isinstance(res, Exception):
      print(f"  {serial}: FAILED -- {res}")
    else:
      print(f"  {serial}: OK")


async def main(args) -> int:
  print(f"[concurrent] sidecar = {args.sidecar_url}")

  async with CFXMaestroSession(args.sidecar_url, request_timeout=args.timeout) as session:
    blocks = await session.list_instruments()
    available = [b.serial_number for b in blocks]
    print(f"[concurrent] available instruments: {available}")

    if args.serials:
      wanted = [s.strip() for s in args.serials.split(",") if s.strip()]
      missing = [s for s in wanted if s not in available]
      if missing:
        print(f"[concurrent] requested but not connected: {missing}", file=sys.stderr)
        return 1
      target_serials = wanted
    else:
      target_serials = available
    print(f"[concurrent] targeting: {target_serials}")

    # One backend per target unit, all sharing the session.
    backends = {
      sn: CFXMaestroBackend(session=session, serial_number=sn) for sn in target_serials
    }
    await asyncio.gather(*(b.setup() for b in backends.values()))

    await _snapshot("initial", backends)

    if not args.skip_lid:
      await _gather_labelled(
        "OpenLid (concurrent)",
        {sn: b.open_lid() for sn, b in backends.items()},
      )
      await asyncio.sleep(args.lid_wait)
      await _snapshot("after OpenLid", backends)

      await _gather_labelled(
        "CloseLid (concurrent)",
        {sn: b.close_lid() for sn, b in backends.items()},
      )
      await asyncio.sleep(args.lid_wait)
      await _snapshot("after CloseLid", backends)

    # Tear down per-backend (no-ops for shared session) before session.disconnect.
    await asyncio.gather(*(b.stop() for b in backends.values()))

  print("\n[concurrent] done")
  return 0


if __name__ == "__main__":
  p = argparse.ArgumentParser(description="Concurrent multi-CFX exercise via shared session")
  p.add_argument("--sidecar-url", default="http://localhost:8080/xmlcommand")
  p.add_argument("--timeout", type=float, default=30.0)
  p.add_argument(
    "--serials",
    default="",
    help="Comma-separated serial numbers. Default: every connected instrument.",
  )
  p.add_argument("--lid-wait", type=float, default=20.0,
                 help="seconds to wait after each concurrent lid op")
  p.add_argument("--skip-lid", action="store_true")
  args = p.parse_args()
  sys.exit(asyncio.run(main(args)))
