#!/usr/bin/env python3
"""Set and verify a Generac H-100/G-Panel controller clock.

The controller clock is set to current America/Los_Angeles time by default.
The program is dry-run by default; pass --apply to perform the Modbus write.
Requires Python 3.7+ and pymodbus 2.x or 3.x.

Exercise scheduling is handled separately by set_hpanel_exercise.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import inspect
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

try:
    from pymodbus.client import ModbusSerialClient
except ImportError:
    try:
        from pymodbus.client.sync import ModbusSerialClient
    except ImportError as exc:
        print(
            "Could not import PyModbus in this Python interpreter "
            f"({sys.executable}): {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2)

from pymodbus.exceptions import ModbusException


UNIT = 0x64
REG_CLOCK = 0x00E0
SERIAL_PORT_CANDIDATES = ("/dev/USB232", "/dev/ttySTM1", "/dev/ttyUSB0", "/dev/serial0")


class ControllerError(RuntimeError):
    pass


def handle_termination(_signum, _frame) -> None:
    raise KeyboardInterrupt


def resolve_serial_port(requested_port: str) -> str:
    if requested_port != "auto":
        return requested_port
    for candidate in SERIAL_PORT_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    raise ControllerError(
        "No supported serial device found; checked " + ", ".join(SERIAL_PORT_CANDIDATES)
    )


@dataclass
class ModbusRTU:
    port: str
    baud: int = 9600
    unit: int = UNIT
    timeout: float = 1.5

    def __post_init__(self) -> None:
        parameters = {
            "port": self.port,
            "baudrate": self.baud,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
            "timeout": self.timeout,
            "retries": 2,
        }
        if "method" in inspect.signature(ModbusSerialClient).parameters:
            parameters["method"] = "rtu"
        self.client = ModbusSerialClient(**parameters)
        if not self.client.connect():
            raise ControllerError(f"Could not open/connect to {self.port}")

        read_parameters = inspect.signature(self.client.read_holding_registers).parameters
        if "device_id" in read_parameters:
            self.unit_parameter = "device_id"
        elif "slave" in read_parameters:
            self.unit_parameter = "slave"
        else:
            self.unit_parameter = "unit"

    def _unit_args(self) -> dict[str, int]:
        return {self.unit_parameter: self.unit}

    def close(self) -> None:
        self.client.close()

    def read_holding(self, address: int, count: int) -> list[int]:
        response = self.client.read_holding_registers(
            address, count=count, **self._unit_args()
        )
        if response.isError():
            raise ControllerError(f"Read 0x{address:04X} failed: {response}")
        values = list(response.registers)
        if len(values) != count:
            raise ControllerError(
                f"Read 0x{address:04X} returned {len(values)} registers; expected {count}"
            )
        return values

    def write_holding(self, address: int, values: list[int]) -> None:
        response = self.client.write_registers(
            address, values=values, **self._unit_args()
        )
        if response.isError():
            raise ControllerError(f"Write 0x{address:04X} failed: {response}")
        time.sleep(0.2)


def packed(high: int, low: int) -> int:
    if not (0 <= high <= 0xFF and 0 <= low <= 0xFF):
        raise ValueError("Packed bytes must be in the range 0..255")
    return (high << 8) | low


def current_time_in_zone(timezone: str) -> dt.datetime:
    zone_path = os.path.realpath(os.path.join("/usr/share/zoneinfo", timezone))
    zone_root = os.path.realpath("/usr/share/zoneinfo") + os.sep
    if zone_path.startswith(zone_root) and os.path.isfile(zone_path):
        timezone_spec = timezone
    elif timezone == "America/Los_Angeles":
        timezone_spec = "PST8PDT,M3.2.0/2,M11.1.0/2"
    else:
        raise ValueError(f"Unknown timezone {timezone!r}. Install the system tzdata package.")

    previous_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = timezone_spec
        time.tzset()
        return dt.datetime.now().astimezone().replace(microsecond=0)
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()


def hpanel_weekday(value: dt.datetime) -> int:
    """Return the H-Panel weekday: Sunday=1 through Saturday=7."""
    return 1 if value.weekday() == 6 else value.weekday() + 2


def encode_clock(value: dt.datetime) -> list[int]:
    return [
        packed(value.hour, value.minute),
        packed(value.second, hpanel_weekday(value)),
        packed(value.month, value.day),
        packed(value.year - 2000, 0),
    ]


def decode_clock(values: list[int], timezone: dt.tzinfo) -> dt.datetime:
    if len(values) != 4:
        raise ControllerError(f"H-Panel clock needs four registers; received {len(values)}")
    hour, minute = values[0] >> 8, values[0] & 0xFF
    second, weekday = values[1] >> 8, values[1] & 0xFF
    month, day = values[2] >> 8, values[2] & 0xFF
    year, unused = 2000 + (values[3] >> 8), values[3] & 0xFF
    if not 1 <= weekday <= 7:
        raise ControllerError(f"Invalid H-Panel weekday {weekday}")
    if unused != 0:
        print(f"WARNING: unexpected low byte in 0x00E3: 0x{unused:02X}", file=sys.stderr)
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=timezone)


def format_clock(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S (%A)")


def control_service(service: str, action: str) -> None:
    try:
        result = subprocess.run(
            ["systemctl", action, service],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise ControllerError(
            f"Timed out after 60 seconds while asking systemd to {action} {service!r}"
        )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        raise ControllerError(f"Could not {action} service {service!r}: {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="auto")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--unit", type=lambda value: int(value, 0), default=UNIT)
    parser.add_argument("--timezone", default="America/Los_Angeles")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--service", default="olmaster")
    parser.add_argument("--no-service-control", action="store_true")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, handle_termination)

    try:
        now = current_time_in_zone(args.timezone)
        serial_port = resolve_serial_port(args.port)
        requested_clock = encode_clock(now)
    except (ControllerError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"Pacific H-Panel time to set: {now.isoformat()} ({now.tzname()})")
    print(f"Serial port: {serial_port}")
    print("Clock registers 00E0..00E3:", " ".join(f"{value:04X}" for value in requested_clock))
    print("Exercise schedule: not modified by this clock-only utility")

    if not args.apply:
        print("Dry run only. Re-run with --apply to write the clock.")
        return 0

    bus = None
    service_stopped = False
    exit_code = 0
    try:
        if not args.no_service_control:
            print(f"Stopping service {args.service}...")
            service_stopped = True
            control_service(args.service, "stop")

        bus = ModbusRTU(serial_port, args.baud, args.unit)
        before_values = bus.read_holding(REG_CLOCK, 4)
        before = decode_clock(before_values, now.tzinfo)
        print("Controller time before:", format_clock(before))
        print(f"Clock drift: {abs((before - now).total_seconds()) / 60.0:.1f} minute(s)")

        # Mirror GenMon's established four-register H-Panel write.
        bus.write_holding(REG_CLOCK, requested_clock)
        after_values = bus.read_holding(REG_CLOCK, 4)
        after = decode_clock(after_values, now.tzinfo)
        print("Controller time after: ", format_clock(after))
        after_drift = abs((after - now).total_seconds())
        if after_drift > 60:
            raise ControllerError(
                f"Clock verification failed: controller is still {after_drift / 60.0:.1f} minutes off"
            )
        if (after_values[1] & 0xFF) != hpanel_weekday(now):
            raise ControllerError("Clock verification failed: weekday did not match")
        print("H-Panel clock verified successfully.")
    except (ControllerError, ModbusException, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        print("Interrupted; restoring service state...", file=sys.stderr)
        exit_code = 130
    finally:
        if bus is not None:
            try:
                bus.close()
            except Exception as exc:
                print(f"WARNING: error while closing Modbus client: {exc}", file=sys.stderr)
                if exit_code == 0:
                    exit_code = 1
        if service_stopped:
            print(f"Starting service {args.service}...")
            try:
                control_service(args.service, "start")
                print(f"Service {args.service} started.")
            except ControllerError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                if exit_code == 0:
                    exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
