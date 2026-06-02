# Bio-Rad CFX (CFX Maestro API)

`CFXMaestroBackend` drives any Bio-Rad CFX real-time PCR system (CFX384, CFX96,
CFX Opus, …) through Bio-Rad's documented CFX Maestro Software API. The same
driver controls one instrument or four, locally on the CFX Maestro host or
remotely over the network from any OS that runs Python.

## Why a sidecar

CFX Maestro publishes its API as a `WSDualHttpBinding` WCF service using
WS-SecureConversation (SPNEGO message security), WS-ReliableMessaging, and a
duplex callback contract. That combination is effectively unreachable from
Python — `zeep` doesn't speak WS-SecureConversation, and the duplex callback
requires the client to host its own listener.

PyLabRobot solves this with a **small .NET 4.8 sidecar** that runs on the CFX
Maestro Windows host. The sidecar owns the WCF complexity and exposes one
trivial HTTP endpoint:

```
POST  /xmlcommand       Content-Type: application/xml
body: the <Message> XML document (built by PyLabRobot)
→ 200 OK
body: the <Blocks> XML document (CFX Maestro's XmlCommandResult)
```

All of the protocol logic — building `Message` XML, parsing `Blocks` XML,
mapping to the `ThermocyclerBackend` interface — lives in Python and is fully
unit-tested. The sidecar is a dumb pass-through that does not interpret the
XML.

```
PyLabRobot (any OS)            sidecar (Windows)              CFX Maestro
  CFXMaestroBackend  --HTTP-->  CfxMaestroSidecar.exe  --WCF/SOAP-->  WCF service
  build Message XML             dumb pass-through                    XmlCommand(xml)
  parse Blocks XML              + self-healing channel
```

The sidecar lives at `tools/cfx_maestro_sidecar/` in this repo.

## What `run_protocol` accepts

Unlike most PLR thermocyclers, the CFX Maestro API does **not** accept an
inline thermal profile. A run references **files that already exist on the CFX
Maestro host**:

- A protocol file (`.pcrd`, LIMS `.plrn`, or PrimePCR `.csv`), and
- A plate file (`.pltd`) — required when the protocol file is `.pcrd`,
  must be empty when it's `.csv` (PrimePCR runs are self-contained).

Author these once in CFX Maestro, save in a known location on the Windows host,
then reference by full Windows path forever:

```python
await backend.run_protocol(
    protocol_file=r"C:\protocols\my_qpcr.pcrd",
    plate_file=r"C:\protocols\my_qpcr_384.pltd",
    data_file=r"C:\runs\plate_42.pcrd",   # optional; empty = Maestro default
)
```

The base `ThermocyclerBackend.run_protocol` signature still takes a
`Protocol` object first (for compatibility); `CFXMaestroBackend` ignores it
and uses the file paths.

## Prerequisites

| Component | Where | Why |
|---|---|---|
| CFX Maestro Software | Windows host | provides the WCF service |
| .NET Framework 4.8 Developer Pack | Windows host | builds the sidecar (net48 target) |
| .NET SDK (8.x recommended) | Windows host | `dotnet build` for the sidecar |
| Python ≥ 3.9 + PyLabRobot | any OS | runs `CFXMaestroBackend` |

`WSDualHttpBinding` is not supported in .NET Core/5+, so the sidecar must
target .NET Framework 4.8.

## Setting up the sidecar

On the Windows host:

```cmd
git clone <your-pylabrobot-fork>
cd pylabrobot\tools\cfx_maestro_sidecar
dotnet build CfxMaestroSidecar.csproj -c Release
```

Start it in an **elevated** PowerShell (elevation lets WCF bind the
`+:8081/cfx-callback` duplex callback URL without a one-time `netsh urlacl`):

```powershell
.\bin\Release\net48\CfxMaestroSidecar.exe --listen http://localhost:8080/
```

For remote access from other machines on the LAN, open the firewall port and
bind to all interfaces:

```powershell
netsh advfirewall firewall add rule name="CFX sidecar" dir=in action=allow protocol=TCP localport=8080
.\bin\Release\net48\CfxMaestroSidecar.exe --listen http://+:8080/
```

