#
# Domoticz Plugin for IPMI / BMC sensor polling
# Connects out-of-band (RMCP+ / lanplus) to a BMC and publishes Fan (RPM),
# Voltage (V) and Temperature (degC) sensors as Domoticz devices.
#
"""
<plugin key="ipmi-bmc" name="IPMI BMC sensors" author="obedouet" version="0.1" wikilink="https://github.com/obedouet/domoticz-ipmi-plugin">
    <params>
        <param field="mAddress" label="BMC IP / host" width="200px" required="true" default="192.168.1.100"/>
        <param field="mPort" label="RMCP port" width="75px" required="true" default="623"/>
        <param field="Password1" label="User" width="150px" required="true" default="admin"/>
        <param field="Password2" label="Password" width="150px" required="true" default=""/>
        <param field="Mode1" label="Poll interval (s)" width="75px" required="true" default="60"/>
    </params>
</plugin>
"""

import Domoticz
import os
import re
import shutil
import subprocess
import threading
import time

# Domoticz device types
T_TEMP  = (80, 5)     # Temp
T_VOLT  = (243, 8)    # General / Voltage
T_FAN   = (243, 7)    # General / Fan

# what each accepted kind needs to build/update a device
KINDS = {
    "temp": {"types": T_TEMP, "label": "Temp", "fallback_custom": "1;C"},
    "volt": {"types": T_VOLT, "label": "Volt", "fallback_custom": "1;V"},
    "fan":  {"types": T_FAN,  "label": "Fan",  "fallback_custom": "1;RPM"},
}

# sensor 'type' strings reported by pyghmi -> kind
TYPE_MAP = {
    "temperature": "temp", "temp": "temp",
    "voltage": "volt", "volt": "volt",
    "fan": "fan", "tachometer": "fan",
}

_NAME_RE = re.compile(r"[^A-Za-z0-9 _./-]+")
# matches "45 degrees C", "1200 RPM", "12.100 Volts", "3.30 V", "24.5 deg C"
_READ_RE = re.compile(r"^(-?\d+(?:[.,]\d+)?)\s*([A-Za-z° ]+)$")

_UNITS = {
    "C": "temp", "°C": "temp", "DEGREES C": "temp", "DEG C": "temp",
    "RPM": "fan", "TACH": "fan",
    "V": "volt", "VOLTS": "volt", "VOLT": "volt", "M": "volt",
}

# readings that carry no value at all -> discard
_NA = {"ok", "na", "ns", "nc", "unknown", "not readable", "discrete"}


def sanitize(name):
    return _NAME_RE.sub("", str(name)).strip()[:64] or "sensor"

def parse_reading(field):
    """'45 degrees C' -> (45.0, 'temp'). Comma decimals ('12,06 Volts')
    are accepted too. Non-numeric/NA -> (None, None)."""
    f = " ".join(field.strip().split())
    if f.lower() in _NA:
        return None, None
    m = _READ_RE.match(f)
    if not m:
        return None, None
    key = m.group(2).strip().upper()
    kind = _UNITS.get(key)
    if kind is None:
        return None, None
    try:
        return float(m.group(1).replace(",", ".")), kind
    except ValueError:
        return None, None

def parse_sdr(text):
    """Yield (name, value, kind) from `ipmitool sdr list`/`sdr elist` output.

    `sdr list`  -> name | reading | status
    `sdr elist` -> name | id | status | entity | reading
    The reading column position differs between the two, so scan every
    column after the name and take the first field that parses as a
    numeric reading (status/id/entity columns never match)."""
    for line in text.splitlines():
        if "|" not in line:
            continue
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 2:
            continue
        name = sanitize(cols[0])
        if not name:
            continue
        for field in cols[1:]:           # reading is the first parseable field
            value, kind = parse_reading(field)
            if value is not None:
                yield name, value, kind
                break

