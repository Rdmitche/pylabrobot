"""Bio-Rad CFX Maestro (CFX Manager) API backend.

CFX Maestro / CFX Manager publishes a Windows Communication Foundation (WCF)
service that controls any connected CFX real-time PCR system (CFX384, CFX96,
CFX Opus, …). The service exposes a single operation, ``XmlCommand``, which
accepts a ``Message`` XML document and returns a ``Blocks`` XML document, both
defined by ``CfxComSchema.xsd``.

Transport note
--------------
The service is published with ``WSDualHttpBinding`` using WS-SecureConversation
(SPNEGO/Windows auth, message-level encryption), WS-ReliableMessaging, and a
duplex callback contract. That binding is not reachable from a hand-rolled
Python SOAP client or from ``zeep``. PyLabRobot therefore talks to a small
**.NET sidecar** that runs on the CFX Maestro host, owns the generated WCF
client, and exposes a trivial HTTP endpoint (see ``tools/cfx_maestro_sidecar``):

    POST {sidecar_url}        # e.g. http://<host>:8080/xmlcommand
    Content-Type: application/xml
    body: the <Message> XML document

    200 OK
    body: the <Blocks> XML document (the XmlCommandResult string)

This keeps the proprietary WCF complexity in .NET while *all* protocol logic
(building ``Message`` XML, parsing ``Blocks`` XML, mapping to the
``ThermocyclerBackend`` interface) lives here in Python and is fully unit
testable. The sidecar is a dumb pass-through; it does not interpret the XML.

``RunProtocol`` semantics
-------------------------
The CFX Maestro API does not accept an inline thermal profile. A run references
**files that already exist on the host**: a protocol file (``.pcrd`` / LIMS
``.plrn`` / PrimePCR ``.csv``) and a plate file (``.pltd``). These are opaque,
encrypted archives authored in CFX Maestro. Accordingly, :meth:`run_protocol`
takes file *paths*, not a :class:`~pylabrobot.thermocycling.standard.Protocol`.
"""

import asyncio
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Optional

from pylabrobot.resources import Coordinate
from pylabrobot.thermocycling.backend import ThermocyclerBackend
from pylabrobot.thermocycling.standard import BlockStatus, LidStatus, Protocol
from pylabrobot.thermocycling.thermocycler import Thermocycler

# Schema namespace. CfxComSchema.xsd uses prefix "BioRad" bound to this URI and
# elementFormDefault="qualified".
CFX_NS = "http://schemas.bio-rad.com/LSG/GXD/CfxComSchema.xsd"

# CFX384 optical channels (informational; reads are retrieved post-run from the
# .pcrd data file, not over this control API).
CFX384_OPTICAL_CHANNELS = ("FAM", "HEX", "ROX", "Cy5", "Quasar705")


# ---------------------------------------------------------------------------
# Status mapping (StatusType enum from CfxComSchema.xsd)
# ---------------------------------------------------------------------------

# Operational states in which the block/lid is actively driven to a target.
_ACTIVE_STATES = {
  "Running",
  "Infinite Hold",
  "Initializing",
  "Paused",
  "Preserving",
  "Waiting Manual Start",
}
_RUNNING_STATES = {"Running", "Infinite Hold", "Initializing", "Waiting Manual Start"}
_LID_OPEN_STATES = {"Lid Open"}


# ---------------------------------------------------------------------------
# Parsed status record
# ---------------------------------------------------------------------------


@dataclass
class CFXInstrumentBlock:
  """Parsed ``InstrumentBlockType`` from a ``Blocks`` response."""

  serial_number: str
  status: str
  step: int
  steps: int
  cycle: int
  cycles: int
  sample_volume_uL: float
  lid_temperature: float
  block_temperature: float
  estimated_remaining_run_time_s: float
  nickname: str = ""
  errors: List[str] = field(default_factory=list)


@dataclass
class CFXBlocksResponse:
  """Parsed ``Blocks`` / ``InstrumentBlocksType`` response."""

  cfx_manager_version: str
  user: str
  registration_id: str
  blocks: List[CFXInstrumentBlock]
  errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# XML building / parsing (pure functions, transport-independent)
# ---------------------------------------------------------------------------


def _q(tag: str) -> str:
  """Qualify a tag with the BioRad schema namespace."""
  return f"{{{CFX_NS}}}{tag}"


def _text(parent: ET.Element, tag: str, value: str) -> ET.Element:
  el = ET.SubElement(parent, _q(tag))
  el.text = value
  return el