Sanity-check from anywhere on the network:

```bash
curl http://<windows-host>:8080/health      # → ok
```

The sidecar uses a self-healing WCF channel manager: if the cached channel
faults (idle timeout, server hiccup, prior session ended abnormally), it
recreates the channel and retries the call once, logging
`[sidecar] channel faulted (...); recreating and retrying once.` to its
console. You should not have to restart it in normal operation.

## Quick start (single instrument)

```python
import asyncio
from pylabrobot.thermocycling import CFXMaestroBackend, cfx384

async def main():
    backend = CFXMaestroBackend(sidecar_url="http://<windows-host>:8080/xmlcommand")
    tc = cfx384(name="cfx_a", backend=backend)
    await tc.setup()                          # RegisterService + adopt first block

    print("status:", await tc.get_block_status())
    print("block temp:", await tc.get_block_current_temperature())
    print("lid open?:", await tc.get_lid_open())

    await tc.open_lid()
    await tc.close_lid()

    await tc.backend.run_protocol(
        protocol_file=r"C:\protocols\my_qpcr.pcrd",
        plate_file=r"C:\protocols\my_qpcr_384.pltd",
        data_file=r"C:\runs\plate_42.pcrd",
        run_id="plate_42",
    )
    await tc.wait_for_profile_completion(poll_interval=30)

    await tc.backend.stop()                   # UnRegisterService

asyncio.run(main())
```

`cfx384(name, backend)` returns a `Thermocycler` resource preset with CFX384
chassis dimensions. The same `CFXMaestroBackend` drives a CFX96 — it's the
plate that's geometry-specific, not the driver.

## Multi-instrument concurrent control: `CFXMaestroSession`

CFX Maestro enforces **one registered API client at a time**. To control
multiple instruments concurrently, all controllers must share one
registration. `CFXMaestroSession` owns that single registration; multiple
`CFXMaestroBackend` instances attach to it by passing `session=...`, each
targeting a different `serial_number`.

```python
import asyncio
from pylabrobot.thermocycling import CFXMaestroSession, CFXMaestroBackend, cfx384

CFX_FLEET = {
    "Jimdalf": "CT059744",
    "Eowyn":   "CT045747",
    "Gollum":  "CT020727",
    "Samwise": "CT041292",
}

async def main():
    async with CFXMaestroSession("http://<windows-host>:8080/xmlcommand") as session:
        # discover instruments (optional — you may already know the serials)
        blocks = await session.list_instruments()
        print("connected:", [b.serial_number for b in blocks])

        # one backend per unit, all sharing the session
        cfxs = {
            name: cfx384(
                name=f"cfx_{name.lower()}",
                backend=CFXMaestroBackend(session=session, serial_number=serial),
            )
            for name, serial in CFX_FLEET.items()
        }
        await asyncio.gather(*(tc.backend.setup() for tc in cfxs.values()))

        # status read across all four, concurrently
        statuses = await asyncio.gather(*(tc.get_block_status() for tc in cfxs.values()))
        print(dict(zip(CFX_FLEET, statuses)))

        # start a different protocol on each unit at the same time
        await asyncio.gather(
            cfxs["Jimdalf"].backend.run_protocol(
                protocol_file=r"C:\protocols\a.pcrd", plate_file=r"C:\protocols\a.pltd"
            ),
            cfxs["Eowyn"].backend.run_protocol(
                protocol_file=r"C:\protocols\b.pcrd", plate_file=r"C:\protocols\b.pltd"
            ),
        )

asyncio.run(main())
```

When a backend attaches to a shared session, `serial_number` is **required**
— auto-adoption is disabled to avoid silently targeting the wrong unit.

## Workcell integration

The `cfx384()` helper returns a `Thermocycler`, which is a `ResourceHolder` —
so plates assign onto it the same way they assign onto a carrier or a hotel.
A plate-moving arm (PF-400, Hamilton iSWAP, …) drops the plate onto the
thermocycler resource via the standard PLR resource API.

A complete STAR + PF-400 + 4×CFX384 orchestration:

