#!/usr/bin/env python3
"""End-to-end smoke test for the CFX Maestro sidecar bridge.

Runs the smallest meaningful round-trip against a live (or Simulation-mode) CFX
Maestro via the .NET sidecar:

    RegisterService  ->  QueryBlocks  ->  UnRegisterService

and prints the parsed instrument status for every connected block. This is the
first thing to run after building/starting the sidecar: it validates the
Message/Blocks namespace and element mapping against a real instance.

Prereqs:
  1. CFX Maestro running (User mode, or the "CFX Maestro (Simulation)" shortcut).
  2. CfxMaestroSidecar.exe running and reachable.

Usage:
    python smoke_test.py --sidecar-url http://<windows-host>:8080/xmlcommand

Run from the repo root (so `pylabrobot` imports), or with PYTHONPATH set to it.
"""

import argparse
import asyncio
import os
import sys

# Allow running directly (python tools/cfx_maestro_sidecar/smoke_test.py) by
# putting the repo root (two levels up) on sys.path.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
  sys.path.insert(0, _REPO_ROOT)

from pylabrobot.thermocycling.biorad.cfx_maestro import (
  CFXMaestroBackend,
  build_message,
  parse_blocks,
)


def _fmt_block(b) -> str:
  lines = [
    f"  serial            : {b.serial_number}",
    f"  nickname          : {b.nickname}",
    f"  status            : {b.status}",
    f"  cycle / cycles    : {b.cycle} / {b.cycles}",
    f"  step  / steps     : {b.step} / {b.steps}",
    f"  block temp (°C)   : {b.block_temperature}",
    f"  lid temp   (°C)   : {b.lid_temperature}",
    f"  sample volume (µL): {b.sample_volume_uL}",
    f"  est. remaining (s): {b.estimated_remaining_run_time_s}",
  ]
  if b.errors:
    lines.append(f"  errors            : {b.errors}")
  return "\n".join(lines)


async def main(sidecar_url: str, timeout: float) -> int:
  backend = CFXMaestroBackend(sidecar_url=sidecar_url, request_timeout=timeout)

  # 1. Health-check the raw transport first, so a connection problem is obvious
  #    before we involve the WCF layer.
  print(f"[smoke] sidecar      : {sidecar_url}")

  # 2. RegisterService (setup adopts the first block's serial number).
  print("[smoke] RegisterService ...")
  try:
    await backend.setup()
  except Exception as e:  # noqa: BLE001
    print(f"[smoke] FAILED at register: {e}", file=sys.stderr)
    print(
      "[smoke] check: CFX Maestro running? sidecar running and reachable? "
      "only-one-client rule (a stale registration may block you)?",
      file=sys.stderr,
    )
    return 1
  print(f"[smoke]   registration id = {backend._registration_id!r}")
  print(f"[smoke]   adopted serial  = {backend.serial_number!r}")

  try:
    # 3. QueryBlocks — print the full parsed snapshot for every block.
    print("[smoke] QueryBlocks ...")
    resp = parse_blocks(
      await backend._xml_command(build_message(backend._registration_id, "QueryBlocks"))
    )
    print(f"[smoke]   CFX Manager version = {resp.cfx_manager_version}")
    print(f"[smoke]   user                = {resp.user}")
    if not resp.blocks:
      print("[smoke]   (no instrument blocks reported)")
    for i, b in enumerate(resp.blocks):
      print(f"[smoke] block {i}:")
      print(_fmt_block(b))

    # 4. Exercise a couple of the high-level getters through the normal path.
    print("[smoke] block status via backend:", await backend.get_block_status())
    print("[smoke] lid open via backend    :", await backend.get_lid_open())
  finally:
    # 5. Always unregister so the next client can connect.
    print("[smoke] UnRegisterService ...")
    await backend.stop()

  print("[smoke] OK")
  return 0


if __name__ == "__main__":
  parser = argparse.ArgumentParser(description="CFX Maestro sidecar smoke test")
  parser.add_argument(
    "--sidecar-url",
    default="http://localhost:8080/xmlcommand",
    help="sidecar XmlCommand endpoint (default: http://localhost:8080/xmlcommand)",
  )
  parser.add_argument(
    "--timeout", type=float, default=30.0, help="HTTP request timeout in seconds"
  )
  args = parser.parse_args()
  sys.exit(asyncio.run(main(args.sidecar_url, args.timeout)))