def build_message(registration_id: str, operation: str, params: Optional[dict] = None) -> str:
  """Build a ``Message`` request XML document.

  Args:
    registration_id: Registration ID from RegisterService. Any value is accepted
      for the RegisterService request itself.
    operation: One of the supported operation element names, e.g.
      ``RegisterService``, ``UnRegisterService``, ``QueryBlocks``, ``OpenLid``,
      ``CloseLid``, ``FlashLed``, ``RunProtocol``, ``StopRun``, ``PauseRun``,
      ``ResumeRun``, ``ShutDown``.
    params: Ordered sub-elements of the operation element, e.g.
      ``{"SerialNumber": "12345"}``. Booleans are serialized as ``true``/``false``.

  Returns:
    A serialized XML document (``str``) with a ``Message`` root.
  """
  ET.register_namespace("BioRad", CFX_NS)
  root = ET.Element(_q("Message"))
  _text(root, "RegistrationID", registration_id)
  op_el = ET.SubElement(root, _q(operation))
  for key, value in (params or {}).items():
    if isinstance(value, bool):
      value = "true" if value else "false"
    _text(op_el, key, str(value))
  return ET.tostring(root, encoding="unicode")


def _find_text(el: Optional[ET.Element], tag: str, default: str = "") -> str:
  if el is None:
    return default
  child = el.find(_q(tag))
  return child.text if (child is not None and child.text is not None) else default


def _parse_errors(parent: ET.Element) -> List[str]:
  out: List[str] = []
  for err in parent.findall(_q("ErrorArray")):
    desc = _find_text(err, "ErrorDescriptionInvariantCulture") or _find_text(
      err, "ErrorDescriptionCurrentCulture"
    )
    code = _find_text(err, "ErrorCode")
    out.append(f"[{code}] {desc}".strip())
  return out


