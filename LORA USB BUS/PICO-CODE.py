"""
Raspberry Pi Pico YD-RP2040 - LoRa to USB Bridge
Half-duplex operation - TX or RX, never both simultaneously
NO MESSAGE QUEUING - immediate transmission only

HARDWARE CONTROL:
- EN = Low (0) = Switch enabled
- CTRL = Low (0) = RF2 active (TX path)
- CTRL = High (1) = RF1 active (RX path)
- RELAY = Low (0) = Amplifier ON (ACTIVE LOW)
- RELAY = High (1) = Amplifier OFF (ACTIVE LOW)

FIXED VERSION - TX_TIMEOUT bug resolved
CRITICAL FIXES:
1. Added PaDac register configuration (0x4D = 0x87) for +20dBm PA_BOOST
2. Increased impedance matching delays
3. Added TX mode verification
4. Improved startup synchronization
"""

import machine
import utime
import sys
import neopixel
import select
from machine import Pin, SPI

# Pin Definitions
SPDT_EN = Pin(10, Pin.OUT)
SPDT_CTRL = Pin(11, Pin.OUT)
RELAY_IN1 = Pin(12, Pin.OUT)  # ACTIVE LOW relay

# LoRa SPI Pins
SPI_SCK = 2
SPI_MOSI = 3
SPI_MISO = 4
LORA_CS = 5
LORA_RST = 6
LORA_DIO0 = 7

# Initialize SPI
spi = SPI(0, 
          baudrate=1000000,
          polarity=0,
          phase=0,
          sck=Pin(SPI_SCK),
          mosi=Pin(SPI_MOSI),
          miso=Pin(SPI_MISO))

lora_cs = Pin(LORA_CS, Pin.OUT)
lora_rst = Pin(LORA_RST, Pin.OUT)
lora_dio0 = Pin(LORA_DIO0, Pin.IN)

# LED indicator
np = neopixel.NeoPixel(Pin(23), 1)

# Global state - HALF DUPLEX
current_mode = None  # 'TX' or 'RX' or None

# Setup stdin polling
poll_obj = select.poll()
poll_obj.register(sys.stdin, select.POLLIN)

def init_hardware():
    """Initialize hardware in RX mode"""
    global current_mode
    
    print("INIT_START")
    
    # Ensure amplifier is OFF (active-low, so HIGH = OFF)
    RELAY_IN1.value(1)
    utime.sleep_ms(50)
    
    # Reset LoRa module
    lora_rst.value(0)
    utime.sleep_ms(10)
    lora_rst.value(1)
    utime.sleep_ms(10)
    
    lora_cs.value(1)
    
    # Start in RX mode
    set_rx_mode()
    current_mode = 'RX'
    
    # Flash green LED = ready
    for _ in range(3):
        np[0] = (0, 255, 0)
        np.write()
        utime.sleep_ms(100)
        np[0] = (0, 0, 0)
        np.write()
        utime.sleep_ms(100)
    
    print("READY")

def set_tx_mode():
    """
    Switch to TX mode - BLOCKS RX
    CRITICAL FIX: Verify and restore LoRa mode if needed
    """
    global current_mode
    
    if current_mode == "TX":
        return True
    
    print("DEBUG:TX_MODE_START")
    
    # CRITICAL FIX: Verify we're in LoRa mode before proceeding
    try:
        mode_reg = lora_read_reg(0x01)
        print(f"DEBUG:INITIAL_MODE:0x{mode_reg:02X}")
        
        # Check if LoRa mode bit (bit 7) is set
        if (mode_reg & 0x80) == 0:
            print("WARN:NOT_IN_LORA_MODE_RESTORING")
            lora_write_reg(0x01, 0x80)  # LoRa sleep mode
            utime.sleep_ms(20)
            mode_reg = lora_read_reg(0x01)
            print(f"DEBUG:AFTER_RESTORE:0x{mode_reg:02X}")
    except Exception as e:
        print(f"ERROR:MODE_CHECK_FAIL:{e}")
    
    # STEP 1: Put LoRa in standby FIRST (before any RF switching)
    try:
        lora_write_reg(0x01, 0x81)  # LoRa + Standby mode
        utime.sleep_ms(10)
        
        # Verify standby was set
        verify_reg = lora_read_reg(0x01)
        print(f"DEBUG:LORA_STANDBY:0x{verify_reg:02X}")
        
        if (verify_reg & 0x87) != 0x81:
            print(f"WARN:STANDBY_UNEXPECTED:0x{verify_reg:02X}")
            # Try again
            lora_write_reg(0x01, 0x80)  # Sleep
            utime.sleep_ms(10)
            lora_write_reg(0x01, 0x81)  # Standby
            utime.sleep_ms(10)
            
    except Exception as e:
        print(f"ERROR:LORA_STANDBY_FAIL:{e}")
        return False
    
    # STEP 2: Switch RF path to TX (before powering amplifier)
    SPDT_CTRL.value(0)  # RF2 (TX)
    SPDT_EN.value(0)    # Enable
    utime.sleep_ms(50)
    print("DEBUG:RF_SWITCH_TX")
    
    # STEP 3: Power on amplifier (ACTIVE LOW - set to 0)
    RELAY_IN1.value(0)
    print("DEBUG:AMP_POWER_ON")
    utime.sleep_ms(150)  # Amplifier stabilization
    
    # FIX: Additional impedance matching delay
    utime.sleep_ms(100)  # Allow RF path to stabilize
    print("DEBUG:IMPEDANCE_STABLE")
    
    current_mode = 'TX'
    np[0] = (255, 0, 0)  # Red LED = TX
    np.write()
    
    print("DEBUG:TX_MODE_COMPLETE")
    return True

