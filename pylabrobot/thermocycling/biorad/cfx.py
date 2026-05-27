"""Device-free CFX simulator for tests, protocol authoring, and the visualizer.

The real driver lives in :mod:`pylabrobot.thermocycling.biorad.cfx_maestro`
(``CFXMaestroBackend``), which talks to the CFX Maestro WCF API via a .NET
sidecar. This module provides a chatterbox that mimics a CFX system's status
polling behavior without any hardware or sidecar.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from pylabrobot.thermocycling.backend import ThermocyclerBackend
from pylabrobot.thermocycling.biorad.cfx_maestro import (
  CFX384_OPTICAL_CHANNELS,
  CFXMaestroBackend,
  cfx384,
)
from pylabrobot.thermocycling.standard import BlockStatus, LidStatus, Protocol

__all__ = [
  "CFX384ChatterboxBackend",
  "CFXMaestroBackend",
  "CFX384Backend",
  "cfx384",
  "CFX384_OPTICAL_CHANNELS",
]

# Re-export for convenience/back-compat.
CFX384Backend = CFXMaestroBackend


@dataclass
class _CFXSimState:
  block_temp: float = 25.0
  lid_temp: float = 25.0
  lid_open: bool = False
  status: str = "Idle"
  step: int = 0
  steps: int = 0
  cycle: int = 0
  cycles: int = 0
  protocol: Optional[Protocol] = None


class CFX384ChatterboxBackend(ThermocyclerBackend):
  """Logs operations to stdout and simulates CFX status polling instantly."""

  def __init__(self, name: str = "cfx384_chatterbox"):
    super().__init__()
    self.name = name
    self._state = _CFXSimState()

  async def setup(self):
    print(f"[{self.name}] Setting up Bio-Rad CFX (simulated).")

  async def stop(self):
    print(f"[{self.name}] Stopping Bio-Rad CFX (simulated).")

  async def open_lid(self):
    print(f"[{self.name}] Opening lid.")
    self._state.lid_open = True
    self._state.status = "Lid Open"

  async def close_lid(self):
    print(f"[{self.name}] Closing lid.")
    self._state.lid_open = False
    self._state.status = "Idle"

  async def run_protocol(  # type: ignore[override]
    self,
    protocol: Optional[Protocol] = None,
    block_max_volume: float = 0.0,
    *,
    protocol_file: str = "",
    plate_file: str = "",
    **kwargs,
  ):
    print(
      f"[{self.name}] Running protocol "
      f"(protocol_file={protocol_file!r}, plate_file={plate_file!r})."
    )
    self._state.protocol = protocol
    self._state.status = "Running"
    if protocol is not None:
      self._state.cycles = max((s.repeats for s in protocol.stages), default=0)
      self._state.steps = max((len(s.steps) for s in protocol.stages), default=0)
      self._state.cycle = self._state.cycles
      self._state.step = self._state.steps
    self._state.status = "Idle"

  async def stop_run(self):
    print(f"[{self.name}] Stopping run.")
    self._state.status = "Idle"

  async def pause_run(self):
    print(f"[{self.name}] Pausing run.")
    self._state.status = "Paused"

  async def resume_run(self):
    print(f"[{self.name}] Resuming run.")
    self._state.status = "Running"

  # ----- status getters ----------------------------------------------------

  async def get_block_current_temperature(self) -> List[float]:
    return [self._state.block_temp]

  async def get_lid_current_temperature(self) -> List[float]:
    return [self._state.lid_temp]

  async def get_lid_open(self) -> bool:
    return self._state.lid_open

  async def get_lid_status(self) -> LidStatus:
    return LidStatus.IDLE if self._state.status == "Idle" else LidStatus.HOLDING_AT_TARGET

  async def get_block_status(self) -> BlockStatus:
    return BlockStatus.IDLE if self._state.status == "Idle" else BlockStatus.HOLDING_AT_TARGET

  async def get_current_cycle_index(self) -> int:
    return max(self._state.cycle - 1, 0)

  async def get_total_cycle_count(self) -> int:
    return self._state.cycles

  async def get_current_step_index(self) -> int:
    return max(self._state.step - 1, 0)

  async def get_total_step_count(self) -> int:
    return self._state.steps

  # ----- unsupported on CFX Maestro API ------------------------------------

  async def set_block_temperature(self, temperature: List[float]):
    raise NotImplementedError("CFX sets block temperature via protocol files")

  async def set_lid_temperature(self, temperature: List[float]):
    raise NotImplementedError("CFX sets lid temperature via protocol files")

  async def deactivate_block(self):
    raise NotImplementedError("CFX has no direct block deactivate command")

  async def deactivate_lid(self):
    raise NotImplementedError("CFX has no direct lid deactivate command")

  async def get_block_target_temperature(self) -> List[float]:
    raise NotImplementedError("CFX does not report a block target temperature")

  async def get_lid_target_temperature(self) -> List[float]:
    raise NotImplementedError("CFX does not report a lid target temperature")

  async def get_hold_time(self) -> float:
    raise NotImplementedError("CFX does not report per-step hold time")
