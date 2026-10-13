# fpv_telemetry_taysinc

Telemetry for Taysinc — a always-on-top desktop overlay showing the state of a
Taisync FPV link: signal, link quality, antennas, temperatures, ping latency,
distance, and uplink/downlink traffic.

Current version: **`fpv_telemetry_v1_39.py`** (earlier versions are kept for
reference).

## Running it

**The easy way — no Python needed.** Install or unzip a build (see *Downloads*
below) and double-click `TaisyncTelemetry.exe`.

**From source:**

```bash
pip install -r requirements.txt
python fpv_telemetry_v1_39.py
```

## Test data (no hardware)

Press the **TEST** button in the title bar, or the **`D`** key. The app then
feeds itself a simulated 3-minute flight: the aircraft flies out to ~2.8 km and
back, signal and link quality degrade with range, temperatures climb, errors
appear at the far end and the link drops out at the turnaround. That exercises
every card, colour threshold and warning without a radio attached.

While it is on, the window title reads **TEST DATA (simulated)** and the button
is highlighted, so simulated numbers can't be mistaken for real telemetry. Real
telemetry is ignored until you turn it back off.

## Reading the display

| Card | What it shows |
|---|---|
| **AIR / GROUND SIGNAL** | RSSI of **both** antennas (RF1, RF2) as bars with dBm; the dot marks the active antenna. The pill shows the module's configured **TX power** (dBm and mW). The % in the title is the stronger antenna on a −95…−45 dBm scale. |
| **AIR / GROUND QUALITY** | **T** — computed here: the worse of SNR and the FEC (LDPC) error rate over the last ~6 s. **P** — the link quality the unit reports itself. |
| **LINK** | Modulation (MCS), channel capacity, frequency, current downlink / uplink. |
| **CONTROL** | Bind status, SBUS / PPM frames per second, RC ping. |
| **TEMP**, **LAT** | AD9361 transceiver temperature; ping latency to each unit. |

The compact overlay (**COMPACT**) keeps both antennas and the TX power of
each side on screen: `AIR · TX 1` over `-29 / -20`.

The unit reports LDPC fails/passes, TX overflows and RX discards as totals since
power-on. Quality and warnings use how much those totals moved recently, so a
clean link reads 100 % however long the session, and a warning such as
`GND TX overflow +3` clears by itself a few seconds after the overflows stop.

The signal bar scale is `RSSI_MIN_DBM` / `RSSI_MAX_DBM` at the top of the
script, if it needs tuning for a different link.

## Settings

**SET** in the title bar configures the Ground / Air / RC IPs. The API URL is
derived as `http://<ground_ip>/v2/baseband.json`.

Settings and CSV logs are stored per user in:

```
%APPDATA%\TaisyncTelemetry\
    telemetry_settings.json
    logs\
    ping_debug.log
```

A `telemetry_settings.json` sitting in the working directory still takes
precedence, so existing source checkouts keep their configuration.

## Hotkeys

| Key | Action |
|---|---|
| `T` | Always on top on/off |
| `L` | Lock / unlock dragging |
| `R` | CSV logging on/off |
| `D` | Test data on/off |
| `Ctrl` `+` / `-` | Zoom in / out |
| `Ctrl` `0` | Reset zoom |

## Building

Requires Python 3.10+ and PyInstaller. For the installer, also
[Inno Setup 6](https://jrsoftware.org/isdl.php).

```powershell
.\build.ps1              # exe + portable zip
.\build.ps1 -Installer   # also compile the installer
.\build.ps1 -SkipTest    # skip the post-build selftest
```

Output:

| Path | What it is |
|---|---|
| `dist\TaisyncTelemetry\TaisyncTelemetry.exe` | the application (~136 MB folder) |
| `dist_zip\TaisyncTelemetry-1.39-portable.zip` | unzip and run, no install (~54 MB) |
| `dist_installer\TaisyncTelemetry-Setup-1.39.exe` | installer (~39 MB) |

If `ISCC.exe` is not in one of the usual locations, point `build.ps1` at it with
the `ISCC` environment variable.

The build is one-folder rather than one-file on purpose: a one-file build
unpacks ~136 MB of Qt into a temp directory on every launch, which costs several
seconds of startup and often trips antivirus heuristics.

### Tests

```bash
python tests/test_telemetry.py
```

Headless regression suite (also runs under pytest). The fixture
`tests/fixtures/live_samples.json` is a real capture from the ground unit, so
the counter-related bugs are tested against the hardware's actual behaviour.
`build.ps1` runs the suite first and refuses to build if anything fails.

Setting `TAISYNC_OFFLINE=1` builds the UI without polling or pinging anything;
the tests use it so live hardware on the network can't leak into them.

### Verifying a build

```powershell
.\dist\TaisyncTelemetry\TaisyncTelemetry.exe --selftest
```

Builds the window, feeds it simulated data, cycles every tab and layout
breakpoint, and checks the icon and the writable data directory. Exit code `0`
means the build is good; a report is written to
`%APPDATA%\TaisyncTelemetry\selftest.txt`. `build.ps1` runs this automatically
and refuses to package a build that fails.

## Installing on another computer

Use either artifact:

- **Installer** — defaults to a per-user install, so it needs no administrator
  rights on the target machine. Adds a Start Menu entry and an uninstaller;
  a desktop shortcut is optional. Uninstalling leaves `%APPDATA%\TaisyncTelemetry`
  in place so a reinstall keeps the configured IPs.
- **Portable zip** — unpack anywhere and run `TaisyncTelemetry.exe`. Nothing is
  written outside `%APPDATA%\TaisyncTelemetry`.

Both are unsigned, so Windows SmartScreen will show a "Windows protected your
PC" prompt on first run — *More info* → *Run anyway*. Getting rid of that needs
a code-signing certificate.
