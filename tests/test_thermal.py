"""Температура CPU: hwmon, sysfs, WMI и кэш — без железа, на подставных файлах."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from vp import thermal


def _temp(value):
    return type("Temp", (), {"label": "", "current": value})()


def _labelled(label, value):
    return type("Temp", (), {"label": label, "current": value})()


class ThermalTestCase(unittest.TestCase):
    def setUp(self):
        thermal.forget()
        self.addCleanup(thermal.forget)

    def _dir(self, tag, **files):
        """Каталог вида ``.../hwmon0`` с файлами датчика; возвращает (корень, каталог)."""
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        folder = os.path.join(root, tag)
        os.makedirs(folder)
        for name, text in files.items():
            with open(os.path.join(folder, name), "w", encoding="ascii") as handle:
                handle.write(text)
        return root, folder

    # ── hwmon через psutil ───────────────────────────────────────

    def test_package_sensor_wins_over_cores(self):
        sensors = {"coretemp": [_labelled("Core 0", 55.0), _labelled("Package id 0", 65.0)]}
        with mock.patch.object(thermal.psutil, "sensors_temperatures", lambda: sensors, create=True):
            self.assertEqual(thermal.cpu_temperature(use_cache=False), 65.0)

    def test_hottest_core_is_used_when_there_is_no_package_line(self):
        sensors = {"k10temp": [_labelled("Tctl", 61.0), _labelled("Tccd1", 55.0)]}
        with mock.patch.object(thermal.psutil, "sensors_temperatures", lambda: sensors, create=True):
            self.assertEqual(thermal.cpu_temperature(use_cache=False), 61.0)

    def test_sensor_with_an_unfamiliar_name_still_counts_if_it_is_about_cpu(self):
        sensors = {"cpu_thermal_custom": [_temp(48.0)]}
        with mock.patch.object(thermal.psutil, "sensors_temperatures", lambda: sensors, create=True):
            self.assertEqual(thermal.cpu_temperature(use_cache=False), 48.0)

    def test_no_sensor_at_all_is_none_not_an_error(self):
        with mock.patch.object(thermal.psutil, "sensors_temperatures", lambda: {}, create=True):
            self.assertIsNone(thermal._from_psutil())

    # ── sysfs ────────────────────────────────────────────────────

    def test_thermal_zone_gives_celsius_from_millidegrees(self):
        root, _folder = self._dir("thermal_zone0", type="x86_pkg_temp", temp="54000")
        with mock.patch.object(thermal, "THERMAL_GLOB", os.path.join(root, "thermal_zone*")):
            self.assertEqual(thermal._from_sysfs_thermal(), 54.0)

    def test_thermal_zone_ignores_non_cpu_zones(self):
        root, _folder = self._dir("thermal_zone0", type="iwlwifi_1", temp="90000")
        with mock.patch.object(thermal, "THERMAL_GLOB", os.path.join(root, "thermal_zone*")):
            self.assertIsNone(thermal._from_sysfs_thermal())

    def test_thermal_zone_takes_the_hottest_cpu_zone(self):
        root, _folder = self._dir("thermal_zone0", type="x86_pkg_temp", temp="44000")
        for name, zone_type, value in (("thermal_zone1", "cpu-1-0", "49000"),
                                       ("thermal_zone2", "gpu", "80000")):
            folder = os.path.join(root, name)
            os.makedirs(folder)
            with open(os.path.join(folder, "type"), "w", encoding="ascii") as handle:
                handle.write(zone_type)
            with open(os.path.join(folder, "temp"), "w", encoding="ascii") as handle:
                handle.write(value)
        with mock.patch.object(thermal, "THERMAL_GLOB", os.path.join(root, "thermal_zone*")):
            self.assertEqual(thermal._from_sysfs_thermal(), 49.0)   # gpu не в счёт

    def test_hwmon_reads_package_label(self):
        root, _folder = self._dir("hwmon0", name="coretemp\n", temp1_input="50000\n",
                                  temp1_label="Core 0\n", temp2_input="70000\n",
                                  temp2_label="Package id 0\n")
        with mock.patch.object(thermal, "HWMON_GLOB", os.path.join(root, "hwmon*")):
            self.assertEqual(thermal._from_sysfs_hwmon(), 70.0)

    def test_hwmon_ignores_unknown_chips(self):
        root, _folder = self._dir("hwmon0", name="nvme\n", temp1_input="80000\n")
        with mock.patch.object(thermal, "HWMON_GLOB", os.path.join(root, "hwmon*")):
            self.assertIsNone(thermal._from_sysfs_hwmon())

    # ── windows: WMI через powershell ────────────────────────────

    def test_wmi_output_is_parsed(self):
        with mock.patch.object(thermal, "_powershell", lambda script: "65.5\r\n"):
            self.assertEqual(thermal._from_wmi(), 65.5)

    def test_wmi_comma_decimals_are_understood(self):
        with mock.patch.object(thermal, "_powershell", lambda script: "61,5\n"):
            self.assertEqual(thermal._from_wmi(), 61.5)

    def test_wmi_silence_is_none(self):
        with mock.patch.object(thermal, "_powershell", lambda script: ""):
            self.assertIsNone(thermal._from_wmi())

    def test_wmi_garbage_is_none(self):
        with mock.patch.object(thermal, "_powershell", lambda script: "Invalid namespace\n"):
            self.assertIsNone(thermal._from_wmi())

    def test_nonsense_reading_is_rejected(self):
        self.assertIsNone(thermal._sane(0))
        self.assertIsNone(thermal._sane(-5))
        self.assertIsNone(thermal._sane(300))
        self.assertEqual(thermal._sane(65.44), 65.4)

    # ── кэш ──────────────────────────────────────────────────────

    def test_temperature_is_asked_once_per_ttl(self):
        calls = []

        def source():
            calls.append(1)
            return 50.0
        with mock.patch.object(thermal, "sources", lambda: [source]):
            self.assertEqual(thermal.cpu_temperature(), 50.0)
            self.assertEqual(thermal.cpu_temperature(), 50.0)
        self.assertEqual(len(calls), 1)

    def test_forget_drops_the_cache(self):
        calls = []

        def source():
            calls.append(1)
            return 50.0
        with mock.patch.object(thermal, "sources", lambda: [source]):
            thermal.cpu_temperature()
            thermal.forget()
            thermal.cpu_temperature()
        self.assertEqual(len(calls), 2)

    def test_a_broken_source_does_not_hide_the_next_one(self):
        def broken():
            raise OSError(13, "denied")
        with mock.patch.object(thermal, "sources", lambda: [broken, lambda: 42.0]):
            self.assertEqual(thermal.cpu_temperature(), 42.0)
