# CFX Maestro Sidecar

A tiny Windows bridge that lets PyLabRobot (Python, any OS) drive a Bio-Rad CFX
real-time PCR system through the **CFX Maestro / CFX Manager WCF API**.

## Why this exists

The CFX Maestro API is published with `WSDualHttpBinding` using
WS-SecureConversation (SPNEGO/Windows auth, message-level encryption),
WS-ReliableMessaging, and a duplex callback contract. None of that is reachable
from a hand-rolled Python SOAP client or from `zeep`. This sidecar owns the WCF
complexity and exposes one trivial HTTP endpoint that PyLabRobot talks to.

```
PyLabRobot (any OS)            this sidecar (Windows)              CFX Maestro
  CFXMaestroBackend  --HTTP-->  CfxMaestroSidecar.exe  --WCF/SOAP-->  WCF service
  build Message XML             dumb pass-through                    XmlCommand(xml)
  parse Blocks  XML             (no XML parsing here)
```

## HTTP contract

| Method | Path          | Body (in)                  | Body (out)                |
|--------|---------------|----------------------------|---------------------------|
| POST   | `/xmlcommand` | `<Message>` XML document   | `<Blocks>` XML document   |
| GET    | `/health`     | —                          | `ok`                      |

`502` is returned if the underlying WCF call fails (message in the body).

## Build (on the CFX Maestro Windows host)

Requires the **.NET Framework 4.8** developer pack. .NET Core / .NET 5+ will NOT
work — they dropped `WSDualHttpBinding` and WS-SecureConversation message
security.

```cmd
dotnet build -c Release
```

or open `CfxMaestroSidecar.csproj` in Visual Studio 2019+ (.NET desktop workload).

## Run

Start CFX Maestro first (User mode, or the "CFX Maestro (Simulation)" shortcut
for hardware-free testing), then:

```cmd
CfxMaestroSidecar.exe ^
  --service-url http://localhost:8003/BioRad.PCR.CommandManager/SOCFXCommandService ^
  --listen http://+:8080/ ^
  --callback-base http://localhost:8081/cfx-callback
```

All flags are optional; the defaults above are assumed when omitted.

- `--listen http://+:8080/` accepts connections from other hosts. Binding `+`
  (all interfaces) on Windows may require admin rights or a one-time URL
  reservation:
  ```cmd
  netsh http add urlacl url=http://+:8080/ user=%USERDOMAIN%\%USERNAME%
  ```
  Use `http://localhost:8080/` if the Python client runs on the same machine.

## First validation: smoke test

After CFX Maestro and the sidecar are both running, confirm the round-trip with
the bundled script (RegisterService → QueryBlocks → UnRegisterService, printing
parsed status for every connected block):

```bash
python smoke_test.py --sidecar-url http://<windows-host>:8080/xmlcommand
```

This is the fastest way to verify the `Message`/`Blocks` namespace and element
mapping against a real instance. If a field comes back empty or wrong, it points
straight at the parser in `cfx_maestro.py`.

## Point PyLabRobot at it

```python
from pylabrobot.thermocycling import CFXMaestroBackend, cfx384

backend = CFXMaestroBackend(sidecar_url="http://<windows-host>:8080/xmlcommand")
tc = cfx384(name="cfx", backend=backend)
await tc.setup()                 # RegisterService; adopts the first block's serial
print(await tc.get_block_status())
await tc.backend.run_protocol(
    protocol_file=r"C:\Path\To\protocol.pcrd",
    plate_file=r"C:\Path\To\plate.pltd",
    data_file=r"C:\Path\To\out.pcrd",
)
```

## Real hardware: multiple CFX384 units

CFX Maestro can host many instruments behind one sidecar; you target each one
by **base serial number**. The driver itself works unchanged on real hardware
(the Simulation milestone validated every state-changing operation against the
live API).

### Step 1 — bring CFX Maestro up with all instruments connected