def set_rx_mode():
    """Switch to RX mode - BLOCKS TX"""
    global current_mode
    
    if current_mode == "RX":
        return True
    
    print("DEBUG:RX_MODE_START")
    
    # STEP 1: Put LoRa in standby FIRST
    try:
        lora_write_reg(0x01, 0x81)  # Standby mode
        utime.sleep_ms(10)
    except:
        pass
    
    # STEP 2: Power off amplifier (ACTIVE LOW - set to 1)
    RELAY_IN1.value(1)
    print("DEBUG:AMP_POWER_OFF")
    utime.sleep_ms(100)
    
    # STEP 3: Switch RF path to RX (bypass amplifier)
    SPDT_CTRL.value(1)  # RF1 (RX)
    SPDT_EN.value(0)    # Enable
    utime.sleep_ms(50)
    
    # STEP 4: Set LoRa to continuous RX mode
    try:
        lora_write_reg(0x01, 0x85)  # RX continuous
        utime.sleep_ms(10)
    except:
        pass
    
    current_mode = 'RX'
    np[0] = (0, 0, 0)  # LED off = RX
    np.write()
    
    print("DEBUG:RX_MODE_COMPLETE")
    return True

def set_all_off():
    """Safe shutdown - isolate all RF paths"""
    # Put LoRa in sleep mode
    try:
        lora_write_reg(0x01, 0x00)
    except:
        pass
    
    utime.sleep_ms(10)
    
    # Power off amplifier (ACTIVE LOW - set to 1)
    RELAY_IN1.value(1)
    utime.sleep_ms(50)
    
    # Disable RF switch (all paths isolated)
    SPDT_EN.value(1)  # Disable = all off
    utime.sleep_ms(10)
    
    np[0] = (0, 0, 0)
    np.write()

def lora_write_reg(addr, value):
    """Write to LoRa register"""
    lora_cs.value(0)
    spi.write(bytes([addr | 0x80, value]))
    lora_cs.value(1)

def lora_read_reg(addr):
    """Read from LoRa register"""
    lora_cs.value(0)
    spi.write(bytes([addr & 0x7F]))
    result = spi.read(1)
    lora_cs.value(1)
    return result[0]

def lora_init():
    """
    Initialize LoRa module
    CRITICAL FIX: Properly configure PA_BOOST for external amplifier
    """
    lora_write_reg(0x01, 0x00)  # Sleep
    utime.sleep_ms(10)
    
    lora_write_reg(0x01, 0x80)  # LoRa mode
    utime.sleep_ms(10)
    
    # 915 MHz frequency
    lora_write_reg(0x06, 0xE4)
    lora_write_reg(0x07, 0xC0)
    lora_write_reg(0x08, 0x00)
    
    # CRITICAL FIX: Configure PA for external amplifier
    # This was the root cause of TX_TIMEOUT!
    lora_write_reg(0x4D, 0x87)  # Enable high power +20dBm mode on PA_BOOST
    utime.sleep_ms(5)
    print("DEBUG:PADAC_CONFIGURED")
    
    # PA configuration for external amplifier
    # 0xFF = PA_BOOST selected, MaxPower=7, OutputPower=15 → +20dBm
    lora_write_reg(0x09, 0xFF)  # Full power with PA_BOOST
    lora_write_reg(0x0B, 0x3B)  # OCP 240mA (increased for high power)
    lora_write_reg(0x0C, 0x23)  # LNA max gain
    lora_write_reg(0x1E, 0x74)  # SF7, CRC on
    lora_write_reg(0x1D, 0x72)  # BW=125kHz, CR=4/5
    
    # Preamble
    lora_write_reg(0x20, 0x00)
    lora_write_reg(0x21, 0x08)
    
    lora_write_reg(0x39, 0x34)  # Sync word
    
    print("LORA_INIT_OK")

