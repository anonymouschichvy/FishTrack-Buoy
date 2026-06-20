import serial
import time
import json
import zlib
import base64
import hashlib
import uuid
import threading
from datetime import datetime
import config

try:
    import smbus
except ImportError:
    smbus = None


class LightRelayController:
    def __init__(self, port=config.LIGHT_PORT, baudrate=9600):
        # We wrap serial initialization in a try-except or just let it run.
        # To be safe for debugging/running on dev machines without hardware, we can log if it fails,
        # but let's keep the exact original behavior of raising serial exceptions unless desired.
        self.ser = serial.Serial(port, baudrate, timeout=1)
        time.sleep(2)

    def red_on(self): 
        self._send("R1")
    
    def red_off(self): 
        self._send("R0")
    
    def green_on(self): 
        self._send("G1")
    
    def green_off(self): 
        self._send("G0")
    
    def yellow_on(self): 
        self._send("Y1")
    
    def yellow_off(self): 
        self._send("Y0")
    
    def blue_on(self): 
        self._send("B1")
    
    def blue_off(self): 
        self._send("B0")
    
    def all_on(self): 
        self._send("ALL1")
    
    def all_off(self): 
        self._send("ALL0")

    def _send(self, cmd):
        self.ser.write(f"{cmd}\n".encode())
        time.sleep(0.05)

    def creative_sequence(self, delay=0.5):
        """
        Creative LED sequence with thermal management.
        For 3W LEDs without heatsink:
        - Max ON time: 3-5 seconds
        - Cooldown time: 10-15 seconds between activations
        """
        lights = [
            (self.blue_on, self.blue_off),
            (self.green_on, self.green_off),
            (self.yellow_on, self.yellow_off),
            (self.red_on, self.red_off)
        ]
        
        # Phase 1: Sequential buildup (short pulses)
        for i, (on, _) in enumerate(lights):
            on()
            time.sleep(delay * 0.8)  # 0.4s on
        
        time.sleep(delay)
        self.all_off()
        time.sleep(12)  # 12s cooldown after all LEDs were on
        
        # Phase 2: Individual flashes with cooldown
        for on, off in lights:
            on()
            time.sleep(delay * 6)  # 3s on time
            off()
            time.sleep(12)  # 12s cooldown per LED
        
        # Phase 3: Quick pulses (minimal heat)
        for on, off in lights:
            on()
            time.sleep(delay * 2)  # 1s on
            off()
            time.sleep(delay * 2)  # 1s off (brief pause)
        
        time.sleep(10)  # 10s cooldown
        
        # Phase 4: Double blink (all LEDs)
        for _ in range(2):
            self.all_on()
            time.sleep(delay * 2)  # 1s on
            self.all_off()
            time.sleep(delay * 4)  # 2s off
        
        time.sleep(15)  # 15s cooldown after all-on
        
        # Phase 5: Reverse sequence
        for on, off in reversed(lights):
            on()
            time.sleep(delay * 4)  # 2s on
            off()
            time.sleep(10)  # 10s cooldown per LED
        
        # Phase 6: Pair combinations
        self.blue_on()
        self.red_on()
        time.sleep(delay * 6)  # 3s on
        self.all_off()
        time.sleep(12)  # 12s cooldown
        
        self.green_on()
        self.yellow_on()
        time.sleep(delay * 6)  # 3s on
        self.all_off()
        time.sleep(12)  # 12s cooldown
        
        # Phase 7: Grand finale (controlled)
        self.all_on()
        time.sleep(delay * 6)  # 3s on (max safe duration)
        self.all_off()
    
    def is_night_time(self):
        """Return True if current local time is night (18:00-06:00)"""
        hour = datetime.now().hour
        return hour >= 18 or hour < 6

    def night_controller(self, repeat_interval=900):
        """
        Night-only LED controller
        Increased interval to 900s (15 min) for thermal safety
        """
        was_night = False
        last_run = 0

        while True:
            try:
                now = time.time()
                night = self.is_night_time()

                if night and not was_night:
                    config.logger.info("🌙 Night mode activated")
                    self.creative_sequence()
                    last_run = now

                elif night and (now - last_run >= repeat_interval):
                    self.creative_sequence()
                    last_run = now

                elif not night and was_night:
                    self.all_off()
                    config.logger.info("☀ Day mode → LEDs released")

                was_night = night
                time.sleep(5)

            except Exception as e:
                config.logger.error(f"LED error: {e}")
                time.sleep(5)

    def startup_blink(self, success=True, duration=30, interval=3):
        """
        Blink LEDs on startup regardless of day/night
        Thermal-safe parameters:
        - 3s ON / 15s OFF cycle (1:5 duty cycle)
        - Total duration: 30s for safety
        """
        end_time = time.time() + duration
        action_on = self.green_on if success else self.red_on
        action_off = self.green_off if success else self.red_off

        while time.time() < end_time:
            action_on()
            time.sleep(interval)  # 3s on
            action_off()
            time.sleep(interval * 5)  # 15s cooldown