- Power on every CFX384 and plug it into the host's USB.
- Launch CFX Maestro (User mode). Wait until every instrument appears in its
  detected-instruments list and reports an Idle status (real units go through
  `Synchronizing → Idle` after CFX Maestro starts).
- Start `CfxMaestroSidecar.exe` exactly as for the Simulation tests.

### Step 2 — discover what's connected

```powershell
py tools\cfx_maestro_sidecar\list_instruments.py --sidecar-url http://localhost:8080/xmlcommand
```

Sample output:
```
[list] 2 instrument(s) connected:
  [0] serial=12345  model=CFX384  wells=384 (16x24)  simulated=false  status='Idle'  lid=CLOSED  nickname=BenchA
  [1] serial=67890  model=CFX384  wells=384 (16x24)  simulated=false  status='Idle'  lid=CLOSED  nickname=BenchB
```

Use the **`serial`** values for every subsequent command. Confirm
`model=CFX384` and `wells=384`; the driver doesn't care about chassis but your
**plate (`.pltd`) files must match** the instrument's well count, or CFX Maestro
will reject `RunProtocol` with *"plate file with N wells cannot be run on an
M-well instrument"*.

### Step 3 — exercise each unit one at a time

```powershell
# read-only smoke test against unit A
py tools\cfx_maestro_sidecar\smoke_test.py --sidecar-url http://localhost:8080/xmlcommand --serial-number 12345

# full lid + run + pause/resume/stop exercise against unit A
py tools\cfx_maestro_sidecar\exercise_test.py --sidecar-url http://localhost:8080/xmlcommand --serial-number 12345 `
   --protocol-file "C:\path\to\your_384.pcrd" --plate-file "C:\path\to\your_384.pltd" `
   --data-file C:\Temp\runA.pcrd --run-wait 60 --pause-resume --stop-after

# repeat against unit B
py tools\cfx_maestro_sidecar\exercise_test.py --sidecar-url http://localhost:8080/xmlcommand --serial-number 67890 ...
```

A run on real hardware takes the protocol's full duration (typically 30–120
minutes), not seconds. Either let `exercise_test.py --run-wait` run that long,
or attach to status polling from your own code via `await tc.get_block_status()`
and `await tc.is_profile_running()`.

### Notes on the single-client rule

CFX Maestro only allows **one registered API client at a time**. Each
`CFXMaestroBackend.setup()` registers, and `stop()` unregisters. To control
multiple instruments **concurrently** (e.g. start a run on A and B in
parallel), you'd want a shared session — open an issue / ping me and we'll
add a `CFXMaestroSession` that holds one registration and serves multiple
backends. For **sequential** real-hardware testing (one instrument at a time),
the current code is sufficient — just call `setup`/`stop` per backend.

If a previous Python process died without unregistering, the next `setup()`
will fail. Restart CFX Maestro to clear the dangling registration.

## Troubleshooting

- **Python sees `HTTP Error 502: Bad Gateway`** — the WCF call threw inside the
  sidecar. Check the sidecar console for the `[sidecar] request error: ...` line,
  which carries the real exception. The most common cause is a faulted WCF
  channel (WSDualHttpBinding reliable sessions fault on timeout or after a prior
  error); the sidecar now detects this and recreates the channel automatically,
  retrying the call once. If 502s persist, the message usually points at a
  CFX-side issue (e.g. a stale registration — restart CFX Maestro, since only one
  client may be registered at a time).

## Notes

- The sidecar keeps a single WCF channel and reuses it while healthy, recreating
  it on fault (`ClientManager`). Calls are serialized — the duplex/reliable
  session is not built for concurrent callers.
- The duplex `OnServiceIsClosing` callback is reserved for Bio-Rad internal use;
  `CallbackStub` implements it as a no-op, which is all the contract needs.
- Only one client may be registered with CFX Maestro at a time. `setup()`
  registers and `stop()` unregisters — always call `stop()` so the next client
  can connect.
- The WCF contract is hand-written in `Program.cs`. To regenerate it from the
  live service instead, run `generate-proxy.cmd` (uses `svcutil`).
