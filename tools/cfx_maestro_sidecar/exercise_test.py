#!/usr/bin/env python3
"""Exercise CFX Maestro write operations: lid control and a protocol run.

Builds on smoke_test.py (which validated reads). This drives state-changing
operations and polls status so you can watch the transitions:

    OpenLid -> (status: Lid Open) -> CloseLid -> (Idle)
    RunProtocol -> (Idle -> Initializing -> Running -> ... )

Run against a live or Simulation-mode CFX Maestro via the sidecar.

Usage (PowerShell, on the Windows host; one line):
    py tools\\cfx_maestro_sidecar\\exercise_test.py --sidecar-url http://localhost:8080/xmlcommand

By default it references the PrimePCR run file shipped in SupportFiles, which is
self-contained (protocol + plate) and explicitly accepted by the RunProtocol
operation. If that does not start a run on your install, try the --protocol-file
/ --plate-file pair (e.g. the Qualification_Plate_384.prcl + .pltd).
"""

import argparse
import asyncio
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
  sys.path.insert(0, _REPO_ROOT)

from pylabrobot.thermocycling.biorad.cfx_maestro import CFXMaestroBackend

_SUPPORT = r"C:\Program Files (x86)\Bio-Rad\CFX\SupportFiles"
_DEFAULT_PROTOCOL = os.path.join(_SUPPORT, "Default_PrimePCR_384_runfile.csv")


async def _print_status(backend: CFXMaestroBackend, label: str) -> str:
  b = await backend._status()
  print(
    f"[{label}] status={b.status!r}  lid_open={b.status == 'Lid Open'}  "
    f"cycle={b.cycle}/{b.cycles} step={b.step}/{b.steps}  "
    f"block={b.block_temperature}C lid={b.lid_temperature}C  "
    f"eta={b.estimated_remaining_run_time_s}s"
  )
  return b.status


async def _poll(backend: CFXMaestroBackend, label: str, seconds: float, interval: float = 2.0):
  """Poll status for `seconds`, printing each sample and noting transitions."""
  prev = None
  elapsed = 0.0
  while elapsed < seconds:
    status = await _print_status(backend, label)
    if prev is not None and status != prev:
      print(f"    --> transition: {prev} -> {status}")
    prev = status
    await asyncio.sleep(interval)
    elapsed += interval


async def main(args) -> int:
  backend = CFXMaestroBackend(sidecar_url=args.sidecar_url, request_timeout=args.timeout)

  if args.dump_xml:
    # Wrap the transport to log every Message/Blocks pair to stdout.
    inner = backend._xml_command  # type: ignore[attr-defined]

    async def _logging_xml_command(msg: str) -> str:
      print("\n--- REQUEST XML ---\n" + msg)
      resp = await inner(msg)
      print("\n--- RESPONSE XML ---\n" + resp + "\n--- end ---\n")
      return resp

    backend._xml_command = _logging_xml_command  # type: ignore[assignment]

  print(f"[exercise] sidecar = {args.sidecar_url}")
  await backend.setup()
  print(f"[exercise] registered; serial = {backend.serial_number!r}")
  await _print_status(backend, "initial")

  try:
    # ----- Lid -------------------------------------------------------------
    if not args.skip_lid:
      print("\n=== OpenLid ===")
      try:
        await backend.open_lid()
        await _poll(backend, "open_lid", seconds=args.lid_wait)
        print(f"[exercise] get_lid_open() -> {await backend.get_lid_open()}")
      except Exception as e:  # noqa: BLE001
        print(f"[exercise] OpenLid failed: {e}", file=sys.stderr)

      print("\n=== CloseLid ===")
      try:
        await backend.close_lid()
        await _poll(backend, "close_lid", seconds=args.lid_wait)
        print(f"[exercise] get_lid_open() -> {await backend.get_lid_open()}")
      except Exception as e:  # noqa: BLE001
        print(f"[exercise] CloseLid failed: {e}", file=sys.stderr)

    # ----- Run protocol ----------------------------------------------------
    if not args.skip_run:
      print(f"\n=== RunProtocol ===")
      print(f"[exercise] protocol_file = {args.protocol_file}")
      print(f"[exercise] plate_file    = {args.plate_file or '(none)'}")
      print(f"[exercise] data_file     = {args.data_file or '(CFX Maestro default)'}")
      try:
        await backend.run_protocol(
          protocol_file=args.protocol_file,
          plate_file=args.plate_file,
          data_file=args.data_file,
          run_id="PLR_exercise",
          note="PyLabRobot sidecar exercise run",
        )
        print("[exercise] RunProtocol accepted; polling status transitions...")
        await _poll(backend, "run", seconds=args.run_wait, interval=args.poll_interval)

        if args.stop_after:
          print("\n=== StopRun ===")
          await backend.stop_run()
          await _poll(backend, "stop", seconds=10)
      except Exception as e:  # noqa: BLE001
        print(f"[exercise] RunProtocol failed: {e}", file=sys.stderr)
        print(
          "[exercise] If the message mentions the protocol/plate file, try the "
          "--protocol-file/--plate-file pair (e.g. Qualification_Plate_384.prcl + .pltd), "
          "or a valid --data-file output path.",
          file=sys.stderr,
        )
  finally:
    print("\n[exercise] UnRegisterService ...")
    await backend.stop()

  print("[exercise] done")
  return 0


if __name__ == "__main__":
  p = argparse.ArgumentParser(description="CFX Maestro lid + run_protocol exercise")
  p.add_argument("--sidecar-url", default="http://localhost:8080/xmlcommand")
  p.add_argument("--timeout", type=float, default=30.0)
  p.add_argument("--protocol-file", default=_DEFAULT_PROTOCOL, help="host path to .csv/.pcrd/.plrn")
  p.add_argument("--plate-file", default="", help="host path to .pltd (empty for PrimePCR csv)")
  p.add_argument("--data-file", default="", help="host output .pcrd path (empty = Maestro default)")
  p.add_argument("--lid-wait", type=float, default=12.0, help="seconds to poll after each lid op")
  p.add_argument("--run-wait", type=float, default=60.0, help="seconds to poll after starting run")
  p.add_argument("--poll-interval", type=float, default=3.0)
  p.add_argument("--skip-lid", action="store_true")
  p.add_argument("--skip-run", action="store_true")
  p.add_argument("--stop-after", action="store_true", help="StopRun after polling the run")
  p.add_argument("--dump-xml", action="store_true", help="Log every Message/Blocks XML pair")
  args = p.parse_args()
  sys.exit(asyncio.run(main(args)))
