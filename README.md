# Domoticz IPMI / BMC sensor plugin

Polls a Baseboard Management Controller (BMC) out-of-band over IPMI and
publishes its **Fan (RPM)**, **Voltage (V)** and **Temperature (°C)** sensors
as Domoticz devices. Sensor devices are created automatically from whatever
the BMC reports — you just point the plugin at an IP, user and password.

Works with any BMC that exposes a readable SDR (`ipmitool sdr elist`), which
covers most server/workstation motherboards with IPMI/iDRAC/iLO/AMT/BMC.

## Why `ipmitool` and not `pyghmi`?

The natural choice in Python is [`pyghmi`](https://github.com/brendan-sherman/pyghmi),
but it (via its `cryptography`/Rust backend) fails to load inside Domoticz with:

```
ImportError: PyO3 modules do not yet support subinterpreters
```

Domoticz loads each plugin in its own **Python subinterpreter**, and PyO3-built
native modules are not multi-interpreter safe — there is nothing the plugin can
do to work around this. So the plugin talks to the BMC through the external
`ipmitool` binary and parses its text output. No native Python module is ever
imported into Domoticz's interpreter, so the plugin always loads.

## Features

- Out-of-band polling via `ipmitool -I lanplus` (RMCP+), IPMI v2 / 20-byte passwords supported.
- Auto-creates one Domoticz device per **numeric** Fan / Voltage / Temperature sensor found in the SDR.
- Discards non-numeric / `na` / `not readable` / discrete readings — nothing is written on a bad sample.
- A failed poll (timeout, auth error, empty output) keeps the last-known values instead of blanking your dashboard.
- Device units are stable across restarts: existing devices are reused by name, so SDR discovery-order changes never orphan history.
- Password is passed via the `IPMI_PASSWORD` environment variable + `-E`, never on the command line, so it does not appear in `ps`.

## Requirements

- Domoticz with plugin support and a Python 3 interpreter.
- The **`ipmitool`** system package installed on the machine running Domoticz:

```bash
# Debian / Ubuntu / Raspberry Pi OS
sudo apt install ipmitool

# Fedora / RHEL
sudo dnf install ipmitool
```

- The BMC must allow out-of-band IPMI over the network (`lanplus`) for the
  user you configure.

> No pip packages are required — the plugin uses only the Python standard
> library and Domoticz's built-in module.

## Installation

1. Copy `plugin.py` into Domoticz's `plugins/` directory
   (e.g. `~/domoticz/plugins/domoticz-ipmi-plugin/plugin.py`).
2. Restart Domoticz.
3. Go to **Setup → Hardware → Add**.
4. Select **Type → "IPMI BMC sensors"** and fill in the parameters below.
5. Save. Devices appear within one poll interval; subsequent values update on
   the configured cadence.

## Configuration

| Field | Label | Default | Description |
|-------|-------|---------|-------------|
| `mAddress` | BMC IP / host | `192.168.1.100` | Hostname or IPv4 of the BMC |
| `mPort` | RMCP port | `623` | Standard IPMI RMCP+ port |
| `Password1` | User | `admin` | IPMI username (entered in this field — the field name is misleading but it is a plain text box) |
| `Password2` | Password | *(empty)* | IPMI password. Sent via `IPMI_PASSWORD`, not on the command line |
| `Mode1` | Poll interval (s) | `60` | How often to read the SDR; a minimum of 10 s is enforced |

**Privilege level** is fixed to `USER`, which is sufficient to read sensors on
most BMCs. If your BMC requires a higher level, change `self.priv` in
`onStart()` (e.g. to `ADMINISTRATOR`).

### Sanity-check outside Domoticz

Before wiring it in, confirm your credentials and that the sensors are
readable:

```bash
export IPMI_PASSWORD='yourpassword'
ipmitool -I lanplus -H 192.168.1.100 -U admin -E sdr elist
```

Anything that prints a numeric reading with a unit will become a Domoticz
device.

## How it works

- `ipmitool ... sdr elist` is invoked once per poll. Its `|`-delimited output is
  parsed; each row whose reading matches a numeric value + a recognised unit
  becomes a device.
- Reading kind is decided from the unit token: `C/°C/degrees C → temperature`,
  `RPM/TACH → fan`, `V/Volts → voltage`. Anything else is skipped.
- Device mapping:
  - Temperature → Domoticz `Type=80, Subtype=5` (native temp).
  - Voltage → Domoticz `Type=243, Subtype=8` (native voltage), with a
    custom-sensor fallback of `Type=243, Subtype=31` and `Custom=1;V`.
  - Fan → Domoticz `Type=243, Subtype=7` (native fan), with a custom-sensor
    fallback of `Type=243, Subtype=31` and `Custom=1;RPM`.
- Devices are created lazily on first valid reading, under a lock, with
  `Used=1`, and are updated every poll. Stable readings still get a `Touch()`
  so Domoticz keeps the device's `LastUpdate` timestamp fresh even when the
  value itself has not changed.
- Existing devices are reused by sensor name and, when necessary, by the
  hardware-prefixed display name suffix. Devices left from older plugin
  versions are retyped to the current native type when Domoticz accepts that
  type; otherwise they are converted to the custom-sensor fallback.

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `ImportError: PyO3 modules do not yet support subinterpreters` | You tried a PyO3-based IPMI lib (`pyghmi`). This plugin avoids it by using `ipmitool`. Make sure no such import is added. |
| `ipmitool not found` | Install `ipmitool` on the Domoticz host (`apt install ipmitool`). |
| `ipmitool rc=1: Error: Unable to establish IPMI v2 / RMCP+ session` | Wrong user/password, or BMC doesn't allow lanplus / network IPMI. Verify with the `ipmitool` sanity-check above. |
| No devices appear | The sensor's reading isn't numeric or uses an unrecognised unit; or the SDR is empty. Check `sdr elist` output. |
| Only temperature shows, no fans/voltages | Those sensors may report `na`/discrete on your BMC, or your privilege level can't read them — try `ADMINISTRATOR`. |
| Devices get new units on restart | Should not happen (units reused by name). If you renamed a sensor on the BMC, it's treated as new. |

## Security note

The IPMI password is stored in Domoticz's hardware settings (stored on disk in
Domoticz's usual way) and is passed to `ipmitool` via the environment rather
than the command line so it is not visible in the process list. For stronger
protection, run Domoticz on a trusted host and restrict network access to the
BMC.

## License

Apache 2.0 - see LICENSE
