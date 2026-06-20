import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler

BASE_DIR = Path(__file__).resolve().parent

# Configuration Defaults (can be overridden at runtime)
SERIAL_PORT = "/dev/serial/by-id/usb-Teensyduino_USB_Serial_12345678-if00"
LIGHT_PORT = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_ABCDEFGH-if00"
BAUD_RATE = 115200
CODENAME = "BUOY-POSEIDON"
BASE_NODE = "ATLAS-BASE_STATION"
NEIGHBOR_TIMEOUT = 300
DEFAULT_TTL = 5
RX_BUFFER_MAX = 2048
MAX_SEEN_MESSAGES = 1000
CHUNK_TIMEOUT = 120
FORWARD_JITTER_MIN = 0.05
FORWARD_JITTER_MAX = 0.25
MAX_PARSE_DEPTH = 5
COMPRESSION_LEVEL = 9
MAX_SEND_QUEUE_SIZE = 50
MAX_OUTPUT_QUEUE_SIZE = 1000
BATTERY_I2C_ADDR = 0x2d
LOW_VOL = 3150
I2C_BUS_ID = 1
CHUNK_ACK_TIMEOUT = 10
MAX_CHUNK_RETRIES = 3
CHUNK_RETRY_DELAY = 2

# Message types
MSG_TYPE_PING = "ping"
MSG_TYPE_ACK = "ack"
MSG_TYPE_LINUX_CMD = "linux_cmd"
MSG_TYPE_CMD_ACK = "cmd_ack"
MSG_TYPE_CMD_STATUS = "cmd_status"
MSG_TYPE_DATA = "data"
MSG_TYPE_CHUNK = "chunk"
MSG_TYPE_COMMAND = "command"
MSG_TYPE_ALERT = "alert"
MSG_TYPE_ALIVE = "alive"
MSG_TYPE_BATTERY = "battery"
MSG_TYPE_CHUNK_ACK = "chunk_ack"

# File paths using absolute paths with BASE_DIR
SONAR_RESULTS = str(BASE_DIR / "SONAR_DATA" / "sonar_detection_results.json")
FISH_RESULTS = str(BASE_DIR / "FISHCOMPILE" / "data" / "compiled_fish_results.json")
GPS_DATA_RESULTS = str(BASE_DIR / "GPS_MODULE" / "data" / "gps_data.json")

# Script paths
FISH_DETECTION_SCRIPT = str(BASE_DIR / "FISHDETECTION" / "fish_detect.py")
SONAR_SCRIPT = str(BASE_DIR / "SONAR_DATA" / "echo_interface.py")
GPS_SCRIPT = str(BASE_DIR / "GPS_MODULE" / "linux_gps_reader.py")

COMMAND_RESULT_REGISTRY = {
    "fish_detect": {
        "script_label": "FishDetection",
        "result_file": FISH_RESULTS
    },
    "sonar": {
        "script_label": "Sonar",
        "result_file": SONAR_RESULTS
    },
    "gps": {
        "script_label": "GPS",
        "result_file": GPS_DATA_RESULTS
    }
}

# Statistics tracking (Shared across modules)
stats = {
    "packets_sent": 0,
    "packets_received": 0,
    "packets_forwarded": 0,
    "chunks_reassembled": 0,
    "duplicates_dropped": 0,
    "send_failures": 0,
    "reconnections": 0,
    "chunk_retries": 0
}

# Global Logger instance
logger = logging.getLogger("LoRaController")

def setup_logging(codename=None):
    """Setup rotating file logger"""
    global CODENAME, logger
    if codename:
        CODENAME = codename
        
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    
    logger.setLevel(logging.INFO)
    
    # Remove existing handlers to avoid duplicates
    logger.handlers.clear()
    
    handler = RotatingFileHandler(
        log_dir / f"{CODENAME}.log",
        maxBytes=10*1024*1024,
        backupCount=5
    )
    handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    ))
    logger.addHandler(handler)
    
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(console)
    
    return logger