def lora_send_immediate(data):
    """
    Send data IMMEDIATELY via LoRa
    BLOCKING operation - waits for TX complete
    Returns: True if sent successfully
    
    CRITICAL FIX: Properly maintain LoRa mode bit when switching to TX
    """
    try:
        print("DEBUG:SEND_START")
        
        if isinstance(data, str):
            data = data.encode('utf-8')
        
        if len(data) > 255:
            print("ERROR:TX_TOO_LONG")
            return False
        
        # MUST be in TX mode
        if current_mode != 'TX':
            print("ERROR:NOT_IN_TX_MODE")
            return False
        
        print(f"DEBUG:DATA_LEN:{len(data)}")
        
        # CRITICAL FIX: First verify we're in LoRa mode
        current_reg = lora_read_reg(0x01)
        print(f"DEBUG:CURRENT_MODE_REG:0x{current_reg:02X}")
        
        # If not in LoRa mode (bit 7 = 0), re-enter LoRa mode
        if (current_reg & 0x80) == 0:
            print("DEBUG:RESTORING_LORA_MODE")
            lora_write_reg(0x01, 0x80)  # LoRa mode, sleep
            utime.sleep_ms(10)
            current_reg = lora_read_reg(0x01)
            print(f"DEBUG:AFTER_LORA_RESTORE:0x{current_reg:02X}")
        
        # Ensure standby mode (preserve LoRa mode bit)
        lora_write_reg(0x01, 0x81)  # LoRa + Standby
        utime.sleep_ms(10)
        print("DEBUG:STANDBY_SET")
        
        # Verify we're in LoRa Standby
        verify_reg = lora_read_reg(0x01)
        print(f"DEBUG:STANDBY_VERIFY:0x{verify_reg:02X}")
        if (verify_reg & 0x87) != 0x81:
            print(f"ERROR:STANDBY_FAILED:0x{verify_reg:02X}")
            # Try to recover
            lora_write_reg(0x01, 0x80)  # LoRa sleep
            utime.sleep_ms(20)
            lora_write_reg(0x01, 0x81)  # LoRa standby
            utime.sleep_ms(10)
            verify_reg = lora_read_reg(0x01)
            print(f"DEBUG:RECOVERY_ATTEMPT:0x{verify_reg:02X}")
        
        # Set FIFO pointers
        lora_write_reg(0x0D, 0x00)
        lora_write_reg(0x0E, 0x00)
        lora_write_reg(0x22, len(data))
        print("DEBUG:FIFO_CONFIG")
        
        # Write data to FIFO
        lora_cs.value(0)
        spi.write(bytes([0x80]))
        spi.write(data)
        lora_cs.value(1)
        print("DEBUG:DATA_WRITTEN")
        
        # Clear any previous flags
        lora_write_reg(0x12, 0xFF)
        utime.sleep_ms(5)
        
        # Extra settling time for amplifier and RF path
        utime.sleep_ms(30)
        print("DEBUG:AMP_READY")
        
        # Read current mode register to verify
        mode_reg = lora_read_reg(0x01)
        print(f"DEBUG:MODE_REG_BEFORE_TX:0x{mode_reg:02X}")
        
        # CRITICAL FIX: Ensure LoRa mode bit is preserved when entering TX
        # Write 0x83 = 0b10000011 = LoRa mode (bit 7) + TX mode (bits 2:0 = 011)
        lora_write_reg(0x01, 0x83)
        utime.sleep_ms(15)  # Increased delay for mode transition
        
        # Verify TX mode was set correctly
        mode_reg = lora_read_reg(0x01)
        print(f"DEBUG:MODE_REG_AFTER_TX:0x{mode_reg:02X}")
        
        # Check if we're in LoRa TX mode
        # Bits 2:0 should be 011 (TX) and bit 7 should be 1 (LoRa)
        if (mode_reg & 0x83) != 0x83:
            print(f"ERROR:MODE_NOT_TX:0x{mode_reg:02X}")
            print(f"ERROR:EXPECTED:0x83_GOT:0x{mode_reg:02X}")
            
            # CRITICAL: Try one more time with explicit steps
            print("DEBUG:RETRY_TX_MODE")
            lora_write_reg(0x01, 0x81)  # Back to standby
            utime.sleep_ms(20)
            lora_write_reg(0x01, 0x83)  # TX mode
            utime.sleep_ms(20)
            mode_reg = lora_read_reg(0x01)
            print(f"DEBUG:RETRY_RESULT:0x{mode_reg:02X}")
            
            if (mode_reg & 0x83) != 0x83:
                print(f"ERROR:TX_MODE_RETRY_FAILED:0x{mode_reg:02X}")
                return False
        
        print("DEBUG:TX_STARTED")
        
        # Wait for TX done (BLOCKING)
        timeout = 0
        last_flags = 0
        while timeout < 5000:  # 5 second max
            flags = lora_read_reg(0x12)
            
            # Report flag changes
            if flags != last_flags:
                print(f"DEBUG:FLAGS_CHANGED:0x{flags:02X}")
                last_flags = flags
            
            if flags & 0x08:  # TxDone
                print("DEBUG:TX_DONE_FLAG")
                break
            
            utime.sleep_ms(10)
            timeout += 10
            
            # Debug output every 500ms
            if timeout % 500 == 0:
                print(f"DEBUG:TX_WAIT:{timeout}ms:FLAGS:0x{flags:02X}")
        
        # Clear flags
        lora_write_reg(0x12, 0xFF)
        
        # Return to standby
        lora_write_reg(0x01, 0x81)
        
        if timeout >= 5000:
            print("ERROR:TX_TIMEOUT")
            # Diagnostic info
            final_flags = lora_read_reg(0x12)
            final_mode = lora_read_reg(0x01)
            final_padac = lora_read_reg(0x4D)
            final_paconfig = lora_read_reg(0x09)
            print(f"DEBUG:FINAL_FLAGS:0x{final_flags:02X}")
            print(f"DEBUG:FINAL_MODE:0x{final_mode:02X}")
            print(f"DEBUG:FINAL_PADAC:0x{final_padac:02X}")
            print(f"DEBUG:FINAL_PACONFIG:0x{final_paconfig:02X}")
            return False
        
        print(f"DEBUG:TX_COMPLETE:{timeout}ms")
        return True
        
    except Exception as e:
        print(f"ERROR:TX_EXCEPTION:{e}")
        return False

