"""Tests for check_esphome_sensor against a fake ESPHome web_server (version 3)."""
import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import check_esphome_sensor as plugin

SRC = os.path.dirname(os.path.dirname(os.path.abspath(plugin.__file__)))

SENSORS = {
    "wall_temperature": (15.1, "15.1 °C"),
    "dew_point": (11.9, "11.9 °C"),
    "room_temperature": (24, "24.0 °C"),
    "outside_temperature": (30, "30.0 °C"),
    "outside_fahrenheit": (86, "86.0 °F"),
    "humidity": (45, "45 %"),
}
TEXT_SENSORS = {"status": "ok", "wall_text": "15.1"}


class FakeWebServer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        domain, _, name = urllib.parse.unquote(self.path.lstrip("/")).partition("/")
        if domain == "sensor" and name in SENSORS:
            value, state = SENSORS[name]
            return self.reply(200, {"id": f"sensor-{name}", "value": value, "state": state})
        if domain == "text_sensor" and name in TEXT_SENSORS:
            return self.reply(200, {"id": f"text_sensor-{name}", "value": TEXT_SENSORS[name],
                                    "state": TEXT_SENSORS[name]})
        self.send_response(404)
        self.end_headers()


class TestCompare(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeWebServer)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def expect(self, state, pattern, *argv):
        result = subprocess.run(
            [sys.executable, "-m", "check_esphome_sensor", "-H", "127.0.0.1", "-p", str(self.port)] + list(argv),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            env=dict(os.environ, PYTHONPATH=SRC), timeout=60)
        self.assertEqual(state, result.returncode, result)
        self.assertRegex(result.stdout, pattern)
        if result.stdout:
            self.assertEqual(1, len(result.stdout.splitlines()), result)
        return result

    def compare(self, entity, other, must_be, warning, critical, *extra):
        return ["--check", "compare", "--entity", entity, "--compare-entity", other, "--must-be", must_be,
                "-w", str(warning), "-c", str(critical)] + list(extra)

    def test_sensor(self):
        self.expect(plugin.STATE_OK, r"^OK: wall_temperature is 15.1°C \| ", "--check", "sensor",
                    "--entity", "wall_temperature", "-w", "30", "-c", "35")

    def test_dew_point_ok(self):
        self.expect(plugin.STATE_OK, r"^OK: wall_temperature is 3.2°C above dew_point \(15.1°C vs 11.9°C\) \| "
                                     r"'margin'=3.2°C;3;1 'wall_temperature'=15.1°C 'dew_point'=11.9°C$",
                    *self.compare("wall_temperature", "dew_point", "above", 3, 1))

    def test_close(self):
        self.expect(plugin.STATE_WARNING, r"^WARNING: wall_temperature is 3.2°C above",
                    *self.compare("wall_temperature", "dew_point", "above", 4, 1))
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: wall_temperature is 3.2°C above",
                    *self.compare("wall_temperature", "dew_point", "above", 5, 3.2))

    def test_wrong_side(self):
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: dew_point is 3.2°C below, not above, wall_temperature "
                                           r"\(11.9°C vs 15.1°C\) \| 'margin'=-3.2°C;3;1 ",
                    *self.compare("dew_point", "wall_temperature", "above", 3, 1))

    def test_below(self):
        self.expect(plugin.STATE_OK, r"^OK: room_temperature is 6°C below outside_temperature \(24°C vs 30°C\) "
                                     r"\| 'margin'=6°C;2;0 ",
                    *self.compare("room_temperature", "outside_temperature", "below", 2, 0))
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: outside_temperature is 6°C above, not below",
                    *self.compare("outside_temperature", "room_temperature", "below", 2, 0))

    def test_equal(self):
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: room_temperature is equal to, not below, room_temperature",
                    *self.compare("room_temperature", "room_temperature", "below", 2, 0))

    def test_text_sensor_domain(self):
        self.expect(plugin.STATE_OK, r"^OK: wall_text is 3.2°C above dew_point",
                    *self.compare("wall_text", "dew_point", "above", 3, 1, "--entity-domain", "text_sensor"))

    def test_different_units(self):
        self.expect(plugin.STATE_UNKNOWN, r"^UNKNOWN: room_temperature is in °C, but outside_fahrenheit in °F$",
                    *self.compare("room_temperature", "outside_fahrenheit", "below", 2, 0))

    def test_not_numeric(self):
        self.expect(plugin.STATE_UNKNOWN, r"^UNKNOWN: Value of status is not numeric: 'ok'$",
                    *self.compare("status", "dew_point", "above", 3, 1, "--entity-domain", "text_sensor"))

    def test_usage(self):
        result = subprocess.run(
            [sys.executable, "-m", "check_esphome_sensor", "-H", "127.0.0.1", "-p", str(self.port)]
            + self.compare("wall_temperature", "dew_point", "above", 1, 3),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            env=dict(os.environ, PYTHONPATH=SRC), timeout=60)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("-w must not be smaller than -c", result.stderr)


if __name__ == "__main__":
    unittest.main()
