#!/usr/bin/env python3
"""Set a Generac Evolution controller clock and weekly exercise schedule.

Defaults:
  * Controller clock: current America/Los_Angeles time (PST/PDT as applicable)
  * Exercise: Tuesday at 09:00, weekly, 30 minutes
  * Modbus RTU: unit 0x9D, 9600 8N1

The program is dry-run by default. Pass --apply to perform writes.
Requires Python 3.7+ and pymodbus.
"""

from __future__ import annotations

import argparse
import datetime as dt
import inspect
import os
import subprocess
import sys
import time
from dataclasses import dataclass

try:
    from pymodbus.client import ModbusSerialClient
    from pymodbus.exceptions import ModbusException
except ImportError:
    print("Missing dependency: install pymodbus with: python3 -m pip install pymodbus", file=sys.stderr)
    raise SystemExit(2)


UNIT = 0x9D
REG_CLOCK = 0x000E       # 000E..0010: hour/minute, month/day, weekday/year
REG_EX_TIME = 0x002C     # high byte hour, low byte minute
REG_EX_FREQUENCY = 0x002D
REG_EX_DAY = 0x002E
REG_EX_QUIET = 0x002F
REG_EX_DURATION = 0x023E


class ControllerError(RuntimeError):
    pass


@dataclass
class ModbusRTU:
    port: str
    baud: int = 9600
    unit: int = UNIT
    timeout: float = 1.5

    def __post_init__(self) -> None:
        self.client = ModbusSerialClient(
            port=self.port,
            baudrate=self.baud,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=self.timeout,
            retries=2,
        )
        if not self.client.connect():
            raise ControllerError(f"Could not open/connect to {self.port}")

        # PyModbus <=3.10 calls this parameter 'slave'; >=3.11 calls it 'device_id'.
        parameters = inspect.signature(self.client.read_holding_registers).parameters
        self.unit_parameter = "device_id" if "device_id" in parameters else "slave"

    def close(self) -> None:
        self.client.close()

    def _unit_args(self) -> dict[str, int]:
        return {self.unit_parameter: self.unit}

    def read_holding(self, address: int, count: int) -> list[int]:
        response = self.client.read_holding_registers(
            address, count=count, **self._unit_args()
        )
        if response.isError():
            raise ControllerError(f"Read 0x{address:04X} failed: {response}")
        registers = list(response.registers)
        if len(registers) != count:
            raise ControllerError(
                f"Read 0x{address:04X} returned {len(registers)} registers; expected {count}"
            )
        return registers

    def write_holding(self, address: int, values: list[int]) -> None:
        for value in values:
            if not 0 <= value <= 0xFFFF:
                raise ValueError(f"Register value out of range: {value}")
        response = self.client.write_registers(
            address, values=values, **self._unit_args()
        )
        if response.isError():
            raise ControllerError(f"Write 0x{address:04X} failed: {response}")
        time.sleep(0.15)


def packed(high: int, low: int) -> int:
    if not (0 <= high <= 0xFF and 0 <= low <= 0xFF):
        raise ValueError("Packed bytes must be in the range 0..255")
    return (high << 8) | low


def current_time_in_zone(timezone: str) -> dt.datetime:
    """Return an aware datetime using the host's standard Linux tzdata.

    This deliberately avoids Python's zoneinfo module so the utility works on
    Python 3.7 and 3.8 systems without installing the backports.zoneinfo package.
    """
    zone_path = os.path.realpath(os.path.join("/usr/share/zoneinfo", timezone))
    zone_root = os.path.realpath("/usr/share/zoneinfo") + os.sep
    if not zone_path.startswith(zone_root) or not os.path.isfile(zone_path):
        raise ValueError(f"Unknown timezone {timezone!r}. Install the system tzdata package.")

    previous_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = timezone
        time.tzset()
        return dt.datetime.now().astimezone().replace(second=0, microsecond=0)
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()


def decode_clock(values: list[int]) -> str:
    clock, date, weekday_year = values
    hour, minute = clock >> 8, clock & 0xFF
    month, day = date >> 8, date & 0xFF
    weekday, year = weekday_year >> 8, 2000 + (weekday_year & 0xFF)
    weekday_names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    weekday_text = weekday_names[weekday] if weekday < len(weekday_names) else f"weekday={weekday}"
    return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d} ({weekday_text})"


