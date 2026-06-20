import os
import sys
import time
import subprocess
import threading
import queue
import config
from mesh import send_command_status


def check_camera_available():
    """
    Check if camera is available and not in use by another process.
    Returns True if camera is free, False otherwise.
    """
    try:
        result = subprocess.run(
            ["libcamera-hello", "--list-cameras"],
            capture_output=True,
            text=True,
            timeout=3
        )
        if result.returncode == 0 and "Available cameras" in result.stdout:
            return True
        return False
    except subprocess.TimeoutExpired:
        config.logger.warning("⚠ Camera check timed out")
        return False
    except FileNotFoundError:
        config.logger.warning("⚠ libcamera-hello not found, skipping camera check")
        return True
    except Exception as e:
        config.logger.warning(f"⚠ Camera check error: {e}")
        return True


def is_stderr_error(line):
    """
    Determine if a stderr line is an actual error or just informational.
    Many programs (especially camera/hardware tools) output INFO to stderr.
    """
    line_lower = line.lower()
    
    # Informational patterns that are NOT errors
    info_patterns = [
        'info ',
        'qobject::',
        'qtimer',
        'timers cannot be stopped',
        'qobject::~qobject',
        'libcamera',
        'libpisp',
        'camera_manager',
        'rpi ',
        'pisp.cpp',
        'version',
        'initialized',
        'starting',
        'loaded',
        'detected',
        'using',
    ]
    
    for pattern in info_patterns:
        if pattern in line_lower:
            return False
    
    # Error patterns that ARE actual errors
    error_patterns = [
        'error:',
        'exception',
        'traceback',
        'failed',
        'fatal',
        'critical',
        'cannot',
        'unable',
        'denied',
        'not found',
        'no such',
    ]
    
    for pattern in error_patterns:
        if pattern in line_lower:
            return True
    
    return False


