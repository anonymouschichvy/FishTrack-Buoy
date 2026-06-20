import sys
import numpy as np
import serial
import serial.tools.list_ports
import struct
import time
import socket
import os
import cv2
import threading
import argparse
import json
import torch
import signal
signal.signal(signal.SIGINT, lambda sig, frame: safe_exit())
from datetime import datetime
from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget, QComboBox, QPushButton, QLabel, QLineEdit
from PyQt5.QtCore import QThread, pyqtSignal, QTimer, QSize
from PyQt5.QtGui import QPainter, QPixmap
import pyqtgraph as pg
import qdarktheme
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QComboBox, QPushButton, QWidget, QCheckBox
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPalette, QColor

# Check if YOLO is available
YOLO_AVAILABLE = False
YOLO_ERROR_MSG = None
YOLO = None  # Define YOLO as None if import fails
SERIAL_PORT = "/dev/serial/by-id/usb-Arduino__www.arduino.cc__0043_24238313635351910130-if00"   # <-- CHANGE THIS to your Pi device

def try_load_yolo():
    """Safely import YOLO after GUI and PyTorch are initialized."""
    global YOLO_AVAILABLE, YOLO_ERROR_MSG, YOLO
    try:
        from ultralytics import YOLO
        YOLO_AVAILABLE = True
        print("✅ YOLO loaded successfully (delayed import)")
    except OSError as e:
        if "DLL" in str(e) or "WinError 1114" in str(e):
            YOLO_ERROR_MSG = "PyTorch DLL initialization failed"
            print("⚠️  PyTorch DLL initialization failed")
        else:
            YOLO_ERROR_MSG = f"OS Error: {str(e)}"
            print(f"⚠️  OS Error while loading YOLO: {e}")
    except ImportError as e:
        YOLO_ERROR_MSG = f"Import error: {str(e)}"
        print(f"⚠️  Import error loading YOLO: {e}")
    except Exception as e:
        YOLO_ERROR_MSG = f"Unknown error: {str(e)}"
        print(f"⚠️  Unknown error loading YOLO: {e}")

# ============= SIMULATION MODE =============
SIMULATION_MODE = False  # Set to True to enable simulation
SIMULATION_UPDATE_RATE = 50  # milliseconds between updates
# ===========================================

# Serial Configuration
BAUD_RATE = 250000
NUM_SAMPLES = 1800  # Number of frequency/amplitude bins (X-axis)

MAX_ROWS = 300  # Number of time steps (Y-axis)
Y_LABEL_DISTANCE = 50  # distance between labels in cm

SPEED_OF_SOUND = 1500  # meters per second in water
# SPEED_OF_SOUND = 330  # meters per second in air
SAMPLE_TIME = 13.2e-6  # 13.2 microseconds in seconds Atmega328 sample speed
# SAMPLE_TIME = 7.682e-6  # 7.682 microseconds in seconds STM32 sample speed

DEFAULT_LEVELS = (0, 256)  # Expected data range

SAMPLE_RESOLUTION = (SPEED_OF_SOUND * SAMPLE_TIME * 100) / 2  # cm per row (0.99 cm per row)
PACKET_SIZE = 1 + 6 + 2 * NUM_SAMPLES + 1  # header + payload + checksum
MAX_DEPTH = NUM_SAMPLES * SAMPLE_RESOLUTION  # Total depth in cm
depth_labels = {int(i / SAMPLE_RESOLUTION): f"{i / 100}" for i in range(0, int(MAX_DEPTH), Y_LABEL_DISTANCE)}

# Snapshot configuration
SNAPSHOT_DIR = "image"
OUTPUT_DIR = "output"
DATA_DIR = "data"

# Fish detection configuration
MODEL_PATH = 'model.pt'
MIN_CONFIDENCE = 0.5
DETECTION_ENABLED = True  # Will be set based on model availability

# Bounding box colors for detections
bbox_colors = [(164, 120, 87), (68, 148, 228), (93, 97, 209), (178, 182, 133), (88, 159, 106),
               (96, 202, 231), (159, 124, 168), (169, 162, 241), (98, 118, 150), (172, 176, 184)]

def ensure_directories():
    """Create necessary directories if they don't exist."""
    for directory in [SNAPSHOT_DIR, OUTPUT_DIR, DATA_DIR]:
        if not os.path.exists(directory):
            os.makedirs(directory)
            print(f"📁 Created directory: {directory}")

def read_packet(ser):
    while True:
        header = ser.read(1)
        if header != b"\xaa":
            continue  # Wait for the start byte

        payload = ser.read(6 + NUM_SAMPLES)
        checksum = ser.read(1)

        if len(payload) != 6 + NUM_SAMPLES or len(checksum) != 1:
            continue  # Incomplete packet

        # Verify checksum
        calc_checksum = 0
        for byte in payload:
            calc_checksum ^= byte
        if calc_checksum != checksum[0]:
            print("⚠️ Checksum mismatch: {} != {}".format(calc_checksum, checksum[0]))
            continue

        # Unpack payload (firmware sends little-endian raw struct bytes)
        depth, temp_scaled, vDrv_scaled = struct.unpack("<HhH", payload[:6])
        depth = min(depth, NUM_SAMPLES)

        sample_bytes = payload[6:6+NUM_SAMPLES]
        values = np.frombuffer(sample_bytes, dtype=np.uint8, count=NUM_SAMPLES)

        temperature = temp_scaled / 100.0
        drive_voltage = vDrv_scaled / 100.0

        return values, depth, temperature, drive_voltage