def control_service(service: str, action: str) -> None:
    """Run systemctl without a shell and fail if the requested action fails."""
    result = subprocess.run(
        ["systemctl", action, service],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        raise ControllerError(f"Could not {action} service {service!r}: {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/USB232", help="Serial device; default: /dev/USB232")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--unit", type=lambda x: int(x, 0), default=UNIT, help="Modbus unit, default 0x9D")
    parser.add_argument("--timezone", default="America/Los_Angeles", help="IANA timezone; default observes PST/PDT")
    parser.add_argument("--apply", action="store_true", help="Actually write settings; otherwise perform a dry run")
    parser.add_argument("--quiet-test", action="store_true", help="Enable quiet-test mode; default is off")
    parser.add_argument("--skip-duration", action="store_true", help="Do not write 0x023E if this model rejects duration")
    parser.add_argument("--service", default="olmaster", help="Service to stop during writes; default: olmaster")
    parser.add_argument("--no-service-control", action="store_true", help="Do not stop or start a service")
    args = parser.parse_args()

    try:
        now = current_time_in_zone(args.timezone)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    # The known Evolution write path uses zero for the weekday byte and lets the controller derive it.
    clock_values = [packed(now.hour, now.minute), packed(now.month, now.day), packed(0, now.year - 2000)]
    exercise_values = {
        REG_EX_FREQUENCY: 0x0000,          # weekly
        REG_EX_DAY: 0x0002,                # Sunday=0, Monday=1, Tuesday=2
        REG_EX_TIME: packed(9, 0),          # 09:00
        REG_EX_QUIET: int(args.quiet_test),
        REG_EX_DURATION: 30,
    }

    print(f"Pacific controller time to set: {now.isoformat()} ({now.tzname()})")
    print("Exercise to set: weekly, Tuesday 09:00, 30 minutes, " + ("quiet test" if args.quiet_test else "normal exercise"))
    print("Clock registers:", " ".join(f"{v:04X}" for v in clock_values))
    print("Exercise registers:", " ".join(f"{a:04X}={v:04X}" for a, v in exercise_values.items()))

    if not args.apply:
        print("Dry run only. Re-run with --apply to write these values.")
        return 0

    bus = None
    service_stopped = False
    exit_code = 0
    try:
        if not args.no_service_control:
            print(f"Stopping service {args.service}...")
            control_service(args.service, "stop")
            service_stopped = True

        bus = ModbusRTU(args.port, args.baud, args.unit)
        before = bus.read_holding(REG_CLOCK, 3)
        print("Controller time before:", decode_clock(before))

        # Match the established controller write behavior: all three clock registers in one 0x10 request.
        bus.write_holding(REG_CLOCK, clock_values)

        # Match the established exercise write behavior: frequency, day, and time are separate writes.
        bus.write_holding(REG_EX_FREQUENCY, [exercise_values[REG_EX_FREQUENCY]])
        bus.write_holding(REG_EX_DAY, [exercise_values[REG_EX_DAY]])
        bus.write_holding(REG_EX_TIME, [exercise_values[REG_EX_TIME]])
        bus.write_holding(REG_EX_QUIET, [exercise_values[REG_EX_QUIET]])
        if not args.skip_duration:
            bus.write_holding(REG_EX_DURATION, [exercise_values[REG_EX_DURATION]])

        after = bus.read_holding(REG_CLOCK, 3)
        schedule = bus.read_holding(REG_EX_TIME, 4)
        print("Controller time after: ", decode_clock(after))
        print(
            "Exercise readback 002C..002F:",
            " ".join(f"{REG_EX_TIME + i:04X}={value:04X}" for i, value in enumerate(schedule)),
        )
        if not args.skip_duration:
            duration = bus.read_holding(REG_EX_DURATION, 1)[0]
            print(f"Exercise duration 023E={duration} minute(s)")

        expected_schedule = [
            exercise_values[REG_EX_TIME], exercise_values[REG_EX_FREQUENCY],
            exercise_values[REG_EX_DAY], exercise_values[REG_EX_QUIET],
        ]
        if schedule != expected_schedule:
            raise ControllerError(f"Exercise verification failed: expected {expected_schedule}, received {schedule}")
        if not args.skip_duration and duration != 30:
            raise ControllerError(f"Duration verification failed: expected 30, received {duration}")
        print("All requested settings verified successfully.")
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