def lora_receive_check():
    """
    Check for received data (non-blocking)
    Returns: dict with data/rssi/snr or None
    """
    try:
        # MUST be in RX mode
        if current_mode != 'RX':
            return None
        
        # Ensure in continuous RX
        lora_write_reg(0x01, 0x85)
        
        # Check flags
        flags = lora_read_reg(0x12)
        
        if flags & 0x40:  # RxDone
            # Check CRC
            if flags & 0x20:
                lora_write_reg(0x12, 0xFF)
                print("DEBUG:RX_CRC_ERROR")
                return None
            
            # Read RSSI
            rssi_value = lora_read_reg(0x1A)
            rssi = -157 + rssi_value
            
            # Read SNR
            snr_value = lora_read_reg(0x19)
            if snr_value & 0x80:
                snr = -((~snr_value + 1) & 0xFF) / 4.0
            else:
                snr = snr_value / 4.0
            
            # Read packet
            length = lora_read_reg(0x13)
            fifo_addr = lora_read_reg(0x10)
            lora_write_reg(0x0D, fifo_addr)
            
            lora_cs.value(0)
            spi.write(bytes([0x00]))
            data = spi.read(length)
            lora_cs.value(1)
            
            # Clear flags
            lora_write_reg(0x12, 0xFF)
            
            return {
                'data': data,
                'rssi': rssi,
                'snr': snr
            }
        
        return None
        
    except Exception as e:
        print(f"ERROR:RX_EXCEPTION:{e}")
        return None