def generate_simulated_data():
    """Generate realistic simulated sonar echogram data with fish echoes."""
    global t
    try:
        t += 1
    except NameError:
        t = 0

    # Base noise floor (stronger near surface, decaying with depth)
    depth = np.linspace(0, 1, NUM_SAMPLES)
    noise_floor = 30 + 40 * np.exp(-4 * depth)
    samples = noise_floor + np.random.normal(0, 5, NUM_SAMPLES)

    # Simulate moving thermocline layer (strong reflective band)
    thermocline_depth = int(0.6 * NUM_SAMPLES + 0.05 * NUM_SAMPLES * np.sin(t * 0.03))
    band_thickness = 10
    band_strength = 80 + 20 * np.sin(t * 0.1)
    samples[max(0, thermocline_depth - band_thickness // 2):thermocline_depth + band_thickness // 2] += band_strength

    # Simulate fish schools as moving Gaussian echoes
    num_fish = np.random.randint(3, 8)
    for _ in range(num_fish):
        fish_depth = np.random.randint(50, NUM_SAMPLES - 100)
        fish_strength = np.random.randint(120, 255)
        fish_width = np.random.randint(3, 8)
        fish_echo = fish_strength * np.exp(-0.5 * ((np.arange(NUM_SAMPLES) - fish_depth) / fish_width) ** 2)
        samples += fish_echo

    # Clamp and convert to integer array
    samples = np.clip(samples, 0, 255).astype(np.uint16)

    # Simulated parameters
    depth_index = thermocline_depth
    temperature = 23.0 + np.sin(t * 0.05) * 1.5 + np.random.randn() * 0.1
    drive_voltage = 12.0 + np.random.randn() * 0.1

    return samples, depth_index, temperature, drive_voltage

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class NMEAServerThread(QThread):
    """Background thread to handle NMEA TCP connections without blocking the GUI."""
    client_connected = pyqtSignal()
    connection_failed = pyqtSignal(str)
    
    def __init__(self, port):
        super().__init__()
        self.port = port
        self.running = True
        self.server_socket = None
        self.client_socket = None
        
    def run(self):
        """Run the NMEA server in a background thread."""
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.settimeout(1.0)  # Set timeout for accept()
            self.server_socket.bind(('0.0.0.0', self.port))
            self.server_socket.listen(1)
            print(f"📡 Waiting for TCP NMEA connection on port {self.port}...")
            
            while self.running:
                try:
                    self.client_socket, addr = self.server_socket.accept()
                    print(f"✅ NMEA client connected from {addr} on port {self.port}")
                    self.client_connected.emit()
                    break
                except socket.timeout:
                    continue  # Keep checking if we should still be running
                except Exception as e:
                    if self.running:
                        print(f"⚠️ Error accepting connection: {e}")
                    break
                    
        except Exception as e:
            error_msg = f"Failed to set up NMEA server: {e}"
            print(f"❌ {error_msg}")
            self.connection_failed.emit(error_msg)
            
    def stop(self):
        """Stop the server thread."""
        self.running = False
        if self.client_socket:
            try:
                self.client_socket.close()
            except:
                pass
        if self.server_socket:
            try:
                self.server_socket.close()
            except:
                pass
                
    def send_data(self, data):
        """Send data to connected client."""
        if self.client_socket:
            try:
                self.client_socket.sendall(data.encode())
                return True
            except:
                return False
        return False


class FishDetectionThread(QThread):
    """
    Background thread for processing images with fish detection.
    """
    detection_complete = pyqtSignal(str, int, list)  # image_name, fish_count, detections
    
    def __init__(self, model_path):
        super().__init__()
        self.model_path = model_path
        self.model = None
        self.running = True
        self.image_queue = []
        self.processed_images = {}
        self.json_file = os.path.join(DATA_DIR, 'sonar_detection_results.json')
        
        # Load processed images history
        self.load_processed_images()
        
    def load_processed_images(self):
        """Load the list of already processed images from JSON file"""
        if os.path.exists(self.json_file):
            try:
                with open(self.json_file, 'r') as f:
                    data = json.load(f)
                    self.processed_images = {item['Image Name']: item for item in data}
                    print(f"📊 Loaded {len(self.processed_images)} previously processed images")
            except json.JSONDecodeError:
                print(f'⚠️ Could not read {self.json_file}. Starting fresh.')
                self.processed_images = {}
    
    def save_results(self):
        """Save all results to JSON file"""
        results_list = list(self.processed_images.values())
        with open(self.json_file, 'w') as f:
            json.dump(results_list, f, indent=2)
    
    def add_image_to_queue(self, image_path):
        """Add an image to the processing queue"""
        if image_path not in self.image_queue:
            self.image_queue.append(image_path)
    
    def run(self):
        """Process images from the queue"""
        if not YOLO_AVAILABLE or YOLO is None:
            print("❌ YOLO not available. Fish detection thread stopped.")
            return
            
        try:
            # Load the model
            print(f'🔄 Loading YOLO model from {self.model_path}...')
            self.model = YOLO(self.model_path, task='detect')
            print(f'✅ Model loaded. Classes: {self.model.names}')
            
            while self.running:
                if self.image_queue:
                    image_path = self.image_queue.pop(0)
                    self.process_image(image_path)
                else:
                    time.sleep(0.1)  # Wait for new images
                    
        except Exception as e:
            print(f"❌ Error in FishDetectionThread: {e}")
    
    def process_image(self, img_path):
        """Process a single image and detect fish"""
        img_name = os.path.basename(img_path)
        
        # Skip if already processed
        if img_name in self.processed_images:
            return
        
        print(f'🔍 Processing: {img_name}')
        
        # Load image
        frame = cv2.imread(img_path)
        if frame is None:
            print(f'  ⚠️ Could not read image {img_path}')
            return
        
        # Run inference
        results = self.model(frame, verbose=False)
        detections = results[0].boxes
        
        # Count fish detections
        fish_count = 0
        detected_objects = []
        
        # Process each detection
        for i in range(len(detections)):
            # Get bounding box coordinates
            xyxy_tensor = detections[i].xyxy.cpu()
            xyxy = xyxy_tensor.numpy().squeeze()
            xmin, ymin, xmax, ymax = xyxy.astype(int)
            
            # Get class and confidence
            classidx = int(detections[i].cls.item())
            classname = self.model.names[classidx]
            conf = detections[i].conf.item()
            
            # Only process detections above confidence threshold
            if conf > MIN_CONFIDENCE:
                fish_count += 1
                
                # Store detection info
                detected_objects.append({
                    'class': classname,
                    'confidence': float(conf),
                    'bbox': [int(xmin), int(ymin), int(xmax), int(ymax)]
                })
                
                # Draw bounding box
                color = bbox_colors[classidx % 10]
                cv2.rectangle(frame, (xmin, ymin), (xmax, ymax), color, 2)
                
                # Draw label
                label = f'{classname}: {int(conf * 100)}%'
                labelSize, baseLine = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
                label_ymin = max(ymin, labelSize[1] + 10)
                cv2.rectangle(frame, (xmin, label_ymin - labelSize[1] - 10),
                             (xmin + labelSize[0], label_ymin + baseLine - 10), color, cv2.FILLED)
                cv2.putText(frame, label, (xmin, label_ymin - 7),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
        
        # Add detection count to image
        cv2.putText(frame, f'Fish Detected: {fish_count}', (10, 30),
                   cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)
        
        # Save annotated image to output directory
        output_path = os.path.join(OUTPUT_DIR, img_name)
        cv2.imwrite(output_path, frame)
        
        # Create result entry
        result = {
            'Image Name': img_name,
            'Fish Detect': fish_count,
            'Detections': detected_objects
        }
        
        # Store and save results
        self.processed_images[img_name] = result
        self.save_results()
        
        print(f'✅ Detected {fish_count} fish. Saved to {output_path}')
        
        # Emit signal
        self.detection_complete.emit(img_name, fish_count, detected_objects)
        # Remove image after processing
        try:
            os.remove(img_path)
            print(f"🗑️  Deleted source image: {img_path}")
        except Exception as e:
            print(f"⚠️  Failed to delete image {img_path}: {e}")

    
    def stop(self):
        self.running = False
        self.quit()
        self.wait()


class DataCaptureThread(QThread):
    """
    Background thread that continuously captures echogram data.
    """
    data_received = pyqtSignal(np.ndarray, float, float, float)
    snapshot_ready = pyqtSignal(np.ndarray, str)  # data array, filename

    def __init__(self, port, baud_rate):
        super().__init__()
        self.port = port
        self.baud_rate = baud_rate
        self.running = True
        self.data = np.zeros((MAX_ROWS, NUM_SAMPLES))
        self.rows_received = 0

    def run(self):
        """Continuously read serial data and emit processed arrays."""
        try:
            if self.port == "SIMULATION":
                print("🎮 Starting simulation mode...")
                while self.running:
                    values, depth, temperature, drive_voltage = generate_simulated_data()
                    self._process_data(values, depth, temperature, drive_voltage)
                    time.sleep(SIMULATION_UPDATE_RATE / 1000.0)
            else:
                try:
                    ser = serial.Serial(self.port, BAUD_RATE, timeout=1)
                    print(f"✅ Connected to {self.port}")
                    while self.running:
                        result = read_packet(ser)
                        if result:
                            values, depth, temperature, drive_voltage = result
                            self._process_data(values, depth, temperature, drive_voltage)
                except serial.SerialException as e:
                    print(f"❌ Serial Error: {e}")
                except OSError as e:
                    print(f"❌ Serial Port Error: {e}")
                    print(f"⚠️  Port {self.port} may be in use or unavailable")
                finally:
                    if 'ser' in locals() and ser.is_open:
                        ser.close()
                        print(f"🔌 Port {self.port} closed")
        except Exception as e:
            print(f"❌ Unexpected error in DataCaptureThread: {e}")

    def _process_data(self, values, depth, temperature, drive_voltage):
        """Process incoming data and update internal buffer."""
        # Update internal data buffer
        self.data = np.roll(self.data, -1, axis=0)
        self.data[-1, :] = values
        self.rows_received += 1

        # Emit data for GUI update
        self.data_received.emit(values, depth, temperature, drive_voltage)

        # Check if it's time to save a snapshot
        if self.rows_received % MAX_ROWS == 0:
            timestamp = datetime.now().strftime("%d_%m_%Y_%H_%M_%S")
            filename = f"{timestamp}.png"
            self.snapshot_ready.emit(self.data.copy(), filename)

    def stop(self):
        self.running = False
        self.quit()
        self.wait()


class SettingsDialog(QWidget):
    def __init__(self, parent=None, current_gradient='thermal', current_speed=1500, 
                 nmea_enabled=False, nmea_port=10110, nmea_address="127.0.0.1",
                 detection_enabled=False):
        super().__init__(parent)
        self.setWindowTitle("Chart Settings")
        self.setFixedSize(320, 650)

        self.main_app = parent

        # Outer layout for centering
        outer_layout = QVBoxLayout(self)
        outer_layout.setAlignment(Qt.AlignCenter)

        # === Card container ===
        card = QWidget()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 20, 20, 20)
        card_layout.setSpacing(15)

        # --- Color Map ---
        card_layout.addWidget(QLabel("Color Map:"))
        self.gradient_dropdown = QComboBox()
        self.gradient_dropdown.addItems([
            'viridis', 'plasma', 'inferno', 'magma',
            'thermal', 'flame', 'yellowy', 'bipolar',
            'spectrum', 'cyclic', 'greyclip', 'grey'
        ])
        self.gradient_dropdown.setCurrentText(current_gradient)
        card_layout.addWidget(self.gradient_dropdown)

        # --- Speed of Sound ---
        card_layout.addWidget(QLabel("Speed of Sound:"))
        self.speed_dropdown = QComboBox()
        self.speed_dropdown.addItems(["330m/s (Air)", "1500m/s (Water)"])
        self.speed_dropdown.setCurrentIndex(1 if current_speed == 1500 else 0)
        card_layout.addWidget(self.speed_dropdown)

        # --- Fish Detection Section ---
        if YOLO_AVAILABLE and YOLO is not None and os.path.exists(MODEL_PATH):
            detection_section = QVBoxLayout()
            detection_section.setSpacing(8)

            detection_label = QLabel("Fish Detection:")
            detection_label.setStyleSheet("font-weight: bold;")
            detection_section.addWidget(detection_label)

            self.detection_checkbox = QCheckBox("Enable Fish Detection")
            self.detection_checkbox.setChecked(detection_enabled)
            self.detection_checkbox.setStyleSheet("QCheckBox:hover { text-decoration: none; }")
            detection_section.addWidget(self.detection_checkbox)

            card_layout.addLayout(detection_section)

        # --- NMEA Output Section ---
        nmea_section = QVBoxLayout()
        nmea_section.setSpacing(8)

        nmea_label = QLabel("NMEA TCP Output:")
        nmea_label.setStyleSheet("font-weight: bold;")
        nmea_section.addWidget(nmea_label)

        self.nmea_enable_checkbox = QCheckBox("Enable NMEA Output")
        self.nmea_enable_checkbox.setStyleSheet("QCheckBox:hover { text-decoration: none; }")
        nmea_section.addWidget(self.nmea_enable_checkbox)

        # Address display row
        addr_row = QHBoxLayout()
        addr_label = QLabel("Address:")
        addr_label.setMinimumWidth(60)

        self.addr_display = QLabel(nmea_address)
        self.addr_display.setStyleSheet("color: #cccccc; padding: 2px;")
        self.addr_display.setTextInteractionFlags(Qt.TextSelectableByMouse)

        copy_button = QPushButton("Copy")
        copy_button.setFixedHeight(22)
        copy_button.setStyleSheet("font-size: 11px; padding: 2px 6px;")
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(nmea_address))

        addr_row.addWidget(addr_label)
        addr_row.addWidget(self.addr_display)
        addr_row.addWidget(copy_button)
        addr_row.addStretch()
        nmea_section.addLayout(addr_row)

        # Port input
        port_row = QHBoxLayout()
        port_label = QLabel("Port:")
        port_label.setMinimumWidth(40)

        self.port_input = QLineEdit()
        self.port_input.setPlaceholderText("TCP Port (default: 10110)")
        self.port_input.setText(str(nmea_port))
        self.port_input.setMaximumWidth(200)

        port_row.addWidget(port_label)
        port_row.addWidget(self.port_input)
        port_row.addStretch()
        nmea_section.addLayout(port_row)

        self.nmea_enable_checkbox.toggled.connect(self.port_input.setEnabled)
        self.nmea_enable_checkbox.setChecked(nmea_enabled)
        self.port_input.setEnabled(nmea_enabled)

        card_layout.addLayout(nmea_section)

        # --- Buttons ---
        button_layout = QHBoxLayout()
        apply_button = QPushButton("Apply")
        apply_button.clicked.connect(self.apply_settings)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.close)
        button_layout.addWidget(apply_button)
        button_layout.addWidget(cancel_button)
        card_layout.addLayout(button_layout)

        # Add card to outer layout
        outer_layout.addWidget(card)

        # --- Styling ---
        self.setStyleSheet("""
                QDialog {
                    background-color: #1e1e1e;
                }
                QWidget#Card {
                    background-color: #2b2b2b;
                    border-radius: 12px;
                    padding: 15px;
                }
                QLabel {
                    color: #ffffff;
                    font-size: 14px;
                }
                QComboBox {
                    background-color: #3c3c3c;
                    color: white;
                    padding: 4px;
                    border-radius: 4px;
                }
                QPushButton {
                    background-color: #444444;
                    border: 1px solid #666;
                    padding: 5px 10px;
                    border-radius: 6px;
                }
                QPushButton:hover {
                    background-color: #555;
                }
            """)

        card.setObjectName("Card")
        self.setLayout(outer_layout)

    def apply_settings(self):
        selected_gradient = self.gradient_dropdown.currentText()
        selected_speed = 330 if self.speed_dropdown.currentIndex() == 0 else 1500
        nmea_enabled = self.nmea_enable_checkbox.isChecked()
        nmea_port = int(self.port_input.text()) if self.port_input.text().isdigit() else 10110
        
        detection_enabled = False
        if YOLO_AVAILABLE and YOLO is not None and os.path.exists(MODEL_PATH) and hasattr(self, 'detection_checkbox'):
            detection_enabled = self.detection_checkbox.isChecked()

        if self.main_app:
            self.main_app.set_gradient(selected_gradient)
            self.main_app.set_sound_speed(selected_speed)
            self.main_app.configure_nmea_output(enabled=nmea_enabled, port=nmea_port)
            self.main_app.set_detection_enabled(detection_enabled)

        self.close()


