import asyncio
import unittest
import unittest.mock
import xml.etree.ElementTree as ET

from pylabrobot.thermocycling.biorad.cfx import CFX384ChatterboxBackend
from pylabrobot.thermocycling.biorad.cfx_maestro import (
  CFX_NS,
  CFX384Backend,
  CFXMaestroBackend,
  CFXMaestroSession,
  build_message,
  cfx384,
  parse_blocks,
)
from pylabrobot.thermocycling.standard import BlockStatus, LidStatus

NS = {"b": CFX_NS}


def _blocks_xml(
  status="Running",
  step=2,
  steps=3,
  cycle=5,
  cycles=40,
  serial="SN12345",
  block_temp=60.0,
  lid_temp=105.0,
  reg_id="REG-1",
  errors_xml="",
):
  return f"""<?xml version="1.0"?>
<Blocks xmlns="{CFX_NS}">
  <CFXManagerVersion>3.1</CFXManagerVersion>
  <User>tester</User>
  <RegistrationID>{reg_id}</RegistrationID>
  {errors_xml}
  <BlockArray>
    <SerialNumber>{serial}</SerialNumber>
    <Status>{status}</Status>
    <Step>{step}</Step>
    <Steps>{steps}</Steps>
    <Cycle>{cycle}</Cycle>
    <Cycles>{cycles}</Cycles>
    <SampleVolume><Volume>20</Volume><Units>Microliter</Units></SampleVolume>
    <LidTemperature><Temperature>{lid_temp}</Temperature><Units>Celsius</Units></LidTemperature>
    <BlockTemperature><Temperature>{block_temp}</Temperature><Units>Celsius</Units></BlockTemperature>
    <EstimatedRemainingRunTime>1234.5</EstimatedRemainingRunTime>
    <NickName>MyCFX</NickName>
  </BlockArray>
</Blocks>"""


class TestBuildMessage(unittest.TestCase):
  def test_register_message(self):
    xml = build_message("", "RegisterService")
    root = ET.fromstring(xml)
    self.assertTrue(root.tag.endswith("Message"))
    # Empty text round-trips through ElementTree as None.
    self.assertIn(root.find("b:RegistrationID", NS).text, (None, ""))
    self.assertIsNotNone(root.find("b:RegisterService", NS))

  def test_operation_with_params_and_bool(self):
    xml = build_message(
      "REG-1",
      "RunProtocol",
      {"SerialNumber": "SN1", "ProtocolFile": "C:/p.pcrd", "LockInstrumentPanel": True},
    )
    root = ET.fromstring(xml)
    self.assertEqual(root.find("b:RegistrationID", NS).text, "REG-1")
    op = root.find("b:RunProtocol", NS)
    self.assertEqual(op.find("b:SerialNumber", NS).text, "SN1")
    self.assertEqual(op.find("b:ProtocolFile", NS).text, "C:/p.pcrd")
    self.assertEqual(op.find("b:LockInstrumentPanel", NS).text, "true")

  def test_namespace_qualified(self):
    xml = build_message("R", "QueryBlocks")
    self.assertIn(CFX_NS, xml)


class TestParseBlocks(unittest.TestCase):
  def test_parse_status_fields(self):
    resp = parse_blocks(_blocks_xml())
    self.assertEqual(resp.cfx_manager_version, "3.1")
    self.assertEqual(resp.registration_id, "REG-1")
    self.assertEqual(len(resp.blocks), 1)
    b = resp.blocks[0]
    self.assertEqual(b.serial_number, "SN12345")
    self.assertEqual(b.status, "Running")
    self.assertEqual((b.step, b.steps, b.cycle, b.cycles), (2, 3, 5, 40))
    self.assertEqual(b.block_temperature, 60.0)
    self.assertEqual(b.lid_temperature, 105.0)
    self.assertEqual(b.sample_volume_uL, 20.0)
    self.assertAlmostEqual(b.estimated_remaining_run_time_s, 1234.5)
    self.assertEqual(b.nickname, "MyCFX")

  def test_parse_server_error(self):
    err = (
      "<ErrorArray><ErrorCode>42</ErrorCode>"
      "<ErrorDescriptionInvariantCulture>boom</ErrorDescriptionInvariantCulture></ErrorArray>"
    )
    resp = parse_blocks(_blocks_xml(errors_xml=err))
    self.assertEqual(resp.errors, ["[42] boom"])


