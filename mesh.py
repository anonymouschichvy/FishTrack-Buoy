import time
import random
import json
import hashlib
import uuid
import base64
import cbor2
import zlib
import os
import threading
from collections import deque
import config


class MeshNetwork:
    """
    Manages multi-hop routing, neighbor discovery, alive status checks,
    packet deduplication, and chunked packet reassembly.
    """
    
    def __init__(self):
        self.neighbor_list = {}
        self.seen_messages = {}
        self.seen_messages_lru = deque(maxlen=config.MAX_SEEN_MESSAGES)
        self.chunk_buffer = {}
        self.receiving_chunks = {}
        self.pending_acks = {}
        self.json_send_queue = deque()
        
        self.seen_lock = threading.Lock()
        self.chunk_lock = threading.Lock()
        self.chunk_reception_lock = threading.Lock()

    def is_receiving_chunks(self):
        """
        Check if actively receiving chunks from any node.
        Prevents alive pings during chunk reception.
        """
        current_time = time.time()
        with self.chunk_reception_lock:
            # Clean up old entries (>5 seconds)
            expired = [sender for sender, last_time in self.receiving_chunks.items()
                      if current_time - last_time > 5.0]
            for sender in expired:
                del self.receiving_chunks[sender]
            
            return len(self.receiving_chunks) > 0

    def mark_chunk_received(self, sender):
        """Mark that we just received a chunk from a sender"""
        with self.chunk_reception_lock:
            self.receiving_chunks[sender] = time.time()
            config.logger.debug(f"📦 Marked chunk reception from {sender}")

    def reassemble_chunks(self, sender, packet):
        """
        Reassemble chunked packets into original JSON object.
        Thread-safe implementation with validation.
        """
        seq = packet.get("seq")
        total = packet.get("total")
        parent_id = packet.get("parent_id")
        checksum = packet.get("checksum")
        compressed = packet.get("compressed", False)
        is_b64 = packet.get("b64", True)
        data = packet.get("data")
        
        # Validate required fields
        if None in (seq, total, parent_id, data, checksum):
            config.logger.error(f"[X] Invalid chunk packet from {sender}: missing fields")
            return None
        
        if not isinstance(seq, int) or not isinstance(total, int):
            config.logger.error(f"[X] Invalid chunk fields from {sender}: seq={seq}, total={total}")
            return None
        
        if total <= 0:
            config.logger.error(f"[X] Invalid chunk total={total} from {sender}")
            return None
        
        if seq < 0 or seq >= total:
            config.logger.error(f"[X] Invalid chunk index {seq}/{total} from {sender}")
            return None
        
        key = f"{sender}_{parent_id}"
        
        with self.chunk_lock:
            # Initialize buffer if first chunk
            if key not in self.chunk_buffer:
                self.chunk_buffer[key] = {
                    "chunks": {},
                    "checksum": checksum,
                    "total": total,
                    "compressed": compressed,
                    "b64": is_b64,
                    "last_seen": time.time(),
                    "sender": sender
                }
            
            buffer = self.chunk_buffer[key]
            buffer["last_seen"] = time.time()
            
            # Decode Base64 with error handling
            try:
                if is_b64:
                    data_bytes = base64.b64decode(data)
                else:
                    data_bytes = data if isinstance(data, bytes) else data.encode()
            except Exception as e:
                config.logger.error(f"[X] Base64 decode error for chunk {seq} from {sender}: {e}")
                return None
            
            # Store chunk
            buffer["chunks"][seq] = data_bytes
            
            # Check if complete - ALL chunks must be present
            if len(buffer["chunks"]) < total:
                config.logger.info(f"⏳ Chunk {seq+1}/{total} from {sender} (have {len(buffer['chunks'])}/{total})")
                return None
            
            # Verify ALL chunk indices are present before assembling
            missing_chunks = [i for i in range(total) if i not in buffer["chunks"]]
            if missing_chunks:
                config.logger.error(f"[X] Missing chunks {missing_chunks} from {sender} (have {len(buffer['chunks'])}/{total})")
                config.logger.error(f"   Present chunks: {sorted(buffer['chunks'].keys())}")
                return None
            
            # Safe reassembly - all chunks confirmed present
            try:
                full_bytes = b''.join(buffer["chunks"][i] for i in range(total))
            except KeyError as e:
                config.logger.error(f"[X] Missing chunk {e} from {sender} during assembly")
                del self.chunk_buffer[key]
                return None
            
            # Verify checksum (MD5, first 8 chars like BUOY)
            actual_checksum = hashlib.md5(full_bytes).hexdigest()[:8]
            if actual_checksum != checksum:
                config.logger.error(
                    f"[X] Checksum mismatch from {sender}: "
                    f"expected {checksum}, got {actual_checksum}"
                )
                del self.chunk_buffer[key]
                return None
            
            # Decode CBOR+zlib or plain JSON
            try:
                if compressed:
                    json_obj = cbor2.loads(zlib.decompress(full_bytes))
                else:
                    json_obj = json.loads(full_bytes.decode())
            except Exception as e:
                config.logger.error(f"[X] Decode error from {sender}: {e}")
                del self.chunk_buffer[key]
                return None
            
            # Success - cleanup and return
            del self.chunk_buffer[key]
            config.stats["chunks_reassembled"] += 1
            config.logger.info(f"✓ Reassembled {total} chunks from {sender} ({len(full_bytes)} bytes)")
            
            return json_obj

    def is_duplicate(self, sender, msg_id, parent_id=None, seq=None):
        """Check if message has been seen before (thread-safe)"""
        current_time = time.time()
        
        if parent_id is not None and seq is not None:
            key = (sender, parent_id, seq)
        else:
            key = (sender, msg_id)
        
        with self.seen_lock:
            if key in self.seen_messages:
                if current_time - self.seen_messages[key] < 300:
                    config.stats["duplicates_dropped"] += 1
                    return True
                else:
                    del self.seen_messages[key]
            
            self.seen_messages[key] = current_time
            self.seen_messages_lru.append(key)
            
            if len(self.seen_messages) > config.MAX_SEEN_MESSAGES:
                cutoff_time = current_time - 300
                to_remove = [k for k, t in self.seen_messages.items() if t < cutoff_time]
                for k in to_remove[:config.MAX_SEEN_MESSAGES // 4]:
                    if k in self.seen_messages:
                        del self.seen_messages[k]
        
        return False

    def update_neighbor(self, sender, hops=None, direct=False, rssi=None):
        """Update neighbor information with hop count and direct/indirect status"""
        current_time = time.time()
        
        if sender not in self.neighbor_list:
            self.neighbor_list[sender] = {
                "last_seen": current_time,
                "last_direct": None,
                "hops": hops if hops is not None else 999,
                "rssi": rssi
            }
        
        self.neighbor_list[sender]["last_seen"] = current_time
        
        if direct:
            self.neighbor_list[sender]["last_direct"] = current_time
            self.neighbor_list[sender]["hops"] = 0
        elif hops is not None:
            if hops < self.neighbor_list[sender]["hops"]:
                self.neighbor_list[sender]["hops"] = hops
        
        if rssi is not None:
            self.neighbor_list[sender]["rssi"] = rssi

    def forward_packet(self, packet, lora_controller):
        """Forward a packet to the next hop (multi-hop relay)"""
        if packet.get("ttl", 0) <= 0:
            config.logger.debug(f"⊗ TTL expired for packet {packet.get('msg_id')}")
            return False
        
        via = packet.get("via", [])
        if config.CODENAME in via:
            config.logger.warning(f"⊗ Routing loop detected for packet {packet.get('msg_id')}")
            return False
        
        # Limit via array size for chunk packets to prevent TX_TOO_LONG
        max_via_length = 2 if packet.get("type") == config.MSG_TYPE_CHUNK else 5
        
        if len(via) >= max_via_length:
            config.logger.warning(f"⊗ Via array too long ({len(via)} hops) for packet {packet.get('msg_id')}, dropping")
            return False
        
        time.sleep(random.uniform(config.FORWARD_JITTER_MIN, config.FORWARD_JITTER_MAX))
        
        packet["ttl"] -= 1
        packet["via"] = via + [config.CODENAME]
        hops = len(packet["via"]) + 1
        packet["hops"] = hops
        
        config.logger.info(f"↻ Forwarding packet {packet.get('msg_id')} from {packet.get('from')} (TTL={packet['ttl']}, hops={hops})")
        
        json_str = json.dumps(packet, separators=(',', ':'))
        
        # Use transmission lock of lora_controller
        with lora_controller.transmission_lock:
            success = lora_controller.send_message(json_str)
        
        if success:
            config.stats["packets_forwarded"] += 1
        
        return success

    def send_json_files(self):
        """Check for new JSON files to send and add to queue"""
        files_to_check = [
            ("sonar", config.SONAR_RESULTS),
            ("fish", config.FISH_RESULTS),
            ("gps", config.GPS_DATA_RESULTS)
        ]
        
        for file_type, file_path in files_to_check:
            if os.path.exists(file_path):
                try:
                    with open(file_path, 'r') as f:
                        payload = json.load(f)
                    
                    # Check if already in queue
                    if not any(fp == file_path for _, fp, _ in self.json_send_queue):
                        # Check queue size limit
                        if len(self.json_send_queue) >= config.MAX_SEND_QUEUE_SIZE:
                            config.logger.warning(f"⚠ Send queue full ({config.MAX_SEND_QUEUE_SIZE}), dropping oldest entry")
                            self.json_send_queue.popleft()
                        
                        self.json_send_queue.append((file_type, file_path, payload))
                        config.logger.info(f"📋 Queued {file_type} data for sending (queue size: {len(self.json_send_queue)})")
                except Exception as e:
                    config.logger.error(f"[X] Failed to read {file_path}: {e}")

    def process_json_send_queue(self, lora_controller):
        """Send the next JSON file in the queue if no pending ACKs"""
        if not self.json_send_queue or self.pending_acks:
            return
        
        file_type, file_path, payload = self.json_send_queue.popleft()
        result = lora_controller.send_json(payload, to=config.BASE_NODE, require_ack=True)
        
        if isinstance(result, tuple):
            msg_id, packets = result
            if msg_id:
                config.logger.info(f"✓ Sent {file_type} data successfully")
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        config.logger.debug(f"🗑️ Deleted {file_path} after successful transmission")
                except Exception as e:
                    config.logger.warning(f"⚠ Failed to delete {file_path}: {e}")
            else:
                config.logger.error(f"[X] Failed to send {file_type} data, will retry")
                if len(self.json_send_queue) < config.MAX_SEND_QUEUE_SIZE:
                    self.json_send_queue.append((file_type, file_path, payload))
        elif result:
            config.logger.info(f"✓ Sent {file_type} data successfully (chunked)")
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    config.logger.debug(f"🗑️ Deleted {file_path} after successful transmission")
            except Exception as e:
                config.logger.warning(f"⚠ Failed to delete {file_path}: {e}")
        else:
            config.logger.error(f"[X] Failed to send {file_type} data, will retry")
            if len(self.json_send_queue) < config.MAX_SEND_QUEUE_SIZE:
                self.json_send_queue.append((file_type, file_path, payload))

    def retry_pending_acks(self, lora_controller):
        """Retry sending files with exponential backoff"""
        current_time = time.time()
        to_retry = []
        
        for msg_id, (timestamp, file_path, retry_count, payload) in list(self.pending_acks.items()):
            timeout = 30 * (2 ** retry_count)
            
            if current_time - timestamp > timeout:
                if retry_count < 3:
                    to_retry.append((msg_id, file_path, retry_count, payload))
                    del self.pending_acks[msg_id]
                else:
                    config.logger.error(f"[X] Max retries reached for {file_path}")
                    del self.pending_acks[msg_id]
        
        for msg_id, file_path, retry_count, payload in to_retry:
            time.sleep(random.uniform(0.5, 2.0))
            config.logger.info(f"⟳ Retrying {file_path} (attempt {retry_count + 2}/4)")
            result = lora_controller.send_json(payload, to=config.BASE_NODE, require_ack=True)
            
            if isinstance(result, tuple):
                new_msg_id, _ = result
                if new_msg_id:
                    self.pending_acks[new_msg_id] = (time.time(), file_path, retry_count + 1, payload)
                    config.logger.info(f"✓ Retry queued with msg_id {new_msg_id}")
                else:
                    config.logger.error(f"[X] Retry failed for {file_path}")
            elif result:
                config.logger.info(f"✓ Retry succeeded for {file_path} (chunked, no ACK)")
            else:
                config.logger.error(f"[X] Retry failed for {file_path}")

    def cleanup_chunk_buffer(self):
        """Remove incomplete chunks that have timed out"""
        current_time = time.time()
        purged_count = 0
        total_chunks_lost = 0
        
        with self.chunk_lock:
            to_delete = []
            for key, buffer in self.chunk_buffer.items():
                if current_time - buffer.get("last_seen", 0) > config.CHUNK_TIMEOUT:
                    to_delete.append(key)
                    chunks_received = len(buffer.get("chunks", {}))
                    total_expected = buffer.get("total", 0)
                    total_chunks_lost += (total_expected - chunks_received)
            
            for key in to_delete:
                del self.chunk_buffer[key]
                purged_count += 1
            
            if purged_count > 0:
                config.logger.warning(f"⚠ Purged {purged_count} incomplete transmissions ({total_chunks_lost} chunks lost)")
            
            return purged_count

    def discover_neighbors(self, lora_controller):
        """Send ping to discover neighbors (BASE-driven only) - Uses transmission lock"""
        if config.CODENAME != config.BASE_NODE:
            return
        
        if lora_controller.transmission_lock.locked():
            config.logger.debug("⏸ Skipping discovery ping - active transmission in progress")
            return
        
        with lora_controller.transmission_lock:
            ping = {
                "from": config.CODENAME,
                "msg_id": str(uuid.uuid4())[:8],
                "type": config.MSG_TYPE_PING,
                "target": "ALL",
                "to": "ALL",
                "ttl": config.DEFAULT_TTL,
                "via": [],
                "timestamp": time.time()
            }
            lora_controller.send_packet(ping)
            config.logger.info("📡 BASE neighbor discovery ping sent")

    def send_alive_ping(self, lora_controller):
        """Send alive ping from buoy to BASE - Uses transmission lock to prevent interference"""
        if config.CODENAME == config.BASE_NODE:
            return
        
        if self.is_receiving_chunks():
            config.logger.debug("⏸ Skipping alive ping - receiving chunks")
            return
        
        if lora_controller.transmission_lock.locked():
            config.logger.debug("⏸ Skipping alive ping - active transmission in progress")
            return
        
        with lora_controller.transmission_lock:
            alive = {
                "from": config.CODENAME,
                "msg_id": str(uuid.uuid4())[:8],
                "type": config.MSG_TYPE_ALIVE,
                "to": config.BASE_NODE,
                "ttl": config.DEFAULT_TTL,
                "via": [],
                "timestamp": time.time()
            }
            lora_controller.send_packet(alive)
            config.logger.info("💓 Alive ping sent to BASE")

    def cleanup_stale_neighbors(self):
        """Remove neighbors that haven't been seen recently"""
        current_time = time.time()
        stale = [n for n, info in self.neighbor_list.items() 
                if current_time - info["last_seen"] > config.NEIGHBOR_TIMEOUT]
        
        for n in stale:
            del self.neighbor_list[n]
            config.logger.warning(f"⚠ Removed stale neighbor {n}")

    def print_neighbor_table(self):
        """Display current neighbor information with statistics"""
        if not self.neighbor_list:
            config.logger.info("📊 Neighbor Table: (empty)")
            return
        
        config.logger.info("📊 Neighbor Table:")
        current_time = time.time()
        direct_count = 0
        relay_count = 0
        
        for n, info in sorted(self.neighbor_list.items()):
            last_seen = int(current_time - info["last_seen"])
            hops = info.get('hops', '?')
            rssi = info.get('rssi', 'N/A')
            is_direct = info.get("last_direct") is not None
            direct_indicator = "✓ direct" if is_direct else "via relay"
            
            if is_direct:
                direct_count += 1
            else:
                relay_count += 1
            
            config.logger.info(f"  {n}: hops={hops}, last_seen={last_seen}s ago, rssi={rssi}, {direct_indicator}")
        
        config.logger.info(f"  Summary: {len(self.neighbor_list)} total ({direct_count} direct, {relay_count} relayed)")


def send_command_status(lora_controller, sender, command_id, script_name, status, exit_code=0, message=""):
    """
    Send command status update to BASE station
    CRITICAL FIX: Compact packet format to stay under 255 byte LoRa limit
    """
    try:
        status_packet = {
            "from": config.CODENAME,
            "msg_id": str(uuid.uuid4())[:8],
            "type": config.MSG_TYPE_CMD_STATUS,
            "to": sender,
            "cmd_id": command_id,
            "script": script_name[:15],
            "status": status,
            "code": exit_code,
            "ttl": config.DEFAULT_TTL
        }
        
        if message and status == "error":
            status_packet["msg"] = message[:20]
        
        with lora_controller.transmission_lock:
            success = lora_controller.send_packet(status_packet)
        
        if success:
            config.logger.info(f"✓ Sent {status} status for {script_name} to {sender}")
        else:
            config.logger.error(f"[X] Failed to send status for {script_name}")
        
        return success
    except Exception as e:
        config.logger.error(f"[X] Error sending command status: {e}")
        return False