class LoRaController:
    """
    LoRa hardware controller - HALF DUPLEX operation
    Must explicitly switch between TX and RX modes
    Pico does NOT queue messages - sends immediately
    """
    
    def __init__(self, port=config.SERIAL_PORT, baudrate=config.BAUD_RATE):
        self.port = port
        self.baudrate = baudrate
        self.ser = None
        self.current_mode = "RX"
        self.lock = threading.RLock()
        self.rx_buffer = ""
        self.read_line_buffer = ""  # FIXED: Persistent buffer for _read_line_internal
        self.connection_healthy = False
        self.initialized = False
        self.is_reconnecting = False
        self.transmission_lock = threading.Lock()  # Encapsulated from global
    
    def _read_line_internal(self, timeout=1.0):
        """Read a single line from Pico - FIXED to preserve buffer across calls"""
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                # Check if we already have a complete line in the buffer
                if '\n' in self.read_line_buffer:
                    line, self.read_line_buffer = self.read_line_buffer.split('\n', 1)
                    line = line.strip()
                    if line:
                        return line
                
                # Read more data if available
                if self.ser and self.ser.is_open and self.ser.in_waiting:
                    chunk = self.ser.read(self.ser.in_waiting).decode('utf-8', errors='replace')
                    self.read_line_buffer += chunk
                    
                    # Check again if we now have a complete line
                    if '\n' in self.read_line_buffer:
                        line, self.read_line_buffer = self.read_line_buffer.split('\n', 1)
                        line = line.strip()
                        if line:
                            return line
            except Exception as e:
                config.logger.debug(f"[!] Read error: {e}")
                return None
            
            time.sleep(0.01)
        
        return None
    
    def _wait_for_response(self, expected_prefix, timeout=2.0, skip_debug=True):
        """
        Wait for a specific response from Pico
        FIXED: Skip DEBUG messages and increase timeout for mode switches
        """
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            line = self._read_line_internal(timeout=0.5)
            if line:
                # Skip DEBUG messages unless we're looking for them
                if skip_debug and line.startswith("DEBUG:"):
                    config.logger.debug(f"[PICO-DEBUG] {line}")
                    continue
                
                config.logger.debug(f"[PICO] {line}")
                if line.startswith(expected_prefix):
                    return line
            time.sleep(0.01)
        
        return None
    
    def _flush_buffers(self):
        """Flush serial buffers"""
        try:
            if self.ser and self.ser.is_open:
                self.ser.reset_input_buffer()
                self.ser.reset_output_buffer()
                self.read_line_buffer = ""  # FIXED: Clear internal buffer too
                time.sleep(0.05)
        except Exception as e:
            config.logger.debug(f"[!] Flush error: {e}")
    
    def connect(self):
        """Connect to Pico with initialization"""
        with self.lock:
            self.is_reconnecting = True
            attempt = 0
            
            while attempt < 5:
                try:
                    if self.ser and self.ser.is_open:
                        try:
                            self.ser.close()
                        except:
                            pass
                        time.sleep(0.5)
                    
                    config.logger.info(f"[>] Connecting to {self.port} (attempt {attempt + 1}/5)...")
                    self.ser = serial.Serial(
                        self.port, 
                        self.baudrate, 
                        timeout=0.1,
                        write_timeout=1.0
                    )
                    
                    config.logger.info("[>] Waiting for Pico initialization...")
                    time.sleep(1.0)  # Reduced initial wait
                    
                    # CRITICAL FIX: Consume ALL startup messages first
                    config.logger.debug("[>] Consuming Pico startup messages...")
                    startup_timeout = time.time() + 4.0
                    startup_messages = []
                    
                    while time.time() < startup_timeout:
                        if self.ser.in_waiting:
                            try:
                                chunk = self.ser.read(self.ser.in_waiting).decode('utf-8', errors='replace')
                                if chunk:
                                    lines = chunk.strip().split('\n')
                                    for line in lines:
                                        line = line.strip()
                                        if line:
                                            startup_messages.append(line)
                                            config.logger.debug(f"[PICO-STARTUP] {line}")
                                            if line.startswith("RX:"):
                                                config.logger.info(f"[+] Early RX message detected")
                            except Exception as e:
                                config.logger.debug(f"[!] Startup read error: {e}")
                        time.sleep(0.05)
                    
                    if startup_messages:
                        config.logger.info(f"[+] Pico sent {len(startup_messages)} startup messages")
                    else:
                        config.logger.warning("[!] No startup messages (Pico may have been running)")
                    
                    # Clear buffers after consuming startup
                    self._flush_buffers()
                    time.sleep(0.1)
                    
                    # Verify with STATUS
                    config.logger.debug("[>] Sending STATUS command...")
                    self.ser.write(b"STATUS\n")
                    self.ser.flush()
                    
                    response = self._wait_for_response("MODE:", timeout=3.0, skip_debug=True)
                    
                    if response and response.startswith("MODE:"):
                        config.logger.info(f"[+] Connected to {self.port}")
                        config.logger.info(f"[+] Pico status: {response}")
                        
                        # Parse mode
                        try:
                            self.current_mode = response.split(':')[1].strip()
                            config.logger.info(f"[+] Current mode: {self.current_mode}")
                        except:
                            self.current_mode = "RX"
                        
                        self.connection_healthy = True
                        self.initialized = True
                        self.is_reconnecting = False
                        config.stats["reconnections"] += attempt
                        return True
                    else:
                        raise Exception(f"Pico not responding to STATUS - got: {response}")
                        
                except Exception as e:
                    attempt += 1
                    config.logger.error(f"[X] Connection attempt {attempt}/5 failed: {e}")
                    if attempt < 5:
                        config.logger.info(f"[>] Retrying in 2 seconds...")
                        time.sleep(2)
                    else:
                        config.logger.error(f"[X] All connection attempts failed")
            
            self.connection_healthy = False
            self.initialized = False
            self.is_reconnecting = False
            return False
    
    def reconnect(self):
        """Reconnect to Pico"""
        config.logger.warning("[!] Attempting to reconnect...")
        self.connection_healthy = False
        self.initialized = False
        return self.connect()
    
    def is_connected(self):
        """Check connection health"""
        with self.lock:
            return (self.ser is not None and 
                    self.ser.is_open and 
                    self.connection_healthy and 
                    self.initialized and
                    not self.is_reconnecting)
    
    def set_tx_mode(self):
        """
        Switch to TX mode (HALF DUPLEX - blocks RX)
        Must be called BEFORE sending
        """
        with self.lock:
            if not self.is_connected():
                return False
                
            if self.current_mode == "TX":
                return True
            
            try:
                config.logger.debug("[>] Switching to TX mode...")
                self.ser.write(b"TX\n")
                self.ser.flush()
                
                response = self._wait_for_response("OK:TX", timeout=1.0)
                
                if response == "OK:TX":
                    self.current_mode = "TX"
                    config.logger.debug("[+] TX mode active (RX blocked)")
                    time.sleep(0.05)
                    return True
                else:
                    config.logger.warning(f"[X] TX mode failed: {response}")
                    self.connection_healthy = False
                    return False
                    
            except Exception as e:
                config.logger.error(f"[X] TX mode error: {e}")
                self.connection_healthy = False
                return False
    
    def set_rx_mode(self):
        """
        Switch to RX mode (HALF DUPLEX - blocks TX)
        Call AFTER sending to resume receiving
        """
        with self.lock:
            if not self.is_connected():
                return False
                
            if self.current_mode == "RX":
                return True
            
            try:
                config.logger.debug("[>] Switching to RX mode...")
                self.ser.write(b"RX\n")
                self.ser.flush()
                
                response = self._wait_for_response("OK:RX", timeout=1.0)
                
                if response == "OK:RX":
                    self.current_mode = "RX"
                    config.logger.debug("[+] RX mode active (TX blocked)")
                    time.sleep(0.05)
                    return True
                else:
                    config.logger.warning(f"[X] RX mode failed: {response}")
                    self.connection_healthy = False
                    return False
                    
            except Exception as e:
                config.logger.error(f"[X] RX mode error: {e}")
                self.connection_healthy = False
                return False
    
    def send_message(self, message):
        """
        Send message via LoRa - BLOCKING operation
        Handles full TX cycle: switch to TX -> send -> switch to RX
        """
        if not self.is_connected():
            if not self.reconnect():
                config.stats["send_failures"] += 1
                return False
            if not self.is_connected():  # Verify again
                config.stats["send_failures"] += 1
                return False
        
        with self.lock:
            if not self.is_connected():
                config.stats["send_failures"] += 1
                return False
            
            try:
                # Step 1: Switch to TX mode (HALF DUPLEX)
                if not self.set_tx_mode():
                    config.logger.error("[X] Failed to enter TX mode")
                    config.stats["send_failures"] += 1
                    return False
                
                # Step 2: Send immediately (Pico does NOT queue)
                cmd = f"SEND:{message}\n"
                config.logger.info(f"[>] Transmitting ({len(message)} bytes): {message[:50]}...")
                
                self.ser.write(cmd.encode())
                self.ser.flush()
                
                # Step 3: Wait for TX complete (BLOCKING on Pico)
                start_time = time.time()
                success = False
                
                while time.time() - start_time < 5.0:
                    line = self._read_line_internal(timeout=0.2)
                    if line:
                        config.logger.debug(f"[PICO] {line}")
                        
                        if line == "OK:SENT":
                            config.logger.info(f"[+] ✓ Transmission confirmed")
                            success = True
                            break
                        elif line.startswith("ERROR:"):
                            config.logger.error(f"[X] TX error: {line}")
                            success = False
                            break
                    
                    time.sleep(0.05)
                
                if not success:
                    config.logger.warning(f"[X] TX timeout or failed")
                    config.stats["send_failures"] += 1
                else:
                    config.stats["packets_sent"] += 1
                
                # Step 4: Always return to RX mode (restore HALF DUPLEX)
                self.set_rx_mode()
                return success
                    
            except Exception as e:
                config.logger.error(f"[X] Send error: {e}")
                self.connection_healthy = False
                config.stats["send_failures"] += 1
                # Try to restore RX mode
                try:
                    self.set_rx_mode()
                except:
                    pass
                return False
    
    def receive_message(self, timeout=0.5):
        """
        Receive message from LoRa (only works in RX mode)
        Non-blocking with timeout
        FIXED: Better handling of non-JSON RX messages and parse errors
        """
        if not self.is_connected():
            return None
        
        try:
            with self.lock:
                if not self.is_connected():
                    return None
                
                # Can only receive in RX mode (HALF DUPLEX)
                if self.current_mode != "RX":
                    return None
                
                start_time = time.time()
                line = None  # Initialize for error handling
                json_part = None  # Initialize for error handling
                
                while time.time() - start_time < timeout:
                    line = self._read_line_internal(timeout=0.1)
                    
                    if line:
                        if line.startswith("RX:"):
                            content = line[3:]
                            
                            # CRITICAL FIX: Filter out non-JSON RX messages
                            # Debug/status/error messages from Pico may start with RX: but aren't JSON
                            if (content.startswith("DEBUG:") or 
                                content.startswith("ERROR:") or 
                                content.startswith("STATUS:") or
                                content.startswith("CRC_ERROR") or
                                content.startswith("TIMEOUT")):
                                config.logger.debug(f"[PICO-RX] {content}")
                                continue
                            
                            # Split by pipe separator
                            parts = content.split('|')
                            json_part = parts[0].strip()
                            
                            # CRITICAL FIX: Validate it looks like JSON before parsing
                            # Valid JSON messages must start with '{'
                            if not json_part.startswith('{'):
                                config.logger.warning(f"[!] RX message not JSON-formatted: {json_part[:50]}")
                                continue
                            
                            # CRITICAL FIX: Check for minimum valid JSON structure
                            if len(json_part) < 2 or not json_part.endswith('}'):
                                config.logger.warning(f"[!] RX message incomplete: {json_part[:50]}")
                                continue
                            
                            try:
                                packet = json.loads(json_part)
                                
                                # Parse RSSI/SNR from additional parts
                                for p in parts[1:]:
                                    p = p.strip()
                                    if p.startswith("RSSI:"):
                                        try:
                                            packet["rssi"] = int(p.split(":")[1])
                                        except (ValueError, IndexError):
                                            config.logger.debug(f"[!] Invalid RSSI format: {p}")
                                    elif p.startswith("SNR:"):
                                        try:
                                            packet["snr"] = float(p.split(":")[1])
                                        except (ValueError, IndexError):
                                            config.logger.debug(f"[!] Invalid SNR format: {p}")
                                
                                # Successfully parsed packet
                                return packet
                                
                            except json.JSONDecodeError as e:
                                config.logger.error(f"[X] JSON parse error: {e}")
                                config.logger.error(f"[X] Line number: {e.lineno}, Column: {e.colno}")
                                config.logger.error(f"[X] Problematic content: {json_part[:200]}")
                                # Log full content if small enough
                                if len(json_part) < 500:
                                    config.logger.error(f"[X] Full content: {json_part}")
                                continue
                    
                    time.sleep(0.01)
                
                return None
                
        except json.JSONDecodeError as e:
            config.logger.error(f"[X] JSON decode error in receive_message: {e}")
            config.logger.error(f"[X] Error position: line {e.lineno}, column {e.colno}")
            if line:
                config.logger.error(f"[X] Raw line: {line}")
            if json_part:
                config.logger.error(f"[X] JSON part: {json_part[:200]}")
            return None
            
        except Exception as e:
            config.logger.error(f"[X] Unexpected RX error: {type(e).__name__}: {e}")
            if line:
                config.logger.error(f"[X] Raw line was: {line}")
            self.connection_healthy = False
            return None
    
    def close(self):
        """Close serial connection"""
        with self.lock:
            try:
                if self.ser and self.ser.is_open:
                    self.ser.close()
                    config.logger.info("[>] LoRa connection closed")
            except:
                pass
    
    def send_packet(self, packet_dict):
        """Send a packet with auto-populated fields"""
        if "msg_id" not in packet_dict:
            packet_dict["msg_id"] = str(uuid.uuid4())[:8]
        
        if "from" not in packet_dict:
            packet_dict["from"] = config.CODENAME
        
        if "ttl" not in packet_dict:
            packet_dict["ttl"] = config.DEFAULT_TTL
        
        if "via" not in packet_dict:
            packet_dict["via"] = []
        
        json_str = json.dumps(packet_dict, separators=(',', ':'))
        return self.send_message(json_str), packet_dict["msg_id"]
    
    def _send_chunk(self, packet):
        """
        Send chunk without waiting for ACK (fire-and-forget)
        Retries only on transmission failure
        """
        seq = packet["seq"]
        total = packet["total"]
        retry_count = 0
        
        while retry_count <= config.MAX_CHUNK_RETRIES:
            # Send the packet
            config.logger.info(f"📤 Sending chunk {seq+1}/{total} (attempt {retry_count+1}/{config.MAX_CHUNK_RETRIES+1})...")
            success, _ = self.send_packet(packet)
            
            if success:
                config.logger.info(f"✓ Chunk {seq+1}/{total} transmitted successfully")
                return True
            
            # TX failed, retry
            config.logger.error(f"❌ TX FAILED for chunk {seq+1}/{total}")
            retry_count += 1
            
            if retry_count <= config.MAX_CHUNK_RETRIES:
                delay = config.CHUNK_RETRY_DELAY * (2 ** (retry_count - 1))
                config.logger.warning(f"⏳ Retrying in {delay}s...")
                time.sleep(delay)
        
        # All retries failed
        config.logger.error(f"❌ CHUNK {seq+1}/{total} FAILED AFTER {config.MAX_CHUNK_RETRIES+1} ATTEMPTS")
        return False

    def send_json(self, json_obj, to=None, require_ack=False, compress=True, use_base64=True):
        """
        Send JSON data with simple chunk handling (no chunk ACKs)
        Uses global transmission lock to prevent interference
        CRITICAL FIX: Added 'to' parameter to specify destination
        """
        with self.transmission_lock:  # Encapsulated transmission lock
            json_str = json.dumps(json_obj, separators=(',', ':'))
            original_size = len(json_str.encode())
            
            if compress:
                cbor_bytes = cbor2.dumps(json_obj)
                cbor_size = len(cbor_bytes)
                compressed_bytes = zlib.compress(cbor_bytes, level=config.COMPRESSION_LEVEL)
                compressed_size = len(compressed_bytes)
                compression_ratio = (1 - compressed_size / original_size) * 100
                config.logger.info(f"📦 JSON: {original_size}B, CBOR: {cbor_size}B, Compressed: {compressed_size}B ({compression_ratio:.1f}% reduction)")
                data_bytes = compressed_bytes
            else:
                data_bytes = json_str.encode()
            
            checksum = hashlib.md5(data_bytes).hexdigest()[:8]
            msg_id = str(uuid.uuid4())[:8]
            
            # Chunking
            MAX_PACKET_SIZE = 255
            # CRITICAL FIX: Actual measured overhead is ~208-240 bytes for chunk packets
            CHUNK_OVERHEAD_ESTIMATE = 235  # Increased from 125 to prevent TX_TOO_LONG
            chunk_payload_size = ((MAX_PACKET_SIZE - CHUNK_OVERHEAD_ESTIMATE) * 3) // 4
            chunks = [data_bytes[i:i+chunk_payload_size] 
                    for i in range(0, len(data_bytes), chunk_payload_size)]
            
            if len(chunks) == 1:
                # Single packet (no chunking)
                packet_data = base64.b64encode(chunks[0]).decode() if use_base64 else chunks[0]
                
                packet = {
                    "from": config.CODENAME,
                    "msg_id": msg_id,
                    "type": config.MSG_TYPE_DATA,
                    "to": to if to else config.BASE_NODE,
                    "ttl": config.DEFAULT_TTL,
                    "via": [],
                    "checksum": checksum,
                    "compressed": compress,
                    "b64": use_base64,
                    "data": packet_data
                }
                success, _ = self.send_packet(packet)
                if require_ack:
                    return (msg_id, [packet]) if success else (None, [])
                return success
            else:
                # Multi-packet transmission (no ACKs per chunk)
                packets = []
                failed_chunks = []
                
                config.logger.info(f"📊 Sending {len(chunks)} chunks (total {len(data_bytes)} bytes)")
                config.logger.info(f"🔄 Starting chunked transmission (parent_id: {msg_id})")

                for idx, chunk in enumerate(chunks):
                    chunk_id = f"{msg_id}_{idx}"
                    chunk_data = base64.b64encode(chunk).decode() if use_base64 else chunk
                    
                    packet = {
                        "from": config.CODENAME,
                        "msg_id": chunk_id,
                        "type": config.MSG_TYPE_CHUNK,
                        "to": to if to else config.BASE_NODE,
                        "ttl": config.DEFAULT_TTL,
                        "via": [],
                        "seq": idx,
                        "total": len(chunks),
                        "parent_id": msg_id,
                        "checksum": checksum,
                        "compressed": compress,
                        "b64": use_base64,
                        "data": chunk_data
                    }
                    
                    # Send chunk without waiting for ACK
                    config.logger.info(f"📤 Chunk {idx+1}/{len(chunks)}: {len(chunk)} bytes")
                    success = self._send_chunk(packet)
                    
                    if not success:
                        failed_chunks.append(idx)
                        config.logger.error(f"[X] Failed to send chunk {idx+1}/{len(chunks)} after retries")
                    else:
                        packets.append(packet)
                        config.logger.info(f"✓ Chunk {idx+1}/{len(chunks)} transmitted")
                    
                    # Small delay between chunks
                    if idx < len(chunks) - 1:  # Don't delay after last chunk
                        inter_chunk_delay = 0.3
                        config.logger.debug(f"⏸ Waiting {inter_chunk_delay}s before next chunk...")
                        time.sleep(inter_chunk_delay)

                if failed_chunks:
                    config.logger.error(f"[X] Transmission incomplete - failed chunks: {failed_chunks}")
                    config.logger.error(f"[X] Successfully sent: {len(packets)}/{len(chunks)} chunks")
                    return False

                config.logger.info(f"✓ All {len(chunks)} chunks transmitted successfully")
                return True


