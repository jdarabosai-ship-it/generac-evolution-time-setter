#!/usr/bin/env python3
"""Set a Generac Evolution controller clock and weekly exercise schedule.

Defaults:
  * Controller clock: current America/Los_Angeles time (PST/PDT as applicable)
  * Exercise: Tuesday at 09:05, weekly, 25 minutes
  * Modbus RTU: unit 0x9D, 9600 8N1

The program is dry-run by default. Pass --apply to perform writes.
Requires Python 3.7+ and pymodbus.
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
    # PyModbus 3.x
    from pymodbus.client import ModbusSerialClient
except ImportError:
    try:
        # PyModbus 2.x, commonly shipped on older embedded Linux images
        from pymodbus.client.sync import ModbusSerialClient
    except ImportError as exc:
        print(
            "Could not import PyModbus in this Python interpreter "
            f"({sys.executable}): {exc}",
            file=sys.stderr,
        )
        print(
            "olmaster may use a different interpreter or virtual environment. "
            "Check it with: systemctl cat olmaster",
            file=sys.stderr,
        )
        raise SystemExit(2)

from pymodbus.exceptions import ModbusException


UNIT = 0x9D
REG_CLOCK = 0x000E       # 000E..0010: hour/minute, month/day, weekday/year
REG_EX_TIME = 0x002C     # high byte hour, low byte minute
REG_EX_FREQUENCY = 0x002D
REG_EX_DAY = 0x002E
REG_EX_QUIET = 0x002F
REG_EX_DURATION = 0x023E
SERIAL_PORT_CANDIDATES = ("/dev/USB232", "/dev/ttySTM1", "/dev/ttyUSB0", "/dev/serial0")


class ControllerError(RuntimeError):
    pass


def handle_termination(_signum, _frame) -> None:
    """Route SIGTERM through the normal cleanup path."""
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
        client_parameters = {
            "port": self.port,
            "baudrate": self.baud,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
            "timeout": self.timeout,
            "retries": 2,
        }
        # PyModbus 2.x defaults to ASCII framing unless RTU is requested.
        # PyModbus 3.x removed this keyword and uses RTU for the serial client.
        if "method" in inspect.signature(ModbusSerialClient).parameters:
            client_parameters["method"] = "rtu"
        self.client = ModbusSerialClient(**client_parameters)
        if not self.client.connect():
            raise ControllerError(f"Could not open/connect to {self.port}")

        # The unit keyword changed across PyModbus releases.
        parameters = inspect.signature(self.client.read_holding_registers).parameters
        if "device_id" in parameters:
            self.unit_parameter = "device_id"
        elif "slave" in parameters:
            self.unit_parameter = "slave"
        else:
            self.unit_parameter = "unit"

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


def clock_datetime(values: list[int], timezone: dt.tzinfo) -> dt.datetime:
    clock, date, weekday_year = values
    return dt.datetime(
        2000 + (weekday_year & 0xFF),
        date >> 8,
        date & 0xFF,
        clock >> 8,
        clock & 0xFF,
        tzinfo=timezone,
    )


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
    parser.add_argument(
        "--port",
        default="auto",
        help="Serial device; default auto-detects /dev/USB232 or OL V4 /dev/ttySTM1",
    )
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--unit", type=lambda x: int(x, 0), default=UNIT, help="Modbus unit, default 0x9D")
    parser.add_argument("--timezone", default="America/Los_Angeles", help="IANA timezone; default observes PST/PDT")
    parser.add_argument("--apply", action="store_true", help="Actually write settings; otherwise perform a dry run")
    parser.add_argument("--clock-only", action="store_true", help="Sync only the clock; do not alter the exercise schedule")
    parser.add_argument("--max-drift-minutes", type=float, default=5.0, help="With --clock-only, write only when drift is greater than this value; default: 5")
    parser.add_argument("--quiet-test", action="store_true", help="Enable quiet-test mode; default is off")
    parser.add_argument("--exercise-hour", type=int, choices=range(24), default=9, metavar="0-23")
    parser.add_argument("--exercise-minute", type=int, choices=range(60), default=5, metavar="0-59")
    parser.add_argument("--duration", type=int, choices=range(1, 256), default=25, metavar="1-255")
    parser.add_argument("--skip-duration", action="store_true", help="Do not write 0x023E if this model rejects duration")
    parser.add_argument("--service", default="olmaster", help="Service to stop during writes; default: olmaster")
    parser.add_argument("--no-service-control", action="store_true", help="Do not stop or start a service")
    args = parser.parse_args()

    # Let systemd and fleet-command timeout SIGTERM follow the same cleanup path
    # as Ctrl-C, ensuring olmaster is restarted from the finally block.
    signal.signal(signal.SIGTERM, handle_termination)

    if args.max_drift_minutes < 0:
        parser.error("--max-drift-minutes cannot be negative")

    try:
        now = current_time_in_zone(args.timezone)
        serial_port = resolve_serial_port(args.port)
    except (ControllerError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    # The known Evolution write path uses zero for the weekday byte and lets the controller derive it.
    clock_values = [packed(now.hour, now.minute), packed(now.month, now.day), packed(0, now.year - 2000)]
    exercise_values = {
        REG_EX_FREQUENCY: 0x0000,          # weekly
        REG_EX_DAY: 0x0002,                # Sunday=0, Monday=1, Tuesday=2
        REG_EX_TIME: packed(args.exercise_hour, args.exercise_minute),
        REG_EX_QUIET: int(args.quiet_test),
        REG_EX_DURATION: args.duration,
    }

    print(f"Pacific controller time to set: {now.isoformat()} ({now.tzname()})")
    print(f"Serial port: {serial_port}")
    print("Clock registers:", " ".join(f"{v:04X}" for v in clock_values))
    if args.clock_only:
        print(f"Clock-only mode: write only when drift is greater than {args.max_drift_minutes:g} minutes")
    else:
        print(
            f"Exercise to set: weekly, Tuesday {args.exercise_hour:02d}:{args.exercise_minute:02d}, "
            f"{args.duration} minutes, " + ("quiet test" if args.quiet_test else "normal exercise")
        )
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

        bus = ModbusRTU(serial_port, args.baud, args.unit)
        before = bus.read_holding(REG_CLOCK, 3)
        print("Controller time before:", decode_clock(before))

        controller_before = clock_datetime(before, now.tzinfo)
        drift_minutes = abs((controller_before - now).total_seconds()) / 60.0
        print(f"Clock drift: {drift_minutes:.1f} minute(s)")

        if args.clock_only and drift_minutes <= args.max_drift_minutes:
            print("Clock is within tolerance; no write needed.")
        else:
            # Match the established controller write behavior: all three clock registers in one 0x10 request.
            bus.write_holding(REG_CLOCK, clock_values)
            after = bus.read_holding(REG_CLOCK, 3)
            print("Controller time after: ", decode_clock(after))
            after_drift = abs((clock_datetime(after, now.tzinfo) - now).total_seconds()) / 60.0
            if after_drift > 1.0:
                raise ControllerError(f"Clock verification failed: still {after_drift:.1f} minutes off")

        if not args.clock_only:
            # Match the established exercise write behavior: frequency, day, and time are separate writes.
            bus.write_holding(REG_EX_FREQUENCY, [exercise_values[REG_EX_FREQUENCY]])
            bus.write_holding(REG_EX_DAY, [exercise_values[REG_EX_DAY]])
            bus.write_holding(REG_EX_TIME, [exercise_values[REG_EX_TIME]])
            bus.write_holding(REG_EX_QUIET, [exercise_values[REG_EX_QUIET]])
            if not args.skip_duration:
                bus.write_holding(REG_EX_DURATION, [exercise_values[REG_EX_DURATION]])

            schedule = bus.read_holding(REG_EX_TIME, 4)
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
            if not args.skip_duration and duration != args.duration:
                raise ControllerError(f"Duration verification failed: expected {args.duration}, received {duration}")
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
