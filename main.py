#!/usr/bin/env python3
"""
LoRa Main Controller - Multi-Hop Mesh Networking
BASE STATION Configuration with New Pico Architecture

Refactored to be modular and clean.
"""

import time
import json
import os
import sys
import subprocess
import threading
import argparse
import uuid

import config
import hardware
from mesh import MeshNetwork, send_command_status
from executor import ScriptExecutor


def parse_command(message, lora, mesh_manager, executor):
    """Parse and handle incoming messages with command/mesh support"""
    try:
        if isinstance(message, str):
            packet = json.loads(message)
        elif isinstance(message, dict):
            packet = message
        else:
            config.logger.error(f"[X] Invalid message type: {type(message)}")
            return
        
        sender = packet.get("from", "?")
        to = packet.get("to", "?")
        msg_id = packet.get("msg_id", "?")
        cmd_type = packet.get("type", "?")
        hops = packet.get("hops", 0)
        rssi = packet.get("rssi")
        via = packet.get("via", [])
        
        # Update neighbor info (direct if no hops)
        direct = (len(via) == 0)
        mesh_manager.update_neighbor(sender, hops=hops, direct=direct, rssi=rssi)
        
        # Check for duplicates
        if cmd_type == config.MSG_TYPE_CHUNK:
            parent_id = packet.get("parent_id")
            seq = packet.get("seq")
            if mesh_manager.is_duplicate(sender, msg_id, parent_id=parent_id, seq=seq):
                config.logger.debug(f"⊗ Duplicate chunk {seq} from {sender}")
                return
        else:
            if mesh_manager.is_duplicate(sender, msg_id):
                config.logger.debug(f"⊗ Duplicate packet {msg_id} from {sender}")
                return
        
        # Handle if message is for us
        if to == config.CODENAME or to == "ALL":
            # Handle chunk reassembly first
            if cmd_type == config.MSG_TYPE_CHUNK:
                mesh_manager.mark_chunk_received(sender)
                json_obj = mesh_manager.reassemble_chunks(sender, packet)
                if json_obj:
                    config.logger.info(f"📦 Reassembled data from {sender}")
                return
            
            # Handle PING
            if cmd_type == config.MSG_TYPE_PING:
                config.logger.info(f"📡 PING from {sender} (hops={hops})")
                
                with lora.transmission_lock:
                    ack = {
                        "from": config.CODENAME,
                        "msg_id": str(uuid.uuid4())[:8],
                        "type": config.MSG_TYPE_ACK,
                        "to": sender,
                        "ttl": config.DEFAULT_TTL,
                        "via": [],
                        "ack_msg_id": msg_id,
                        "timestamp": time.time()
                    }
                    lora.send_packet(ack)
                    config.logger.info(f"📨 Sent ACK to {sender}")
        
            elif cmd_type == config.MSG_TYPE_ACK:
                ack_msg_id = packet.get("ack_msg_id", "?")
                config.logger.info(f"✓ ACK received from {sender} for message {ack_msg_id}")
                
                if ack_msg_id in mesh_manager.pending_acks:
                    del mesh_manager.pending_acks[ack_msg_id]
        
            elif cmd_type == config.MSG_TYPE_COMMAND:
                command_id = msg_id
                sender_node = sender
                
                with lora.transmission_lock:
                    ack = {
                        "from": config.CODENAME,
                        "msg_id": str(uuid.uuid4())[:8],
                        "type": config.MSG_TYPE_CMD_ACK,
                        "to": sender_node,
                        "ack_msg_id": command_id,
                        "ttl": config.DEFAULT_TTL,
                        "via": [],
                        "timestamp": time.time()
                    }
                    lora.send_packet(ack)
                
                scripts_requested = packet.get("script")
                args_dict = packet.get("args", {})
                
                if isinstance(scripts_requested, str):
                    scripts_list = [scripts_requested]
                elif isinstance(scripts_requested, list):
                    scripts_list = scripts_requested
                else:
                    config.logger.warning(f"⚠ Invalid script format in command: {scripts_requested}")
                    return
                
                executor.command_result_map[command_id] = {}
                for script in scripts_list:
                    if script in config.COMMAND_RESULT_REGISTRY:
                        executor.command_result_map[command_id][script] = config.COMMAND_RESULT_REGISTRY[script]
                
                config.logger.info(f"⚙ Executing command {command_id} from {sender_node}: {scripts_list}")
                
                for script_name in scripts_list:
                    script_args = args_dict.get(script_name, [])
                    
                    if script_name == "fish_detect":
                        executor.start_background_script(
                            config.FISH_DETECTION_SCRIPT,
                            script_args,
                            f"FishDetection:{command_id}",
                            command_id=command_id,
                            sender=sender_node
                        )
                    
                    elif script_name == "sonar":
                        executor.start_background_script(
                            config.SONAR_SCRIPT,
                            script_args,
                            f"Sonar:{command_id}",
                            command_id=command_id,
                            sender=sender_node
                        )

                    elif script_name == "gps":
                        executor.start_background_script(
                            config.GPS_SCRIPT,
                            script_args,
                            f"GPS:{command_id}",
                            command_id=command_id,
                            sender=sender_node
                        )

                    elif script_name == "battery":
                        send_command_status(lora, sender_node, command_id, "battery",
                                            "running", 0, "")
                        
                        try:
                            battery_data = hardware.read_battery_status()
                            lora.send_json(battery_data, to=sender_node, require_ack=True)
                            config.logger.info("🔋 Battery status sent to BASE")
                            
                            send_command_status(lora, sender_node, command_id, "battery",
                                                "completed", 0, "OK")
                        except Exception as e:
                            config.logger.error(f"[X] Battery read error: {e}")
                            send_command_status(lora, sender_node, command_id, "battery",
                                                "error", -1, str(e)[:20])
                    else:
                        config.logger.warning(f"⚠ Unknown script: {script_name}")
                        send_command_status(lora, sender_node, command_id, script_name,
                                            "error", -1, "Unknown")
            
            elif cmd_type == config.MSG_TYPE_LINUX_CMD:
                command_id = msg_id
                sender_node = sender

                with lora.transmission_lock:
                    ack = {
                        "from": config.CODENAME,
                        "msg_id": str(uuid.uuid4())[:8],
                        "type": config.MSG_TYPE_CMD_ACK,
                        "to": sender_node,
                        "ack_msg_id": command_id,
                        "ttl": config.DEFAULT_TTL,
                        "via": [],
                        "timestamp": time.time()
                    }
                    lora.send_packet(ack)

                command = packet.get("command")
                if not command:
                    config.logger.warning("⚠ Linux command missing")
                    return

                config.logger.info(f"🖥 Executing Linux command: {command}")

                threading.Thread(
                    target=executor.run_linux_command,
                    args=(command, command_id, sender_node),
                    daemon=True
                ).start()

            elif cmd_type == config.MSG_TYPE_ALERT:
                alert_msg = packet.get("message", "")
                config.logger.warning(f"🚨 ALERT from {sender} (hops={hops}): {alert_msg}")
            
            elif cmd_type == config.MSG_TYPE_ALIVE:
                config.logger.info(f"💓 Alive ping from {sender} (hops={hops})")
        
        elif cmd_type == config.MSG_TYPE_DATA:
            if to == config.CODENAME or to == config.BASE_NODE:
                with lora.transmission_lock:
                    ack = {
                        "from": config.CODENAME,
                        "msg_id": str(uuid.uuid4())[:8],
                        "type": config.MSG_TYPE_ACK,
                        "to": sender,
                        "ttl": config.DEFAULT_TTL,
                        "via": [],
                        "ack_msg_id": msg_id,
                        "timestamp": time.time()
                    }
                    lora.send_packet(ack)
                    config.logger.info(f"📥 Data from {sender} (hops={hops})")
            else:
                mesh_manager.forward_packet(packet, lora)
        
        else:
            if to != config.CODENAME and to != "ALL":
                if packet.get("ttl", 0) > 0:
                    config.logger.debug(f"📤 Message not for us, forwarding to {to}")
                    mesh_manager.forward_packet(packet, lora)
        
    except json.JSONDecodeError as e:
        config.logger.error(f"[X] JSON parse error: {e}")
    except Exception as e:
        config.logger.error(f"[X] Command parse error: {e}")