def read_battery_status():
    """
    Read battery data over I2C and return structured JSON-safe dict.
    Compatible with BASE station display format.
    """
    if smbus is None:
        config.logger.error("Battery read error: smbus module is not available (run on Linux/Raspberry Pi)")
        return {
            "type": "battery",
            "error": "smbus module not available",
            "timestamp": time.time()
        }
        
    try:
        bus = smbus.SMBus(config.I2C_BUS_ID)

        # Read charge status
        status_reg = bus.read_i2c_block_data(config.BATTERY_I2C_ADDR, 0x02, 1)[0]

        if status_reg & 0x40:
            charge_state = "fast_charging"
        elif status_reg & 0x80:
            charge_state = "charging"
        elif status_reg & 0x20:
            charge_state = "discharging"
        else:
            charge_state = "idle"

        # Read VBUS data
        vbus = bus.read_i2c_block_data(config.BATTERY_I2C_ADDR, 0x10, 6)
        
        # Read battery data
        batt = bus.read_i2c_block_data(config.BATTERY_I2C_ADDR, 0x20, 12)
        
        # Read cell voltages
        cells = bus.read_i2c_block_data(config.BATTERY_I2C_ADDR, 0x30, 8)

        # Process battery current (signed integer)
        battery_current = batt[2] | batt[3] << 8
        if battery_current > 0x7FFF:
            battery_current -= 0xFFFF

        # Extract cell voltages
        cell1_mv = cells[0] | cells[1] << 8
        cell2_mv = cells[2] | cells[3] << 8
        cell3_mv = cells[4] | cells[5] << 8
        cell4_mv = cells[6] | cells[7] << 8

        # Check for low voltage condition
        low_voltage = any(
            v < config.LOW_VOL for v in [cell1_mv, cell2_mv, cell3_mv, cell4_mv]
        )

        # Build payload matching BASE station expectations
        payload = {
            "from": config.CODENAME,
            "type": "battery",
            "timestamp": time.time(),
            "charge_state": charge_state,
            "voltage_v": (batt[0] | batt[1] << 8) / 1000,  # Convert mV to V
            "current_ma": battery_current,
            "percent": batt[4] | batt[5] << 8,
            "remaining_mah": batt[6] | batt[7] << 8,
            "time_to_empty_min": (batt[8] | batt[9] << 8) if battery_current < 0 else None,
            "time_to_full_min": (batt[10] | batt[11] << 8) if battery_current >= 0 else None,
            "low_voltage": low_voltage,
            "vbus_voltage_v": (vbus[0] | vbus[1] << 8) / 1000,  # Convert mV to V
            "cells": {
                "cell1": cell1_mv,
                "cell2": cell2_mv,
                "cell3": cell3_mv,
                "cell4": cell4_mv
            },
            # Keep original nested structure for backwards compatibility
            "vbus": {
                "voltage_mv": vbus[0] | vbus[1] << 8,
                "current_ma": vbus[2] | vbus[3] << 8,
                "power_mw": vbus[4] | vbus[5] << 8
            },
            "battery": {
                "voltage_mv": batt[0] | batt[1] << 8,
                "current_ma": battery_current,
                "percent": batt[4] | batt[5] << 8,
                "remaining_mah": batt[6] | batt[7] << 8,
                "time_to_empty_min": (batt[8] | batt[9] << 8) if battery_current < 0 else None,
                "time_to_full_min": (batt[10] | batt[11] << 8) if battery_current >= 0 else None
            },
            "cells_mv": {
                "cell1": cell1_mv,
                "cell2": cell2_mv,
                "cell3": cell3_mv,
                "cell4": cell4_mv
            }
        }

        return payload

    except Exception as e:
        config.logger.error(f"Battery read error: {e}")
        return {
            "type": "battery",
            "error": str(e),
            "timestamp": time.time()
        }
