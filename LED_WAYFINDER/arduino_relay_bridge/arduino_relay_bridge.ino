/*
 * Arduino Nano - 4 Channel Relay USB Bridge
 * Controls 4 relays (Red, Green, Yellow, Blue lights) via USB serial commands
 */

// Define relay pins
const int RED_RELAY = 2;     // IN3 - Red Light
const int GREEN_RELAY = 3;   // IN2 - Green Light
const int YELLOW_RELAY = 4;  // IN4 - Yellow Light
const int BLUE_RELAY = 5;    // IN1 - Blue Light

void setup() {
  // Initialize serial communication
  Serial.begin(9600);
  
  // Set relay pins as outputs
  pinMode(RED_RELAY, OUTPUT);
  pinMode(GREEN_RELAY, OUTPUT);
  pinMode(YELLOW_RELAY, OUTPUT);
  pinMode(BLUE_RELAY, OUTPUT);
  
  // Initialize all relays to OFF (HIGH for active-low relays)
  // Note: Most relay modules are active-LOW (LOW = ON, HIGH = OFF)
  digitalWrite(RED_RELAY, HIGH);
  digitalWrite(GREEN_RELAY, HIGH);
  digitalWrite(YELLOW_RELAY, HIGH);
  digitalWrite(BLUE_RELAY, HIGH);
  
  // Send ready message
  Serial.println("Arduino Relay Bridge Ready");
}

void loop() {
  // Check if data is available
  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');
    command.trim(); // Remove whitespace
    
    processCommand(command);
  }
}

void processCommand(String cmd) {
  cmd.toUpperCase(); // Convert to uppercase for easier parsing
  
  // Command format: "R1" = Red ON, "R0" = Red OFF
  // G1/G0 = Green, Y1/Y0 = Yellow, B1/B0 = Blue
  // "ALL1" = All ON, "ALL0" = All OFF
  
  if (cmd == "R1") {
    digitalWrite(RED_RELAY, LOW);
    Serial.println("Red: ON");
  }
  else if (cmd == "R0") {
    digitalWrite(RED_RELAY, HIGH);
    Serial.println("Red: OFF");
  }
  else if (cmd == "G1") {
    digitalWrite(GREEN_RELAY, LOW);
    Serial.println("Green: ON");
  }
  else if (cmd == "G0") {
    digitalWrite(GREEN_RELAY, HIGH);
    Serial.println("Green: OFF");
  }
  else if (cmd == "Y1") {
    digitalWrite(YELLOW_RELAY, LOW);
    Serial.println("Yellow: ON");
  }
  else if (cmd == "Y0") {
    digitalWrite(YELLOW_RELAY, HIGH);
    Serial.println("Yellow: OFF");
  }
  else if (cmd == "B1") {
    digitalWrite(BLUE_RELAY, LOW);
    Serial.println("Blue: ON");
  }
  else if (cmd == "B0") {
    digitalWrite(BLUE_RELAY, HIGH);
    Serial.println("Blue: OFF");
  }
  else if (cmd == "ALL1") {
    digitalWrite(RED_RELAY, LOW);
    digitalWrite(GREEN_RELAY, LOW);
    digitalWrite(YELLOW_RELAY, LOW);
    digitalWrite(BLUE_RELAY, LOW);
    Serial.println("All: ON");
  }
  else if (cmd == "ALL0") {
    digitalWrite(RED_RELAY, HIGH);
    digitalWrite(GREEN_RELAY, HIGH);
    digitalWrite(YELLOW_RELAY, HIGH);
    digitalWrite(BLUE_RELAY, HIGH);
    Serial.println("All: OFF");
  }
  else if (cmd == "STATUS") {
    Serial.print("Red:");
    Serial.print(digitalRead(RED_RELAY) == LOW ? "ON" : "OFF");
    Serial.print(" Green:");
    Serial.print(digitalRead(GREEN_RELAY) == LOW ? "ON" : "OFF");
    Serial.print(" Yellow:");
    Serial.print(digitalRead(YELLOW_RELAY) == LOW ? "ON" : "OFF");
    Serial.print(" Blue:");
    Serial.println(digitalRead(BLUE_RELAY) == LOW ? "ON" : "OFF");
  }
  else {
    Serial.println("ERROR: Unknown command");
  }
}