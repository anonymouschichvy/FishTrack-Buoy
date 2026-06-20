/*
 * GPS UART to USB Bridge for Arduino Nano
 * Reads NMEA data from NEO-M8N and forwards to USB serial
 * 
 * Connections:
 * - GPS TX -> Arduino Pin D4 (RX)
 * - GPS RX -> Arduino Pin D5 (TX)
 * - GPS GND -> Arduino GND
 * - GPS VCC -> Arduino 3.3V or 5V (check your module)
 * - LED indicator on Pin 13 (built-in LED)
 * 
 * Note: USB communication uses hardware serial (D0/RXD and D1/TXD)
 *       These are automatically used by Serial.begin()
 */

#include <SoftwareSerial.h>

// GPS UART Configuration
// Using D4 and D5 to avoid conflicts with hardware serial and built-in LEDs
const int GPS_RX_PIN = 4;  // Arduino Pin D4 <- GPS TX
const int GPS_TX_PIN = 5;  // Arduino Pin D5 -> GPS RX
const long GPS_BAUD_RATE = 9600;

// USB Serial (Hardware Serial uses D0/RXD and D1/TXD)
const long USB_BAUD_RATE = 9600;

// LED Pin (built-in LED on Nano)
// Pin 13 has built-in LED, but also TX LED uses D1
const int LED_PIN = 13;

// Create software serial for GPS
SoftwareSerial gpsSerial(GPS_RX_PIN, GPS_TX_PIN);

// Buffer for incoming data
const int BUFFER_SIZE = 256;
char buffer[BUFFER_SIZE];
int bufferIndex = 0;

// LED blink variables
bool ledState = false;
unsigned long lastBlink = 0;
const unsigned long BLINK_INTERVAL = 100; // milliseconds

void setup() {
  // Initialize USB Serial
  Serial.begin(USB_BAUD_RATE);
  
  // Initialize GPS Serial
  gpsSerial.begin(GPS_BAUD_RATE);
  
  // Initialize LED
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);
  
  // Startup message
  Serial.println("GPS Bridge Started");
  Serial.println("Waiting for GPS data...");
  
  // Small delay for stability
  delay(100);
}

void loop() {
  // Check if data available from GPS
  while (gpsSerial.available()) {
    char c = gpsSerial.read();
    
    // Forward to USB Serial immediately
    Serial.write(c);
    
    // Blink LED to show activity
    unsigned long currentTime = millis();
    if (currentTime - lastBlink >= BLINK_INTERVAL) {
      ledState = !ledState;
      digitalWrite(LED_PIN, ledState);
      lastBlink = currentTime;
    }
  }
  
  // Optional: Forward data from USB to GPS (for configuration commands)
  // Uncomment if you need bidirectional communication
  /*
  while (Serial.available()) {
    char c = Serial.read();
    gpsSerial.write(c);
  }
  */
  
  // Small delay to prevent CPU hogging
  delay(10);
}