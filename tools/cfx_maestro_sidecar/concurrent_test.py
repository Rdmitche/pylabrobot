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

    # also start a protocol on every targeted unit (concurrent RunProtocol):
    py tools\\cfx_maestro_sidecar\\concurrent_test.py --skip-lid \\
       --protocol-file "C:\\Program Files (x86)\\Bio-Rad\\CFX\\SupportFiles\\Default_PrimePCR_384_runfile.csv" \\
       --data-dir C:\\Temp\\plr_concurrent --run-wait 30 --stop-after

A PrimePCR run file (.csv) is self-contained, so --plate-file should be empty
when --protocol-file ends in .csv. For .pcrd protocols, pass --plate-file too.
"""

import argparse
import asyncio
import os
import posixpath
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
  sys.path.insert(0, _REPO_ROOT)

from pylabrobot.thermocycling.biorad.cfx_maestro import (
  CFXMaestroBackend,
  CFXMaestroSession,
)


async def _snapshot(label: str, backends: dict) -> dict:
  """Print a status snapshot across all backends and return {serial: result}.

  Each value is either a CFXInstrumentBlock (on success) or an Exception.
  """
  results = await asyncio.gather(
    *(b._status() for b in backends.values()), return_exceptions=True
  )
  print(f"\n--- {label} ---")
  out = {}
  for serial, res in zip(backends.keys(), results):
    out[serial] = res
    if isinstance(res, Exception):
      print(f"  {serial}: ERROR {res}")
    else:
      print(
        f"  {serial}: status={res.status!r}  block={res.block_temperature}C  "
        f"lid={res.lid_temperature}C  cycle={res.cycle}/{res.cycles}  "
        f"step={res.step}/{res.steps}"
      )
  return out


# Statuses that signal a run has finished (terminal for our polling purposes).
_TERMINAL = {"Idle", "Error", "Preserving"}


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

    if args.protocol_file:
      # Per-unit data file paths so the four units don't fight over one output.
      # We join with posixpath then swap separators so Windows paths render right.
      def _data_file_for(serial: str) -> str:
        joined = posixpath.join(args.data_dir.replace("\\", "/"), f"{serial}.pcrd")
        # If the dir looks like a Windows path, restore backslashes for CFX Maestro.
        return joined.replace("/", "\\") if "\\" in args.data_dir or args.data_dir[1:3] == ":/" else joined

      print(f"\n[concurrent] protocol_file = {args.protocol_file}")
      print(f"[concurrent] plate_file    = {args.plate_file or '(none — PrimePCR csv)'}")
      print(f"[concurrent] data_dir      = {args.data_dir}")

      await _gather_labelled(
        "RunProtocol (concurrent)",
        {
          sn: b.run_protocol(
            protocol_file=args.protocol_file,
            plate_file=args.plate_file,
            data_file=_data_file_for(sn),
            run_id=f"PLR_concurrent_{sn}",
            note="PyLabRobot concurrent_test",
          )
          for sn, b in backends.items()
        },
      )

      # Poll status across all units. Each unit must first leave Idle
      # (so we don't false-positive complete the moment we look) and then
      # return to a terminal state. We always exit early when every unit has
      # completed; --stop-after is a safety net that issues StopRun on any
      # still-running units once --run-wait elapses.
      started = {sn: False for sn in backends}
      done = {sn: False for sn in backends}
      elapsed = 0.0
      while elapsed < args.run_wait:
        snap = await _snapshot(f"run @ t={int(elapsed)}s", backends)
        for sn, res in snap.items():
          if isinstance(res, Exception):
            continue
          if res.status not in _TERMINAL:
            started[sn] = True
          elif started[sn]:
            done[sn] = True

        if all(done.values()):
          print(
            f"\n[concurrent] all {len(done)} run(s) completed naturally at t={int(elapsed)}s"
          )
          break

        await asyncio.sleep(args.poll_interval)
        elapsed += args.poll_interval

      unfinished = [sn for sn, d in done.items() if not d]
      if unfinished:
        print(
          f"\n[concurrent] --run-wait ({args.run_wait}s) reached before "
          f"completion. Unfinished: {unfinished}"
        )
        if args.stop_after:
          await _gather_labelled(
            "StopRun (safety, unfinished only)",
            {sn: backends[sn].stop_run() for sn in unfinished},
          )
          await asyncio.sleep(args.lid_wait)
          await _snapshot("after StopRun", backends)

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
  p.add_argument("--protocol-file", default="",
                 help="If set, fan a concurrent RunProtocol across all targeted units. "
                      "Host path to .pcrd / .plrn / PrimePCR .csv.")
  p.add_argument("--plate-file", default="",
                 help="host path to .pltd (leave empty for PrimePCR .csv)")
  p.add_argument("--data-dir", default=r"C:\Temp\plr_concurrent",
                 help="output dir on the CFX Maestro host; one {serial}.pcrd per unit")
  p.add_argument("--run-wait", type=float, default=900.0,
                 help="max seconds to poll after starting runs. Without --stop-after, "
                      "the script exits as soon as every unit returns to Idle (post-Running). "
                      "With --stop-after, it waits this long and then issues StopRun.")
  p.add_argument("--poll-interval", type=float, default=5.0)
  p.add_argument("--stop-after", action="store_true",
                 help="StopRun on every unit after polling completes")
  args = p.parse_args()
  sys.exit(asyncio.run(main(args)))
