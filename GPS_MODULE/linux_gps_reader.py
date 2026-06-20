#!/usr/bin/env python3
"""
One-time GPS Data Reader for NEO-M8N via Raspberry Pi Pico Bridge

"""

import serial
import pynmea2
import sys
import os
import json
import time
from datetime import datetime

# Configuration
SERIAL_PORT = "COM4"  # Update as needed
BAUD_RATE = 9600
SERIAL_TIMEOUT = 1

FIX_TIMEOUT_SECONDS = 60     # Time per attempt
MAX_RETRIES = 3             # Number of attempts
RETRY_DELAY_SECONDS = 5     # Delay between attempts

OUTPUT_DIR = "gps_data"


class GPSReader:
    def __init__(self, port=SERIAL_PORT, baudrate=BAUD_RATE):
        try:
            self.ser = serial.Serial(port, baudrate=baudrate, timeout=SERIAL_TIMEOUT)
            print(f"✓ Connected to GPS on {port} at {baudrate} baud")
        except serial.SerialException as e:
            print(f"✗ Error opening serial port {port}: {e}")
            sys.exit(1)

    def attempt_fix(self):
        """
        Attempt to obtain a GPS fix within FIX_TIMEOUT_SECONDS.
        Returns gps_data dict whether successful or not.
        """

        gps_data = {
            "latitude": None,      # Decimal degrees (pynmea2 default)
            "longitude": None,     # Decimal degrees (pynmea2 default)
            "altitude_m": None,
            "speed_kmh": None,     # May be None if RMC not received
            "utc_time": None,
            "num_satellites": None,
            "fix_quality": 0
        }

        start_time = time.time()

        while time.time() - start_time < FIX_TIMEOUT_SECONDS:
            line = self.ser.readline()
            if not line:
                continue

            try:
                line_str = line.decode("ascii", errors="ignore").strip()
                if not line_str:
                    continue

                msg = pynmea2.parse(line_str)

                # GGA: position + fix
                if isinstance(msg, pynmea2.types.talker.GGA):
                    gps_data["latitude"] = msg.latitude
                    gps_data["longitude"] = msg.longitude
                    gps_data["altitude_m"] = msg.altitude
                    gps_data["num_satellites"] = msg.num_sats
                    gps_data["fix_quality"] = msg.gps_qual
                    gps_data["utc_time"] = (
                        msg.timestamp.isoformat() if msg.timestamp else None
                    )

                    if msg.gps_qual and msg.gps_qual > 0:
                        return gps_data, True

                # RMC: speed
                elif isinstance(msg, pynmea2.types.talker.RMC):
                    if msg.status == "A" and msg.spd_over_grnd is not None:
                        gps_data["speed_kmh"] = msg.spd_over_grnd * 1.852

            except pynmea2.ParseError:
                continue

        return gps_data, False

    def read_with_retries(self):
        """
        Perform multiple GPS fix attempts.
        """
        for attempt in range(1, MAX_RETRIES + 1):
            print(f"Attempt {attempt}/{MAX_RETRIES} - waiting up to {FIX_TIMEOUT_SECONDS}s")

            gps_data, success = self.attempt_fix()

            if success:
                gps_data["attempt"] = attempt
                gps_data["status"] = "fix_acquired"
                return gps_data

            print("✗ No fix acquired")
            if attempt < MAX_RETRIES:
                print(f"Retrying in {RETRY_DELAY_SECONDS}s...\n")
                time.sleep(RETRY_DELAY_SECONDS)

        gps_data["status"] = "failed"
        gps_data["error"] = "GPS fix not acquired after retries"
        gps_data["attempt"] = MAX_RETRIES
        return gps_data

    def close(self):
        if self.ser.is_open:
            self.ser.close()


def save_to_json(data):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    filename = f"gps_data.json"
    filepath = os.path.join(OUTPUT_DIR, filename)

    data["recorded_at_utc"] = datetime.utcnow().isoformat()

    with open(filepath, "w") as f:
        json.dump(data, f, indent=4)

    print(f"✓ GPS data written to {filepath}")


def main():
    port = SERIAL_PORT
    if len(sys.argv) > 1:
        port = sys.argv[1]

    gps = GPSReader(port=port, baudrate=BAUD_RATE)

    try:
        gps_data = gps.read_with_retries()
        save_to_json(gps_data)
    except KeyboardInterrupt:
        print("\n✗ Interrupted by user")
    finally:
        gps.close()


if __name__ == "__main__":
    main()