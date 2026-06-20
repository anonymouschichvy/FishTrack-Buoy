#!/usr/bin/env python3
import time
import signal
import subprocess
from pathlib import Path

import gpiod
from gpiod.line import Direction, Value

# ===================== CONFIG =====================
IR_RELAY_PIN = 16            # BCM GPIO
CPU_TEMP_THRESHOLD = 30.0    # Celsius
CHECK_INTERVAL = 10          # seconds
GPIOCHIP_PATH = "/dev/gpiochip0"
THERMAL_PATH = Path("/sys/class/thermal/thermal_zone0/temp")
# ==================================================

# ===================== RELAY TRUTH TABLE =====================
# Active-Low Relay Logic:
# +----------------+------------+-----------------+
# | Intent         | GPIO Level | Relay State     |
# +----------------+------------+-----------------+
# | Relay ON       | LOW        | ON              |
# | Relay OFF      | HIGH       | OFF             |
# +--------------------------------------------------+

class RelayController:
    """Controls an active-low relay based on CPU temperature."""

    def __init__(self):
        self.relay_state = "OFF"
        self.relay_request = None
        self.running = True

        # Setup signal handlers for safe shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Initialize GPIO relay
        self._setup_relay()

    # ---------- SIGNAL HANDLER ----------
    def _signal_handler(self, *_):
        self.running = False

    # ---------- CPU TEMPERATURE ----------
    def get_cpu_temp(self):
        """Return CPU temperature in Celsius, or None if unavailable."""
        # Method 1: vcgencmd (Raspberry Pi)
        try:
            output = subprocess.check_output(
                ["vcgencmd", "measure_temp"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2
            )
            return float(output.split("=")[1].replace("'C", ""))
        except Exception:
            pass

        # Method 2: sysfs fallback
        try:
            if THERMAL_PATH.exists():
                return float(THERMAL_PATH.read_text()) / 1000.0
        except Exception:
            pass

        return None

    # ---------- RELAY SETUP ----------
    def _setup_relay(self):
        """Initialize relay GPIO as active-low (ON=LOW, OFF=HIGH)."""
        try:
            self.relay_request = gpiod.request_lines(
                GPIOCHIP_PATH,
                consumer="cpu-temp-relay",
                config={
                    IR_RELAY_PIN: gpiod.LineSettings(
                        direction=Direction.OUTPUT,
                        output_value=Value.ACTIVE  # OFF → HIGH
                    )
                }
            )
            self.relay_state = "OFF"
            print("Relay initialized (OFF, GPIO HIGH)")
        except Exception as e:
            print(f"GPIO init failed: {e}")
            self.relay_request = None

    # ---------- RELAY CONTROL ----------
    def relay_on(self):
        """Turn relay ON (active-low: GPIO LOW)."""
        if self.relay_request is None or self.relay_state == "ON":
            return
        try:
            self.relay_request.set_value(IR_RELAY_PIN, Value.INACTIVE)  # GPIO LOW
            self.relay_state = "ON"
            print("Relay ON (GPIO LOW)")
        except Exception as e:
            print(f"Relay ON failed: {e}")
            self.relay_off()

    def relay_off(self):
        """Turn relay OFF (active-low: GPIO HIGH)."""
        if self.relay_request is None or self.relay_state == "OFF":
            return
        try:
            self.relay_request.set_value(IR_RELAY_PIN, Value.ACTIVE)  # GPIO HIGH
            self.relay_state = "OFF"
            print("Relay OFF (GPIO HIGH)")
        except Exception as e:
            print(f"Relay OFF failed: {e}")

    # ---------- MAIN LOOP ----------
    def run(self):
        """Continuously monitor CPU temp and control relay."""
        while self.running:
            cpu_temp = self.get_cpu_temp()

            # Active-low relay logic
            if cpu_temp is None:
                print("CPU temperature unavailable → Relay OFF (safe)")
                self.relay_off()
            elif cpu_temp > CPU_TEMP_THRESHOLD:
                self.relay_on()
            else:
                self.relay_off()

            if cpu_temp is not None:
                print(f"CPU Temp: {cpu_temp:.1f}C | Relay: {self.relay_state}")

            time.sleep(CHECK_INTERVAL)

        self.cleanup()

    # ---------- CLEANUP ----------
    def cleanup(self):
        """Turn off relay and release GPIO safely."""
        print("Shutting down safely...")
        self.relay_off()

        try:
            if self.relay_request:
                self.relay_request.release()
        except Exception:
            pass

        print("Cleanup complete")


# ===================== MAIN =====================
if __name__ == "__main__":
    controller = RelayController()
    controller.run()