class TestCFXMaestroBackend(unittest.IsolatedAsyncioTestCase):
  def setUp(self):
    self.backend = CFXMaestroBackend(sidecar_url="http://localhost:9/x", serial_number="SN12345")
    self.backend._registration_id = "REG-1"
    self.sent = []

    async def fake_xml_command(message_xml: str) -> str:
      self.sent.append(message_xml)
      root = ET.fromstring(message_xml)
      # Return a registration response for RegisterService, else a status snapshot.
      if root.find("b:RegisterService", NS) is not None:
        return _blocks_xml(status="Idle", reg_id="REG-NEW")
      return _blocks_xml()

    self.backend._session._xml_command = fake_xml_command  # type: ignore[assignment]

  def _last_op(self) -> str:
    root = ET.fromstring(self.sent[-1])
    for child in root:
      if not child.tag.endswith("RegistrationID"):
        return child.tag.split("}")[-1]
    return ""

  def test_alias(self):
    self.assertIs(CFX384Backend, CFXMaestroBackend)

  async def test_setup_registers_and_adopts_serial(self):
    b = CFXMaestroBackend(sidecar_url="http://localhost:9/x")
    b._session._xml_command = self.backend._session._xml_command  # type: ignore[assignment]
    await b.setup()
    self.assertEqual(b._registration_id, "REG-NEW")
    self.assertEqual(b.serial_number, "SN12345")

  async def test_status_mapping(self):
    self.assertEqual(await self.backend.get_block_status(), BlockStatus.HOLDING_AT_TARGET)
    self.assertEqual(await self.backend.get_lid_status(), LidStatus.HOLDING_AT_TARGET)
    self.assertEqual(await self.backend.get_block_current_temperature(), [60.0])
    self.assertEqual(await self.backend.get_lid_current_temperature(), [105.0])

  async def test_run_progress_zero_based(self):
    # Cycle 5/40, Step 2/3 -> zero-based indices 4 and 1.
    self.assertEqual(await self.backend.get_current_cycle_index(), 4)
    self.assertEqual(await self.backend.get_total_cycle_count(), 40)
    self.assertEqual(await self.backend.get_current_step_index(), 1)
    self.assertEqual(await self.backend.get_total_step_count(), 3)

  async def test_open_lid_sends_serial(self):
    await self.backend.open_lid()
    self.assertEqual(self._last_op(), "OpenLid")
    root = ET.fromstring(self.sent[-1])
    self.assertEqual(root.find("b:OpenLid/b:SerialNumber", NS).text, "SN12345")

  async def test_run_protocol_requires_file(self):
    with self.assertRaisesRegex(ValueError, "protocol_file"):
      await self.backend.run_protocol(protocol_file="")

  async def test_run_protocol_sends_paths(self):
    await self.backend.run_protocol(
      protocol_file="C:/prot.pcrd", plate_file="C:/plate.pltd", data_file="C:/out.pcrd"
    )
    self.assertEqual(self._last_op(), "RunProtocol")
    root = ET.fromstring(self.sent[-1])
    op = root.find("b:RunProtocol", NS)
    self.assertEqual(op.find("b:ProtocolFile", NS).text, "C:/prot.pcrd")
    self.assertEqual(op.find("b:PlateFile", NS).text, "C:/plate.pltd")
    self.assertEqual(op.find("b:DataFile", NS).text, "C:/out.pcrd")
    # GenerateReportType is required by the schema (minOccurs=1) and must sit
    # between GenerateReportEndOfRun and GenerateReportTemplateFile.
    self.assertEqual(op.find("b:GenerateReportType", NS).text, "pdf")
    child_tags = [c.tag.split("}")[-1] for c in op]
    self.assertEqual(
      child_tags,
      [
        "SerialNumber",
        "ProtocolFile",
        "PlateFile",
        "Note",
        "RunId",
        "DataFile",
        "LockInstrumentPanel",
        "GenerateReportEndOfRun",
        "GenerateReportType",
        "GenerateReportTemplateFile",
        "GenerateReportOutputFile",
        "EmailAddresses",
      ],
    )

  async def test_error_response_raises(self):
    async def erroring(message_xml: str) -> str:
      return _blocks_xml(
        errors_xml="<ErrorArray><ErrorCode>7</ErrorCode>"
        "<ErrorDescriptionInvariantCulture>bad</ErrorDescriptionInvariantCulture></ErrorArray>"
      )

    self.backend._session._xml_command = erroring  # type: ignore[assignment]
    with self.assertRaisesRegex(RuntimeError, "bad"):
      await self.backend.stop_run()

  async def test_unset_serial_uses_first_block(self):
    self.backend.serial_number = None
    self.assertEqual(await self.backend.get_block_current_temperature(), [60.0])

  async def test_setup_warns_when_multiple_blocks_and_no_serial(self):
    import warnings as _w

    def _two_blocks(message_xml: str) -> str:
      # Hand-built two-block response.
      return f"""<?xml version="1.0"?>
<Blocks xmlns="{CFX_NS}">
  <CFXManagerVersion>3.1</CFXManagerVersion><User>t</User><RegistrationID>R</RegistrationID>
  <BlockArray><SerialNumber>SN-A</SerialNumber><Status>Idle</Status>
    <Step>0</Step><Steps>0</Steps><Cycle>0</Cycle><Cycles>0</Cycles>
    <SampleVolume><Volume>0</Volume><Units>u</Units></SampleVolume>
    <LidTemperature><Temperature>25</Temperature><Units>C</Units></LidTemperature>
    <BlockTemperature><Temperature>25</Temperature><Units>C</Units></BlockTemperature>
    <EstimatedRemainingRunTime>0</EstimatedRemainingRunTime><NickName/>
  </BlockArray>
  <BlockArray><SerialNumber>SN-B</SerialNumber><Status>Idle</Status>
    <Step>0</Step><Steps>0</Steps><Cycle>0</Cycle><Cycles>0</Cycles>
    <SampleVolume><Volume>0</Volume><Units>u</Units></SampleVolume>
    <LidTemperature><Temperature>25</Temperature><Units>C</Units></LidTemperature>
    <BlockTemperature><Temperature>25</Temperature><Units>C</Units></BlockTemperature>
    <EstimatedRemainingRunTime>0</EstimatedRemainingRunTime><NickName/>
  </BlockArray>
</Blocks>"""

    async def fake(msg: str) -> str:
      return _two_blocks(msg)

    b = CFXMaestroBackend(sidecar_url="http://localhost:9/x")
    b._session._xml_command = fake  # type: ignore[assignment]

    with _w.catch_warnings(record=True) as caught:
      _w.simplefilter("always")
      await b.setup()
    msgs = [str(w.message) for w in caught]
    self.assertTrue(any("2 connected instruments" in m for m in msgs), msgs)
    self.assertEqual(b.serial_number, "SN-A")  # first block adopted

  async def test_direct_setpoints_not_supported(self):
    with self.assertRaises(NotImplementedError):
      await self.backend.set_block_temperature([60.0])
    with self.assertRaises(NotImplementedError):
      await self.backend.set_lid_temperature([105.0])


