#!/usr/bin/env python3
"""Read and decode the GenLink H-Panel exercise configuration.

This utility is deliberately read-only. It reads Modbus file 1, record 0x00EB,
which GenLink 4.0.1 identifies as the six-byte _Exercise_Control_Struct.
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


UNIT = 0x9D
FILE_NUMBER = 1
RECORD_NUMBER = 0x00EB
RECORD_WORDS = 3
SERIAL_PORT_CANDIDATES = ("/dev/USB232", "/dev/ttyUSB0", "/dev/ttySTM1", "/dev/serial0")
DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


class ProbeError(RuntimeError):
    pass


def handle_termination(_signum, _frame) -> None:
    raise KeyboardInterrupt


def resolve_serial_port(requested_port: str) -> str:
    if requested_port != "auto":
        return requested_port
    for candidate in SERIAL_PORT_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    raise ProbeError("No supported serial device found")


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
        raise ProbeError(f"Timed out while asking systemd to {action} {service!r}")
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ProbeError(f"Could not {action} service {service!r}: {detail}")


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
        raise ProbeError(f"Short Modbus response: received {len(data)} of {count} bytes")
    return bytes(data)


def read_exercise(port_name: str, baud: int, unit: int) -> bytes:
    # Function 0x14: Read File Record. The response for one three-word
    # subrequest is 13 bytes including address, function and CRC.
    request = add_crc(
        bytes((unit, 0x14, 0x07, 0x06))
        + struct.pack(">HHH", FILE_NUMBER, RECORD_NUMBER, RECORD_WORDS)
    )
    with serial.Serial(
        port=port_name,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=2.0,
    ) as port:
        port.reset_input_buffer()
        port.reset_output_buffer()
        port.write(request)
        port.flush()
        response = read_exact(port, 13)

    if modbus_crc(response[:-2]) != struct.unpack("<H", response[-2:])[0]:
        raise ProbeError("Invalid Modbus CRC in response")
    if response[0] != unit:
        raise ProbeError(f"Response came from unexpected unit 0x{response[0]:02X}")
    if response[1] & 0x80:
        raise ProbeError(f"Controller returned Modbus exception 0x{response[2]:02X}")
    if response[:5] != bytes((unit, 0x14, 0x08, 0x07, 0x06)):
        raise ProbeError("Unexpected Read File Record response format")
    return response[5:11]


def decode_exercise(data: bytes) -> None:
    if len(data) != 6:
        raise ProbeError(f"Expected six exercise bytes; received {len(data)}")
    flags, day, hour, minute, duration, transfer = data
    day_text = DAY_NAMES[day] if day < len(DAY_NAMES) else f"invalid({day})"
    print("Raw exercise bytes:", " ".join(f"{value:02X}" for value in data))
    print("Enabled:", "yes" if flags & 0x01 else "no")
    print("Low-speed/quiet:", "yes" if flags & 0x02 else "no")
    print("Day:", day_text, f"({day})")
    print(f"Start time: {hour:02d}:{minute:02d}")
    print(f"Duration: {duration} minute(s)")
    print("Transfer on exercise:", "yes" if transfer else "no", f"(raw {transfer})")
    if day > 6 or hour > 23 or minute > 59:
        raise ProbeError("Decoded schedule contains an invalid day or time")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="auto")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--unit", type=lambda value: int(value, 0), default=UNIT)
    parser.add_argument("--service", default="olmaster")
    parser.add_argument("--no-service-control", action="store_true")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, handle_termination)
    service_stopped = False
    exit_code = 0
    try:
        port_name = resolve_serial_port(args.port)
        print("Serial port:", port_name)
        print(f"Reading Modbus file {FILE_NUMBER}, record 0x{RECORD_NUMBER:04X}, {RECORD_WORDS} words")
        if not args.no_service_control:
            print(f"Stopping service {args.service}...")
            service_stopped = True
            control_service(args.service, "stop")
        decode_exercise(read_exercise(port_name, args.baud, args.unit))
        print("Read-only H-Panel exercise probe completed successfully.")
    except (ProbeError, OSError, ValueError, serial.SerialException) as exc:
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
            except ProbeError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                exit_code = exit_code or 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
