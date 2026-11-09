"""Температура CPU — единственное, чего нет в psutil на всех системах сразу.

Источники перебираются по очереди, первый ответивший побеждает:

* linux — ``psutil.sensors_temperatures()`` (hwmon), затем ``/sys/class/thermal``
  и ``/sys/class/hwmon`` напрямую: в контейнере psutil может не увидеть датчик,
  а sysfs читается и так;
* windows — WMI: LibreHardwareMonitor, OpenHardwareMonitor
  (``root/LibreHardwareMonitor``, ``root/OpenHardwareMonitor``) и, если ничего не
  запущено, ``MSAcpi_ThermalZoneTemperature`` — он есть не на всех платах.

Опрос WMI — это запуск powershell и сотни миллисекунд, поэтому результат (в том
числе «датчика нет») кэшируется: виджет спрашивает раз в poll, а датчик не
обязан быть быстрее.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
import threading
import time

try:
    import psutil           # уже установлен ради cpu/ram/disk; здесь — только hwmon
    assert psutil is not None
except (ImportError, AssertionError):   # pragma: no cover — зависит от окружения
    psutil = None

CACHE_TTL = 15.0           # чаще спрашивать датчик незачем
MAX_CELSIUS = 125.0        # выше и ниже нуля — уже не температура CPU, а мусор в датчике
WMI_TIMEOUT = 8.0

_LOCK = threading.Lock()
_CACHE: dict = {"at": 0.0, "value": None}

# имена датчиков hwmon: у intel — coretemp, у amd — k10temp/zenpower, у arm/soc — остальные
PREFERRED_SENSORS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal", "acpitz")
# зоны /sys/class/thermal, которые действительно про CPU
ZONE_HINTS = ("x86_pkg_temp", "cpu", "soc", "acpitz", "package")
# ярлыки «температура всего CPU», а не одного ядра
PACKAGE_HINTS = ("package", "tdie", "tctl", "average", "core max")

NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")

# пути sysfs вынесены в константы: их подменяют тесты, а не боевой код
THERMAL_GLOB = "/sys/class/thermal/thermal_zone*"
HWMON_GLOB = "/sys/class/hwmon/hwmon*"


def cpu_temperature(use_cache: bool = True) -> float | None:
    """Температура CPU в °C; None — датчика нет (или он молчит)."""
    if use_cache:
        with _LOCK:
            if time.time() - _CACHE["at"] < CACHE_TTL:
                return _CACHE["value"]
    value = _first_source()
    with _LOCK:
        _CACHE["at"], _CACHE["value"] = time.time(), value
    return value


def forget() -> None:
    """Сбросить кэш (тесты и смена окружения)."""
    with _LOCK:
        _CACHE["at"], _CACHE["value"] = 0.0, None


def sources() -> list:
    """Источники по порядку для текущей платформы."""
    found = [_from_psutil]
    if sys.platform.startswith("linux"):
        found += [_from_sysfs_thermal, _from_sysfs_hwmon]
    elif sys.platform == "win32":
        found += [_from_wmi]
    return found


def _first_source() -> float | None:
    for source in sources():
        try:
            value = source()
        except Exception:          # noqa: BLE001 — датчик не имеет права ронять виджет
            value = None
        if value is not None:
            return value
    return None


def _sane(value) -> float | None:
    """Правдоподобная температура или None: 0 — это «датчик молчит», а не «-273°"."""
    if value is None:
        return None
    try:
        celsius = float(value)
    except (TypeError, ValueError):
        return None
    if celsius <= 0.0 or celsius > MAX_CELSIUS:
        return None
    return round(celsius, 1)


def _entry_value(entry) -> float | None:
    return _sane(getattr(entry, "current", None))


