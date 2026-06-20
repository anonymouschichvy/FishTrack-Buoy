#!/usr/bin/env python3
import cv2
import numpy as np
import json
import os
import sys
import signal
from datetime import datetime, timedelta
from pathlib import Path
import threading
import queue
from typing import Dict, List, Tuple, Optional
import torch
from PIL import Image
import atexit
import argparse
import time
import re

# Picamera2 import (optional — not required when using folder input mode)
try:
    from picamera2 import Picamera2
    PICAMERA2_AVAILABLE = True
    USING_PICAMERA2 = True
except ImportError:
    PICAMERA2_AVAILABLE = False
    USING_PICAMERA2 = False

# Supported image extensions for folder input mode
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif', '.webp'}

try:
    import gpiod
    GPIO_AVAILABLE = True
    USING_GPIOD = True
    
    # Detect gpiod version
    GPIOD_VERSION = getattr(gpiod, '__version__', '1.0')
    GPIOD_V2 = GPIOD_VERSION.startswith('2.')
    
    print(f"gpiod library loaded (version {GPIOD_VERSION})")
except ImportError:
    try:
        import RPi.GPIO as GPIO
        GPIO_AVAILABLE = True
        USING_GPIOD = False
        GPIOD_V2 = False
        print("Using RPi.GPIO (legacy)")
    except ImportError:
        GPIO_AVAILABLE = False
        USING_GPIOD = False
        GPIOD_V2 = False
        print("No GPIO library available - relay control disabled")

IR_RELAY_PIN = 17
SUNSET_HOUR = 18
SUNRISE_HOUR = 6

# Global gpiod objects
chip = None
relay_line = None
relay_request = None

def find_gpiochip():
    """Auto-detect the correct gpiochip for Raspberry Pi - handles symlinks"""
    chip_names = ['gpiochip4', 'gpiochip0', 'gpiochip1', 'gpiochip2']
    
    for chip_name in chip_names:
        chip_path = f'/dev/{chip_name}'
        if os.path.exists(chip_path):
            # Resolve symlinks to get the real device
            real_path = os.path.realpath(chip_path)
            real_chip_name = os.path.basename(real_path)
            
            try:
                if GPIOD_V2:
                    # gpiod v2.x test - use real path
                    test_chip = gpiod.Chip(real_path)
                    num_lines = test_chip.get_info().num_lines
                    test_chip.close()
                else:
                    # gpiod v1.x test - use chip name (not path)
                    test_chip = gpiod.Chip(real_chip_name)
                    num_lines = test_chip.num_lines()
                    test_chip.close()
                
                if num_lines >= 28:  # Pi should have at least 28 GPIO lines
                    if chip_name != real_chip_name:
                        print(f"Found GPIO chip: {chip_name} -> {real_chip_name} ({num_lines} lines)")
                    else:
                        print(f"Found GPIO chip: {chip_name} ({num_lines} lines)")
                    return real_chip_name  # Return the real chip name, not the symlink
            except Exception as e:
                print(f"  Failed to open {chip_name}: {e}")
                continue
    
    print("\nAvailable GPIO devices:")
    for item in os.listdir('/dev'):
        if item.startswith('gpiochip'):
            real = os.path.realpath(f'/dev/{item}')
            print(f"  - {item} -> {os.path.basename(real)}")
    
    return None

# ===================== RELAY TRUTH TABLE =====================
# Active-Low Relay Logic:
# +----------------+------------+-----------------+
# | Intent         | GPIO Level | Relay State     |
# +----------------+------------+-----------------+
# | Relay ON       | LOW (0)    | ACTIVE          |
# | Relay OFF      | HIGH (1)   | INACTIVE        |
# +--------------------------------------------------+

