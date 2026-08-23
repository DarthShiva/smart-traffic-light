/*
  Traffic Intersection LED Driver (Dashboard-Controlled, RED/GREEN input,
  auto-inserted YELLOW on Green->Red transition, rolling-counter anti-replay)
  --------------------------------------------------------------------------------
  Dashboard still only sends R/G per lane, same as before, but every line
  now carries a rolling counter so a captured-and-replayed line (or any
  line not produced by serial_link.py) is rejected instead of silently
  re-applied. Arduino automatically flashes Yellow for each lane that goes
  from Green to Red, before switching it to Red.

  Expected Serial input format (one line per update):
      <North><East><South><West>:<counter>\n
  Each state character is one of:
      R = Red
      G = Green
  <counter> is a non-negative integer that MUST be strictly greater than
  the counter of the last line this firmware accepted. Lines with an
  equal or smaller counter are rejected as replays/forgeries and ignored.
  The counter resets (to -1, so the very next line is accepted regardless
  of its value) whenever the board itself resets/reboots.

  Example:
      "RGRR:101\n"   -> East Green, North/South/West Red, counter 101
      "GRRR:102\n"   -> North Green, rest Red, counter 102

  Wiring (3 LEDs per lane - Red, Yellow, Green):
    Lane      Red Pin   Yellow Pin   Green Pin
    North     2         10           3
    East      4         11           5
    South     6         12           7
    West      8         13           9

  For EACH LED:
    Arduino pin ->  resistor -> LED long leg (anode)
    LED short leg (cathode) -> Arduino GND

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

// Rolling anti-replay counter: only a line whose counter is strictly
// greater than this is applied. Starts at -1 so the first line received
// after boot (any non-negative counter) is always accepted.
long lastCounter = -1;

String serialBuffer = "";

void setup() {
  Serial.begin(9600);

  for (uint8_t i = 0; i < NUM_LANES; i++) {
    pinMode(lanePins[i][0], OUTPUT); // Red
    pinMode(lanePins[i][1], OUTPUT); // Green
    pinMode(lanePins[i][2], OUTPUT); // Yellow
  }

  allRed();
  Serial.println("LED Driver Ready (Red/Green input, rolling-counter anti-replay, auto Yellow on G->R). Waiting for <state>:<counter>...");
}

void loop() {
  while (Serial.available() > 0) {
    char c = Serial.read();

    if (c == '\n') {
      processLine(serialBuffer);
      serialBuffer = "";
    } else if (c != '\r') {
      serialBuffer += c;
    }
  }
}

// Parses "<state>:<counter>", enforces the rolling-counter anti-replay
// check, and only then hands the state characters to applyStateString.
void processLine(String line) {
  line.trim();

  int colonIdx = line.lastIndexOf(':');
  if (colonIdx < 0) {
    Serial.print("Ignored - missing ':<counter>' in: ");
    Serial.println(line);
    return;
  }

  String stateStr = line.substring(0, colonIdx);
  String counterStr = line.substring(colonIdx + 1);
  stateStr.trim();
  counterStr.trim();

  if (counterStr.length() == 0) {
    Serial.println("Ignored - empty counter");
    return;
  }
  // Reject anything that isn't a plain non-negative integer, so junk like
  // "RRRR:abc" (which toInt() would silently read as 0) is not mistaken
  // for a low-but-valid counter.
  for (unsigned int i = 0; i < counterStr.length(); i++) {
    if (!isDigit(counterStr.charAt(i))) {
      Serial.print("Ignored - non-numeric counter: ");
      Serial.println(counterStr);
      return;
    }
  }

  long receivedCounter = counterStr.toInt();
  if (receivedCounter <= lastCounter) {
    Serial.print("REJECTED - replay or stale counter (");
    Serial.print(receivedCounter);
    Serial.print(" <= ");
    Serial.print(lastCounter);
    Serial.println(")");
    return;
  }

  if (stateStr.length() != NUM_LANES) {
    // Do NOT advance lastCounter on a malformed state - a genuinely
    // malformed-but-fresh line should not let a later replay of an
    // in-between counter value slip through.
    Serial.print("Ignored - expected ");
    Serial.print(NUM_LANES);
    Serial.print(" state characters, got: ");
    Serial.println(stateStr);
    return;
  }

  lastCounter = receivedCounter;
  applyStateString(stateStr);
}

void applyStateString(String line) {
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
