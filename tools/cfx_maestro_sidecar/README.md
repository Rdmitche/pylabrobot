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