class EnhancedRelayControl:
    """Enhanced relay control with clear active-low logic"""
    
    def __init__(self):
        self.relay_state = None  # Track actual relay state (True=ON/ACTIVE, False=OFF/INACTIVE)
        self.gpio_available = GPIO_AVAILABLE
        self.using_gpiod = USING_GPIOD
        self.gpiod_v2 = GPIOD_V2
        
    def relay_setup(self):
        """Setup GPIO relay - Works with gpiod v1.x and v2.x"""
        global chip, relay_line, relay_request
        
        if not self.gpio_available:
            print("GPIO not available - skipping relay setup")
            return False
        
        try:
            if self.using_gpiod:
                chip_name = find_gpiochip()
                
                if not chip_name:
                    raise Exception("No suitable gpiochip found")
                
                if self.gpiod_v2:
                    # ===== gpiod v2.x API (modern) =====
                    from gpiod.line import Direction, Value
                    
                    # Start with relay OFF (GPIO HIGH = INACTIVE)
                    relay_request = gpiod.request_lines(
                        f"/dev/{chip_name}",
                        consumer="fishtrack",
                        config={
                            IR_RELAY_PIN: gpiod.LineSettings(
                                direction=Direction.OUTPUT,
                                output_value=Value.ACTIVE  # HIGH = Relay OFF
                            )
                        }
                    )
                    self.relay_state = False  # Relay is OFF
                    print(f"✓ GPIO relay initialized on pin {IR_RELAY_PIN} using {chip_name} (gpiod v2)")
                    print(f"  Initial state: OFF (GPIO HIGH)")
                    
                else:
                    # ===== gpiod v1.x API (legacy) =====
                    chip = gpiod.Chip(chip_name)
                    relay_line = chip.get_line(IR_RELAY_PIN)
                    
                    # Request line as output, starting HIGH (relay OFF)
                    relay_line.request(
                        consumer="fishtrack",
                        type=gpiod.LINE_REQ_DIR_OUT,
                        default_vals=[1]  # HIGH = Relay OFF
                    )
                    self.relay_state = False  # Relay is OFF
                    print(f"✓ GPIO relay initialized on pin {IR_RELAY_PIN} using {chip_name} (gpiod v1)")
                    print(f"  Initial state: OFF (GPIO HIGH)")
            else:
                # RPi.GPIO setup for older Pi models
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(IR_RELAY_PIN, GPIO.OUT)
                GPIO.output(IR_RELAY_PIN, GPIO.HIGH)  # HIGH = Relay OFF
                self.relay_state = False  # Relay is OFF
                print(f"✓ GPIO relay initialized on pin {IR_RELAY_PIN} (RPi.GPIO)")
                print(f"  Initial state: OFF (GPIO HIGH)")
            
            return True
        
        except Exception as e:
            print(f"✗ GPIO initialization failed: {e}")
            print("  Running without relay control")
            import traceback
            traceback.print_exc()
            self.gpio_available = False
            return False

    def relay_on(self):
        """
        Turn relay ON (ACTIVE)
        Active-Low Logic: Set GPIO to LOW (0)
        """
        global relay_line, relay_request
        
        if not self.gpio_available:
            return
        
        # Skip if already ON
        if self.relay_state is True:
            return
        
        try:
            if self.using_gpiod:
                if self.gpiod_v2:
                    # gpiod v2.x: Set to INACTIVE (LOW) for relay ON
                    from gpiod.line import Value
                    if relay_request:
                        relay_request.set_value(IR_RELAY_PIN, Value.INACTIVE)  # LOW = ON
                        self.relay_state = True
                        print("IR LED RELAY → ON (GPIO LOW)")
                else:
                    # gpiod v1.x: Set to 0 (LOW) for relay ON
                    if relay_line:
                        relay_line.set_value(0)  # LOW = ON
                        self.relay_state = True
                        print("IR LED RELAY → ON (GPIO LOW)")
            else:
                # RPi.GPIO: Set to LOW for relay ON
                GPIO.output(IR_RELAY_PIN, GPIO.LOW)  # LOW = ON
                self.relay_state = True
                print("IR LED RELAY → ON (GPIO LOW)")
                
        except Exception as e:
            print(f"✗ Failed to turn relay ON: {e}")
            self.relay_state = None  # Unknown state

    def relay_off(self):
        """
        Turn relay OFF (INACTIVE)
        Active-Low Logic: Set GPIO to HIGH (1)
        """
        global relay_line, relay_request
        
        if not self.gpio_available:
            return
        
        # Skip if already OFF
        if self.relay_state is False:
            return
        
        try:
            if self.using_gpiod:
                if self.gpiod_v2:
                    # gpiod v2.x: Set to ACTIVE (HIGH) for relay OFF
                    from gpiod.line import Value
                    if relay_request:
                        relay_request.set_value(IR_RELAY_PIN, Value.ACTIVE)  # HIGH = OFF
                        self.relay_state = False
                        print("IR LED RELAY → OFF (GPIO HIGH)")
                else:
                    # gpiod v1.x: Set to 1 (HIGH) for relay OFF
                    if relay_line:
                        relay_line.set_value(1)  # HIGH = OFF
                        self.relay_state = False
                        print("IR LED RELAY → OFF (GPIO HIGH)")
            else:
                # RPi.GPIO: Set to HIGH for relay OFF
                GPIO.output(IR_RELAY_PIN, GPIO.HIGH)  # HIGH = OFF
                self.relay_state = False
                print("IR LED RELAY → OFF (GPIO HIGH)")
                
        except Exception as e:
            print(f"✗ Failed to turn relay OFF: {e}")
            self.relay_state = None  # Unknown state

    def relay_cleanup(self):
        """Cleanup GPIO resources and ensure relay is OFF"""
        global chip, relay_line, relay_request
        
        if not self.gpio_available:
            return
        
        try:
            # Ensure relay is OFF before cleanup
            self.relay_off()
            
            if self.using_gpiod:
                if self.gpiod_v2:
                    # gpiod v2.x cleanup
                    if relay_request:
                        relay_request.release()
                        relay_request = None
                        print("✓ GPIO cleaned up (gpiod v2)")
                else:
                    # gpiod v1.x cleanup
                    if relay_line:
                        relay_line.release()
                        relay_line = None
                    if chip:
                        chip.close()
                        chip = None
                    print("✓ GPIO cleaned up (gpiod v1)")
            else:
                # RPi.GPIO cleanup
                GPIO.cleanup()
                print("✓ GPIO cleaned up (RPi.GPIO)")
            
            self.relay_state = None
            
        except Exception as e:
            print(f"✗ Error during GPIO cleanup: {e}")

    def get_relay_status(self) -> str:
        """Get human-readable relay status"""
        if not self.gpio_available:
            return "UNAVAILABLE"
        elif self.relay_state is True:
            return "ON (GPIO LOW)"
        elif self.relay_state is False:
            return "OFF (GPIO HIGH)"
        else:
            return "UNKNOWN"