def _from_psutil() -> float | None:
    """hwmon через psutil: сначала «пакет», потом самое горячее ядро."""
    reader = getattr(psutil, "sensors_temperatures", None)
    if reader is None:
        return None
    try:
        sensors = reader() or {}
    except (OSError, RuntimeError, NotImplementedError):
        return None
    for name in PREFERRED_SENSORS:
        value = _pick_sensor(sensors.get(name) or [])
        if value is not None:
            return value
    for name, entries in sensors.items():          # имя незнакомое, но про CPU — годится
        if any(hint in name.lower() for hint in ("cpu", "core", "soc", "pkg")):
            value = _pick_sensor(entries or [])
            if value is not None:
                return value
    return None


def _pick_sensor(entries) -> float | None:
    for entry in entries:
        if any(hint in (getattr(entry, "label", "") or "").lower() for hint in PACKAGE_HINTS):
            value = _entry_value(entry)
            if value is not None:
                return value
    values = [value for value in (_entry_value(entry) for entry in entries) if value is not None]
    return max(values) if values else None


def _read(path: str) -> str:
    with open(path, "r", encoding="ascii", errors="replace") as handle:
        return handle.read().strip()


def _from_sysfs_thermal() -> float | None:
    """``/sys/class/thermal/thermal_zone*/temp`` — то, что видно и в контейнере."""
    best = None
    for folder in sorted(glob.glob(THERMAL_GLOB)):
        try:
            zone = _read(os.path.join(folder, "type")).lower()
            raw = _read(os.path.join(folder, "temp"))
        except OSError:
            continue
        if not any(hint in zone for hint in ZONE_HINTS):
            continue
        value = _sane(float(raw) / 1000.0 if raw else None)   # millidegrees
        if value is not None and (best is None or value > best):
            best = value
    return best


def _from_sysfs_hwmon() -> float | None:
    """``/sys/class/hwmon/hwmon*/temp*_input`` — если psutil недоступен."""
    for folder in sorted(glob.glob(HWMON_GLOB)):
        try:
            name = _read(os.path.join(folder, "name")).lower()
        except OSError:
            continue
        if name not in PREFERRED_SENSORS:
            continue
        package = []
        others = []
        for path in sorted(glob.glob(os.path.join(folder, "temp*_input"))):
            try:
                value = _sane(float(_read(path)) / 1000.0)
            except (OSError, ValueError):
                continue
            if value is None:
                continue
            label_path = path.replace("_input", "_label")
            try:
                label = _read(label_path).lower()
            except OSError:
                label = ""
            (package if any(hint in label for hint in PACKAGE_HINTS) else others).append(value)
        chosen = (package or others)
        if chosen:
            return max(chosen)
    return None


COMPUTER = r"""
$ErrorActionPreference = 'SilentlyContinue'
foreach ($ns in 'root/LibreHardwareMonitor', 'root/OpenHardwareMonitor') {
  $t = Get-CimInstance -Namespace $ns -ClassName Sensor |
       Where-Object { $_.SensorType -eq 'Temperature' -and $_.Identifier -match 'cpu' }
  if ($t) {
    $preferred = $t | Where-Object { $_.Name -match 'Package|Tctl|Tdie|Average|Core Max' }
    if (-not $preferred) { $preferred = $t }
    $value = ($preferred | Measure-Object -Property Value -Maximum).Maximum
    [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0:F1}', $value)
    break
  }
}
if (-not $value) {
  $zone = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature
  if ($zone) {
    $kelvin = ($zone | Measure-Object -Property CurrentTemperature -Maximum).Maximum / 10
    $celsius = $kelvin - 273.15
    [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0:F1}', $celsius)
  }
}
"""


def _powershell(script: str) -> str:
    flags = 0x08000000 if sys.platform == "win32" else 0      # CREATE_NO_WINDOW
    done = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        capture_output=True, text=True, timeout=WMI_TIMEOUT, creationflags=flags,
    )
    return done.stdout or ""


def _from_wmi() -> float | None:
    """LibreHardwareMonitor/OpenHardwareMonitor, затем ACPI-зона Windows."""
    for line in reversed(_powershell(COMPUTER).splitlines()):
        match = NUMBER.search(line.strip())
        if match:
            return _sane(match.group(0).replace(",", "."))
    return None
