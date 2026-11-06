#!/usr/bin/env python3
"""Safely set and verify a Generac H-100 exercise schedule over Modbus RTU.

The schedule is stored in GenLink's six-byte _Exercise_Control_Struct at
Modbus file 1, record 0x00EB. The program reads the current structure first,
preserves unknown flag bits, and is dry-run unless --apply is supplied.
"""

from __future__ import annotations

import argparse
import os
import signal
import struct
import subprocess
import sys

try:
    import serial
except ImportError as exc:
    print(f"Could not import pyserial in {sys.executable}: {exc}", file=sys.stderr)
    raise SystemExit(2)


UNIT = 0x64
FILE_NUMBER = 1
RECORD_NUMBER = 0x00EB
RECORD_WORDS = 3
SERIAL_PORT_CANDIDATES = ("/dev/USB232", "/dev/ttySTM1", "/dev/ttyUSB0", "/dev/serial0")
DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


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
    raise ControllerError("No supported serial device found")


def control_service(service: str, action: str) -> None:
    try:
        result = subprocess.run(
            ["systemctl", action, service], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise ControllerError(f"Timed out while asking systemd to {action} {service!r}")
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ControllerError(f"Could not {action} service {service!r}: {detail}")


def modbus_crc(data: bytes) -> int:
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def add_crc(data: bytes) -> bytes:
    return data + struct.pack("<H", modbus_crc(data))


def read_exact(port: serial.Serial, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        chunk = port.read(count - len(data))
        if not chunk:
            break
        data.extend(chunk)
    if len(data) != count:
        raise ControllerError(f"Short Modbus response: received {len(data)} of {count} bytes")
    return bytes(data)


def transact(port: serial.Serial, request: bytes, expected_function: int) -> bytes:
    port.reset_input_buffer()
    port.reset_output_buffer()
    port.write(add_crc(request))
    port.flush()
    header = read_exact(port, 3)
    if header[0] != request[0]:
        raise ControllerError(f"Response came from unexpected unit 0x{header[0]:02X}")
    if header[1] == (expected_function | 0x80):
        response = header + read_exact(port, 2)
        if modbus_crc(response[:-2]) != struct.unpack("<H", response[-2:])[0]:
            raise ControllerError("Invalid Modbus CRC in exception response")
        raise ControllerError(f"Controller returned Modbus exception 0x{header[2]:02X}")
    if header[1] != expected_function:
        raise ControllerError(f"Unexpected Modbus function 0x{header[1]:02X}")
    response = header + read_exact(port, header[2] + 2)
    if modbus_crc(response[:-2]) != struct.unpack("<H", response[-2:])[0]:
        raise ControllerError("Invalid Modbus CRC in response")
    return response


def read_exercise(port: serial.Serial, unit: int) -> bytes:
    request = bytes((unit, 0x14, 0x07, 0x06)) + struct.pack(
        ">HHH", FILE_NUMBER, RECORD_NUMBER, RECORD_WORDS
    )
    response = transact(port, request, 0x14)
    if response[:5] != bytes((unit, 0x14, 0x08, 0x07, 0x06)) or len(response) != 13:
        raise ControllerError("Unexpected Read File Record response format")
    return response[5:11]


def write_exercise(port: serial.Serial, unit: int, data: bytes) -> None:
    if len(data) != 6:
        raise ValueError("H-Panel exercise structure must contain exactly six bytes")
    request = bytes((unit, 0x15, 0x0D, 0x06)) + struct.pack(
        ">HHH", FILE_NUMBER, RECORD_NUMBER, RECORD_WORDS
    ) + data
    response = transact(port, request, 0x15)
    if response != add_crc(request):
        raise ControllerError("Write File Record response did not echo the request")


def describe(label: str, data: bytes) -> None:
    if len(data) != 6:
        raise ControllerError(f"Expected six exercise bytes; received {len(data)}")
    flags, day, hour, minute, duration, transfer = data
    day_text = DAY_NAMES[day] if day < len(DAY_NAMES) else f"invalid({day})"
    print(f"{label} raw bytes:", " ".join(f"{value:02X}" for value in data))
    print(
        f"{label}: enabled={'yes' if flags & 0x01 else 'no'}, "
        f"quiet={'yes' if flags & 0x02 else 'no'}, {day_text} "
        f"{hour:02d}:{minute:02d}, {duration} minutes, "
        f"transfer={'yes' if transfer else 'no'}"
    )
    if day > 6 or hour > 23 or minute > 59:
        raise ControllerError(f"{label} schedule contains an invalid day or time")


def requested_schedule(current: bytes, day: int, hour: int, minute: int, duration: int) -> bytes:
    # GenLink clears bits 1 and 2 for normal exercise. Preserve all other bits,
    # including observed bit 4, and set bit 0 to enable exercise.
    flags = (current[0] & 0xF9) | 0x01
    return bytes((flags, day, hour, minute, duration, 0))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="auto")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--unit", type=lambda value: int(value, 0), default=UNIT)
    parser.add_argument("--day", type=int, choices=range(7), default=2, metavar="0..6")
    parser.add_argument("--hour", type=int, choices=range(24), default=9)
    parser.add_argument("--minute", type=int, choices=range(60), default=5)
    parser.add_argument("--duration", type=int, choices=range(1, 256), default=18)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--service", default="olmaster")
    parser.add_argument("--no-service-control", action="store_true")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, handle_termination)
    service_stopped = False
    exit_code = 0
    try:
        port_name = resolve_serial_port(args.port)
        print("Serial port:", port_name)
        print(f"Modbus unit: 0x{args.unit:02X}")
        print(
            f"Exercise target: {DAY_NAMES[args.day]} {args.hour:02d}:{args.minute:02d}, "
            f"{args.duration} minutes, normal exercise, no transfer"
        )
        if not args.no_service_control:
            print(f"Stopping service {args.service}...")
            service_stopped = True
            control_service(args.service, "stop")
        with serial.Serial(
            port=port_name, baudrate=args.baud, bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE, timeout=2.0,
        ) as port:
            current = read_exercise(port, args.unit)
            describe("Current", current)
            desired = requested_schedule(current, args.day, args.hour, args.minute, args.duration)
            describe("Requested", desired)
            if not args.apply:
                print("Dry run only. Re-run with --apply to write and verify the schedule.")
            else:
                write_exercise(port, args.unit, desired)
                verified = read_exercise(port, args.unit)
                describe("Verified", verified)
                if verified != desired:
                    raise ControllerError(
                        "Exercise verification failed: readback did not match requested bytes"
                    )
                print("H-Panel exercise schedule verified successfully.")
    except (ControllerError, OSError, ValueError, serial.SerialException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        exit_code = 1
    except KeyboardInterrupt:
        print("Interrupted; restoring service state...", file=sys.stderr)
        exit_code = 130
    finally:
        if service_stopped:
            print(f"Starting service {args.service}...")
            try:
                control_service(args.service, "start")
                print(f"Service {args.service} started.")
            except ControllerError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                exit_code = exit_code or 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