```python
async def run_one_plate(plate, star, pf, cfxs):
    # 1. pipette the reaction on the STAR
    await star.transfer(sources=..., targets=plate.children, volume=...)

    # 2. find an idle CFX (poll all four in parallel via the shared session)
    while True:
        statuses = await asyncio.gather(*(tc.get_block_status() for tc in cfxs.values()))
        lids     = await asyncio.gather(*(tc.get_lid_open()     for tc in cfxs.values()))
        for (name, tc), s, lid in zip(cfxs.items(), statuses, lids):
            if s == BlockStatus.IDLE and not lid:
                break
        else:
            await asyncio.sleep(5); continue
        break

    # 3. open lid, wait for it to actually be open, then drop the plate
    await tc.open_lid()
    while not await tc.get_lid_open():
        await asyncio.sleep(0.5)
    await pf.pick_up_resource(plate)
    await pf.drop_resource(plate, destination=tc)
    await tc.close_lid()
    while await tc.get_lid_open():
        await asyncio.sleep(0.5)

    # 4. fire the protocol, wait for completion
    await tc.backend.run_protocol(
        protocol_file=PROTOCOL_FILE, plate_file=PLATE_FILE,
        data_file=fr"C:\runs\{plate.name}.pcrd", run_id=plate.name,
    )
    await tc.wait_for_profile_completion(poll_interval=30)

    # 5. open lid, retrieve the plate, close lid for the next round
    await tc.open_lid()
    while not await tc.get_lid_open():
        await asyncio.sleep(0.5)
    await pf.pick_up_resource(plate)
    await pf.drop_resource(plate, destination=finished_stack)
    await tc.close_lid()
```

While CFX `Jimdalf` is running, the PF-400 can move the next plate onto
`Eowyn`, the STAR can be filling a plate destined for `Gollum`, and so on —
all four CFXs run **truly in parallel** through one `CFXMaestroSession`.

## Diagnostic and exercise scripts

All under `tools/cfx_maestro_sidecar/`. Each accepts `--sidecar-url` and
(where relevant) `--serial-number` or `--serials` to target specific units.

| Script | What it does |
|---|---|
| `smoke_test.py` | RegisterService → QueryBlocks → UnRegisterService; prints the full parsed snapshot for every connected block. Safest first run. |
| `list_instruments.py` | Discovery: serial, model, well count, simulated flag, status, lid position, nickname. |
| `exercise_test.py` | Single instrument: OpenLid → CloseLid → RunProtocol → poll → optional Pause/Resume → optional Stop. `--dump-xml` logs every Message/Blocks pair. |
| `concurrent_test.py` | Multi-instrument via `CFXMaestroSession`: concurrent OpenLid/CloseLid/RunProtocol/StopRun across every targeted unit. Polls until each unit returns to Idle naturally; `--stop-after` is a safety cap that only stops unfinished units at the timeout. |
| `self_heal_test.py` | Validates the sidecar's self-healing channel: two register/query rounds separated by a configurable idle gap (default 11 min, exceeds WCF's 10-min `InactivityTimeout`). |

### Real-hardware multi-unit playbook

```powershell
# 1. discover what's connected (read-only; safe)
py tools\cfx_maestro_sidecar\list_instruments.py --sidecar-url http://localhost:8080/xmlcommand

# 2. validate read paths against each unit individually
foreach ($sn in 'CT059744','CT045747','CT020727','CT041292') {
  py tools\cfx_maestro_sidecar\smoke_test.py --sidecar-url http://localhost:8080/xmlcommand --serial-number $sn
}

# 3. exercise lid open/close on each idle unit
foreach ($sn in 'CT045747','CT020727','CT041292') {
  py tools\cfx_maestro_sidecar\exercise_test.py `
    --sidecar-url http://localhost:8080/xmlcommand --skip-run --serial-number $sn
}

# 4. concurrent run across every unit (PrimePCR CSV is self-contained)
py tools\cfx_maestro_sidecar\concurrent_test.py `
  --sidecar-url http://localhost:8080/xmlcommand --skip-lid `
  --protocol-file "C:\protocols\short_test.csv" --data-dir "C:\Temp\plr_concurrent"
