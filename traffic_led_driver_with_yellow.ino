/*
  Traffic Intersection LED Driver (Dashboard-Controlled, RED/GREEN input,
  auto-inserted YELLOW on Green->Red transition)
  --------------------------------------------------------------------------------
  Dashboard still only sends R/G per lane, same as before. Arduino now
  automatically flashes Yellow for each lane that goes from Green to Red,
  before switching it to Red.

  Expected Serial input format (one line per update):
      <North><East><South><West>\n
  Each character is one of:
      R = Red
      G = Green

  Example:
      "RGRR\n"   -> East Green, North/South/West Red
      "GRRR\n"   -> North Green, rest Red

  Wiring (3 LEDs per lane - Red, Yellow, Green):
    Lane      Red Pin   Yellow Pin   Green Pin
    North     2         10           3
    East      4         11           5
    South     6         12           7
    West      8         13           9

  For EACH LED:
    Arduino pin -> 220-330 ohm resistor -> LED long leg (anode)
    LED short leg (cathode) -> Arduino GND

  All GND wires can share the same GND rail on your breadboard, as long as
  that rail connects back to an actual GND pin on the Arduino.
*/

const uint8_t NUM_LANES = 4;
enum LaneIndex { NORTH = 0, EAST = 1, SOUTH = 2, WEST = 3 };
const char* laneNames[NUM_LANES] = {"NORTH", "EAST", "SOUTH", "WEST"};

const unsigned long YELLOW_DURATION_MS = 1000; // how long yellow stays on before switching to red

// Pin map: {RED, GREEN, YELLOW} per lane
const uint8_t lanePins[NUM_LANES][3] = {
  {2, 3, 10},  // North: Red, Green, Yellow
  {4, 5, 11},  // East
  {6, 7, 12},  // South
  {8, 9, 13}   // West
};

char currentState[NUM_LANES] = {'R', 'R', 'R', 'R'}; // tracks each lane's last applied state

String serialBuffer = "";

void setup() {
  Serial.begin(9600);

  for (uint8_t i = 0; i < NUM_LANES; i++) {
    pinMode(lanePins[i][0], OUTPUT); // Red
    pinMode(lanePins[i][1], OUTPUT); // Green
    pinMode(lanePins[i][2], OUTPUT); // Yellow
  }

  allRed();
  Serial.println("LED Driver Ready (Red/Green input, auto Yellow on G->R). Waiting for state string (e.g. RGRR)...");
}

void loop() {
  while (Serial.available() > 0) {
    char c = Serial.read();

    if (c == '\n') {
      applyStateString(serialBuffer);
      serialBuffer = "";
    } else if (c != '\r') {
      serialBuffer += c;
    }
  }
}

void applyStateString(String line) {
  line.trim();

  if (line.length() != NUM_LANES) {
    Serial.print("Ignored - expected ");
    Serial.print(NUM_LANES);
    Serial.print(" characters, got: ");
    Serial.println(line);
    return;
  }

  for (uint8_t i = 0; i < NUM_LANES; i++) {
    char newState = toupper(line.charAt(i));

    if (newState != 'R' && newState != 'G') {
      Serial.print("Unknown state '");
      Serial.print(newState);
      Serial.print("' for lane ");
      Serial.print(laneNames[i]);
      Serial.println(" - defaulting to Red");
      newState = 'R';
    }

    // If this lane is going from Green to Red, flash Yellow first
    if (currentState[i] == 'G' && newState == 'R') {
      writeLane(i, 'Y');
      Serial.print(laneNames[i]);
      Serial.println(": Green -> Yellow");
      delay(YELLOW_DURATION_MS); // blocking - fine since only a 1s pause per transition
    }

    writeLane(i, newState);
    currentState[i] = newState;
  }

  Serial.print("Applied: ");
  Serial.println(line);
}

void writeLane(uint8_t lane, char state) {
  digitalWrite(lanePins[lane][0], state == 'R'); // Red
  digitalWrite(lanePins[lane][1], state == 'G'); // Green
  digitalWrite(lanePins[lane][2], state == 'Y'); // Yellow
}

void allRed() {
  for (uint8_t i = 0; i < NUM_LANES; i++) {
    writeLane(i, 'R');
    currentState[i] = 'R';
  }
}