class WaterfallApp(QMainWindow):
    def __init__(self, preview_enabled=True):
        super().__init__()
        self.preview_enabled = preview_enabled
        self.capture_thread = None
        self.detection_thread = None
        self.snapshot_count = 0
        self.total_fish_detected = 0

        self.nmea_enabled = False
        self.nmea_port = 10110
        self.nmea_socket = None
        self.nmea_output_enabled = False

        self.current_gradient = 'thermal'
        self.current_speed = SPEED_OF_SOUND
        self.detection_enabled = False

        ensure_directories()

        # Initialize detection thread if available
        if YOLO_AVAILABLE and YOLO is not None and os.path.exists(MODEL_PATH):
            self.detection_thread = FishDetectionThread(MODEL_PATH)
            self.detection_thread.detection_complete.connect(self.on_detection_complete)
            self.detection_thread.start()
            print("🐟 Fish detection thread started")
        else:
            if not YOLO_AVAILABLE or YOLO is None:
                print("⚠️ Fish detection unavailable - YOLO could not be loaded")
            elif not os.path.exists(MODEL_PATH):
                print(f"⚠️ Fish detection unavailable - model file '{MODEL_PATH}' not found")

        self.setWindowTitle("Open Echo Interface" + (" [Preview OFF]" if not preview_enabled else ""))
        self.setGeometry(0, 0, 600, 800)
        self.setFixedSize(600, 800)

        self.data = np.zeros((MAX_ROWS, NUM_SAMPLES))

        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setWindowFlags(self.windowFlags() & ~Qt.FramelessWindowHint)

        palette = self.palette()
        palette.setColor(QPalette.Window, QColor("#2b2b2b"))
        self.setPalette(palette)
        self.setAutoFillBackground(True)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(5, 5, 5, 5)
        main_layout.setSpacing(5)
        central_widget.setLayout(main_layout)

        # === Waterfall Plot ===
        if self.preview_enabled:
            self.waterfall = pg.PlotWidget()
            self.imageitem = pg.ImageItem(axisOrder='row-major')
            self.waterfall.addItem(self.imageitem)
            self.waterfall.setMouseEnabled(x=False, y=False)
            self.waterfall.setMinimumHeight(400)
            main_layout.addWidget(self.waterfall)

            inverted_depth_labels = list(depth_labels.items())[::-1]
            self.waterfall.getAxis("left").setTicks([inverted_depth_labels])
            self.depth_line = pg.InfiniteLine(angle=0, pen=pg.mkPen('r', width=2))
            self.waterfall.addItem(self.depth_line)

            right_axis = self.waterfall.getAxis("right")
            right_axis.setTicks([inverted_depth_labels])
            right_axis.setStyle(showValues=True)

            for i in range(0, int(MAX_DEPTH), Y_LABEL_DISTANCE):
                row_index = int(i / SAMPLE_RESOLUTION)
                hline = pg.InfiniteLine(pos=row_index, angle=0, pen=pg.mkPen(color='w', style=pg.QtCore.Qt.DotLine))
                self.waterfall.addItem(hline)

            self.colorbar = pg.HistogramLUTWidget()
            self.colorbar.setImageItem(self.imageitem)
            self.colorbar.item.gradient.loadPreset('thermal')
            self.imageitem.setLevels(DEFAULT_LEVELS)
        else:
            status_label = QLabel("📊 HEADLESS MODE\n\nData capture is running in background.\nSnapshots are being saved automatically.")
            status_label.setAlignment(Qt.AlignCenter)
            status_label.setStyleSheet("color: #ffffff; font-size: 18px; padding: 40px;")
            main_layout.addWidget(status_label)

        # === Controls ===
        controls_layout = QVBoxLayout()

        # Status row
        status_row = QHBoxLayout()
        self.status_label = QLabel("Status: Connecting...")
        self.status_label.setStyleSheet("color: #ffaa00; font-weight: bold;")
        status_row.addWidget(self.status_label)
        status_row.addStretch()
        controls_layout.addLayout(status_row)

        # Info labels
        info_layout = QHBoxLayout()
        self.depth_label = QLabel("Depth: --- cm")
        self.temperature_label = QLabel("Temperature: --- °C")
        self.drive_voltage_label = QLabel("vDRV: --- V")

        info_layout.addWidget(self.depth_label)
        info_layout.addWidget(self.temperature_label)
        info_layout.addWidget(self.drive_voltage_label)

        info_container = QWidget()
        info_container.setLayout(info_layout)
        controls_layout.addWidget(info_container)

        # Detection stats
        self.detection_label = QLabel("Fish Detected: 0 (Total: 0)")
        self.detection_label.setStyleSheet("color: #00ff00; font-weight: bold;")
        controls_layout.addWidget(self.detection_label)

        # Hex input and buttons
        hex_row = QHBoxLayout()
        self.hex_input = QLineEdit()
        self.hex_input.setPlaceholderText("0x1F")
        hex_row.addWidget(self.hex_input)

        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self.send_hex_value)
        hex_row.addWidget(self.send_button)

        self.settings_button = QPushButton("Settings")
        self.settings_button.clicked.connect(self.open_settings)
        hex_row.addWidget(self.settings_button)

        self.quit_button = QPushButton("Quit")
        self.quit_button.clicked.connect(self.close)
        hex_row.addWidget(self.quit_button)

        controls_layout.addLayout(hex_row)

        controls_container = QWidget()
        controls_container.setLayout(controls_layout)
        main_layout.addWidget(controls_container)

        # Auto-connect on startup
        QTimer.singleShot(500, self.auto_connect)

    def auto_connect(self):
        """Connect using hard-coded serial port or simulation."""
        if SIMULATION_MODE:
            port = "SIMULATION"
        else:
            port = SERIAL_PORT

        print(f"🔌 Connecting to: {port}")
        self.start_capture(port)

    def start_capture(self, port):
        """Start the background data capture thread."""
        if self.capture_thread:
            self.capture_thread.stop()
            self.capture_thread = None

        try:
            # Test if port is accessible before starting thread
            if port != "SIMULATION":
                try:
                    test_ser = serial.Serial(port, BAUD_RATE, timeout=0.5)
                    test_ser.close()
                    print(f"✅ Port {port} is accessible")
                except (serial.SerialException, OSError) as e:
                    print(f"❌ Cannot access port {port}: {e}")
                    self.status_label.setText(f"Status: Port Error ({port})")
                    self.status_label.setStyleSheet("color: #ff0000; font-weight: bold;")
                    return
            
            self.capture_thread = DataCaptureThread(port, BAUD_RATE)
            
            # Connect signals
            if self.preview_enabled:
                self.capture_thread.data_received.connect(self.update_preview)
            else:
                self.capture_thread.data_received.connect(self.update_info_only)
            
            self.capture_thread.snapshot_ready.connect(self.save_snapshot_from_data)
            self.capture_thread.start()
            
            self.status_label.setText(f"Status: Connected ({port})")
            self.status_label.setStyleSheet("color: #00ff00; font-weight: bold;")
            print(f"✅ Data capture started on {port}")
        except Exception as e:
            print(f"❌ Connection failed: {e}")
            self.status_label.setText("Status: Connection failed")
            self.status_label.setStyleSheet("color: #ff0000; font-weight: bold;")

    def update_preview(self, spectrogram, depth_index, temperature, drive_voltage):
        """Update the visual preview (only if preview is enabled)."""
        self.data = np.roll(self.data, -1, axis=0)
        self.data[-1, :] = spectrogram
        self.imageitem.setImage(self.data.T, autoLevels=False)

        sigma = np.std(self.data)
        mean = np.mean(self.data)
        self.imageitem.setLevels((mean - 2 * sigma, mean + 2 * sigma))

        self.depth_label.setText(f"Depth: {depth_index * SAMPLE_RESOLUTION:.1f} cm | Index: {depth_index:.0f}")
        self.temperature_label.setText(f"Temperature: {temperature:.1f} °C")
        self.drive_voltage_label.setText(f"vDRV: {drive_voltage:.1f} V")
        self.depth_line.setPos(depth_index)

        self.send_nmea_data(depth_index)

    def update_info_only(self, spectrogram, depth_index, temperature, drive_voltage):
        """Update info labels without rendering the waterfall (headless mode)."""
        self.depth_label.setText(f"Depth: {depth_index * SAMPLE_RESOLUTION:.1f} cm | Index: {depth_index:.0f}")
        self.temperature_label.setText(f"Temperature: {temperature:.1f} °C")
        self.drive_voltage_label.setText(f"vDRV: {drive_voltage:.1f} V")

        self.send_nmea_data(depth_index)

    def send_nmea_data(self, depth_index):
        """Send NMEA data if enabled."""
        if hasattr(self, 'nmea_output_enabled') and self.nmea_output_enabled:
            now = time.time()

            if not hasattr(self, '_last_nmea_sent') or (now - self._last_nmea_sent) >= 1.0:
                try:
                    depth_cm = depth_index * SAMPLE_RESOLUTION
                    depth_m = depth_cm / 100
                    depth_ft = depth_m * 3.28084
                    depth_fathoms = depth_m * 0.546807

                    def calculate_checksum(sentence):
                        checksum = 0
                        for char in sentence:
                            checksum ^= ord(char)
                        return f"*{checksum:02X}"

                    nmea_sentence = f"DBT,{depth_ft:.1f},f,{depth_m:.1f},M,{depth_fathoms:.1f},F"
                    full_sentence = f"${nmea_sentence}{calculate_checksum(nmea_sentence)}\r\n"

                    # Use the thread's send_data method
                    if hasattr(self, 'nmea_thread') and self.nmea_thread:
                        if not self.nmea_thread.send_data(full_sentence):
                            print(f"⚠️ NMEA send failed: connection lost")
                    self._last_nmea_sent = now

                except Exception as e:
                    print(f"⚠️ NMEA send failed: {e}")

    def save_snapshot_from_data(self, data_array, filename):
        """Save snapshot from the background thread's data buffer."""
        filepath = os.path.join(SNAPSHOT_DIR, filename)
        
        try:
            # Create a temporary PlotWidget for rendering with proper size
            temp_widget = pg.PlotWidget()
            temp_widget.resize(1200, 800)
            
            temp_imageitem = pg.ImageItem(axisOrder='row-major')
            temp_widget.addItem(temp_imageitem)
            
            # Apply thermal colormap
            temp_colorbar = pg.HistogramLUTWidget()
            temp_colorbar.setImageItem(temp_imageitem)
            temp_colorbar.item.gradient.loadPreset(self.current_gradient)
            
            temp_imageitem.setImage(data_array.T, autoLevels=False)
            
            sigma = np.std(data_array)
            mean = np.mean(data_array)
            temp_imageitem.setLevels((mean - 2 * sigma, mean + 2 * sigma))
            
            # Set up axes with proper labels
            inverted_depth_labels = list(depth_labels.items())[::-1]
            temp_widget.getAxis("left").setTicks([inverted_depth_labels])
            temp_widget.getAxis("right").setTicks([inverted_depth_labels])
            temp_widget.getAxis("right").setStyle(showValues=True)
            
            # Add horizontal grid lines
            for i in range(0, int(MAX_DEPTH), Y_LABEL_DISTANCE):
                row_index = int(i / SAMPLE_RESOLUTION)
                hline = pg.InfiniteLine(pos=row_index, angle=0, pen=pg.mkPen(color='w', style=pg.QtCore.Qt.DotLine))
                temp_widget.addItem(hline)
            
            # Set axis labels
            temp_widget.setLabel('left', 'Depth (m)')
            temp_widget.setLabel('bottom', 'Sample Index')
            
            # Force widget to render properly
            temp_widget.show()
            QApplication.processEvents()
            
            # Render to pixmap
            pixmap = QPixmap(temp_widget.size())
            pixmap.fill(QColor("#2b2b2b"))
            
            painter = QPainter(pixmap)
            temp_widget.render(painter)
            painter.end()
            
            pixmap.save(filepath, 'PNG', quality=95)
            self.snapshot_count += 1
            print(f"📸 Snapshot saved: {filepath} (Total: {self.snapshot_count})")
            
            # Clean up
            temp_widget.close()
            temp_widget.deleteLater()
            
            # Add to detection queue if enabled
            if self.detection_enabled and self.detection_thread and YOLO_AVAILABLE and YOLO is not None:
                self.detection_thread.add_image_to_queue(filepath)
                print(f"🔍 Added to detection queue: {filename}")
            
        except Exception as e:
            print(f"❌ Failed to save snapshot: {e}")

    def on_detection_complete(self, image_name, fish_count, detections):
        """Handle fish detection completion"""
        self.total_fish_detected += fish_count
        self.detection_label.setText(f"Fish Detected: {fish_count} (Total: {self.total_fish_detected})")
        print(f"🐟 Detection complete: {image_name} - {fish_count} fish found")
        
        # Update label color based on detection
        if fish_count > 0:
            self.detection_label.setStyleSheet("color: #00ff00; font-weight: bold;")
        else:
            self.detection_label.setStyleSheet("color: #888888; font-weight: bold;")

    def set_detection_enabled(self, enabled):
        """Enable or disable fish detection"""
        self.detection_enabled = enabled
        if enabled:
            if self.detection_thread and not self.detection_thread.isRunning():
                self.detection_thread.start()
            print("🐟 Fish detection ENABLED")
        else:
            print("🐟 Fish detection DISABLED")

    def configure_nmea_output(self, enabled: bool, port: int):
        self.nmea_output_enabled = enabled
        self.nmea_port = port

        # Stop existing NMEA thread if running
        if hasattr(self, 'nmea_thread') and self.nmea_thread:
            self.nmea_thread.stop()
            self.nmea_thread.wait()
            self.nmea_thread = None

        if enabled:
            try:
                # Start NMEA server in background thread (non-blocking)
                self.nmea_thread = NMEAServerThread(port)
                self.nmea_thread.client_connected.connect(self.on_nmea_client_connected)
                self.nmea_thread.connection_failed.connect(self.on_nmea_connection_failed)
                self.nmea_thread.start()
            except Exception as e:
                print(f"❌ Failed to set up NMEA output: {e}")
                self.nmea_output_enabled = False
    
    def on_nmea_client_connected(self):
        """Called when NMEA client successfully connects."""
        print("✅ NMEA output ready")
    
    def on_nmea_connection_failed(self, error_msg):
        """Called when NMEA server fails to start."""
        print(f"❌ NMEA connection failed: {error_msg}")
        self.nmea_output_enabled = False

    def set_gradient(self, gradient_name):
        self.current_gradient = gradient_name
        if self.preview_enabled and hasattr(self, 'colorbar'):
            self.colorbar.item.gradient.loadPreset(gradient_name)

    def set_sound_speed(self, speed):
        global SPEED_OF_SOUND, SAMPLE_RESOLUTION, MAX_DEPTH, depth_labels

        SPEED_OF_SOUND = speed
        self.current_speed = speed
        SAMPLE_RESOLUTION = (SPEED_OF_SOUND * SAMPLE_TIME * 100) / 2
        MAX_DEPTH = NUM_SAMPLES * SAMPLE_RESOLUTION
        depth_labels = {int(i / SAMPLE_RESOLUTION): f"{i / 100}" for i in range(0, int(MAX_DEPTH), Y_LABEL_DISTANCE)}

        if self.preview_enabled and hasattr(self, 'waterfall'):
            inverted_depth_labels = list(depth_labels.items())[::-1]
            self.waterfall.getAxis("left").setTicks([inverted_depth_labels])
            self.waterfall.getAxis("right").setTicks([inverted_depth_labels])

    def keyPressEvent(self, event):
        if event.key() == ord('Q'):
            print("🛑 Quit triggered from keyboard.")
            self.close()
        else:
            super().keyPressEvent(event)

    def send_hex_value(self):
        hex_value = self.hex_input.text().strip()
        print(hex_value)

        if hex_value.startswith("0x") and len(hex_value) > 2:
            try:
                if self.capture_thread and self.capture_thread.isRunning():
                    port = self.capture_thread.port
                    if port != "SIMULATION":
                        with serial.Serial(port, BAUD_RATE) as ser:
                            ser.write(hex_value.encode())
                            print(f"Sent: {hex_value}")
                    else:
                        print("⚠️ Cannot send hex values in simulation mode")
            except ValueError:
                print("❌ Invalid hex format.")
        else:
            print("❌ Invalid hex value. Please enter a valid hex string (e.g., 0x1F)")

    def closeEvent(self, event):
        if self.capture_thread:
            self.capture_thread.stop()
        if self.detection_thread:
            self.detection_thread.stop()
        if hasattr(self, 'nmea_thread') and self.nmea_thread:
            self.nmea_thread.stop()
        event.accept()

    def open_settings(self):
        device_ip = get_local_ip()

        self.settings_dialog = SettingsDialog(
            parent=self,
            current_gradient=self.current_gradient,
            current_speed=self.current_speed,
            nmea_enabled=self.nmea_output_enabled,
            nmea_port=self.nmea_port,
            nmea_address=device_ip,
            detection_enabled=self.detection_enabled
        )
        self.settings_dialog.show()

    def resizeEvent(self, event):
        """Override resize event to prevent window resizing."""
        event.ignore()


