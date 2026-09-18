# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Thomas Wagner <wagner-thomas@gmx.at>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""check_esphome_sensor - Nagios/Icinga plugin for ESPHome devices.

Supports two transports:
  --mode http (default)  The web_server (version: 3) JSON API, with
                          optional HTTP basic auth (-u/-P) as configured
                          under the ESPHome "web_server: auth:" section.
  --mode api              ESPHome's native API component, with an
                          optional plaintext password (--api-password)
                          or Noise PSK encryption key (--api-key), as
                          configured under the ESPHome "api:" section.
                          Requires the python3-aioesphomeapi module.

Four kinds of checks (--check):
  sensor  Compare a numeric sensor/text_sensor value against -w/-c
          thresholds. Unlike most plugins, -c may be smaller than -w:
          whichever of the two is larger decides whether higher or
          lower values are considered worse.
  time    Compare an ESPHome time sensor/text_sensor against the local
          clock (default) or an NTP server (--time-server), in seconds
          of offset.
  binary  Evaluate a boolean expression (and/or/not/parentheses) over
          one or more binary_sensor entities named in the expression,
          e.g. --binary-expr "door_open and not away".
  text    Match a text_sensor's value against a Python regular
          expression (--regex), e.g. to check a status/version string.

Pass --list instead of --check to print the available sensor,
text_sensor, binary_sensor, date, time and datetime entities (with
their current value, if received in time, and the unit of measurement
for sensors) so you know what to pass to --entity/--time-entity/
--binary-expr. Note that ESPHome may expose different identifiers per
transport for the *same* entity (e.g. the HTTP web_server can use the
literal friendly name such as "BME280 Humidity", while the native API
uses the snake_case object_id such as "bme280_humidity") -- always run
--list with the same --mode you intend to use for the actual check.

If -w/-c is given without --uom, the unit of measurement is retrieved
from the device: authoritatively from SensorInfo.unit_of_measurement
in --mode api, or heuristically parsed out of the formatted "state"
text (e.g. "42.3 %" -> "%") in --mode http.