```

## Status and error handling

Every backend method that triggers a state change (`open_lid`, `close_lid`,
`run_protocol`, `stop_run`, `pause_run`, `resume_run`) checks the response's
**per-block** error array (`InstrumentBlockType/ErrorArray`) and raises
`RuntimeError` with the CFX-side message if non-empty. For example, sending a
384-well plate file to a CFX96 surfaces:

```
RuntimeError: CFX Maestro instrument error on RunProtocol:
              [917557] A plate file with 384 wells cannot be run on a 96-well instrument.
```

Note that CFX Maestro sometimes reports operation errors **asynchronously** on
a *later* `QueryBlocks` poll rather than on the immediate response. To catch
those, monitor `b.errors` (where `b = await backend._status()`) during your
own polling loop. The exercise scripts do this automatically (lines marked
`!! instrument error:`).

The `Status` field maps to PLR's `BlockStatus` / `LidStatus`:

| CFX status | `get_lid_open()` | `get_block_status()` |
|---|---|---|
| `Idle` | `False` | `IDLE` |
| `Lid Open` | **`True`** | `HOLDING_AT_TARGET` |
| `Initializing`, `Running`, `Paused`, `Infinite Hold`, `Waiting Manual Start`, `Preserving` | `False` | `HOLDING_AT_TARGET` |

Cycle/step progress comes from the same `QueryBlocks` snapshot
(`b.cycle`, `b.cycles`, `b.step`, `b.steps`) so
`tc.wait_for_profile_completion()` works on real hardware identically to
Simulation.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `HTTPError 502 Bad Gateway` from Python | The WCF call threw inside the sidecar. Check the sidecar console for `[sidecar] request error: ...` or `channel faulted ...`. With the self-healing binary, this usually self-recovers; if it persists, restart the sidecar. |
| `CFX Maestro registration failed: Service not available. No more than 1 connection(s) allowed.` | A previous client didn't `UnRegisterService` (Python process killed mid-session, network blip, …). CFX Maestro holds the dangling registration. **Restart CFX Maestro** (close fully, reopen). A sidecar restart alone does not always clear it. |
| Lid status stays `Lid Open` after `CloseLid` | Real CFX384 lids take 8–10s to fully close. The default `--lid-wait 20` in the exercise scripts is sufficient; if you call `close_lid()` directly, wait with `while await tc.get_lid_open(): await asyncio.sleep(0.5)`. |
| `RunProtocol` returns OK but status stays `Idle` | The response was clean but a later poll surfaced an instrument-level error. Run the exercise script with `--dump-xml` and look at the next `Blocks` response for `ErrorArray` content. Most common cause: well-count mismatch between plate file and instrument. |
| `cannot bind http://+:8080/` | Sidecar isn't elevated and there's no URL ACL reservation. Either run elevated, or `netsh http add urlacl url=http://+:8080/ user="$env:USERDOMAIN\$env:USERNAME"`. |
| Sidecar build error: missing `net48` | Install the .NET Framework 4.8 Developer Pack alongside the .NET SDK. |

## Limitations

- **Direct setpoint commands are not supported.** The CFX Maestro API has no
  `SetBlockTemperature` / `SetLidTemperature` / `DeactivateBlock` /
  `DeactivateLid` operations — temperatures are dictated by the protocol
  file. Those backend methods raise `NotImplementedError`.
- **Hold time per step is not reported.** `get_hold_time()` raises
  `NotImplementedError`. `EstimatedRemainingRunTime` for the whole protocol
  is available via `backend.get_estimated_remaining_run_time()`.
- **One registered API client at a time.** Use `CFXMaestroSession` to share
  that registration across many backends; otherwise you can only drive one
  instrument at a time.

## API reference

- {class}`pylabrobot.thermocycling.CFXMaestroBackend`
- {class}`pylabrobot.thermocycling.CFXMaestroSession`
- {class}`pylabrobot.thermocycling.CFX384ChatterboxBackend` — device-free simulator
- {func}`pylabrobot.thermocycling.cfx384` — `Thermocycler` resource preset
