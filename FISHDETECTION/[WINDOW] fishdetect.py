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
# ===== IR LED RELAY CONTROL =====
try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except:
    GPIO_AVAILABLE = False

IR_RELAY_PIN = 17   # <-- CHANGE THIS TO YOUR GPIO PIN
SUNSET_HOUR = 18   # 6 PM
SUNRISE_HOUR = 6   # 6 AM


class MotionDetector:
    """Handles continuous video capture and motion detection."""
    
    def __init__(self, capture_folder: str = "captures", 
                 motion_threshold: int = 5000,
                 min_area: int = 500,
                 sensitivity: int = 25,
                 camera_mode: str = "day",
                 preview_enabled: bool = True):
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
        
    def get_camera_index(self) -> int:
        """Get camera index based on mode (day=0 for IMX708, night=1 for NONE)."""
        return 0 if self.camera_mode == "day" else 1
        
    def initialize_camera(self) -> bool:
        """Initialize camera feed based on mode."""
        camera_index = self.get_camera_index()
        self.camera = cv2.VideoCapture(camera_index)
        if not self.camera.isOpened():
            print(f"❌ Error: Cannot open camera {camera_index} ({self.camera_mode} mode)")
            return False
        
        # Set camera properties for better performance
        self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        camera_name = "IMX708" if self.camera_mode == "day" else "NONE"
        print(f"✓ Camera initialized: {camera_name} ({self.camera_mode} mode) - "
              f"{int(self.camera.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
              f"{int(self.camera.get(cv2.CAP_PROP_FRAME_HEIGHT))}")
        return True
    
    def detect_motion(self, frame: np.ndarray) -> Tuple[bool, np.ndarray]:
        """Detect motion between current and previous frame."""
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
    
    def capture_snapshot(self, frame: np.ndarray) -> str:
        """Save snapshot with timestamp filename."""
        timestamp = datetime.now().strftime("%d_%m_%Y_%H_%M_%S_%f")
        filename = f"{timestamp}.jpg"
        filepath = self.capture_folder / filename
        cv2.imwrite(str(filepath), frame)
        return filename
    
    def release(self):
        """Release camera resources."""
        if self.camera:
            self.camera.release()
            print("✓ Camera released")


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
        # Ensure parent directory exists (if path includes subdirectories)
        if self.data_json_path.parent != Path('.'):
            self.data_json_path.parent.mkdir(parents=True, exist_ok=True)
        self.confidence_threshold = confidence_threshold
        self.input_size = input_size
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.data_lock = threading.Lock()
        self.model_type = None
        
        # Initialize data.json
        self._initialize_data_json()
        
        # Load labels
        with open(labels_path, 'r') as f:
            labels_data = json.load(f)
            if isinstance(labels_data, dict):
                self.labels = labels_data
            else:
                self.labels = {str(i): label for i, label in enumerate(labels_data)}
        
        print(f"✓ Loaded {len(self.labels)} class labels")
        
        # Load model
        self.model = self._load_model(model_path)
        
        # Color palette
        self.color_palette = self._generate_color_palette()

    def _safe_delete(self, path: Path):
        """Safely delete a file if it exists."""
        try:
            if path.exists():
                path.unlink()
                print(f"🗑️ Deleted: {path}")
        except Exception as e:
            print(f"⚠️ Failed to delete {path}: {e}")
    
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
            print(f"✓ Created data.json at {self.data_json_path}")
    
    def _load_model(self, model_path: str):
        """Load TorchScript YOLO model directly."""
        if not os.path.exists(model_path):
            print(f"❌ Model file not found: {model_path}")
            return None
        
        print(f"Loading TorchScript model from: {model_path}")
        
        try:
            model = torch.jit.load(model_path, map_location=self.device)
            model.eval()
            self.model_type = 'torchscript'
            print(f"✓ Loaded TorchScript model on {self.device}")
            print(f"✓ Model input size: {self.input_size}x{self.input_size}")
            return model
        except Exception as e:
            print(f"❌ Failed to load model: {e}")
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
                print(f"❌ Error updating data.json: {e}")
    
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
            print(f"❌ Error in post-processing: {e}")
        
        return detections
    
    def analyze_image(self, image_name: str) -> Optional[Dict]:
        """Analyze image using TorchScript YOLO model."""
        image_path = self.capture_folder / image_name
        
        if not image_path.exists():
            print(f"❌ Image not found: {image_name}")
            self._safe_delete(image_path)
            return None

        if self.model is None:
            print(f"❌ Model not loaded, skipping {image_name}")
            self._safe_delete(image_path)
            return None
        
        original_img = cv2.imread(str(image_path))
        if original_img is None:
            print(f"❌ Failed to read image: {image_name}")
            self._safe_delete(image_path)
            return None
        
        h, w = original_img.shape[:2]
        
        try:
            input_tensor = self._preprocess_image(original_img)
            
            with torch.no_grad():
                output = self.model(input_tensor)
            
            detections = self._postprocess_detections(output, original_img.shape)
            
            print(f"✓ Detected {len(detections)} objects")
            
        except Exception as e:
            print(f"❌ Error during inference: {e}")
            self._safe_delete(image_path)
            return None
        
        # Draw detections
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
        
        # Info overlay
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
            print(f"✓ Saved annotated image: {output_path}")
        else:
            print("🧹 No objects detected — annotated image will NOT be saved")   

        # Save to JSON
        for detection in detections:
            detection_entry = {
                "Image Name": image_name,
                "Timestamp": timestamp,
                "Label": detection["label"],
                "Confidence Score": detection["confidence_score"],
                "Bounding Box": detection["bounding_box"]
            }
            self._append_to_data_json(detection_entry)

        # --- AUTO CLEANUP ---
        original_path = self.capture_folder / image_name

        # Always delete original captured image
        self._safe_delete(original_path)

        # If no detections, also delete annotated output (if any)
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
                 camera_mode: str = "day",
                 preview_enabled: bool = True,
                 duration_seconds: Optional[int] = None,
                 interval_seconds: Optional[int] = None,
                 kill_switch_path: str = "tmp/stop_now"):
        
        self.motion_detector = MotionDetector(
            capture_folder, 
            sensitivity=motion_sensitivity,
            camera_mode=camera_mode,
            preview_enabled=preview_enabled
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
        
        # Timed execution parameters
        self.duration_seconds = duration_seconds
        self.interval_seconds = interval_seconds
        self.start_time = None
        self.end_time = None
        
        # Kill switch
        self.kill_switch_path = Path(kill_switch_path)
        self.kill_switch_active = True
        
        # Register signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        # Register cleanup on exit
        atexit.register(self._emergency_cleanup)
    
    def _signal_handler(self, signum, frame):
        """Handle Ctrl+C and termination signals."""
        if not self.shutdown_event.is_set():
            print("\n" + "=" * 70)
            print("⚠️  Shutdown signal received - stopping all threads...")
            print("=" * 70)
            self.running = False
            self.shutdown_event.set()
    
    def _emergency_cleanup(self):
        """Emergency cleanup called on program exit."""
        if not self.cleanup_done:
            try:
                cv2.destroyAllWindows()
                if self.motion_detector.camera:
                    self.motion_detector.camera.release()
            except:
                pass
    
    def _check_kill_switch(self):
        """Check for kill switch file."""
        if self.kill_switch_path.exists():
            print(f"\n🛑 Kill switch detected: {self.kill_switch_path}")
            self.running = False
            self.shutdown_event.set()
            return True
        return False
    
    def _check_timer(self):
        """Check if duration has elapsed and handle graceful pause."""
        if self.duration_seconds and self.end_time:
            if datetime.now() >= self.end_time:
                print(f"\n⏰ Timer expired ({self.duration_seconds}s) - pausing motion detection")
                self.capture_paused = True  # Pause new motion detection
                self.running = False        # Stop main loop
                # Do NOT trigger shutdown_event yet; allow analysis to complete
                return True
        return False
    
    def motion_detection_thread(self):
        """Thread 1: Video capture and motion detection with timer and kill switch."""
        print("✓ Motion detection thread started")
        window_name = 'Motion Detection - Press Q to Quit'
        
        try:
            while self.running and not self.shutdown_event.is_set():

                # Auto day/night IR switching
                if self.is_night_time():
                    self.relay_on()
                else:
                    self.relay_off()

                # Check kill switch
                if self.kill_switch_active and self._check_kill_switch():
                    break
                
                # Check timer
                if self._check_timer():
                    break
                
                try:
                    ret, frame = self.motion_detector.camera.read()
                    if not ret:
                        print("⚠️  Failed to read frame")
                        break
                    
                    overlay_frame = frame.copy()
                    queue_size = self.analysis_queue.qsize()
                    
                    # Queue management logic
                    if queue_size >= self.max_queue_size:
                        if not self.capture_paused:
                            self.capture_paused = True
                            print(f"⏸️  Queue full ({queue_size}/{self.max_queue_size}) - PAUSING")
                        
                        status_text = f"PAUSED - Queue: {queue_size}/{self.max_queue_size} | Press Q to quit"
                        status_color = (0, 0, 255)
                    elif self.capture_paused and queue_size > 0:
                        status_text = f"PAUSED - Processing: {queue_size}/{self.max_queue_size} | Press Q to quit"
                        status_color = (0, 165, 255)
                    elif self.capture_paused and queue_size == 0:
                        self.capture_paused = False
                        print(f"▶️  Queue empty - RESUMING")
                        status_text = f"ACTIVE | Queue: {queue_size}/{self.max_queue_size} | Press Q to quit"
                        status_color = (0, 255, 0)
                    else:
                        status_text = f"ACTIVE | Queue: {queue_size}/{self.max_queue_size} | Press Q to quit"
                        status_color = (0, 255, 0)
                    
                    # Add timer info if active
                    if self.duration_seconds and self.end_time:
                        remaining = (self.end_time - datetime.now()).total_seconds()
                        if remaining > 0:
                            status_text = f"{status_text} | Time: {int(remaining)}s"
                    
                    # Draw status overlay (only if preview enabled)
                    if self.motion_detector.preview_enabled:
                        cv2.rectangle(overlay_frame, (0, 0), 
                                     (overlay_frame.shape[1], 35), (0, 0, 0), -1)
                        cv2.putText(overlay_frame, status_text, (10, 22),
                                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, status_color, 2)
                        
                        cv2.imshow(window_name, overlay_frame)
                    
                    # Motion detection
                    if not self.capture_paused:
                        motion_detected, processed_frame = self.motion_detector.detect_motion(frame)
                        
                        if motion_detected:
                            print("🔴 Motion detected! Capturing...")
                            filename = self.motion_detector.capture_snapshot(processed_frame)
                            print(f"📸 Captured: {filename}")
                            
                            try:
                                self.analysis_queue.put(filename, block=False)
                            except queue.Full:
                                print(f"⚠️  Queue full, skipping {filename}")
                                self.capture_paused = True
                    
                    # Handle key press (only if preview enabled)
                    if self.motion_detector.preview_enabled:
                        key = cv2.waitKey(30) & 0xFF
                        if key == ord('q') or key == ord('Q'):
                            print("\n⚠️  Quit key pressed")
                            self.running = False
                            self.shutdown_event.set()
                            break
                    else:
                        time.sleep(0.03)  # Small delay when no preview
                        
                except Exception as e:
                    if self.running:
                        print(f"❌ Error in motion detection: {e}")
                    break
        finally:
            try:
                if self.motion_detector.preview_enabled:
                    cv2.destroyWindow(window_name)
                    cv2.destroyAllWindows()
                    cv2.waitKey(1)
            except:
                pass
            print("✓ Motion detection thread stopped")
    
    def analysis_thread(self):
        """Thread 2: YOLOv10 analysis with fast shutdown."""
        print("✓ Analysis thread started")
        
        try:
            while not self.shutdown_event.is_set():
                try:
                    image_name = self.analysis_queue.get(timeout=0.5)
                    
                    if self.shutdown_event.is_set():
                        print(f"⚠️  Skipping analysis of {image_name} (shutting down)")
                        self.analysis_queue.task_done()
                        break
                    
                    print(f"🔍 Analyzing: {image_name} (Queue: {self.analysis_queue.qsize()})")
                    result = self.yolo_analyzer.analyze_image(image_name)
                    
                    if result:
                        print(f"✓ Found {result['total_detections']} objects:")
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
                        print(f"❌ Analysis error: {e}")
        finally:
            print("✓ Analysis thread stopped")
    
    def _execute_post_processing_scripts(self):
        """Execute fish species detection and ollama AI scripts inline."""
        print("\n" + "=" * 70)
        print("🔄 Starting post-processing scripts...")
        print("=" * 70)
        
        scripts = [
            ("../FISHSPECIES/fish_species_detect.py", "Fish Species Detection"),
            ("../FISHCOMPILE/fish_compile_results.py", "Fish Results Compilation")
        ]
        
        for script_path, script_name in scripts:
            script_path = Path(script_path)
            
            if not script_path.exists():
                print(f"⚠️  {script_name} not found: {script_path}")
                continue
            
            print(f"\n▶️  Executing {script_name}...")
            print(f"\n▶️  Path: {script_path.absolute()}")
            
            try:
                # Save current working directory
                original_cwd = os.getcwd()
                original_sys_path = sys.path.copy()
                
                # Change to script directory
                script_dir = script_path.parent.absolute()
                os.chdir(script_dir)
                
                # Add script directory to sys.path if not already there
                if str(script_dir) not in sys.path:
                    sys.path.insert(0, str(script_dir))
                
                # Execute script in current global namespace with UTF-8 safe reading
                with open(script_path, 'r', encoding='utf-8', errors='replace') as f:
                    exec(f.read(), globals())

                
                print(f"✓ {script_name} completed successfully")
                
            except Exception as e:
                print(f"❌ Error executing {script_name}: {e}")
                import traceback
                traceback.print_exc()
            finally:
                # Restore original working directory and sys.path
                os.chdir(original_cwd)
                sys.path = original_sys_path
        
        print("\n" + "=" * 70)
        print("✓ Post-processing completed")
        print("=" * 70)

    def is_night_time(self):
        now = datetime.now().hour
        return now >= SUNSET_HOUR or now < SUNRISE_HOUR

    def relay_setup(self):
        if GPIO_AVAILABLE:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(IR_RELAY_PIN, GPIO.OUT)
            GPIO.output(IR_RELAY_PIN, GPIO.LOW)

    def relay_on(self):
        if GPIO_AVAILABLE:
            GPIO.output(IR_RELAY_PIN, GPIO.HIGH)
            print("🌙 IR LED RELAY ON")

    def relay_off(self):
        if GPIO_AVAILABLE:
            GPIO.output(IR_RELAY_PIN, GPIO.LOW)
            print("☀️ IR LED RELAY OFF")
    
    def start(self):
        """Start the system."""
        print("=" * 70)
        print("🎥 Motion Detection & YOLOv10 Analysis System")
        print("=" * 70)
        
        if not self.motion_detector.initialize_camera():
            return

        # Setup IR relay
        self.relay_setup()

        # Turn ON IR if night and camera is active
        if self.is_night_time():
            self.relay_on()
        else:
            self.relay_off()

        print(f"📁 Captures: {self.motion_detector.capture_folder}")
        print(f"📁 Output: {self.yolo_analyzer.output_folder}")
        print(f"📄 Data JSON: {self.yolo_analyzer.data_json_path}")
        print(f"🎮 Device: {self.yolo_analyzer.device}")
        print(f"📷 Camera: {self.motion_detector.camera_mode}")
        print(f"👁️  Preview: {'enabled' if self.motion_detector.preview_enabled else 'disabled'}")
        print(f"🎯 Confidence: {self.yolo_analyzer.confidence_threshold}")
        print(f"📊 Max queue: {self.max_queue_size}")
        print(f"🔍 Motion sensitivity: {self.motion_detector.sensitivity}")
        
        if self.duration_seconds:
            print(f"⏱️  Duration: {self.duration_seconds}s")
        if self.interval_seconds:
            print(f"⏰ Interval: {self.interval_seconds}s")
        if self.kill_switch_active:
            print(f"🛑 Kill switch: {self.kill_switch_path}")
        
        print("=" * 70)
        
        self.running = True
        
        # Set timer if duration specified
        if self.duration_seconds:
            self.start_time = datetime.now()
            self.end_time = self.start_time + timedelta(seconds=self.duration_seconds)
            print(f"⏱️  Timer set: {self.start_time.strftime('%H:%M:%S')} → "
                  f"{self.end_time.strftime('%H:%M:%S')}")
        
        # Start threads
        motion_thread = threading.Thread(target=self.motion_detection_thread, 
                                        daemon=False, name="MotionDetection")
        analysis_thread = threading.Thread(target=self.analysis_thread, 
                                          daemon=False, name="YOLOAnalysis")
        
        self.threads = [motion_thread, analysis_thread]
        
        motion_thread.start()
        analysis_thread.start()
        
        print("✓ System running! Monitoring for motion...")
        if self.motion_detector.preview_enabled:
            print("  Press Q in the video window or Ctrl+C to stop")
        else:
            print("  Press Ctrl+C to stop")
        print("=" * 70)
        
        try:
            # Wait for motion thread to finish
            motion_thread.join()
            
            # Stop motion detection (timer expired)
            self.running = False
            print("\n🕒 Duration ended — stopping motion detection, waiting for analysis to finish...")

            # Do NOT set shutdown_event yet — let analysis thread continue processing
            # Wait for analysis to finish all queued items
            try:
                if not self.analysis_queue.empty():
                    remaining = self.analysis_queue.qsize()
                    print(f"⏳ {remaining} images pending analysis... waiting for completion.")
                    self.analysis_queue.join()  # Blocks until all items are processed
                    print("✓ All queued images analyzed.")
                else:
                    print("✓ No pending analyses.")
            except KeyboardInterrupt:
                print("\n⚠️ Forced shutdown during final analysis — stopping early.")

            # Now signal the analysis thread to end
            self.shutdown_event.set()

            # Wait for analysis thread to close gracefully
            analysis_thread.join(timeout=10)
            if analysis_thread.is_alive():
                print("⚠️ Analysis thread still active after timeout.")

            # Ensure analysis thread completes
            analysis_thread.join(timeout=10)
            if analysis_thread.is_alive():
                print("⚠️  Analysis thread still active after timeout")

            
        except KeyboardInterrupt:
            print("\n⚠️  Force shutdown requested")
            self.shutdown_event.set()
            self.running = False
        finally:
            self.relay_off()
            if GPIO_AVAILABLE:
                GPIO.cleanup()
            # Cleanup
            self._cleanup()

    
    def _cleanup(self):
        """Perform cleanup operations."""
        if self.cleanup_done:
            return
        
        print("\n" + "=" * 70)
        print("🧹 Cleaning up...")
        
        try:
            # Destroy OpenCV windows
            cv2.destroyAllWindows()
            cv2.waitKey(1)
            
            # Release camera
            self.motion_detector.release()
            
            # Remove kill switch file if it exists
            if self.kill_switch_path.exists():
                self.kill_switch_path.unlink()
                print(f"✓ Removed kill switch file: {self.kill_switch_path}")
            
            # Report queue status
            remaining = self.analysis_queue.qsize()
            if remaining > 0:
                print(f"⚠️  {remaining} images were not processed")
            
            self.cleanup_done = True
            
        except Exception as e:
            print(f"⚠️  Error during cleanup: {e}")
        
        print("=" * 70)
        print("✓ System stopped successfully")
        print("=" * 70)


def parse_duration(duration_str: str) -> int:
    """Parse duration string like '15min', '2h', '30s' to seconds."""
    duration_str = duration_str.lower().strip()
    
    # Match pattern: number followed by unit
    match = re.match(r'^(\d+)(s|sec|m|min|h|hr|hour)s?$', duration_str)
    
    if not match:
        raise ValueError(f"Invalid duration format: {duration_str}. "
                        f"Use format like: 30s, 15min, 2h")
    
    value = int(match.group(1))
    unit = match.group(2)
    
    # Convert to seconds
    if unit in ['s', 'sec']:
        return value
    elif unit in ['m', 'min']:
        return value * 60
    elif unit in ['h', 'hr', 'hour']:
        return value * 3600
    
    raise ValueError(f"Unknown time unit: {unit}")


def main():
    """Main entry point with command-line argument support."""
    
    # Set up argument parser
    parser = argparse.ArgumentParser(
        description='Fish Detection System with Motion Detection and YOLO Analysis',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run for 15 minutes, then execute post-processing
  python fishdetect.py --time 15min
  
  # Run for 15 minutes, then wait 20 minutes, repeat indefinitely
  python fishdetect.py --interval 20min --time 15min
  
  # Switch to night camera (IMX708)
  python fishdetect.py --camera day
  
  # Disable live preview
  python fishdetect.py --preview off
  
  # Combine options
  python fishdetect.py --time 30min --camera night --preview off
        """
    )
    
    parser.add_argument('--time', type=str, metavar='DURATION',
                       help='Run for specified duration (e.g., 15min, 2h, 30s)')
    
    parser.add_argument('--interval', type=str, metavar='DURATION',
                       help='Interval between cycles (requires --time, e.g., 20min)')
    
    parser.add_argument('--camera', type=str, choices=['day', 'night'],
                       default='day',
                       help='Camera mode: day (IMX708) or night (NONE)')
    
    parser.add_argument('--preview', type=str, choices=['on', 'off'],
                       default='on',
                       help='Enable or disable live video preview')
    
    parser.add_argument('--kill-switch', type=str, metavar='PATH',
                       default='/tmp/stop_now',
                       help='Path to kill switch file (default: /tmp/stop_now)')
    
    parser.add_argument('--no-kill-switch', action='store_true',
                       help='Disable kill switch monitoring')
    
    args = parser.parse_args()
    
    # Validate arguments
    if args.interval and not args.time:
        parser.error("--interval requires --time to be specified")
    
    # Parse durations
    duration_seconds = None
    interval_seconds = None
    
    try:
        if args.time:
            duration_seconds = parse_duration(args.time)
            print(f"✓ Parsed duration: {duration_seconds}s ({args.time})")
        
        if args.interval:
            interval_seconds = parse_duration(args.interval)
            print(f"✓ Parsed interval: {interval_seconds}s ({args.interval})")
    except ValueError as e:
        parser.error(str(e))
    
    # Configuration
    MODEL_PATH = "model.ts"
    LABELS_PATH = "labels.json"
    CAPTURE_FOLDER = "captures"
    OUTPUT_FOLDER = "output"
    MAX_QUEUE_SIZE = 40
    MOTION_SENSITIVITY = 70
    
    # Validate required files
    if not os.path.exists(MODEL_PATH):
        print(f"❌ Model file not found: {MODEL_PATH}")
        return 1
    
    if not os.path.exists(LABELS_PATH):
        print(f"❌ Labels file not found: {LABELS_PATH}")
        return 1
    
    # Convert preview setting
    preview_enabled = (args.preview == 'on')
    kill_switch_active = not args.no_kill_switch
    
    # Main execution loop
    cycle_count = 0
    
    try:
        while True:
            cycle_count += 1
            
            if args.interval:
                print("\n" + "=" * 70)
                print(f"🔄 CYCLE {cycle_count}")
                print("=" * 70)
            
            # Create system instance
            system = MotionYOLOSystem(
                model_path=MODEL_PATH,
                labels_path=LABELS_PATH,
                capture_folder=CAPTURE_FOLDER,
                output_folder=OUTPUT_FOLDER,
                max_queue_size=MAX_QUEUE_SIZE,
                motion_sensitivity=MOTION_SENSITIVITY,
                camera_mode=args.camera,
                preview_enabled=preview_enabled,
                duration_seconds=duration_seconds,
                interval_seconds=interval_seconds,
                kill_switch_path=args.kill_switch
            )
            
            # Start the system
            system.start()
            
            # Check if kill switch was triggered
            if Path(args.kill_switch).exists() and kill_switch_active:
                print("🛑 Kill switch detected - exiting loop")
                Path(args.kill_switch).unlink()
                break
            
            # Execute post-processing scripts if time-limited mode
            if duration_seconds:
                system._execute_post_processing_scripts()
            
            # If no interval specified, exit after one run
            if not args.interval:
                break
            
            # Wait for interval before next cycle
            print("\n" + "=" * 70)
            print(f"⏸️  INTERVAL - Waiting {interval_seconds}s before next cycle...")
            print(f"   Next cycle starts at: {(datetime.now() + timedelta(seconds=interval_seconds)).strftime('%H:%M:%S')}")
            print(f"   Press Ctrl+C to stop")
            print("=" * 70)
            
            try:
                # Sleep with periodic kill switch checks
                interval_end = datetime.now() + timedelta(seconds=interval_seconds)
                while datetime.now() < interval_end:
                    # Check kill switch every second
                    if Path(args.kill_switch).exists() and kill_switch_active:
                        print("\n🛑 Kill switch detected during interval - exiting")
                        Path(args.kill_switch).unlink()
                        return 0
                    
                    remaining = (interval_end - datetime.now()).total_seconds()
                    if remaining > 0:
                        time.sleep(min(1, remaining))
                    else:
                        break
                
            except KeyboardInterrupt:
                print("\n⚠️  Interval interrupted - exiting")
                break
    
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted by user")
        return 130
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
       print("👋 Goodbye!")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())