class ScriptExecutor:
    """
    Manages running background scripts, capturing and routing stdout/stderr,
    and running administrative/Linux shell commands.
    """
    
    def __init__(self, lora_controller=None):
        self.lora_controller = lora_controller
        self.active_scripts = {}
        self.output_queues = {}
        self.command_result_map = {}

    def _read_output(self, pipe, output_queue, prefix=""):
        """Read stdout output with error handling"""
        try:
            for line in iter(pipe.readline, ''):
                if line:
                    output_queue.put(f"{prefix}{line.rstrip()}")
        except ValueError as e:
            if "closed file" not in str(e):
                config.logger.debug(f"stdout pipe read error: {e}")
        finally:
            try:
                pipe.close()
            except:
                pass

    def _read_output_stderr(self, pipe, output_queue, script_name):
        """Read stderr output with intelligent classification"""
        try:
            for line in iter(pipe.readline, ''):
                if line:
                    line_clean = line.rstrip()
                    if is_stderr_error(line_clean):
                        config.logger.error(f"[{script_name} ERROR] {line_clean}")
                        output_queue.put(f"[{script_name} ERROR] {line_clean}")
                    else:
                        config.logger.info(f"[{script_name} INFO] {line_clean}")
                        output_queue.put(f"[{script_name} INFO] {line_clean}")
        except ValueError as e:
            if "closed file" not in str(e):
                config.logger.debug(f"[{script_name}] stderr pipe read error: {e}")
        finally:
            try:
                pipe.close()
            except:
                pass

    def _run_background_script(self, script_path, args, output_queue, script_name, command_id=None, sender=None):
        """Run a script in background and capture output"""
        try:
            cmd = [sys.executable, script_path] + args
            config.logger.info(f"▶ Starting {script_name}: {' '.join(cmd)}")
            
            script_dir = os.path.dirname(os.path.abspath(script_path))
            
            process = subprocess.Popen(
                cmd,
                cwd=script_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )
            
            stdout_thread = threading.Thread(
                target=self._read_output,
                args=(process.stdout, output_queue, f"[{script_name}] ")
            )
            stderr_thread = threading.Thread(
                target=self._read_output_stderr,
                args=(process.stderr, output_queue, script_name)
            )
            
            stdout_thread.daemon = True
            stderr_thread.daemon = True
            stdout_thread.start()
            stderr_thread.start()
            
            returncode = process.wait()
            
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)
            
            try:
                if process.poll() is None:
                    process.terminate()
                    time.sleep(0.5)
                    if process.poll() is None:
                        process.kill()
            except:
                pass
            
            output_queue.put(f"[{script_name}] PROCESS_COMPLETED:{returncode}")
            
            if command_id and sender and self.lora_controller:
                base_script = script_name.split(':')[0] if ':' in script_name else script_name
                if returncode == 0:
                    send_command_status(self.lora_controller, sender, command_id, base_script,
                                        "completed", returncode, "OK")
                else:
                    send_command_status(self.lora_controller, sender, command_id, base_script,
                                        "error", returncode, f"Exit:{returncode}")

            # ---- AUTO-SEND COMMAND RESULTS ----
            if ":" in script_name:
                base_script, cmd_id = script_name.split(":", 1)
                command_entry = self.command_result_map.get(cmd_id)

                if command_entry and base_script.lower() in command_entry:
                    result_info = command_entry[base_script.lower()]

                    if returncode == 0 and result_info:
                        result_file = result_info["result_file"]

                        try:
                            if os.path.exists(result_file):
                                import json
                                with open(result_file, "r") as f:
                                    payload = json.load(f)

                                if "metadata" not in payload:
                                    payload["metadata"] = {}

                                payload["metadata"]["script"] = base_script.lower()
                                payload["metadata"]["command_id"] = cmd_id
                                payload["metadata"]["result_type"] = result_info.get("result_type", "detection")
                                payload["metadata"]["node"] = config.CODENAME
                                payload["metadata"]["completed_at"] = time.time()
                                
                                if base_script.lower() == "fishdetection":
                                    payload["type"] = "fish_detection"
                                elif base_script.lower() == "sonar":
                                    payload["type"] = "sonar_detection"
                                elif base_script.lower() == "gps":
                                    payload["type"] = "gps"

                                if self.lora_controller:
                                    self.lora_controller.send_json(payload, to=config.BASE_NODE, require_ack=True)
                                    config.logger.info(f"✓ Auto-sent {base_script} results (command_id={cmd_id})")
                                else:
                                    config.logger.error(f"[X] LoRa instance unavailable for {base_script} results")
                            else:
                                config.logger.warning(f"⚠ Result file not found: {result_file}")

                        except Exception as e:
                            config.logger.error(f"[X] Failed to read/send {base_script} results: {e}")

        except Exception as e:
            config.logger.error(f"[X] Script execution error: {e}")
            output_queue.put(f"[{script_name}] EXECUTION_ERROR: {e}")
            
            if command_id and sender and self.lora_controller:
                base_script = script_name.split(':')[0] if ':' in script_name else script_name
                send_command_status(self.lora_controller, sender, command_id, base_script,
                                    "error", -1, str(e)[:40])

    def start_background_script(self, script_path, args, script_name, command_id=None, sender=None):
        """Start a background script"""
        if not os.path.exists(script_path):
            config.logger.error(f"[X] Script not found: {script_path}")
            config.logger.error(f"   Working directory: {os.getcwd()}")
            config.logger.error(f"   Expected path: {os.path.abspath(script_path)}")
            
            if command_id and sender and self.lora_controller:
                base_name = script_name.split(':')[0] if ':' in script_name else script_name
                send_command_status(self.lora_controller, sender, command_id, base_name,
                                    "error", -1, "Not found")
            return False
        
        if script_name in self.active_scripts:
            config.logger.warning(f"⚠ {script_name} already running")
            
            if command_id and sender and self.lora_controller:
                base_name = script_name.split(':')[0] if ':' in script_name else script_name
                send_command_status(self.lora_controller, sender, command_id, base_name,
                                    "error", -1, "Already running")
            return False
        
        # Clean up camera resources for fish detection
        if "fish" in script_name.lower() or "fish_detect" in script_path.lower():
            config.logger.info("🎥 Preparing camera for fish detection...")
            
            for attempt in range(2):
                try:
                    subprocess.run(["pkill", "-9", "-f", "fish_detect.py"], capture_output=True, timeout=2)
                    subprocess.run(["pkill", "-9", "-f", "libcamera"], capture_output=True, timeout=2)
                    subprocess.run(["pkill", "-9", "-f", "picamera"], capture_output=True, timeout=2)
                    time.sleep(1.5)
                    
                    if check_camera_available():
                        config.logger.info("✓ Camera ready for fish detection")
                        break
                    elif attempt < 1:
                        config.logger.warning(f"⚠ Camera still busy, retrying cleanup (attempt {attempt+2}/2)...")
                    else:
                        config.logger.warning("⚠ Camera may still be busy, proceeding anyway...")
                        
                except subprocess.TimeoutExpired:
                    config.logger.warning("⚠ Camera cleanup timed out, continuing anyway")
                    break
                except Exception as e:
                    config.logger.warning(f"⚠ Camera cleanup error: {e}, continuing anyway")
                    break
        
        if script_name not in self.output_queues:
            self.output_queues[script_name] = queue.Queue(maxsize=config.MAX_OUTPUT_QUEUE_SIZE)
        
        if command_id and sender and self.lora_controller:
            base_name = script_name.split(':')[0] if ':' in script_name else script_name
            send_command_status(self.lora_controller, sender, command_id, base_name,
                                "running", 0, "")
        
        thread = threading.Thread(
            target=self._run_background_script,
            args=(script_path, args, self.output_queues[script_name], script_name, command_id, sender),
            daemon=True
        )
        thread.start()
        self.active_scripts[script_name] = thread
        
        config.logger.info(f"✓ Started {script_name}")
        return True

    def check_script_outputs(self):
        """Check and print outputs from background scripts"""
        completed_scripts = []
        
        for script_name, q in list(self.output_queues.items()):
            dropped_messages = 0
            while not q.empty():
                try:
                    msg = q.get_nowait()
                    config.logger.info(msg)
                    
                    if "PROCESS_COMPLETED:" in msg:
                        completed_scripts.append(script_name)
                except queue.Empty:
                    break
                except queue.Full:
                    dropped_messages += 1
            
            if dropped_messages > 0:
                config.logger.warning(f"⚠ Dropped {dropped_messages} messages from {script_name} (queue overflow)")
        
        for script_name in list(self.active_scripts.keys()):
            if not self.active_scripts[script_name].is_alive():
                if script_name not in completed_scripts:
                    config.logger.info(f"✓ {script_name} finished")
                
                if "fish" in script_name.lower():
                    config.logger.info("🎥 Cleaning up camera and Qt resources after fish detection...")
                    try:
                        subprocess.run(["pkill", "-15", "-f", "fish_detect.py"], capture_output=True, timeout=1)
                        time.sleep(0.5)
                        subprocess.run(["pkill", "-9", "-f", "libcamera"], capture_output=True, timeout=1)
                        subprocess.run(["pkill", "-9", "-f", "picamera"], capture_output=True, timeout=1)
                        config.logger.info("✓ Camera and Qt resources cleaned up")
                    except Exception as e:
                        config.logger.warning(f"⚠ Cleanup warning: {e}")
                
                self.active_scripts[script_name].join(timeout=1)
                del self.active_scripts[script_name]

    def run_linux_command(self, command, command_id, sender_node):
        """Execute a Linux command and send result via compression + chunking"""
        try:
            config.logger.info(f"🖥️ Executing Linux command: {command}")
            home_dir = os.path.expanduser("~")
            
            result = subprocess.run(
                command,
                shell=True,
                cwd=home_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300
            )

            stdout = result.stdout.strip()
            stderr = result.stderr.strip()
            exit_code = result.returncode

            if exit_code == 0:
                if stdout:
                    feedback_type = "result"
                    feedback_msg = stdout
                else:
                    feedback_type = "status"
                    feedback_msg = "Command executed successfully"
            else:
                feedback_type = "error"
                feedback_msg = stderr if stderr else "Command failed with no error output"

            command_result = {
                "type": "linux_cmd_result",
                "command_id": command_id,
                "command": command,
                "exit_code": exit_code,
                "feedback_type": feedback_type,
                "output": feedback_msg,
                "timestamp": time.time(),
                "from": config.CODENAME
            }
            
            config.logger.info(f"📤 Sending command result via compressed chunks to {sender_node}...")
            if self.lora_controller:
                chunk_success = self.lora_controller.send_json(
                    command_result,
                    to=sender_node,
                    require_ack=False,
                    compress=True
                )
                if chunk_success:
                    config.logger.info(f"✓ Command result sent successfully (exit_code={exit_code})")
                else:
                    config.logger.error(f"[X] Failed to send command result")
            else:
                config.logger.error("[X] LoRa instance unavailable to send command result")

        except subprocess.TimeoutExpired:
            config.logger.error(f"[X] Command timeout after 300s")
            error_data = {
                "type": "linux_cmd_result",
                "command_id": command_id,
                "command": command,
                "exit_code": -1,
                "feedback_type": "error",
                "output": "Command execution timeout (300s)",
                "timestamp": time.time(),
                "from": config.CODENAME
            }
            if self.lora_controller:
                self.lora_controller.send_json(error_data, to=sender_node, compress=True)
        
        except Exception as e:
            config.logger.error(f"[X] Command execution error: {e}")
            error_data = {
                "type": "linux_cmd_result",
                "command_id": command_id,
                "command": command,
                "exit_code": -1,
                "feedback_type": "error",
                "output": f"Execution error: {str(e)}",
                "timestamp": time.time(),
                "from": config.CODENAME
            }
            if self.lora_controller:
                self.lora_controller.send_json(error_data, to=sender_node, compress=True)
