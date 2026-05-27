@echo off
REM Optional: regenerate the WCF proxy from a live CFX Maestro instance instead
REM of using the hand-written contract in Program.cs.
REM
REM Run on the Windows host while CFX Maestro is running (User or Simulation mode).
REM Requires the Windows SDK / Visual Studio (svcutil.exe on PATH).
REM
REM If you use the generated proxy, delete the ISOCFXCommandService /
REM ISOCFXCommandServiceCallback interfaces from Program.cs to avoid duplicates.

svcutil.exe ^
  http://localhost:8003/BioRad.PCR.CommandManager/SOCFXCommandService?singleWsdl ^
  /async ^
  /namespace:*,CfxMaestroSidecar.Generated ^
  /out:SOCFXCommandService.cs ^
  /config:App.generated.config

echo.
echo Generated SOCFXCommandService.cs and App.generated.config.
echo Merge the binding from App.generated.config into App.config if you prefer
echo configuration-driven bindings over the programmatic binding in Program.cs.
