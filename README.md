# Generac Evolution Time and Exercise Setter

Sets a Generac Evolution controller to the current Pacific time and configures a weekly Tuesday 9:05 AM exercise cycle lasting 25 minutes.

The utility communicates over Modbus RTU using PyModbus. By default it uses:

- Serial port: `/dev/USB232`
- Unit address: `0x9D`
- Serial settings: 9600 baud, 8N1
- Time zone: `America/Los_Angeles` (PST/PDT automatically)
- Exercise: Tuesday at 9:05 AM for 25 minutes
- Service control: stops `olmaster` before writing and starts it again afterward

## Requirements

- Linux
- Python 3.7+
- Linux system timezone data (`tzdata`; normally already installed)
- PyModbus
- Permission to use the serial device and control `olmaster`

```bash
python3 -m pip install pymodbus
```

## Usage

Preview the intended settings without writing or controlling the service:

```bash
python3 set_evolution_clock_exercise.py
```

Apply and verify the settings:

```bash
sudo python3 set_evolution_clock_exercise.py --apply
```

Use `--help` for serial-port, unit-address, quiet-test, duration, and service-control options.

## Safety

The program is dry-run by default. With `--apply`, it stops `olmaster`, performs the Modbus writes, verifies the readback, closes the connection, and restarts `olmaster` even when the operation fails or is interrupted.