class TestCFXChatterbox(unittest.IsolatedAsyncioTestCase):
  def setUp(self):
    self.backend = CFX384ChatterboxBackend()

  async def test_run_protocol_tracks_progress(self):
    from pylabrobot.thermocycling.standard import Protocol, Stage, Step

    protocol = Protocol(
      stages=[Stage(steps=[Step(temperature=[95.0], hold_seconds=10)], repeats=40)]
    )
    await self.backend.run_protocol(protocol, protocol_file="x.pcrd")
    self.assertEqual(await self.backend.get_total_cycle_count(), 40)

  async def test_lid(self):
    await self.backend.open_lid()
    self.assertTrue(await self.backend.get_lid_open())
    await self.backend.close_lid()
    self.assertFalse(await self.backend.get_lid_open())


class TestResource(unittest.TestCase):
  def test_resource(self):
    tc = cfx384(name="cfx", backend=CFX384ChatterboxBackend())
    self.assertEqual(tc.model, "BioRad_CFX384")


def _two_block_response(reg_id="REG-1") -> str:
  """Two-instrument QueryBlocks/RegisterService response for shared-session tests."""
  return f"""<?xml version="1.0"?>
<Blocks xmlns="{CFX_NS}">
  <CFXManagerVersion>3.1</CFXManagerVersion><User>t</User>
  <RegistrationID>{reg_id}</RegistrationID>
  <BlockArray><SerialNumber>SN-A</SerialNumber><Status>Idle</Status>
    <Step>0</Step><Steps>0</Steps><Cycle>0</Cycle><Cycles>0</Cycles>
    <SampleVolume><Volume>0</Volume><Units>u</Units></SampleVolume>
    <LidTemperature><Temperature>25</Temperature><Units>C</Units></LidTemperature>
    <BlockTemperature><Temperature>30</Temperature><Units>C</Units></BlockTemperature>
    <EstimatedRemainingRunTime>0</EstimatedRemainingRunTime><NickName>A</NickName>
  </BlockArray>
  <BlockArray><SerialNumber>SN-B</SerialNumber><Status>Idle</Status>
    <Step>0</Step><Steps>0</Steps><Cycle>0</Cycle><Cycles>0</Cycles>
    <SampleVolume><Volume>0</Volume><Units>u</Units></SampleVolume>
    <LidTemperature><Temperature>25</Temperature><Units>C</Units></LidTemperature>
    <BlockTemperature><Temperature>40</Temperature><Units>C</Units></BlockTemperature>
    <EstimatedRemainingRunTime>0</EstimatedRemainingRunTime><NickName>B</NickName>
  </BlockArray>
</Blocks>"""