class BasePlugin:
    def __init__(self):
        self.host = ""
        self.port = "623"
        self.priv = "USER"
        self.user = ""
        self.password = ""
        self.poll_interval = 60
        self.thread = None
        self.stop_event = threading.Event()
        self.data_lock = threading.Lock()
        self.units = {}            # sensor name -> unit number
        self.kinds = {}            # sensor name -> kind
        self.custom_units = set()  # units intentionally using custom fallback
        self.retype_failed = {}    # unit -> kind, after a failed type conversion
        return

    # ---- unit allocation: reuse existing devices by name ----------------
    def _free_unit(self):
        used = set(Devices.keys()) | set(self.units.values())
        u = 1
        while u in used:
            u += 1
        return u

    def _dev_subtype(self, dev):
        return getattr(dev, "SubType", getattr(dev, "Subtype", None))

    def _exact_name_match(self, dev, name):
        return (getattr(dev, "Name", "") or "").strip() == name

    def _suffix_name_match(self, dev, name):
        dev_name = (getattr(dev, "Name", "") or "").strip()
        return dev_name.endswith(" " + name) or dev_name.endswith(name)

    def _types_match(self, dev, kind):
        dtype, dsub = KINDS[kind]["types"]
        return getattr(dev, "Type", None) == dtype and self._dev_subtype(dev) == dsub

    def _is_fallback_custom(self, dev):
        return getattr(dev, "Type", None) == 243 and self._dev_subtype(dev) == 31

    def _options_custom(self, dev):
        options = getattr(dev, "Options", None)
        if isinstance(options, dict):
            return str(options.get("Custom", "")).strip()
        if hasattr(options, "get"):
            try:
                return str(options.get("Custom", "")).strip()
            except Exception:
                pass
        return ""

    def _is_matching_unit(self, unit, dev, kind):
        if self._types_match(dev, kind):
            return True
        if self._is_fallback_custom(dev):
            if unit in self.custom_units:
                return True
            return self._options_custom(dev) == KINDS[kind]["fallback_custom"]
        return False

    def _find_existing_unit(self, name, kind):
        fallback = None
        suffix_fallback = None

        for unit, dev in Devices.items():
            if self._exact_name_match(dev, name):
                if self._is_matching_unit(unit, dev, kind):
                    return unit
                if fallback is None:
                    fallback = unit
            elif self._suffix_name_match(dev, name):
                if self._is_matching_unit(unit, dev, kind):
                    return unit
                if suffix_fallback is None:
                    suffix_fallback = unit

        return fallback or suffix_fallback

    def _create_device(self, unit, name, kind):
        dtype, dsub = KINDS[kind]["types"]
        Domoticz.Device(
            Name=name, Unit=unit, Type=dtype, Subtype=dsub, Used=1,
        ).Create()

        dev = Devices.get(unit)
        if dev is not None and self._types_match(dev, kind):
            return True

        Domoticz.Log("Native %s type unavailable for '%s'; using custom sensor"
                     % (KINDS[kind]["label"], name))
        Domoticz.Device(
            Name=name, Unit=unit, Type=243, Subtype=31,
            Options={"Custom": KINDS[kind]["fallback_custom"]},
            Used=1,
        ).Create()

        dev = Devices.get(unit)
        if dev is not None and self._is_fallback_custom(dev):
            self.custom_units.add(unit)
            return True
        return False

    def _normalize_device(self, unit, kind):
        dev = Devices.get(unit)
        if dev is None or self._is_matching_unit(unit, dev, kind):
            return

        if self.retype_failed.get(unit) == kind:
            return

        dtype, dsub = KINDS[kind]["types"]
        nvalue = getattr(dev, "nValue", 0)
        svalue = getattr(dev, "sValue", "")

        try:
            try:
                dev.Update(
                    nValue=nvalue,
                    sValue=svalue,
                    Type=dtype,
                    Subtype=dsub,
                    Options={},
                    Used=1,
                )
            except TypeError:
                dev.Update(
                    nValue=nvalue,
                    sValue=svalue,
                    Type=dtype,
                    Subtype=dsub,
                    Used=1,
                )
            if self._types_match(dev, kind):
                Domoticz.Log("Retyped existing device '%s' (unit %d) as %s"
                             % (getattr(dev, "Name", ""), unit, KINDS[kind]["label"]))
                return
        except Exception as e:
            Domoticz.Error("Could not retype device unit %d: %s" % (unit, e))

        try:
            try:
                dev.Update(
                    nValue=nvalue,
                    sValue=svalue,
                    Type=243,
                    Subtype=31,
                    Options={"Custom": KINDS[kind]["fallback_custom"]},
                    Used=1,
                )
            except TypeError:
                dev.Update(
                    nValue=nvalue,
                    sValue=svalue,
                    Type=243,
                    Subtype=31,
                    Used=1,
                )
            if self._is_fallback_custom(dev):
                self.custom_units.add(unit)
                Domoticz.Log("Converted existing device '%s' (unit %d) to custom %s"
                             % (getattr(dev, "Name", ""), unit, KINDS[kind]["label"]))
                return
        except Exception as e:
            Domoticz.Error("Could not convert device unit %d to custom sensor: %s"
                           % (unit, e))

        self.retype_failed[unit] = kind
        Domoticz.Error("Could not change device unit %d to a supported type; leaving it unchanged" % unit)

    def _ensure_device(self, name, kind):
        unit = self.units.get(name)
        if unit is not None and unit in Devices:
            return unit

        if unit is not None:
            del self.units[name]

        unit = self._find_existing_unit(name, kind)
        if unit is None:
            unit = self._free_unit()
            if not self._create_device(unit, name, kind):
                Domoticz.Error("Could not create device '%s'" % name)
                return None
            Domoticz.Log("Created %s device '%s' (unit %d)"
                         % (KINDS[kind]["label"], name, unit))
        else:
            self._normalize_device(unit, kind)

        self.units[name] = unit
        self.kinds[name] = kind
        return unit

    def _update_device(self, unit, kind, svalue):
        if unit not in Devices:
            return

        dev = Devices[unit]
        if not self._is_matching_unit(unit, dev, kind):
            self._normalize_device(unit, kind)
            dev = Devices.get(unit)
            if dev is None:
                return

        try:
            try:
                dev.Update(nValue=0, sValue=svalue, TimedOut=0, Used=1)
            except TypeError:
                dev.Update(nValue=0, sValue=svalue)
            if hasattr(dev, "Touch"):
                dev.Touch()
        except Exception as e:
            Domoticz.Error("Update failed for unit %d: %s" % (unit, e))

    # ---- lifecycle ------------------------------------------------------
    def onStart(self):
        Domoticz.Log("Starting IPMI BMC sensors plugin")

        self.host = Parameters["mAddress"].strip()
        self.port = Parameters["mPort"].strip() or "623"
        self.user = Parameters["Password1"]
        self.password = Parameters["Password2"]
        try:
            self.poll_interval = max(10, int(Parameters["Mode1"]))
        except Exception:
            self.poll_interval = 60

        if not shutil.which("ipmitool"):
            Domoticz.Error("ipmitool not found -- install it (apt install ipmitool)")
            return
        if not self.host or not self.user:
            Domoticz.Error("BMC host and user are required")
            return

        Domoticz.Log("Polling %s via ipmitool lanplus every %ds"
                     % (self.host, self.poll_interval))
        if self.thread is None or not self.thread.is_alive():
            self.stop_event.clear()
            self.thread = threading.Thread(target=self.poll_loop)
            self.thread.daemon = True
            self.thread.start()
            Domoticz.Log("Polling thread started")

    def poll_loop(self):
        Domoticz.Log("Polling loop running (every %ds)" % self.poll_interval)
        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:            # never let the loop crash
                Domoticz.Error("Error in polling loop: %s" % e)
            self.stop_event.wait(self.poll_interval)
        Domoticz.Log("Polling loop stopped")

    def _run(self):
        """One ipmitool invocation. Returns stdout text, or None on failure."""
        env = dict(os.environ)
        env["IPMI_PASSWORD"] = self.password      # keep password out of ps(1)
        cmd = [
            "ipmitool", "-I", "lanplus",
            "-H", self.host, "-p", str(self.port),
            "-U", self.user, "-E", "-L", self.priv,
            "sdr", "elist",
        ]
        try:
            res = subprocess.run(
                cmd, env=env, timeout=30,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except subprocess.TimeoutExpired:
            Domoticz.Error("ipmitool timeout on %s" % self.host)
            return None
        except Exception as e:
            Domoticz.Error("ipmitool failed: %s" % e)
            return None

        out = res.stdout.decode("utf-8", "replace")
        if res.returncode != 0 or not out.strip():
            err = res.stderr.decode("utf-8", "replace").strip()
            Domoticz.Error("ipmitool rc=%d: %s" % (res.returncode, err))
            return None                          # keep previous values
        return out

    def poll_once(self):
        text = self._run()
        if text is None:
            return

        seen = set()
        for name, value, kind in parse_sdr(text):
            if name in seen:                    # SDR duplicates -> first wins
                continue
            seen.add(name)
            with self.data_lock:
                unit = self._ensure_device(name, kind)
                if unit is None or unit not in Devices:
                    continue
                svalue = str(round(value, 2))
                self._update_device(unit, kind, svalue)

    # ---- callbacks ----------------------------
    def onConnect(self, Status, Description):
        Domoticz.Log("onConnect called with status: %s" % Status)

    def onMessage(self, Data):
        pass  # IPMI is polled out-of-band; no inbound push channel

    def onStop(self):
        Domoticz.Log("Stopping IPMI BMC sensors plugin")
        if self.thread is not None:
            self.stop_event.set()
            self.thread.join(timeout=5)
            Domoticz.Log("Polling thread stopped")

    def onHeartbeat(self):
        pass


# ── Plugin Instance ───────────────────────────────────────────────────────────
plugin = BasePlugin()

# ── Callbacks ────────────────────────────────────────────────────────────────
def onStart():
    plugin.onStart()

def onConnect(Status, Description):
    plugin.onConnect(Status, Description)

def onMessage(Data):
    plugin.onMessage(Data)

def onDisconnect():
    plugin.onStop()

def onHeartbeat():
    plugin.onHeartbeat()

def onStop():
    plugin.onStop()
