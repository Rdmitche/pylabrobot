#!/usr/bin/env python3
"""List every CFX system connected to CFX Maestro through the sidecar.

Useful as the first step when controlling multiple instruments: print every
block's serial number, model description, status, and whether it is simulated,
so you can pick which serial to pass to smoke_test.py / exercise_test.py.

Usage:
    py tools\\cfx_maestro_sidecar\\list_instruments.py --sidecar-url http://localhost:8080/xmlcommand
"""

import argparse
import asyncio
import os
import sys
import xml.etree.ElementTree as ET

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
  sys.path.insert(0, _REPO_ROOT)

from pylabrobot.thermocycling.biorad.cfx_maestro import (
  CFX_NS,
  CFXMaestroBackend,
  build_message,
  parse_blocks,
)


def _extra(xml: str, serial: str, tag: str) -> str:
  """Pull an extra (non-dataclass) field from the raw Blocks XML for a block."""
  root = ET.fromstring(xml)
  for b in root.findall(f"{{{CFX_NS}}}BlockArray"):
    sn = b.find(f"{{{CFX_NS}}}SerialNumber")
    if sn is not None and sn.text == serial:
      el = b.find(f"{{{CFX_NS}}}{tag}")
      if el is not None and el.text is not None:
        return el.text
  return ""


async def main(args) -> int:
  backend = CFXMaestroBackend(sidecar_url=args.sidecar_url, request_timeout=args.timeout)
  print(f"[list] sidecar = {args.sidecar_url}")

  # Use the transport directly so we can also peek at extra (non-dataclass)
  # fields like Description, Rows/Columns, and Simulated.
  reg = parse_blocks(await backend._xml_command(build_message("", "RegisterService")))
  if reg.errors:
    print(f"[list] register failed: {'; '.join(reg.errors)}", file=sys.stderr)
    return 1
  backend._registration_id = reg.registration_id

  try:
    raw = await backend._xml_command(build_message(reg.registration_id, "QueryBlocks"))
    resp = parse_blocks(raw)
    print(f"[list] CFX Maestro version: {resp.cfx_manager_version}")
    print(f"[list] user               : {resp.user}")
    print(f"[list] {len(resp.blocks)} instrument(s) connected:")
    for i, b in enumerate(resp.blocks):
      desc = _extra(raw, b.serial_number, "Description")
      rows = _extra(raw, b.serial_number, "Rows")
      cols = _extra(raw, b.serial_number, "Columns")
      sim = _extra(raw, b.serial_number, "Simulated")
      lid_pos = _extra(raw, b.serial_number, "MotorizedLidPosition")
      wells = f"{int(rows) * int(cols)}" if rows.isdigit() and cols.isdigit() else "?"
      print(
        f"  [{i}] serial={b.serial_number}  model={desc or '?'}  "
        f"wells={wells} ({rows}x{cols})  simulated={sim or '?'}  "
        f"status={b.status!r}  lid={lid_pos or '?'}  nickname={b.nickname or '(unset)'}"
      )
      if b.errors:
        for err in b.errors:
          print(f"      !! {err}")
  finally:
    print("[list] UnRegisterService ...")
    await backend.stop()

  return 0


if __name__ == "__main__":
  p = argparse.ArgumentParser(description="List CFX systems connected to CFX Maestro")
  p.add_argument("--sidecar-url", default="http://localhost:8080/xmlcommand")
  p.add_argument("--timeout", type=float, default=30.0)
  args = p.parse_args()
  sys.exit(asyncio.run(main(args)))