class TestCFXMaestroSession(unittest.IsolatedAsyncioTestCase):
  def setUp(self):
    self.session = CFXMaestroSession(sidecar_url="http://localhost:9/x")
    self.sent = []

    async def fake(msg: str) -> str:
      self.sent.append(msg)
      return _two_block_response("REG-S")

    self.session._xml_command = fake  # type: ignore[assignment]

  async def test_connect_sets_registration_id_and_returns_blocks(self):
    resp = await self.session.connect()
    self.assertEqual(self.session.registration_id, "REG-S")
    self.assertEqual([b.serial_number for b in resp.blocks], ["SN-A", "SN-B"])

  async def test_double_connect_raises(self):
    await self.session.connect()
    with self.assertRaisesRegex(RuntimeError, "already connected"):
      await self.session.connect()

  async def test_disconnect_clears_registration_and_is_idempotent(self):
    await self.session.connect()
    await self.session.disconnect()
    self.assertEqual(self.session.registration_id, "")
    # second call is a no-op
    await self.session.disconnect()

  async def test_list_instruments_requires_connect(self):
    with self.assertRaisesRegex(RuntimeError, "not connected"):
      await self.session.list_instruments()

  async def test_async_context_manager(self):
    async with self.session as s:
      self.assertEqual(s.registration_id, "REG-S")
    self.assertEqual(self.session.registration_id, "")


class TestCFXMaestroBackendWithSession(unittest.IsolatedAsyncioTestCase):
  def setUp(self):
    self.session = CFXMaestroSession(sidecar_url="http://localhost:9/x")

    self.transport_calls = []

    async def fake(msg: str) -> str:
      self.transport_calls.append(msg)
      return _two_block_response("REG-S")

    self.session._xml_command = fake  # type: ignore[assignment]

  async def test_constructor_requires_url_xor_session(self):
    with self.assertRaisesRegex(ValueError, "exactly one"):
      CFXMaestroBackend()  # neither
    with self.assertRaisesRegex(ValueError, "exactly one"):
      CFXMaestroBackend(sidecar_url="http://x", session=self.session)  # both

  async def test_shared_backend_requires_explicit_serial(self):
    await self.session.connect()
    b = CFXMaestroBackend(session=self.session)  # no serial_number
    with self.assertRaisesRegex(ValueError, "must specify serial_number"):
      await b.setup()

  async def test_shared_backend_setup_validates_session_connected(self):
    b = CFXMaestroBackend(session=self.session, serial_number="SN-A")
    with self.assertRaisesRegex(RuntimeError, "not connected"):
      await b.setup()

  async def test_shared_backends_target_different_units(self):
    await self.session.connect()
    a = CFXMaestroBackend(session=self.session, serial_number="SN-A")
    b = CFXMaestroBackend(session=self.session, serial_number="SN-B")
    await a.setup()
    await b.setup()
    self.assertEqual(await a.get_block_current_temperature(), [30.0])
    self.assertEqual(await b.get_block_current_temperature(), [40.0])

  async def test_shared_backend_stop_does_not_disconnect_session(self):
    await self.session.connect()
    a = CFXMaestroBackend(session=self.session, serial_number="SN-A")
    await a.setup()
    await a.stop()
    self.assertEqual(self.session.registration_id, "REG-S")  # still connected

  async def test_concurrent_status_polls(self):
    """Two backends polling concurrently both succeed and target the right unit."""
    await self.session.connect()
    a = CFXMaestroBackend(session=self.session, serial_number="SN-A")
    b = CFXMaestroBackend(session=self.session, serial_number="SN-B")
    await asyncio.gather(a.setup(), b.setup())
    temps = await asyncio.gather(
      a.get_block_current_temperature(),
      b.get_block_current_temperature(),
    )
    self.assertEqual(temps, [[30.0], [40.0]])


if __name__ == "__main__":
  unittest.main()