def print_statistics(mesh_manager):
    """Print network statistics"""
    config.logger.info("📈 Network Statistics:")
    config.logger.info(f"  Packets sent: {config.stats['packets_sent']}")
    config.logger.info(f"  Packets received: {config.stats['packets_received']}")
    config.logger.info(f"  Packets forwarded: {config.stats['packets_forwarded']}")
    config.logger.info(f"  Chunks reassembled: {config.stats['chunks_reassembled']}")
    config.logger.info(f"  Chunk retries: {config.stats['chunk_retries']}")
    config.logger.info(f"  Duplicates dropped: {config.stats['duplicates_dropped']}")
    config.logger.info(f"  Send failures: {config.stats['send_failures']}")
    config.logger.info(f"  Reconnections: {config.stats['reconnections']}")
    config.logger.info(f"  Queue size: {len(mesh_manager.json_send_queue)}/{config.MAX_SEND_QUEUE_SIZE}")
    config.logger.info(f"  Pending ACKs: {len(mesh_manager.pending_acks)}")


def main():
    parser = argparse.ArgumentParser(description="LoRa Main Controller - Multi-Hop Mesh")
    parser.add_argument("--port", default=config.SERIAL_PORT, help="Serial port")
    parser.add_argument("--codename", default=config.CODENAME, help="Device codename")
    args = parser.parse_args()
    
    # Initialize config state
    config.CODENAME = args.codename
    config.setup_logging(args.codename)
    
    config.logger.info("="*60)
    config.logger.info(f"LoRa Multi-Hop Mesh Controller - {config.CODENAME}")
    config.logger.info("="*60)
    
    config.logger.info(f"[i] BUOY Base Directory: {config.BASE_DIR}")
    config.logger.info(f"[i] Current Working Dir: {os.getcwd()}")
    config.logger.info(f"[i] Home Directory: {os.path.expanduser('~')}")
    config.logger.info(f"[i] Script Paths:")
    config.logger.info(f"    Fish: {config.FISH_DETECTION_SCRIPT}")
    config.logger.info(f"    Sonar: {config.SONAR_SCRIPT}")
    config.logger.info(f"    GPS: {config.GPS_SCRIPT}")
    config.logger.info(f"[i] Result Files:")
    config.logger.info(f"    Fish: {config.FISH_RESULTS}")
    config.logger.info(f"    Sonar: {config.SONAR_RESULTS}")
    config.logger.info(f"    GPS: {config.GPS_DATA_RESULTS}")

    relay = hardware.LightRelayController(config.LIGHT_PORT)
    lora = hardware.LoRaController(args.port)
    
    if not lora.connect():
        sys.exit(1)
        
    mesh_manager = MeshNetwork()
    executor = ScriptExecutor(lora)
    
    try:
        relay.startup_blink(success=True)  # green for successful startup
    except Exception as e:
        try:
            relay.startup_blink(success=False)
        except:
            pass
        sys.exit(1)
    
    led_thread = threading.Thread(
        target=relay.night_controller,
        daemon=True
    )
    led_thread.start()
    
    # Timing trackers
    last_discovery = 0
    last_neighbor_cleanup = 0
    last_retry_check = 0
    last_table_print = 0
    last_chunk_cleanup = 0
    last_alive_ping = 0
    last_stats_print = 0
    
    try:
        config.logger.info("🚀 Controller started, entering main loop...")
        
        while True:
            current_time = time.time()
            
            # BASE-driven discovery
            if config.CODENAME == config.BASE_NODE and current_time - last_discovery > 60:
                mesh_manager.discover_neighbors(lora)
                last_discovery = current_time
            
            # Buoy alive pings
            if config.CODENAME != config.BASE_NODE and current_time - last_alive_ping > 180:
                mesh_manager.send_alive_ping(lora)
                last_alive_ping = current_time
            
            # Periodic neighbor cleanup
            if current_time - last_neighbor_cleanup > 120:
                mesh_manager.cleanup_stale_neighbors()
                last_neighbor_cleanup = current_time
            
            # Retry pending ACKs
            if current_time - last_retry_check > 10:
                mesh_manager.retry_pending_acks(lora)
                last_retry_check = current_time
            
            # Cleanup incomplete chunks
            if current_time - last_chunk_cleanup > 60:
                mesh_manager.cleanup_chunk_buffer()
                last_chunk_cleanup = current_time
            
            # Check background script outputs
            executor.check_script_outputs()
            
            # Read incoming messages
            msg = lora.receive_message(timeout=0.2)
            if msg:
                parse_command(msg, lora, mesh_manager, executor)
            
            # Send any pending data files
            mesh_manager.send_json_files()
            mesh_manager.process_json_send_queue(lora)
            
            # Print neighbor table periodically
            if current_time - last_table_print > 30:
                mesh_manager.print_neighbor_table()
                last_table_print = current_time
            
            # Print statistics periodically
            if current_time - last_stats_print > 60:
                print_statistics(mesh_manager)
                last_stats_print = current_time
            
            time.sleep(0.05)
    
    except KeyboardInterrupt:
        config.logger.info("\n✂ Exiting LoRa controller...")
    
    finally:
        config.logger.info("🛑 Shutting down...")
        
        try:
            subprocess.run(["pkill", "-15", "-f", "fish_detect.py"], capture_output=True, timeout=1)
            subprocess.run(["pkill", "-9", "-f", "libcamera"], capture_output=True, timeout=1)
            subprocess.run(["pkill", "-9", "-f", "picamera"], capture_output=True, timeout=1)
        except:
            pass
        
        lora.close()
        
        for script_name, thread in list(executor.active_scripts.items()):
            config.logger.debug(f"Waiting for {script_name} to finish...")
            thread.join(timeout=2)
        
        config.logger.info("✓ Controller shutdown complete.")


if __name__ == "__main__":
    main()