def process_command(cmd):
    """
    Process USB command IMMEDIATELY
    No queueing, no delays
    """
    cmd = cmd.strip()
    
    if not cmd:
        return
    
    print(f"DEBUG:CMD_RECEIVED:{cmd[:20]}")
    
    if cmd == 'TX':
        # Switch to TX mode
        if set_tx_mode():
            print("OK:TX")
        else:
            print("ERROR:TX_MODE_FAIL")
    
    elif cmd == 'RX':
        # Switch to RX mode
        if set_rx_mode():
            print("OK:RX")
        else:
            print("ERROR:RX_MODE_FAIL")
    
    elif cmd == 'ALLOFF':
        set_all_off()
        print("OK:ALLOFF")
    
    elif cmd.startswith('SEND:'):
        # IMMEDIATE send - no queueing
        message = cmd[5:]
        
        print(f"DEBUG:SEND_CMD_LEN:{len(message)}")
        
        # Must already be in TX mode
        if current_mode != 'TX':
            print("ERROR:NOT_IN_TX_MODE")
            return
        
        # Send immediately (BLOCKING)
        success = lora_send_immediate(message)
        
        if success:
            print("OK:SENT")
        else:
            print("ERROR:SEND_FAILED")
    
    elif cmd == 'STATUS':
        print(f"MODE:{current_mode}")
        print(f"RELAY:{RELAY_IN1.value()}")
        print(f"SPDT_EN:{SPDT_EN.value()}")
        print(f"SPDT_CTRL:{SPDT_CTRL.value()}")
        try:
            lora_mode = lora_read_reg(0x01)
            lora_flags = lora_read_reg(0x12)
            lora_padac = lora_read_reg(0x4D)
            lora_paconfig = lora_read_reg(0x09)
            print(f"LORA_MODE:0x{lora_mode:02X}")
            print(f"LORA_FLAGS:0x{lora_flags:02X}")
            print(f"LORA_PADAC:0x{lora_padac:02X}")
            print(f"LORA_PACONFIG:0x{lora_paconfig:02X}")
        except:
            print("LORA_READ_ERROR")
    
    elif cmd == 'RESET':
        machine.reset()
    
    else:
        print(f"ERROR:UNKNOWN_CMD:{cmd[:20]}")

def main_loop():
    """
    Main loop - HALF DUPLEX operation
    Either TX or RX, never both
    """
    usb_buffer = ""
    
    print("BRIDGE_READY")
    
    while True:
        try:
            # Check for USB commands (non-blocking)
            poll_result = poll_obj.poll(0)
            
            if poll_result:
                try:
                    char = sys.stdin.read(1)
                    
                    if char:
                        if char == '\n' or char == '\r':
                            if usb_buffer:
                                process_command(usb_buffer)
                                usb_buffer = ""
                        else:
                            usb_buffer += char
                            
                            if len(usb_buffer) > 1024:
                                print("ERROR:BUFFER_OVERFLOW")
                                usb_buffer = ""
                
                except Exception as e:
                    print(f"ERROR:STDIN:{e}")
                    usb_buffer = ""
            
            # Check for RX data (only if in RX mode)
            if current_mode == 'RX':
                rx_result = lora_receive_check()
                if rx_result:
                    try:
                        # Try to decode as UTF-8
                        try:
                            msg = rx_result['data'].decode('utf-8')
                        except:
                            # If decode fails, show as hex string
                            msg = ''.join(['%02x' % b for b in rx_result['data']])
                        
                        print(f"RX:{msg}|RSSI:{rx_result['rssi']}|SNR:{rx_result['snr']:.1f}")
                    except Exception as e:
                        print(f"ERROR:DECODE:{type(e).__name__}:{e}")
            
            # Small delay to prevent CPU spin
            utime.sleep_ms(5)
            
        except Exception as e:
            print(f"ERROR:LOOP:{e}")
            utime.sleep_ms(100)

# Main execution
try:
    # CRITICAL: Print startup messages clearly separated
    # This helps PC software synchronize properly
    print("\n" + "="*50)
    print("PICO LORA BRIDGE - STARTING")
    print("VERSION: TX_TIMEOUT_FIX_v1.1")
    print("="*50)
    
    init_hardware()
    lora_init()
    
    print("="*50)
    print("INIT_COMPLETE")
    print("="*50 + "\n")
    
    # Small delay before main loop to let messages flush
    utime.sleep_ms(100)
    
    main_loop()
    
except KeyboardInterrupt:
    print("SHUTDOWN")
    set_all_off()
    
except Exception as e:
    print(f"FATAL_ERROR:{e}")
    try:
        set_all_off()
    except:
        pass