if __name__ == "__main__":
    # Parse command line arguments
    try_load_yolo()
    parser = argparse.ArgumentParser(description='Open Echo Interface - Sonar Data Capture with Fish Detection')
    parser.add_argument('--preview', type=str, choices=['on', 'off'], default='on',
                        help='Enable or disable real-time preview mode (default: on)')
    parser.add_argument('--simulate', action='store_true',
                        help='Enable simulation mode (overrides SIMULATION_MODE constant)')
    parser.add_argument('--detect', action='store_true',
                        help='Enable fish detection on startup')
    parser.add_argument('--time', type=str, metavar='DURATION',
                        help='Run for specified duration (e.g. 15min, 2h, 30s)')
    parser.add_argument('--kill-switch', type=str, metavar='PATH',
                        default='/tmp/stop_now',
                        help='Path to kill switch file (default: /tmp/stop_now)')
    
    args = parser.parse_args()
    
    # Apply simulation mode from CLI
    if args.simulate:
        SIMULATION_MODE = True
        print("🎮 Simulation mode enabled via CLI")
    
    # Apply detection mode from CLI
    if args.detect and YOLO_AVAILABLE and YOLO is not None and os.path.exists(MODEL_PATH):
        DETECTION_ENABLED = True
        print("🐟 Fish detection enabled via CLI")
    
    # Handle runtime duration
    if args.time:
        import re
        import threading

        # Convert string duration to seconds
        match = re.match(r"(\d+)([smh])", args.time.strip().lower())
        if match:
            val, unit = match.groups()
            val = int(val)
            duration_sec = val * {'s': 1, 'm': 60, 'h': 3600}[unit]
            print(f"⏱️ Application will run for {duration_sec} seconds")
        else:
            print(f"⚠️ Invalid --time format: {args.time}. Ignoring.")

    preview_enabled = (args.preview == 'on')
    
    app = QApplication(sys.argv)
    
    window = WaterfallApp(preview_enabled=preview_enabled)

    # --- Graceful exit function ---
    def safe_exit():
        """Stop threads and exit application gracefully."""
        print("🛑 Exiting application...")
        if window.capture_thread:
            window.capture_thread.stop()
        if window.detection_thread:
            window.detection_thread.stop()
        if hasattr(window, 'nmea_thread') and window.nmea_thread:
            window.nmea_thread.stop()
        QApplication.quit()
    
    # Set detection enabled from CLI
    if DETECTION_ENABLED:
        window.set_detection_enabled(True)

    # Handle kill switch in a background thread
    def monitor_kill_switch(path):
        while True:
            if os.path.exists(path):
                print(f"🛑 Kill switch triggered: {path}")
                window.close()
                break
            time.sleep(1)
    
    if args.kill_switch:
        def check_kill_switch():
            if os.path.exists(args.kill_switch):
                print(f"🛑 Kill switch triggered: {args.kill_switch}")
                safe_exit()
        kill_timer = QTimer()
        kill_timer.timeout.connect(check_kill_switch)
        kill_timer.start(1000)  # check every 1 second
    
    # Auto-stop after duration if specified
    if args.time and 'duration_sec' in locals():
        QTimer.singleShot(duration_sec * 1000, safe_exit)
    
    window.show()

    try:
        sys.exit(app.exec())
    except KeyboardInterrupt:
        safe_exit()