ESPHome's "time:" component itself is not queryable; but its newer
"datetime:" component exposes "date", "time" and "datetime" domain
entities distinct from sensor/text_sensor. Of these, only "datetime"
(which reports a Unix timestamp) is usable with --check time; bare
"date"/"time" entities lack enough information for a time offset and
are listed for completeness only.
"""
import argparse
import ast
import asyncio
import json
import re
import socket
import struct
import sys
import time
import urllib.parse
from datetime import datetime, timezone

STATE_OK = 0
STATE_WARNING = 1
STATE_CRITICAL = 2
STATE_UNKNOWN = 3
STATE_NAMES = {
    STATE_OK: "OK",
    STATE_WARNING: "WARNING",
    STATE_CRITICAL: "CRITICAL",
    STATE_UNKNOWN: "UNKNOWN",
}
STATE_BY_NAME = {"ok": STATE_OK, "warning": STATE_WARNING, "critical": STATE_CRITICAL}

NTP_EPOCH_OFFSET = 2208988800  # seconds between 1900-01-01 and 1970-01-01


def die(state, message):
    print(f"{STATE_NAMES[state]}: {message}")
    sys.exit(state)


# ---------------------------------------------------------------------------
# value retrieval
# ---------------------------------------------------------------------------

# entities of these domains carry a raw value/state directly comparable
# to a Unix timestamp; bare "date"/"time" domain entities do not.
TIME_COMPARABLE_DOMAINS = ("sensor", "text_sensor", "datetime")

NUMERIC_PREFIX_RE = re.compile(r"^[-+]?[0-9]+(?:\.[0-9]+)?\s*")


def extract_uom_from_state_text(state):
    """Best-effort unit-of-measurement extraction from a web_server
    "state" string such as "42.3 %" or "-62 dBm" -> "%" / "dBm"."""
    if not isinstance(state, str):
        return None
    remainder = NUMERIC_PREFIX_RE.sub("", state, count=1).strip()
    return remainder or None


def extract_api_state_value(domain, state):
    """Native API state messages use different field names depending on
    entity type: plain domains use .state, "datetime" uses .epoch_seconds
    (a Unix timestamp). "date"/"time" alone have no single usable value."""
    if domain == "datetime":
        return state.epoch_seconds
    return state.state


def format_display_value(domain, state):
    """Human-readable value for --list only; extract_api_state_value() is
    used instead for the actual sensor/time/binary checks."""
    if domain == "date":
        return f"{state.year:04d}-{state.month:02d}-{state.day:02d}"
    if domain == "time":
        return f"{state.hour:02d}:{state.minute:02d}:{state.second:02d}"
    if domain == "datetime":
        try:
            return datetime.fromtimestamp(state.epoch_seconds, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return state.epoch_seconds
    return state.state


def fetch_http(args, wanted):
    """wanted: list of (entity_id, domain). Returns (values, uoms), each a
    dict keyed by entity_id; uoms values are None unless domain is
    "sensor" and a unit could be parsed out of the "state" text."""
    try:
        import requests
    except ImportError:
        die(STATE_UNKNOWN, "python3 module 'requests' is required for --mode http")

    auth = (args.username, args.password or "") if args.username is not None else None
    values = {}
    uoms = {}
    for entity_id, domain in wanted:
        # entity_id may be the object_id (snake_case) on some ESPHome
        # versions/configs, or the literal friendly name (which can contain
        # spaces and other characters) on others -- URL-encode it either way.
        url = f"http://{args.host}:{args.port}/{domain}/{urllib.parse.quote(entity_id, safe='')}"
        try:
            resp = requests.get(url, auth=auth, timeout=args.timeout)
        except requests.exceptions.RequestException as exc:
            die(STATE_UNKNOWN, f"HTTP request to {url} failed: {exc}")
        if resp.status_code == 401:
            die(STATE_UNKNOWN, f"HTTP authentication failed for {url}")
        if resp.status_code != 200:
            die(STATE_UNKNOWN, f"HTTP request to {url} returned status {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            die(STATE_UNKNOWN, f"Could not parse JSON response from {url}")
        if "value" not in data:
            die(STATE_UNKNOWN, f"No 'value' field in response from {url}: {data}")
        values[entity_id] = data["value"]
        uoms[entity_id] = extract_uom_from_state_text(data.get("state")) if domain == "sensor" else None
    return values, uoms


def fetch_api(args, wanted):
    """wanted: list of (entity_id, domain). Returns (values, uoms), each a
    dict keyed by entity_id; uoms values are None except for "sensor"
    entities, where they come from SensorInfo.unit_of_measurement."""
    try:
        import aioesphomeapi
    except ImportError:
        die(STATE_UNKNOWN, "python3 module 'aioesphomeapi' is required for --mode api")

    domain_by_entity_id = {entity_id: domain for entity_id, domain in wanted}
    wanted_names = set(domain_by_entity_id)

    async def run():
        client = aioesphomeapi.APIClient(
            args.host,
            args.port,
            password=args.api_password or None,
            noise_psk=args.api_key or None,
        )
        try:
            await asyncio.wait_for(client.connect(login=True), timeout=args.timeout)
        except Exception as exc:  # noqa: BLE001 - report any connection failure as UNKNOWN
            die(STATE_UNKNOWN, f"Could not connect to {args.host}:{args.port} via native API: {exc}")

        try:
            entities, _services = await asyncio.wait_for(
                client.list_entities_services(), timeout=args.timeout
            )
        except Exception as exc:  # noqa: BLE001
            await client.disconnect()
            die(STATE_UNKNOWN, f"Could not list entities: {exc}")

        key_to_name = {e.key: e.object_id for e in entities if e.object_id in wanted_names}
        missing = wanted_names - set(key_to_name.values())
        if missing:
            await client.disconnect()
            die(STATE_UNKNOWN, f"Unknown entity/entities: {', '.join(sorted(missing))}")

        uoms = {
            e.object_id: getattr(e, "unit_of_measurement", "") or None
            for e in entities
            if e.object_id in wanted_names
        }

        values = {}
        done_event = asyncio.Event()

        def on_state(state):
            name = key_to_name.get(state.key)
            if name is not None and name not in values:
                domain = domain_by_entity_id[name]
                values[name] = extract_api_state_value(domain, state)
                if len(values) == len(key_to_name):
                    done_event.set()

        client.subscribe_states(on_state)
        try:
            await asyncio.wait_for(done_event.wait(), timeout=args.timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            await client.disconnect()

        return values, uoms

    values, uoms = asyncio.run(run())
    missing = wanted_names - set(values.keys())
    if missing:
        die(STATE_UNKNOWN, f"Timed out waiting for state of: {', '.join(sorted(missing))}")
    return values, uoms


def fetch_values(args, wanted):
    """Returns (values, uoms); see fetch_http()/fetch_api() docstrings."""
    if args.mode == "http":
        return fetch_http(args, wanted)
    return fetch_api(args, wanted)


# ---------------------------------------------------------------------------
# entity listing (--list)
# ---------------------------------------------------------------------------

LISTABLE_DOMAINS = ("sensor", "text_sensor", "binary_sensor", "date", "time", "datetime")


def split_domain_and_name(entity_id):
    """Split a web_server "id" field ("sensor/foo" or the older "sensor-foo")
    into (domain, object_id)."""
    for sep in ("/", "-"):
        if sep in entity_id:
            domain, _, name = entity_id.partition(sep)
            return domain, name
    return "", entity_id


def list_entities_http(args):
    try:
        import requests
    except ImportError:
        die(STATE_UNKNOWN, "python3 module 'requests' is required for --mode http")

    auth = (args.username, args.password or "") if args.username is not None else None
    url = f"http://{args.host}:{args.port}/events"
    try:
        resp = requests.get(
            url,
            headers={"Accept": "text/event-stream"},
            auth=auth,
            stream=True,
            timeout=(min(args.timeout, 5), args.timeout),
        )
    except requests.exceptions.RequestException as exc:
        die(STATE_UNKNOWN, f"HTTP request to {url} failed: {exc}")
    if resp.status_code == 401:
        die(STATE_UNKNOWN, f"HTTP authentication failed for {url}")
    if resp.status_code != 200:
        die(STATE_UNKNOWN, f"HTTP request to {url} returned status {resp.status_code}")
    # SSE bodies are UTF-8; without this, requests guesses an encoding from
    # headers (falling back to latin-1 for a text/event-stream content type
    # with no explicit charset), mangling multi-byte units like "°C".
    resp.encoding = "utf-8"

    # The web_server sends the current state of every entity as soon as a
    # client connects to /events (an SSE stream), followed by periodic
    # pings/log lines. We only read for up to --timeout seconds.
    result = {}
    seen = set()
    deadline = time.time() + args.timeout
    event_type = None
    try:
        for raw_line in resp.iter_lines(decode_unicode=True):
            if time.time() > deadline:
                break
            if raw_line is None or raw_line == "":
                event_type = None
                continue
            if raw_line.startswith("event:"):
                event_type = raw_line[len("event:"):].strip()
                continue
            if raw_line.startswith("data:") and event_type == "state":
                payload = raw_line[len("data:"):].strip()
                try:
                    data = json.loads(payload)
                except ValueError:
                    continue
                domain, name = split_domain_and_name(data.get("id", ""))
                if domain in LISTABLE_DOMAINS and name and (domain, name) not in seen:
                    seen.add((domain, name))
                    value = data.get("value", data.get("state"))
                    if domain == "sensor":
                        uom = extract_uom_from_state_text(data.get("state"))
                        if uom and value is not None:
                            value = f"{value} {uom}"
                    result.setdefault(domain, []).append((name, value))
    except requests.exceptions.RequestException:
        pass  # timed out or connection dropped: report whatever we collected
    finally:
        resp.close()
    return result


def list_entities_api(args):
    try:
        import aioesphomeapi
    except ImportError:
        die(STATE_UNKNOWN, "python3 module 'aioesphomeapi' is required for --mode api")

    domain_by_class = {
        aioesphomeapi.SensorInfo: "sensor",
        aioesphomeapi.TextSensorInfo: "text_sensor",
        aioesphomeapi.BinarySensorInfo: "binary_sensor",
        aioesphomeapi.DateInfo: "date",
        aioesphomeapi.TimeInfo: "time",
        aioesphomeapi.DateTimeInfo: "datetime",
    }

    async def run():
        client = aioesphomeapi.APIClient(
            args.host,
            args.port,
            password=args.api_password or None,
            noise_psk=args.api_key or None,
        )
        try:
            await asyncio.wait_for(client.connect(login=True), timeout=args.timeout)
        except Exception as exc:  # noqa: BLE001
            die(STATE_UNKNOWN, f"Could not connect to {args.host}:{args.port} via native API: {exc}")

        try:
            entities, _services = await asyncio.wait_for(
                client.list_entities_services(), timeout=args.timeout
            )
        except Exception as exc:  # noqa: BLE001
            await client.disconnect()
            die(STATE_UNKNOWN, f"Could not list entities: {exc}")

        wanted = {}
        uoms = {}
        for entity in entities:
            for cls, domain in domain_by_class.items():
                if isinstance(entity, cls):
                    wanted[entity.key] = (domain, entity.object_id)
                    if domain == "sensor":
                        uoms[entity.object_id] = getattr(entity, "unit_of_measurement", "") or None
                    break

        values = {}
        done_event = asyncio.Event()

        def on_state(state):
            entry = wanted.get(state.key)
            if entry is not None and state.key not in values:
                domain, _name = entry
                values[state.key] = format_display_value(domain, state)
                if len(values) == len(wanted):
                    done_event.set()

        client.subscribe_states(on_state)
        try:
            # entities that never report (e.g. unavailable sensors) should
            # not hold up the whole listing; wait at most a short slice of
            # the overall timeout for the initial state dump.
            await asyncio.wait_for(done_event.wait(), timeout=min(args.timeout, 5))
        except asyncio.TimeoutError:
            pass
        finally:
            await client.disconnect()

        result = {}
        for key, (domain, name) in wanted.items():
            value = values.get(key)
            uom = uoms.get(name)
            if domain == "sensor" and value is not None and uom:
                value = f"{value} {uom}"
            result.setdefault(domain, []).append((name, value))
        return result

    return asyncio.run(run())


def list_entities(args):
    if args.mode == "http":
        return list_entities_http(args)
    return list_entities_api(args)


def print_entity_list(result, host):
    print(f"Available entities on {host}:")
    if not result:
        print("  (none found within the timeout; the device may need a longer --timeout)")
        return
    for domain in LISTABLE_DOMAINS:
        items = result.get(domain)
        if not items:
            continue
        print(f"  {domain}:")
        for name, value in sorted(items):
            print(f"    {name}" + (f" = {value}" if value is not None else " (no value received yet)"))


# ---------------------------------------------------------------------------
# threshold evaluation (direction-agnostic: -c may be smaller than -w)
# ---------------------------------------------------------------------------

def evaluate_numeric(value, warning, critical, label, uom=""):
    if warning is not None and critical is not None and warning > critical:
        # lower values are worse (e.g. battery / signal level style checks)
        if value <= critical:
            state = STATE_CRITICAL
        elif value <= warning:
            state = STATE_WARNING
        else:
            state = STATE_OK
    else:
        # higher values are worse (the common case)
        if critical is not None and value >= critical:
            state = STATE_CRITICAL
        elif warning is not None and value >= warning:
            state = STATE_WARNING
        else:
            state = STATE_OK

    perf_warn = "" if warning is None else warning
    perf_crit = "" if critical is None else critical
    message = f"{label} is {value}{uom} | '{label}'={value}{uom};{perf_warn};{perf_crit};;"
    return state, message


# ---------------------------------------------------------------------------
# time-offset check
# ---------------------------------------------------------------------------

def query_ntp(server, timeout):
    packet = b"\x1b" + 47 * b"\0"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.sendto(packet, (server, 123))
            data, _ = sock.recvfrom(48)
        except OSError as exc:
            die(STATE_UNKNOWN, f"Could not query NTP server {server}: {exc}")
    if len(data) < 48:
        die(STATE_UNKNOWN, f"Short NTP response from {server}")
    transmit_timestamp = struct.unpack("!12I", data)[10]
    return transmit_timestamp - NTP_EPOCH_OFFSET


def get_reference_time(time_server, timeout):
    if not time_server:
        return time.time()
    return query_ntp(time_server, timeout)


def parse_device_time(value):
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        die(STATE_UNKNOWN, f"Could not parse time value: {text!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ---------------------------------------------------------------------------
# binary_sensor logical expression check
# ---------------------------------------------------------------------------

ALLOWED_EXPR_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.Name,
    ast.Load,
    ast.Constant,
)


def parse_bool_expr(expr):
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        die(STATE_UNKNOWN, f"Invalid --binary-expr: {exc}")

    def check(node):
        if not isinstance(node, ALLOWED_EXPR_NODES):
            die(STATE_UNKNOWN, f"Unsupported element in --binary-expr: {type(node).__name__}")
        for child in ast.iter_child_nodes(node):
            check(child)

    check(tree)
    return tree


def extract_names(tree):
    return sorted({node.id for node in ast.walk(tree) if isinstance(node, ast.Name)})


def eval_bool_expr(tree, values):
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.BoolOp):
            results = [ev(v) for v in node.values]
            return all(results) if isinstance(node.op, ast.And) else any(results)
        if isinstance(node, ast.UnaryOp):
            return not ev(node.operand)
        if isinstance(node, ast.Name):
            if node.id not in values:
                raise KeyError(node.id)
            return bool(values[node.id])
        if isinstance(node, ast.Constant):
            return bool(node.value)
        die(STATE_UNKNOWN, "Unsupported element in --binary-expr")

    return ev(tree)


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Check ESPHome devices via the native API or the web_server v3 HTTP API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s -H esp.local --check sensor --entity outside_temperature -w 30 -c 35\n"
            "  %(prog)s -H esp.local --mode api --api-key <base64 psk> --check sensor "
            "--entity battery_level -w 20 -c 10\n"
            "  %(prog)s -H esp.local --check time --time-entity sntp_time -w 5 -c 30\n"
            "  %(prog)s -H esp.local --check binary "
            "--binary-expr 'door_open and not alarm_disarmed'\n"
            "  %(prog)s -H esp.local --check text --text-entity firmware_status "
            "--regex '^ok$'\n"
            "  %(prog)s -H esp.local --list\n"
        ),
    )
    parser.add_argument("-H", "--host", required=True, help="ESPHome device hostname or IP address")
    parser.add_argument(
        "-p", "--port", type=int, default=None,
        help="port to connect to (default: 6053 for --mode api, 80 for --mode http)",
    )
    parser.add_argument("--mode", choices=("http", "api"), default="http", help="connection method (default: http)")
    parser.add_argument(
        "-t", "--timeout", type=float, default=15,
        help="timeout in seconds (default: 15; ESPHome devices over WiFi can be slow to respond)",
    )

    api_group = parser.add_argument_group("native API (--mode api)")
    api_group.add_argument("--api-password", help="plaintext native API password")
    api_group.add_argument("--api-key", help="base64 Noise PSK encryption key (api: encryption: key:)")

    http_group = parser.add_argument_group("HTTP web_server v3 (--mode http, the default)")
    http_group.add_argument("-u", "--username", help="HTTP basic auth username")
    http_group.add_argument("-P", "--password", help="HTTP basic auth password")

    parser.add_argument(
        "--list", action="store_true",
        help="list available sensor/text_sensor/binary_sensor/date/time/datetime entities "
             "(with their current value, if received in time) and exit, instead of "
             "running a check",
    )
    parser.add_argument("--check", choices=("sensor", "time", "binary", "text"), help="type of check to perform")

    sensor_group = parser.add_argument_group("--check sensor")
    sensor_group.add_argument("--entity", help="sensor/text_sensor entity id (object_id)")
    sensor_group.add_argument("--entity-domain", choices=("sensor", "text_sensor"), default="sensor")
    sensor_group.add_argument("-w", "--warning", type=float, help="warning threshold (may be > or < -c)")
    sensor_group.add_argument("-c", "--critical", type=float, help="critical threshold (may be > or < -w)")
    sensor_group.add_argument(
        "--uom", default="",
        help="unit of measurement for performance data (default: retrieved from the "
             "device -- SensorInfo.unit_of_measurement in --mode api, or parsed out of "
             "the formatted state text in --mode http)",
    )

    time_group = parser.add_argument_group("--check time")
    time_group.add_argument("--time-entity", help="sensor/text_sensor entity id reporting the device's time")
    time_group.add_argument(
        "--time-entity-domain", choices=TIME_COMPARABLE_DOMAINS, default="text_sensor",
        help="domain of --time-entity (default: text_sensor); \"datetime\" is ESPHome's "
             "native datetime: component entity reporting a Unix timestamp",
    )
    time_group.add_argument("--time-server", help="NTP server to compare against (default: local clock)")

    binary_group = parser.add_argument_group("--check binary")
    binary_group.add_argument(
        "--binary-expr",
        help="boolean expression over binary_sensor entity ids, e.g. 'door_open and not alarm_armed'",
    )
    binary_group.add_argument("--true-state", choices=("ok", "warning", "critical"), default="critical")
    binary_group.add_argument("--false-state", choices=("ok", "warning", "critical"), default="ok")

    text_group = parser.add_argument_group("--check text")
    text_group.add_argument("--text-entity", help="text_sensor entity id to match")
    text_group.add_argument("--regex", help="Python regular expression to search for in the text_sensor's value")
    text_group.add_argument(
        "-i", "--ignore-case", action="store_true", help="match --regex case-insensitively"
    )
    text_group.add_argument("--match-state", choices=("ok", "warning", "critical"), default="ok")
    text_group.add_argument("--no-match-state", choices=("ok", "warning", "critical"), default="critical")

    args = parser.parse_args()

    if args.port is None:
        args.port = 6053 if args.mode == "api" else 80

    if args.list:
        return args

    if not args.check:
        parser.error("--check is required (or use --list)")

    if args.check == "sensor":
        if not args.entity or args.warning is None or args.critical is None:
            parser.error("--check sensor requires --entity, -w and -c")
    elif args.check == "time":
        if not args.time_entity or args.warning is None or args.critical is None:
            parser.error("--check time requires --time-entity, -w and -c")
    elif args.check == "binary":
        if not args.binary_expr:
            parser.error("--check binary requires --binary-expr")
    elif args.check == "text":
        if not args.text_entity or not args.regex:
            parser.error("--check text requires --text-entity and --regex")
        try:
            re.compile(args.regex, re.IGNORECASE if args.ignore_case else 0)
        except re.error as exc:
            parser.error(f"Invalid --regex: {exc}")

    return args


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    if args.list:
        result = list_entities(args)
        print_entity_list(result, args.host)
        sys.exit(STATE_OK)

    if args.check == "sensor":
        values, uoms = fetch_values(args, [(args.entity, args.entity_domain)])
        value = values[args.entity]
        try:
            value = float(value)
        except (TypeError, ValueError):
            die(STATE_UNKNOWN, f"Value of {args.entity} is not numeric: {value!r}")
        uom = args.uom or uoms.get(args.entity) or ""
        state, message = evaluate_numeric(value, args.warning, args.critical, args.entity, uom)
        die(state, message)

    elif args.check == "time":
        values, _uoms = fetch_values(args, [(args.time_entity, args.time_entity_domain)])
        device_epoch = parse_device_time(values[args.time_entity])
        reference_epoch = get_reference_time(args.time_server, args.timeout)
        offset = abs(device_epoch - reference_epoch)
        label = f"time offset of {args.time_entity}"
        state, message = evaluate_numeric(offset, args.warning, args.critical, label, "s")
        die(state, message)

    elif args.check == "binary":
        tree = parse_bool_expr(args.binary_expr)
        names = extract_names(tree)
        if not names:
            die(STATE_UNKNOWN, "No binary_sensor entity names found in --binary-expr")
        values, _uoms = fetch_values(args, [(name, "binary_sensor") for name in names])
        try:
            result = eval_bool_expr(tree, values)
        except KeyError as exc:
            die(STATE_UNKNOWN, f"Unknown binary_sensor in expression: {exc}")
        state = STATE_BY_NAME[args.true_state if result else args.false_state]
        details = ", ".join(f"{n}={'ON' if values[n] else 'OFF'}" for n in names)
        die(state, f"'{args.binary_expr}' is {result} ({details})")

    elif args.check == "text":
        values, _uoms = fetch_values(args, [(args.text_entity, "text_sensor")])
        text = str(values[args.text_entity])
        flags = re.IGNORECASE if args.ignore_case else 0
        matched = re.search(args.regex, text, flags) is not None
        state = STATE_BY_NAME[args.match_state if matched else args.no_match_state]
        die(state, f"{args.text_entity} = {text!r} {'matches' if matched else 'does not match'} /{args.regex}/")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die(STATE_UNKNOWN, "Interrupted")