class MotionDetector:
    """
    Handles image input and optional motion detection.

    Supports two input modes:
      • Folder mode  – reads images from a directory (--input-folder)
      • Camera mode  – uses Picamera2 live stream (default, Raspberry Pi only)
    """
    def __init__(self, capture_folder: str = "captures",
                 motion_threshold: int = 5000,
                 min_area: int = 500,
                 sensitivity: int = 25,
                 camera_mode: str = "IMX708",
                 preview_enabled: bool = True,
                 input_folder: Optional[str] = None):
        self.capture_folder = Path(capture_folder)
        self.capture_folder.mkdir(exist_ok=True)
        self.motion_threshold = motion_threshold
        self.min_area = min_area
        self.sensitivity = sensitivity
        self.camera_mode = camera_mode
        self.preview_enabled = preview_enabled
        self.previous_frame = None
        self.camera = None
        self.capture_paused = True

        # Folder-input mode
        self.input_folder = Path(input_folder) if input_folder else None
        self.folder_images: List[Path] = []   # ordered list of image paths
        self.folder_index: int = 0            # next image to serve

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialize_camera(self) -> bool:
        """
        Initialise the image source.
        In folder mode, scan the directory for images.
        In camera mode, start Picamera2.
        """
        if self.input_folder is not None:
            return self._initialize_folder()
        return self._initialize_picamera()

    def _initialize_folder(self) -> bool:
        """Scan input folder for supported image files."""
        if not self.input_folder.exists():
            print(f"Error: input folder does not exist: {self.input_folder}")
            return False

        self.folder_images = sorted([
            p for p in self.input_folder.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
        ])

        if not self.folder_images:
            print(f"Error: no images found in {self.input_folder}")
            print(f"  Supported formats: {', '.join(sorted(IMAGE_EXTENSIONS))}")
            return False

        print(f"Folder input mode: found {len(self.folder_images)} image(s) in {self.input_folder}")
        self.folder_index = 0
        return True

    def _initialize_picamera(self) -> bool:
        """Initialize Picamera2 camera (Raspberry Pi only)."""
        if not PICAMERA2_AVAILABLE:
            print("Error: picamera2 library not available")
            print("Install with: sudo apt install -y python3-picamera2")
            print("Or use --input-folder to process images from a directory.")
            return False

        try:
            self.camera = Picamera2()
            config = self.camera.create_preview_configuration(
                main={"size": (640, 480), "format": "RGB888"},
                controls={"FrameRate": 30}
            )
            self.camera.configure(config)
            self.camera.start()
            time.sleep(2)  # warm-up
            camera_name = "IMX708 (Pi Camera Module 3)"
            print(f"Camera initialised: {camera_name} ({self.camera_mode} mode) - 640x480")
            return True
        except Exception as e:
            print(f"Error initialising camera: {e}")
            return False

    # ------------------------------------------------------------------
    # Frame reading
    # ------------------------------------------------------------------

    def read_frame(self) -> Tuple[bool, Optional[np.ndarray]]:
        """
        Return the next frame.
        • Folder mode: read the next image file from the list. Returns
          (False, None) when all images have been consumed.
        • Camera mode: capture a live frame from Picamera2.
        """
        if self.input_folder is not None:
            return self._read_folder_frame()
        return self._read_camera_frame()

    def _read_folder_frame(self) -> Tuple[bool, Optional[np.ndarray]]:
        """Return the next image from the input folder."""
        if self.folder_index >= len(self.folder_images):
            return False, None  # signal that all images are exhausted

        img_path = self.folder_images[self.folder_index]
        self.folder_index += 1

        frame = cv2.imread(str(img_path))
        if frame is None:
            print(f"Warning: could not read image {img_path} — skipping")
            return self._read_folder_frame()  # try next

        print(f"[{self.folder_index}/{len(self.folder_images)}] Reading: {img_path.name}")
        return True, frame

    def _read_camera_frame(self) -> Tuple[bool, Optional[np.ndarray]]:
        """Capture a frame from Picamera2."""
        try:
            frame = self.camera.capture_array()           # RGB
            frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            return True, frame_bgr
        except Exception as e:
            print(f"Error reading camera frame: {e}")
            return False, None

    # ------------------------------------------------------------------
    # Motion detection (used in camera mode; skipped in folder mode)
    # ------------------------------------------------------------------

    def detect_motion(self, frame: np.ndarray) -> Tuple[bool, np.ndarray]:
        """
        Detect motion between the current frame and the previous one.
        In folder mode every image is treated as a motion event so that
        all images are forwarded to YOLO analysis automatically.
        """
        if self.input_folder is not None:
            # Every folder image is considered a "triggered" capture
            return True, frame

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        if self.previous_frame is None:
            self.previous_frame = gray
            return False, frame

        frame_delta = cv2.absdiff(self.previous_frame, gray)
        thresh = cv2.threshold(frame_delta, self.sensitivity, 255, cv2.THRESH_BINARY)[1]
        thresh = cv2.dilate(thresh, None, iterations=2)

        contours, _ = cv2.findContours(thresh.copy(),
                                       cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)

        motion_detected = False
        for contour in contours:
            if cv2.contourArea(contour) < self.min_area:
                continue
            motion_detected = True
            break

        self.previous_frame = gray
        return motion_detected, frame

    # ------------------------------------------------------------------
    # Snapshot saving
    # ------------------------------------------------------------------

    def capture_snapshot(self, frame: np.ndarray,
                         source_name: Optional[str] = None) -> str:
        """
        Save a frame to the captures folder.
        In folder mode the original filename is preserved (with a timestamp
        prefix to avoid collisions); in camera mode a pure timestamp name
        is used.
        """
        timestamp = datetime.now().strftime("%d_%m_%Y_%H_%M_%S_%f")

        if self.input_folder is not None and source_name:
            # Keep original name so results are easy to correlate
            stem = Path(source_name).stem
            suffix = Path(source_name).suffix or '.jpg'
            filename = f"{timestamp}_{stem}{suffix}"
        else:
            filename = f"{timestamp}.jpg"

        filepath = self.capture_folder / filename
        cv2.imwrite(str(filepath), frame)
        return filename

    # ------------------------------------------------------------------
    # Resource cleanup
    # ------------------------------------------------------------------

    def release(self):
        """Release camera resources (no-op in folder mode)."""
        if self.camera:
            try:
                if USING_PICAMERA2:
                    self.camera.stop()
                    self.camera.close()
                else:
                    self.camera.close()
                print("Camera released")
            except Exception:
                pass