def parse_blocks(xml: str) -> CFXBlocksResponse:
  """Parse a ``Blocks`` response XML document into :class:`CFXBlocksResponse`."""
  root = ET.fromstring(xml)
  blocks: List[CFXInstrumentBlock] = []
  for b in root.findall(_q("BlockArray")):
    # Nested temperature/volume wrappers: <Temperature>, <Volume> floats.
    lid_t = b.find(_q("LidTemperature"))
    blk_t = b.find(_q("BlockTemperature"))
    vol = b.find(_q("SampleVolume"))
    blocks.append(
      CFXInstrumentBlock(
        serial_number=_find_text(b, "SerialNumber"),
        status=_find_text(b, "Status"),
        step=int(_find_text(b, "Step", "0") or 0),
        steps=int(_find_text(b, "Steps", "0") or 0),
        cycle=int(_find_text(b, "Cycle", "0") or 0),
        cycles=int(_find_text(b, "Cycles", "0") or 0),
        sample_volume_uL=float(_find_text(vol, "Volume", "0") or 0),
        lid_temperature=float(_find_text(lid_t, "Temperature", "0") or 0),
        block_temperature=float(_find_text(blk_t, "Temperature", "0") or 0),
        estimated_remaining_run_time_s=float(
          _find_text(b, "EstimatedRemainingRunTime", "0") or 0
        ),
        nickname=_find_text(b, "NickName"),
        errors=_parse_errors(b),
      )
    )
  return CFXBlocksResponse(
    cfx_manager_version=_find_text(root, "CFXManagerVersion"),
    user=_find_text(root, "User"),
    registration_id=_find_text(root, "RegistrationID"),
    blocks=blocks,
    errors=_parse_errors(root),
  )


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class CFXMaestroBackend(ThermocyclerBackend):
  """Backend for any CFX real-time PCR system via the CFX Maestro API sidecar.

  Args:
    sidecar_url: Base URL of the .NET sidecar's ``XmlCommand`` endpoint, e.g.
      ``http://192.168.1.50:8080/xmlcommand``.
    serial_number: Base serial number of the target CFX block. If ``None``,
      :meth:`setup` adopts the first block reported by the status poll.
    request_timeout: HTTP timeout (s) for sidecar calls.
  """

  def __init__(
    self,
    sidecar_url: str,
    serial_number: Optional[str] = None,
    request_timeout: float = 30.0,
  ):
    super().__init__()
    self.sidecar_url = sidecar_url
    self.serial_number = serial_number
    self.request_timeout = request_timeout
    self._registration_id: str = ""

  # ----- transport (the only network-touching method) ---------------------

  async def _xml_command(self, message_xml: str) -> str:
    """POST a ``Message`` XML doc to the sidecar; return the ``Blocks`` XML.

    Isolated so tests can mock it and so an alternative transport (direct SOAP,
    a different bridge) can be swapped in without touching command logic.
    """

    def _post() -> str:
      req = urllib.request.Request(
        self.sidecar_url,
        data=message_xml.encode("utf-8"),
        headers={"Content-Type": "application/xml"},
        method="POST",
      )
      with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
        return resp.read().decode("utf-8")

    return await asyncio.get_running_loop().run_in_executor(None, _post)

  async def _command(self, operation: str, params: Optional[dict] = None) -> CFXBlocksResponse:
    """Build a Message, send it, parse the Blocks response, raise on error."""
    xml = build_message(self._registration_id, operation, params)
    resp = parse_blocks(await self._xml_command(xml))
    if resp.errors:
      raise RuntimeError(f"CFX Maestro error on {operation}: {'; '.join(resp.errors)}")
    return resp

  async def _control_command(self, operation: str, params: dict) -> CFXBlocksResponse:
    """Like ``_command`` but also raises on per-instrument errors.

    Use for state-changing operations (Open/CloseLid, RunProtocol, Stop/Pause/
    ResumeRun). CFX Maestro reports operation-specific failures (e.g. "couldn't
    start the run because the data file path is invalid") in the target block's
    InstrumentBlockType.ErrorArray, not the top-level server ErrorArray.
    """
    resp = await self._command(operation, params)
    block = self._block(resp)
    if block.errors:
      raise RuntimeError(
        f"CFX Maestro instrument error on {operation}: {'; '.join(block.errors)}"
      )
    return resp

  def _block(self, resp: CFXBlocksResponse) -> CFXInstrumentBlock:
    """Return the targeted block's status from a response."""
    if not resp.blocks:
      raise RuntimeError("CFX Maestro returned no instrument blocks")
    if self.serial_number is None:
      return resp.blocks[0]
    for b in resp.blocks:
      if b.serial_number == self.serial_number:
        return b
    raise RuntimeError(f"CFX block with serial {self.serial_number} not connected")

  async def _status(self) -> CFXInstrumentBlock:
    return self._block(await self._command("QueryBlocks"))

  # ----- lifecycle ---------------------------------------------------------

  async def setup(self):
    resp = parse_blocks(
      await self._xml_command(build_message("", "RegisterService"))
    )
    if resp.errors:
      raise RuntimeError(f"CFX Maestro registration failed: {'; '.join(resp.errors)}")
    self._registration_id = resp.registration_id
    if self.serial_number is None and resp.blocks:
      self.serial_number = resp.blocks[0].serial_number

  async def stop(self):
    if self._registration_id:
      try:
        await self._command("UnRegisterService")
      finally:
        self._registration_id = ""

  # ----- lid ---------------------------------------------------------------

  async def open_lid(self):
    await self._control_command("OpenLid", {"SerialNumber": self.serial_number})

  async def close_lid(self):
    await self._control_command("CloseLid", {"SerialNumber": self.serial_number})

  async def get_lid_open(self) -> bool:
    return (await self._status()).status in _LID_OPEN_STATES

  async def get_lid_current_temperature(self) -> List[float]:
    return [(await self._status()).lid_temperature]

  async def get_lid_status(self) -> LidStatus:
    status = (await self._status()).status
    return LidStatus.HOLDING_AT_TARGET if status in _ACTIVE_STATES else LidStatus.IDLE

  async def set_lid_temperature(self, temperature: List[float]):
    raise NotImplementedError(
      "CFX Maestro API sets lid temperature via the protocol/plate files, not directly"
    )

  async def deactivate_lid(self):
    raise NotImplementedError("CFX Maestro API has no direct lid deactivate command")

  async def get_lid_target_temperature(self) -> List[float]:
    raise NotImplementedError("CFX Maestro API does not report a lid target temperature")

  # ----- block -------------------------------------------------------------

  async def get_block_current_temperature(self) -> List[float]:
    return [(await self._status()).block_temperature]

  async def get_block_status(self) -> BlockStatus:
    status = (await self._status()).status
    return BlockStatus.HOLDING_AT_TARGET if status in _ACTIVE_STATES else BlockStatus.IDLE

  async def set_block_temperature(self, temperature: List[float]):
    raise NotImplementedError(
      "CFX Maestro API sets block temperature via the protocol file, not directly"
    )

  async def deactivate_block(self):
    raise NotImplementedError("CFX Maestro API has no direct block deactivate command")

  async def get_block_target_temperature(self) -> List[float]:
    raise NotImplementedError("CFX Maestro API does not report a block target temperature")

  # ----- protocol ----------------------------------------------------------

  async def run_protocol(  # type: ignore[override]
    self,
    protocol: Optional[Protocol] = None,
    block_max_volume: float = 0.0,
    *,
    protocol_file: str,
    plate_file: str = "",
    data_file: str = "",
    run_id: str = "",
    note: str = "",
    lock_instrument_panel: bool = False,
    generate_report_end_of_run: bool = False,
    report_type: str = "pdf",
    report_template_file: str = "",
    report_output_file: str = "",
    email_addresses: str = "",
  ):
    """Start a run on the CFX system from host-side files.

    Note: the CFX Maestro API runs *files*, so the inline ``protocol`` argument
    (required by the abstract base signature) is ignored; pass ``protocol_file``
    and ``plate_file`` paths that exist on the CFX Maestro host instead.

    Args:
      protocol: Ignored. Present only to satisfy the base class signature.
      block_max_volume: Ignored (volume comes from the plate/protocol files).
      protocol_file: Host path to a ``.pcrd`` / ``.plrn`` / PrimePCR ``.csv`` file.
      plate_file: Host path to a ``.pltd`` plate file, or "" if ``protocol_file``
        is not a ``.pcrd``.
      data_file: Host path for the output ``.pcrd`` (".pcrd" extension), or "".
      run_id: Text stored as the run "ID" (often a plate barcode).
      note: Text stored as run "Notes".
      lock_instrument_panel: Hide pause/resume/skip on touch-screen instruments.
      generate_report_end_of_run: Auto-generate a PDF report at run end.
      report_type: End-of-run report format: ``"pdf"`` (default), ``"mht"``, or
        ``"txt"``. Required by the schema even when ``generate_report_end_of_run``
        is False.
      report_template_file: Optional report template path.
      report_output_file: Optional report output path.
      email_addresses: Comma-separated recipients for output/report files.
    """
    if not protocol_file:
      raise ValueError("CFX Maestro run_protocol requires a host-side protocol_file path")
    # NOTE: RunProtocolType is an ordered xs:sequence of required (minOccurs=1)
    # elements; the dict order below must match the schema exactly.
    await self._control_command(
      "RunProtocol",
      {
        "SerialNumber": self.serial_number,
        "ProtocolFile": protocol_file,
        "PlateFile": plate_file,
        "Note": note,
        "RunId": run_id,
        "DataFile": data_file,
        "LockInstrumentPanel": lock_instrument_panel,
        "GenerateReportEndOfRun": generate_report_end_of_run,
        "GenerateReportType": report_type,
        "GenerateReportTemplateFile": report_template_file,
        "GenerateReportOutputFile": report_output_file,
        "EmailAddresses": email_addresses,
      },
    )

  async def stop_run(self):
    await self._control_command("StopRun", {"SerialNumber": self.serial_number})

  async def pause_run(self):
    await self._control_command("PauseRun", {"SerialNumber": self.serial_number})

  async def resume_run(self):
    await self._control_command("ResumeRun", {"SerialNumber": self.serial_number})

  async def get_estimated_remaining_run_time(self) -> float:
    return (await self._status()).estimated_remaining_run_time_s

  # ----- run progress (zero-based, per ThermocyclerBackend contract) -------

  async def get_current_cycle_index(self) -> int:
    return max((await self._status()).cycle - 1, 0)

  async def get_total_cycle_count(self) -> int:
    return (await self._status()).cycles

  async def get_current_step_index(self) -> int:
    return max((await self._status()).step - 1, 0)

  async def get_total_step_count(self) -> int:
    return (await self._status()).steps

  async def get_hold_time(self) -> float:
    raise NotImplementedError("CFX Maestro API does not report per-step hold time")


# Backwards-compatible alias (the API drives all CFX systems, incl. CFX384).
CFX384Backend = CFXMaestroBackend


# ---------------------------------------------------------------------------
# Resource preset
# ---------------------------------------------------------------------------


def cfx384(name: str, backend: ThermocyclerBackend) -> Thermocycler:
  """Construct a :class:`Thermocycler` resource configured for the CFX384."""
  return Thermocycler(
    name=name,
    size_x=325.0,
    size_y=410.0,
    size_z=400.0,
    backend=backend,
    child_location=Coordinate(x=98.62, y=161.26, z=100.0),
    model="BioRad_CFX384",
  )
