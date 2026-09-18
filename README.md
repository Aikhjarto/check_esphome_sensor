# check_esphome_sensor

A Nagios/Icinga plugin for monitoring [ESPHome](https://esphome.io/) devices,
either via ESPHome's native API component or via the `web_server` (version 3)
HTTP JSON API.

## Features

- **Two transports**
  - `--mode http` (default): the `web_server` (`version: 3`) JSON API, with
    optional HTTP basic auth (`-u`/`-P`) as configured under the ESPHome
    `web_server: auth:` section.
  - `--mode api`: ESPHome's native API component, with an optional plaintext
    password (`--api-password`) or Noise PSK encryption key (`--api-key`), as
    configured under the ESPHome `api:` section. Requires the optional
    `aioesphomeapi` dependency (see [Installation](#installation)).
- **Four kinds of checks** (`--check`)
  - `sensor`: compare a numeric `sensor`/`text_sensor` value against `-w`/`-c`
    thresholds. Unlike most plugins, `-c` may be smaller than `-w`: whichever
    of the two is larger decides whether higher or lower values count as
    worse (e.g. a battery percentage where *lower* is bad).
  - `time`: compare an ESPHome time-reporting entity against the local clock
    (default) or an NTP server (`--time-server`), in seconds of offset.
  - `binary`: evaluate a boolean expression (`and`/`or`/`not`/parentheses)
    over one or more `binary_sensor` entities named directly in the
    expression, e.g. `--binary-expr "door_open and not away"`.
  - `text`: match a `text_sensor`'s value against a Python regular expression
    (`--regex`), e.g. to check a status or version string.
- **`--list`**: instead of `--check`, print the available `sensor`,
  `text_sensor`, `binary_sensor`, `date`, `time` and `datetime` entities
  (with their current value, if received in time, and unit of measurement
  for sensors), so you know what to pass to `--entity`/`--time-entity`/
  `--binary-expr`/`--text-entity`.
- **Automatic unit of measurement**: if `-w`/`-c` is given without `--uom`,
  the unit is retrieved from the device -- authoritatively from
  `SensorInfo.unit_of_measurement` in `--mode api`, or parsed out of the
  formatted `state` text (e.g. `"42.3 %"` -> `"%"`) in `--mode http`.

### A note on entity identifiers

ESPHome may expose a different identifier for the *same* entity depending on
transport: the HTTP `web_server` can use the literal friendly name (e.g.
`"BME280 Humidity"`, with spaces), while the native API uses the snake_case
`object_id` (e.g. `bme280_humidity`). Always run `--list` with the same
`--mode` you intend to use for the actual check, and pass whatever it prints
back verbatim.

### A note on ESPHome's time-related entities

ESPHome's `time:` component (e.g. `platform: sntp`) is a core component used
internally via `id(<id>).now()` in lambdas/automations -- it is **not**
exposed as a queryable entity by itself. ESPHome's newer `datetime:`
component does expose `date`, `time` and `datetime` domain entities, but only
`datetime` (which reports a Unix timestamp) carries enough information to be
usable with `--check time`; bare `date`/`time` entities are listed by
`--list` for completeness only.

If you want to monitor an SNTP-style `time:` component's clock, expose it
explicitly, e.g.:

```yaml
text_sensor:
  - platform: template
    name: "ESP Time"
    lambda: |-
      return id(esptime).now().strftime("%Y-%m-%d %H:%M:%S");
    update_interval: 10s
```

and then use `--check time --time-entity "ESP Time" --time-entity-domain text_sensor`.

## Installation

```console
$ pip install .
```

To also use `--mode api` (ESPHome's native API), install the optional extra:

```console
$ pip install .[api]
```

This installs a `check_esphome_sensor` console script.

## Usage

```console
$ check_esphome_sensor --help
```

### Examples

```console
# Numeric sensor threshold check
check_esphome_sensor -H esp.local --check sensor --entity outside_temperature -w 30 -c 35

# Same, via the native API with an encrypted session
check_esphome_sensor -H esp.local --mode api --api-key <base64 psk> \
    --check sensor --entity battery_level -w 20 -c 10

# Time offset check against the local clock
check_esphome_sensor -H esp.local --check time --time-entity sntp_time -w 5 -c 30

# Logical combination of binary_sensor entities
check_esphome_sensor -H esp.local --check binary \
    --binary-expr 'door_open and not alarm_disarmed'

# Regex match against a text_sensor
check_esphome_sensor -H esp.local --check text --text-entity firmware_status --regex '^ok$'

# List all entities you could target with the above
check_esphome_sensor -H esp.local --list
```

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