class YOLOv10Analyzer:
    """Handles YOLOv10 object detection with TorchScript support."""
    
    def __init__(self, model_path: str, labels_path: str, 
                 capture_folder: str = "captures",
                 output_folder: str = "output",
                 data_json_path: str = "data/fish_detect_results.json",
                 confidence_threshold: float = 0.25,
                 input_size: int = 640):
        self.capture_folder = Path(capture_folder)
        self.output_folder = Path(output_folder)
        self.output_folder.mkdir(parents=True, exist_ok=True)
        self.data_json_path = Path(data_json_path)
        if self.data_json_path.parent != Path('.'):
            self.data_json_path.parent.mkdir(parents=True, exist_ok=True)
        self.confidence_threshold = confidence_threshold
        self.input_size = input_size
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.data_lock = threading.Lock()
        self.model_type = None
        
        self._initialize_data_json()
        
        with open(labels_path, 'r') as f:
            labels_data = json.load(f)
            if isinstance(labels_data, dict):
                self.labels = labels_data
            else:
                self.labels = {str(i): label for i, label in enumerate(labels_data)}
        
        print(f"Loaded {len(self.labels)} class labels")
        
        self.model = self._load_model(model_path)
        self.color_palette = self._generate_color_palette()

    def _safe_delete(self, path: Path):
        """Safely delete a file if it exists."""
        try:
            if path.exists():
                path.unlink()
                print(f"Deleted: {path}")
        except Exception as e:
            print(f"Failed to delete {path}: {e}")
    
    def _initialize_data_json(self):
        """Initialize data.json with proper structure."""
        if not self.data_json_path.exists():
            initial_data = {
                "Version": 1,
                "Model": "YOLO v10 Medium",
                "Input Img Size": self.input_size
            }
            with open(self.data_json_path, 'w') as f:
                json.dump(initial_data, f, indent=2)
            print(f"Created data.json at {self.data_json_path}")
    
    def _load_model(self, model_path: str):
        """Load TorchScript YOLO model directly."""
        if not os.path.exists(model_path):
            print(f"Model file not found: {model_path}")
            return None
        
        print(f"Loading TorchScript model from: {model_path}")
        
        try:
            model = torch.jit.load(model_path, map_location=self.device)
            model.eval()
            self.model_type = 'torchscript'
            print(f"Loaded TorchScript model on {self.device}")
            print(f"Model input size: {self.input_size}x{self.input_size}")
            return model
        except Exception as e:
            print(f"Failed to load model: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _generate_color_palette(self) -> Dict[str, Tuple[int, int, int]]:
        """Generate distinct colors for each class."""
        colors = {}
        np.random.seed(42)
        for label_id, label_name in self.labels.items():
            colors[label_name] = tuple(np.random.randint(50, 255, 3).tolist())
        return colors
    
    def _append_to_data_json(self, detection_entry: Dict):
        """Thread-safe append to data.json."""
        with self.data_lock:
            try:
                with open(self.data_json_path, 'r') as f:
                    data = json.load(f)
                
                if "detections" not in data:
                    data["detections"] = []
                
                data["detections"].append(detection_entry)
                
                with open(self.data_json_path, 'w') as f:
                    json.dump(data, f, indent=2)
            except Exception as e:
                print(f"Error updating data.json: {e}")
    
    def _preprocess_image(self, img: np.ndarray) -> torch.Tensor:
        """Preprocess image for YOLO inference."""
        img_resized = cv2.resize(img, (self.input_size, self.input_size))
        img_rgb = cv2.cvtColor(img_resized, cv2.COLOR_BGR2RGB)
        img_tensor = torch.from_numpy(img_rgb).float() / 255.0
        img_tensor = img_tensor.permute(2, 0, 1)
        img_tensor = img_tensor.unsqueeze(0)
        return img_tensor.to(self.device)
    
    def _postprocess_detections(self, output, orig_shape):
        """Post-process YOLO TorchScript output."""
        detections = []
        orig_h, orig_w = orig_shape[:2]
        
        try:
            if isinstance(output, tuple):
                output = output[0]
            
            if isinstance(output, list):
                output = output[0]
            
            if isinstance(output, torch.Tensor):
                output = output.cpu().numpy()
            
            if len(output.shape) == 3:
                output = output[0]
            
            if output.shape[1] >= 6:
                confidences = output[:, 4]
                mask = confidences > self.confidence_threshold
                filtered = output[mask]
                
                for det in filtered:
                    x1, y1, x2, y2, conf, cls = det[:6]
                    
                    x1 = int(x1 * orig_w / self.input_size)
                    y1 = int(y1 * orig_h / self.input_size)
                    x2 = int(x2 * orig_w / self.input_size)
                    y2 = int(y2 * orig_h / self.input_size)
                    
                    class_id = int(cls)
                    label = self.labels.get(str(class_id), f"class_{class_id}")
                    
                    detection = {
                        "label": label,
                        "confidence_score": float(conf),
                        "bounding_box": [x1, y1, x2 - x1, y2 - y1]
                    }
                    detections.append(detection)
        
        except Exception as e:
            print(f"Error in post-processing: {e}")
        
        return detections
    
    def analyze_image(self, image_name: str) -> Optional[Dict]:
        """Analyze image using TorchScript YOLO model."""
        image_path = self.capture_folder / image_name
        
        if not image_path.exists():
            print(f"Image not found: {image_name}")
            self._safe_delete(image_path)
            return None

        if self.model is None:
            print(f"Model not loaded, skipping {image_name}")
            self._safe_delete(image_path)
            return None
        
        original_img = cv2.imread(str(image_path))
        if original_img is None:
            print(f"Failed to read image: {image_name}")
            self._safe_delete(image_path)
            return None
        
        h, w = original_img.shape[:2]
        
        try:
            input_tensor = self._preprocess_image(original_img)
            
            with torch.no_grad():
                output = self.model(input_tensor)
            
            detections = self._postprocess_detections(output, original_img.shape)
            
            print(f"Detected {len(detections)} objects")
            
        except Exception as e:
            print(f"Error during inference: {e}")
            self._safe_delete(image_path)
            return None
        
        annotated_img = original_img.copy()
        
        for idx, detection in enumerate(detections):
            x, y, width, height = detection["bounding_box"]
            label = detection["label"]
            confidence = detection["confidence_score"]
            
            color = self.color_palette.get(label, (0, 255, 0))
            
            cv2.rectangle(annotated_img, (x, y), (x + width, y + height), color, 3)
            
            label_text = f"{label} {confidence:.1%}"
            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = 0.6
            font_thickness = 2
            
            (text_w, text_h), baseline = cv2.getTextSize(
                label_text, font, font_scale, font_thickness
            )
            
            cv2.rectangle(annotated_img, 
                         (x, y - text_h - baseline - 8),
                         (x + text_w + 8, y),
                         color, -1)
            
            cv2.putText(annotated_img, label_text,
                       (x + 4, y - 4), 
                       font, font_scale, (255, 255, 255), font_thickness)
            
            cv2.circle(annotated_img, (x + width - 15, y + 15), 12, color, -1)
            cv2.putText(annotated_img, str(idx + 1),
                       (x + width - 20, y + 20),
                       font, 0.5, (255, 255, 255), 2)
        
        timestamp = datetime.now().strftime("%d/%m/%Y %H:%M")
        info_text = f"Objects: {len(detections)} | {timestamp}"
        
        overlay = annotated_img.copy()
        cv2.rectangle(overlay, (0, 0), (w, 35), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.7, annotated_img, 0.3, 0, annotated_img)
        
        cv2.putText(annotated_img, info_text, (10, 22),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        
        output_path = self.output_folder / image_name

        if len(detections) > 0:
            cv2.imwrite(str(output_path), annotated_img)
            print(f"Saved annotated image: {output_path}")
        else:
            print("No objects detected — annotated image will NOT be saved")   

        for detection in detections:
            detection_entry = {
                "Image Name": image_name,
                "Timestamp": timestamp,
                "Label": detection["label"],
                "Confidence Score": detection["confidence_score"],
                "Bounding Box": detection["bounding_box"]
            }
            self._append_to_data_json(detection_entry)

        original_path = self.capture_folder / image_name
        self._safe_delete(original_path)

        if len(detections) == 0:
            self._safe_delete(output_path)
        
        result = {
            "image_name": image_name,
            "timestamp": timestamp,
            "total_detections": len(detections),
            "detections": detections,
        }
        
        return result


class MotionYOLOSystem:
    """Main system with timed execution, intervals, and kill switch support."""
    def __init__(self, model_path: str, labels_path: str,
                capture_folder: str = "captures",
                output_folder: str = "output",
                max_queue_size: int = 40,
                motion_sensitivity: int = 25,
                camera_mode: str = "IMX708",
                preview_enabled: bool = True,
                duration_seconds: Optional[int] = None,
                interval_seconds: Optional[int] = None,
                kill_switch_path: str = "tmp/stop_now",
                input_folder: Optional[str] = None):

        self.input_folder = input_folder  # None = camera mode

        self.motion_detector = MotionDetector(
            capture_folder,
            sensitivity=motion_sensitivity,
            camera_mode=camera_mode,
            preview_enabled=preview_enabled,
            input_folder=input_folder,
        )
        self.yolo_analyzer = YOLOv10Analyzer(model_path, labels_path, 
                                            capture_folder, output_folder)
        self.analysis_queue = queue.Queue(maxsize=max_queue_size)
        self.max_queue_size = max_queue_size
        self.running = False
        self.threads = []
        self.shutdown_event = threading.Event()
        self.cleanup_done = False
        self.capture_paused = False
        
        self.relay_control = EnhancedRelayControl()
        
        self.duration_seconds = duration_seconds
        self.interval_seconds = interval_seconds
        self.start_time = None
        self.end_time = None
        
        self.kill_switch_path = Path(kill_switch_path)
        self.kill_switch_active = True
        
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        atexit.register(self._emergency_cleanup)
    
    def _signal_handler(self, signum, frame):
        """Handle Ctrl+C and termination signals."""
        if not self.shutdown_event.is_set():
            print("\n" + "=" * 70)
            print("Shutdown signal received - stopping all threads...")
            print("=" * 70)
            self.running = False
            self.shutdown_event.set()
    
    def _emergency_cleanup(self):
        """Emergency cleanup called on program exit."""
        if not self.cleanup_done:
            try:
                cv2.destroyAllWindows()
                if self.motion_detector.camera:
                    self.motion_detector.release()
            except:
                pass
    
    def _check_kill_switch(self):
        """Check for kill switch file."""
        if self.kill_switch_path.exists():
            print(f"\nKill switch detected: {self.kill_switch_path}")
            self.running = False
            self.shutdown_event.set()
            return True
        return False
    
    def _check_timer(self):
        """Check if duration has elapsed and handle graceful pause."""
        if self.duration_seconds and self.end_time:
            if datetime.now() >= self.end_time:
                print(f"\nTimer expired ({self.duration_seconds}s) - pausing motion detection")
                self.capture_paused = True
                self.running = False
                return True
        return False
    
    def motion_detection_thread(self):
        """Thread 1: Image input and motion detection with timer and kill switch.

        In folder mode: iterates over all images in the input folder, queuing
        each one for YOLO analysis, then exits automatically.
        In camera mode: streams live frames and captures on motion, as before.
        """
        print("Motion detection thread started")
        folder_mode = self.input_folder is not None
        window_name = 'Motion Detection - Press Q to Quit'

        try:
            while self.running and not self.shutdown_event.is_set():

                if self.is_night_time():
                    self.relay_on()
                else:
                    self.relay_off()

                if self.kill_switch_active and self._check_kill_switch():
                    break

                if not folder_mode and self._check_timer():
                    break

                try:
                    ret, frame = self.motion_detector.read_frame()

                    # ── Folder mode: no more images → done ──────────────────
                    if folder_mode and (not ret or frame is None):
                        print("All folder images have been processed — stopping.")
                        break

                    # ── Camera mode: frame error → bail ─────────────────────
                    if not folder_mode and (not ret or frame is None):
                        print("Failed to read frame")
                        break

                    queue_size = self.analysis_queue.qsize()

                    # Queue back-pressure (same logic for both modes)
                    if queue_size >= self.max_queue_size:
                        if not self.capture_paused:
                            self.capture_paused = True
                            print(f"Queue full ({queue_size}/{self.max_queue_size}) - PAUSING")
                        if folder_mode:
                            # Wait a moment then re-check without re-reading the frame
                            time.sleep(0.1)
                            continue
                        status_color = (0, 0, 255)
                    elif self.capture_paused and queue_size > 0:
                        status_color = (0, 165, 255)
                    elif self.capture_paused and queue_size == 0:
                        self.capture_paused = False
                        print("Queue empty - RESUMING")
                        status_color = (0, 255, 0)
                    else:
                        status_color = (0, 255, 0)

                    # ── Preview window (camera mode only) ───────────────────
                    if not folder_mode and self.motion_detector.preview_enabled:
                        overlay_frame = frame.copy()
                        status_text = (
                            f"PAUSED - Queue: {queue_size}/{self.max_queue_size} | Press Q to quit"
                            if self.capture_paused else
                            f"ACTIVE | Queue: {queue_size}/{self.max_queue_size} | Press Q to quit"
                        )
                        if self.duration_seconds and self.end_time:
                            remaining = (self.end_time - datetime.now()).total_seconds()
                            if remaining > 0:
                                status_text = f"{status_text} | Time: {int(remaining)}s"
                        cv2.rectangle(overlay_frame, (0, 0),
                                     (overlay_frame.shape[1], 35), (0, 0, 0), -1)
                        cv2.putText(overlay_frame, status_text, (10, 22),
                                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, status_color, 2)
                        cv2.imshow(window_name, overlay_frame)

                    # ── Capture / queue logic ────────────────────────────────
                    if not self.capture_paused:
                        if folder_mode:
                            # In folder mode every image is a "detection event"
                            source_name = self.motion_detector.folder_images[
                                self.motion_detector.folder_index - 1
                            ].name
                            filename = self.motion_detector.capture_snapshot(
                                frame, source_name=source_name
                            )
                            print(f"Queued for analysis: {filename}")
                            try:
                                self.analysis_queue.put(filename, block=False)
                            except queue.Full:
                                print(f"Queue full, skipping {filename}")
                                self.capture_paused = True
                        else:
                            motion_detected, processed_frame = \
                                self.motion_detector.detect_motion(frame)
                            if motion_detected:
                                print("Motion detected! Capturing...")
                                filename = self.motion_detector.capture_snapshot(processed_frame)
                                print(f"Captured: {filename}")
                                try:
                                    self.analysis_queue.put(filename, block=False)
                                except queue.Full:
                                    print(f"Queue full, skipping {filename}")
                                    self.capture_paused = True

                    # ── Key handling (camera mode with preview only) ─────────
                    if not folder_mode and self.motion_detector.preview_enabled:
                        key = cv2.waitKey(30) & 0xFF
                        if key == ord('q') or key == ord('Q'):
                            print("\nQuit key pressed")
                            self.running = False
                            self.shutdown_event.set()
                            break
                    elif not folder_mode:
                        time.sleep(0.03)

                except Exception as e:
                    if self.running:
                        print(f"Error in motion detection: {e}")
                    break

        finally:
            try:
                if not folder_mode and self.motion_detector.preview_enabled:
                    cv2.destroyWindow(window_name)
                    cv2.destroyAllWindows()
                    cv2.waitKey(1)
            except Exception:
                pass
            print("Motion detection thread stopped")
    
    def analysis_thread(self):
        """Thread 2: YOLOv10 analysis with fast shutdown."""
        print("Analysis thread started")
        
        try:
            while not self.shutdown_event.is_set():
                try:
                    image_name = self.analysis_queue.get(timeout=0.5)
                    
                    if self.shutdown_event.is_set():
                        print(f"Skipping analysis of {image_name} (shutting down)")
                        self.analysis_queue.task_done()
                        break
                    
                    print(f"Analyzing: {image_name} (Queue: {self.analysis_queue.qsize()})")
                    result = self.yolo_analyzer.analyze_image(image_name)
                    
                    if result:
                        print(f"Found {result['total_detections']} objects:")
                        for det in result['detections']:
                            print(f"  • {det['label']}: {det['confidence_score']:.1%}")
                    else:
                        print(f"  No objects detected in {image_name}")
                    
                    self.analysis_queue.task_done()
                    
                except queue.Empty:
                    if not self.running and self.analysis_queue.empty():
                        break
                    continue
                except Exception as e:
                    if not self.shutdown_event.is_set():
                        print(f"Analysis error: {e}")
        finally:
            print("Analysis thread stopped")
    
    def _execute_post_processing_scripts(self):
        """Execute fish species detection and ollama AI scripts inline."""
        print("\n" + "=" * 70)
        print("Starting post-processing scripts...")
        print("=" * 70)
        
        scripts = [
            ("../FISHSPECIES/fish_species_detect.py", "Fish Species Detection"),
            ("../FISHCOMPILE/fish_compile_results.py", "Fish Results Compilation")
        ]
        
        for script_path, script_name in scripts:
            script_path = Path(script_path)
            
            if not script_path.exists():
                print(f"{script_name} not found: {script_path}")
                continue
            
            print(f"\nExecuting {script_name}...")
            print(f"\nPath: {script_path.absolute()}")
            
            try:
                original_cwd = os.getcwd()
                original_sys_path = sys.path.copy()
                
                script_dir = script_path.parent.absolute()
                os.chdir(script_dir)
                
                if str(script_dir) not in sys.path:
                    sys.path.insert(0, str(script_dir))
                
                with open(script_path, 'r', encoding='utf-8', errors='replace') as f:
                    exec(f.read(), globals())

                print(f"{script_name} completed successfully")
                
            except Exception as e:
                print(f"Error executing {script_name}: {e}")
                import traceback
                traceback.print_exc()
            finally:
                os.chdir(original_cwd)
                sys.path = original_sys_path
        
        print("\n" + "=" * 70)
        print("Post-processing completed")
        print("=" * 70)

    def is_night_time(self):
        now = datetime.now().hour
        return now >= SUNSET_HOUR or now < SUNRISE_HOUR
    
    def relay_setup(self):
        return self.relay_control.relay_setup()

    def relay_on(self):
        self.relay_control.relay_on()

    def relay_off(self):
        self.relay_control.relay_off()
    
    def _cleanup(self):
        """Perform cleanup operations"""
        global chip, relay_line, relay_request
        
        if self.cleanup_done:
            return
        
        print("\n" + "=" * 70)
        print("Cleaning up...")
        
        try:
            cv2.destroyAllWindows()
            cv2.waitKey(1)
            
            self.motion_detector.release()
            
            # GPIO cleanup
            if GPIO_AVAILABLE:
                self.relay_control.relay_cleanup()
            
            if self.kill_switch_path.exists():
                self.kill_switch_path.unlink()
                print(f"Removed kill switch file: {self.kill_switch_path}")
            
            remaining = self.analysis_queue.qsize()
            if remaining > 0:
                print(f"{remaining} images were not processed")
            
            self.cleanup_done = True
            
        except Exception as e:
            print(f"Error during cleanup: {e}")
        
        print("=" * 70)
        print("System stopped successfully")
        print("=" * 70)
    
    def start(self):
        """Start the system."""
        print("=" * 70)
        print("Motion Detection & YOLOv10 Analysis System")
        print("=" * 70)
        
        if not self.motion_detector.initialize_camera():
            return

        self.relay_setup()

        if self.is_night_time():
            self.relay_on()
        else:
            self.relay_off()

        print(f"Captures: {self.motion_detector.capture_folder}")
        print(f"Output: {self.yolo_analyzer.output_folder}")
        print(f"Data JSON: {self.yolo_analyzer.data_json_path}")
        print(f"Device: {self.yolo_analyzer.device}")
        if self.input_folder:
            print(f"Input mode: FOLDER ({self.input_folder})")
            print(f"Images found: {len(self.motion_detector.folder_images)}")
        else:
            print(f"Input mode: CAMERA ({self.motion_detector.camera_mode})")
            print(f"Preview: {'enabled' if self.motion_detector.preview_enabled else 'disabled'}")
        print(f"Confidence: {self.yolo_analyzer.confidence_threshold}")
        print(f"Max queue: {self.max_queue_size}")
        print(f"Motion sensitivity: {self.motion_detector.sensitivity}")
        
        if self.duration_seconds:
            print(f"Duration: {self.duration_seconds}s")
        if self.interval_seconds:
            print(f"Interval: {self.interval_seconds}s")
        if self.kill_switch_active:
            print(f"Kill switch: {self.kill_switch_path}")
        
        print("=" * 70)
        
        self.running = True
        
        if self.duration_seconds:
            self.start_time = datetime.now()
            self.end_time = self.start_time + timedelta(seconds=self.duration_seconds)
            print(f"Timer set: {self.start_time.strftime('%H:%M:%S')} → "
                  f"{self.end_time.strftime('%H:%M:%S')}")
        
        motion_thread = threading.Thread(target=self.motion_detection_thread, 
                                        daemon=False, name="MotionDetection")
        analysis_thread = threading.Thread(target=self.analysis_thread, 
                                          daemon=False, name="YOLOAnalysis")
        
        self.threads = [motion_thread, analysis_thread]
        
        motion_thread.start()
        analysis_thread.start()
        
        print("System running! Monitoring for motion...")
        if self.motion_detector.preview_enabled:
            print("  Press Q in the video window or Ctrl+C to stop")
        else:
            print("  Press Ctrl+C to stop")
        print("=" * 70)
        
        try:
            motion_thread.join()
            
            self.running = False
            print("\nDuration ended — stopping motion detection, waiting for analysis to finish...")

            try:
                if not self.analysis_queue.empty():
                    remaining = self.analysis_queue.qsize()
                    print(f"{remaining} images pending analysis... waiting for completion.")
                    self.analysis_queue.join()
                    print("All queued images analyzed.")
                else:
                    print("No pending analyses.")
            except KeyboardInterrupt:
                print("\nForced shutdown during final analysis — stopping early.")

            self.shutdown_event.set()

            analysis_thread.join(timeout=10)
            if analysis_thread.is_alive():
                print("Analysis thread still active after timeout.")
            
        except KeyboardInterrupt:
            print("\nForce shutdown requested")
            self.shutdown_event.set()
            self.running = False
        finally:
            self._cleanup()  # cleanup() handles all GPIO cleanup properly

def parse_duration(duration_str: str) -> int:
    """Parse duration string like '15min', '2h', '30s' to seconds."""
    duration_str = duration_str.lower().strip()
    
    match = re.match(r'^(\d+)(s|sec|m|min|h|hr|hour)s?$', duration_str)
    
    if not match:
        raise ValueError(f"Invalid duration format: {duration_str}. "
                        f"Use format like: 30s, 15min, 2h")
    
    value = int(match.group(1))
    unit = match.group(2)
    
    if unit in ['s', 'sec']:
        return value
    elif unit in ['m', 'min']:
        return value * 60
    elif unit in ['h', 'hr', 'hour']:
        return value * 3600
    
    raise ValueError(f"Unknown time unit: {unit}")


def main():
    """Main entry point with command-line argument support."""
    
    parser = argparse.ArgumentParser(
        description='Fish Detection System with Motion Detection and YOLO Analysis',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Camera mode (default — Raspberry Pi with Picamera2)
  python fishdetect_v3.py --time 15min
  python fishdetect_v3.py --interval 20min --time 15min
  python fishdetect_v3.py --preview off

  # Folder mode — process all images in a directory
  python fishdetect_v3.py --input-folder /home/pi/fish_images
  python fishdetect_v3.py --input-folder ./captures --preview off
        """
    )
    
    parser.add_argument('--time', type=str, metavar='DURATION',
                       help='Run for specified duration (e.g., 15min, 2h, 30s)')
    
    parser.add_argument('--interval', type=str, metavar='DURATION',
                       help='Interval between cycles (requires --time, e.g., 20min)')
    
    parser.add_argument('--preview', type=str, choices=['on', 'off'],
                       default='on',
                       help='Enable or disable live video preview')
    
    parser.add_argument('--kill-switch', type=str, metavar='PATH',
                       default='/tmp/stop_now',
                       help='Path to kill switch file (default: /tmp/stop_now)')
    
    parser.add_argument('--input-folder', type=str, metavar='PATH',
                       help='Process images from a folder instead of the camera '
                            '(e.g. --input-folder /home/pi/fish_images). '
                            'Supported formats: jpg, jpeg, png, bmp, tiff, webp')

    parser.add_argument('--no-kill-switch', action='store_true',
                       help='Disable kill switch monitoring')
    
    args = parser.parse_args()
    
    if args.interval and not args.time:
        parser.error("--interval requires --time to be specified")
    
    duration_seconds = None
    interval_seconds = None
    
    try:
        if args.time:
            duration_seconds = parse_duration(args.time)
            print(f"Parsed duration: {duration_seconds}s ({args.time})")
        
        if args.interval:
            interval_seconds = parse_duration(args.interval)
            print(f"Parsed interval: {interval_seconds}s ({args.interval})")
    except ValueError as e:
        parser.error(str(e))
    
    MODEL_PATH = "model.ts"
    LABELS_PATH = "labels.json"
    CAPTURE_FOLDER = "captures"
    OUTPUT_FOLDER = "output"
    MAX_QUEUE_SIZE = 40
    MOTION_SENSITIVITY = 70
    
    if not os.path.exists(MODEL_PATH):
        print(f"Model file not found: {MODEL_PATH}")
        return 1
    
    if not os.path.exists(LABELS_PATH):
        print(f"Labels file not found: {LABELS_PATH}")
        return 1
    
    preview_enabled = (args.preview == 'on')
    kill_switch_active = not args.no_kill_switch
    
    cycle_count = 0
    
    try:
        while True:
            cycle_count += 1
            
            if args.interval:
                print("\n" + "=" * 70)
                print(f"CYCLE {cycle_count}")
                print("=" * 70)
            
            system = MotionYOLOSystem(
                model_path=MODEL_PATH,
                labels_path=LABELS_PATH,
                capture_folder=CAPTURE_FOLDER,
                output_folder=OUTPUT_FOLDER,
                max_queue_size=MAX_QUEUE_SIZE,
                motion_sensitivity=MOTION_SENSITIVITY,
                preview_enabled=preview_enabled,
                duration_seconds=duration_seconds,
                interval_seconds=interval_seconds,
                kill_switch_path=args.kill_switch,
                input_folder=args.input_folder,
            )
            
            system.start()
            
            if Path(args.kill_switch).exists() and kill_switch_active:
                print("Kill switch detected - exiting loop")
                Path(args.kill_switch).unlink()
                break
            
            if duration_seconds:
                system._execute_post_processing_scripts()
            
            if not args.interval:
                break
            
            print("\n" + "=" * 70)
            print(f"INTERVAL - Waiting {interval_seconds}s before next cycle...")
            print(f"   Next cycle starts at: {(datetime.now() + timedelta(seconds=interval_seconds)).strftime('%H:%M:%S')}")
            print(f"   Press Ctrl+C to stop")
            print("=" * 70)
            
            try:
                interval_end = datetime.now() + timedelta(seconds=interval_seconds)
                while datetime.now() < interval_end:
                    if Path(args.kill_switch).exists() and kill_switch_active:
                        print("\nKill switch detected during interval - exiting")
                        Path(args.kill_switch).unlink()
                        return 0
                    
                    remaining = (interval_end - datetime.now()).total_seconds()
                    if remaining > 0:
                        time.sleep(min(1, remaining))
                    else:
                        break
                
            except KeyboardInterrupt:
                print("\nInterval interrupted - exiting")
                break
    
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        return 130
    except Exception as e:
        print(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
       print("Goodbye!")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())