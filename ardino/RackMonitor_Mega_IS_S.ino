/*
  Rack Monitor Motion Controller - Arduino Mega 2560

  Axes:
    X  = horizontal (1 motor, DM556)
    Y  = vertical (2 motors: Y1/Y2, DM556) with two TOP limit switches (auto-squaring)
    C  = camera rail (100 mm travel, DM556)

  Homing sequence (button or G28 with no axes):
    1) Home C to retract limit (C_MIN)
    2) Home Y using top Y1_TOP & Y2_TOP (dual, auto-squaring)
    3) Home X to X_MIN

  G-code subset:
    G90/G91                      absolute/relative
    G0/G1 X.. Y.. C.. F..        linear moves with accel
    G28 [X][Y][C]                home specific axes; none = full sequence C->Y->X
    M114                         report position
    M17 / M18 (M84)              enable/disable drivers
    M700 R<row> C<col>           move to rack cell (0..11, 0..6)
    M710 / M711                  camera in / out
    !                            emergency stop (disable)

  Operator button:
    HOME button (short press >= 50 ms) queues full home sequence; runs when idle.
    No double-press. Long-press E-stop is disabled by default.

  Wiring (recommendation for DM556 STEP/DIR/ENA):
    PUL+ / DIR+ / ENA+ -> +5V
    PUL- / DIR- / ENA- -> Arduino STEP/DIR/EN pins
    Common GND between logic sides.

  Limit switches:
    Use NO->GND to Arduino input with INPUT_PULLUP (LOW = triggered).




Commands
Always start with G28.  This is the reference edge (left side).  Confirm by typing M114.


G90    ; Set Absolute positioning mode (moves go to exact coordinates); Do once after power-up
       ; Example:
       ; G90
       ; G1 X200 Y100
       ; → Moves to exactly X=200mm, Y=100mm

G91    ; Set Relative positioning mode (moves are offsets)
       ; Example:
       ; G91
       ; G1 X10
       ; → Moves +10mm from current X position

G0     ; Rapid move using maximum allowed axis speeds; Rapid move command (move as fast as allowed)
       ; Example:
       ; G0 X100 Y50
       ; → Fast move to X=100mm, Y=50mm

G00    ; Same as G0 (alias for rapid move)
       ; Example:
       ; G00 C40
       ; → Fast move camera to 40mm

G1     ; Controlled linear move using feedrate F (mm/min)
       ; Example:
       ; G1 X100 Y50 C20 FX200 FY150 FC30 //G1 X150 Y80 F1200
       ; → Move at 1200 mm/min

G01    ; Same as G1 (alias for controlled move)
       ; Example:
       ; G01 C20 F600
       ; → Move camera at 600 mm/min

G28    ; Home axes (no parameters = full sequence C → Y → X); Home the machine, this identifies X=0.
       ; Examples:
       ; G28        → Home all axes
       ; G28 X      → Home X only
       ; G28 Y      → Home Y only (dual-motor auto-square)
       ; G28 C      → Home camera axis

M114   ; Report current position (X, Y, C in mm) + homed status; This is to confirm home location.
       ; Example:
       ; M114
       ; → M114 X:120.000 Y:90.000 C:0.000 | homed: X=Y Y=Y C=Y

M17    ; Enable all motor drivers
       ; Example:
       ; M17
       ; → Motors energized

M18    ; Disable all motor drivers
       ; Example:
       ; M18
       ; → Motors released

M84    ; Disable all motor drivers (same as M18 but a delay can be added).  If there's no delay added, then it is exactly the same as M18
       ; Example:
       ; M84 S30

M700   ; Move to rack grid position using R<row> C<col>
       ; Valid range: R0–11, C0–6
       ; Example:
       ; M700 R3 C2
       ; → Move to row 3, column 2

M710   ; Move camera to IN preset position (80mm)
       ; Example:
       ; M710

M711   ; Move camera to OUT preset position (0mm)
       ; Example:
       ; M711

!      ; Emergency stop (immediate stop + disable motors)
       ; Example:
       ; !
       ; → Stops motion immediately

M701   ; Enter number of rows and number of columns
       ; Example:
       ; M701 R10 C5

M702   ; Enter X and Y pitch values
       ; Example:
       ; M702 X410 Y36
       ; M702 X410
       ; M702 Y36

M703   ; Enter x and y offset values
       ; Example:
       ; M703 X90 Y5
       ; M703 X410
       ; M703 Y36

M704   ; Enter camera In and Out position
       ; Example:
       ; M704 I80 O0
       ; M707 I75
       ; M707 O5

M705   ; Query command for number of rows and columns 

M706   ; Query command for X and Y pitch values

M707   ; Query command for X and Y offset values

M708   ; Query command for camera In and Out positions

M709   ; Query command for M701 to M704 values

M500   ; Save to EEPROM

M501   ; LOAD (or auto-load on startup)
*/


#include <AccelStepper.h>      // AccelStepper library for non-blocking stepper control
#include <math.h>              // Math utilities (fabsf, lroundf)
#include <EEPROM.h>


struct Settings {
  int maxRows;
  int maxCols;

  float pitchX;
  float pitchY;

  float offsetX;
  float offsetY;

  float camIn;
  float camOut;

  uint16_t magic;   // used to validate EEPROM
};
const int EEPROM_ADDR = 0;
const uint16_t EEPROM_MAGIC = 0xBEEF;





// ---------------- PIN MAP ----------------

// X axis (horizontal)
#define X_STEP_PIN      22      // Step pin for X axis driver
#define X_DIR_PIN       23      // Direction pin for X axis driver
#define X_EN_PIN        24      // Enable pin for X axis driver
#define X_MIN_PIN       40      // X-axis minimum (home) limit switch pin
#define X_MAX_PIN       -1      // X-axis maximum limit switch (not installed)

// Y axis - dual motors with dual TOP limit switches at home
#define Y1_STEP_PIN     26      // Step pin for Y motor 1
#define Y1_DIR_PIN      27      // Direction pin for Y motor 1
#define Y1_EN_PIN       28      // Enable pin for Y motor 1
#define Y1_TOP_PIN      30      // Top limit switch for Y motor 1 (active LOW)

#define Y2_STEP_PIN     29      // Step pin for Y motor 2
#define Y2_DIR_PIN      33      // Direction pin for Y motor 2
#define Y2_EN_PIN       34      // Enable pin for Y motor 2
#define Y2_TOP_PIN      31      // Top limit switch for Y motor 2 (active LOW)

// Camera C axis (retract home)
#define C_STEP_PIN      35      // Step pin for camera axis
#define C_DIR_PIN       36      // Direction pin for camera axis
#define C_EN_PIN        37      // Enable pin for camera axis
#define C_MIN_PIN       41      // Camera retract (home) limit switch
#define C_MAX_PIN       -1      // Camera max switch (not installed)

// Operator button + LED
#define HOME_BTN_PIN    38      // Manual home button (normally open to GND)
#define STATUS_LED_PIN  39      // Status LED output pin

// Enable logic (DM556 typically active LOW)Y1_TOP_PINvois
const bool ENABLE_ACTIVE_LOW = true;   // TRUE if enable pin is active LOW

// Limit logic (NO switches to GND with INPUT_PULLUP)
const bool LIMIT_ACTIVE_LOW = true;    // TRUE if LOW means switch triggered






















// ---------------- CALIBRATION ----------------

// Motor steps per revolution (1.8° motors)
const float MOTOR_STEPS_PER_REV = 200.0f;

// Microstepping settings (must match driver DIP switches); 36teeth/2.25in=16; number of teeth (36) divide by pinion diameter (2.25in)
const float MICROSTEPS_X = 16.0f;      // X-axis microsteps (float)
const float MICROSTEPS_Y = 16.0f;      // Y-axis microsteps (float)
const float MICROSTEPS_C = 16.0f;      // Camera axis microsteps (float)

// Rack & pinion parameters (36 tooth gear, 16 DP rack); 
// the DP stands for Diametral Pitch. It’s a standard way to describe the size of gear teeth in imperial units.
const float DP_X = 16.0f;              // Diametral pitch
const float TEETH_X = 36.0f;           // Number of teeth on pinion

// Linear travel per motor revolution (converted to mm)
const float TRAVEL_PER_REV_X_MM =
  (TEETH_X * (3.1415926535f / DP_X)) * 25.4f;   //25.4mm = 1 inch

// Y-axis uses same rack & pinion geometry
const float DP_Y = 16.0f;
const float TEETH_Y = 36.0f;
const float TRAVEL_PER_REV_Y_MM =
  (TEETH_Y * (3.1415926535f / DP_Y)) * 25.4f/20;   //(TEETH_Y * (3.1415926535f / DP_Y)) * 25.4f;

// Camera axis leadscrew (T6x1 = 1 mm per revolution)
const float LEAD_C_MM = 1.0f;

// Calculated steps per millimeter for each axis
float stepsPerMM_X = 0.0f;
float stepsPerMM_Y = 0.0f;
float stepsPerMM_C = 0.0f;

// Convert motor + microstepping + mechanics into steps/mm
static inline float calcStepsPerMM(float micro, float travel_per_rev_mm) {
  return (MOTOR_STEPS_PER_REV * micro) / travel_per_rev_mm;
}

// Calculate steps/mm for all axes
void calcStepsPerMM_All() {
  stepsPerMM_X = calcStepsPerMM(MICROSTEPS_X, TRAVEL_PER_REV_X_MM);
  stepsPerMM_Y = calcStepsPerMM(MICROSTEPS_Y, TRAVEL_PER_REV_Y_MM);
  stepsPerMM_C = calcStepsPerMM(MICROSTEPS_C, LEAD_C_MM);
}




//Motion limits, homing config, grid, and state
// ---------------- Motion limits (conservative starters) ----------------

// Maximum linear speed for X axis in mm/s
const float MAX_SPEED_X_MM_S = 400.0f;    //const float MAX_SPEED_X_MM_S = 150.0f;

// Maximum linear speed for Y axis in mm/s
const float MAX_SPEED_Y_MM_S = 5000.0f;    //const float MAX_SPEED_Y_MM_S = 100.0f;

// Maximum linear speed for camera axis in mm/s
const float MAX_SPEED_C_MM_S = 40.0f;

// Acceleration for X axis in mm/s²
const float ACCEL_X_MM_S2    = 1500.0f;    //const float ACCEL_X_MM_S2    = 400.0f;

// Acceleration for Y axis in mm/s²
const float ACCEL_Y_MM_S2    = 2500.0f;    //const float ACCEL_Y_MM_S2    = 300.0f;

// Acceleration for camera axis in mm/s²
const float ACCEL_C_MM_S2    = 200.0f;

// Homing directions: -1 means move toward MIN switch
const int   HOMING_DIR_X = -1; // Move left toward X_MIN
const int   HOMING_DIR_Y = -1; // Move upward toward Y_TOP switches
const int   HOMING_DIR_C = -1; // Retract camera toward C_MIN

// Homing speeds, backoff distances, and slow approach speeds
const float HOMING_SPEED_X_MM_S = 50.0f;   // Fast homing speed for X
const float HOMING_BACKOFF_X_MM = 0.0f;    // Back away distance after hit
const float HOMING_SLOW_X_MM_S  = 10.0f;   // Slow re-approach speed

const float HOMING_SPEED_Y_MM_S = 40.0f;    //const float HOMING_SPEED_Y_MM_S = 40.0f;
const float HOMING_BACKOFF_Y_MM = 0.0f;     //onst float HOMING_BACKOFF_Y_MM = 5.0f;
const float HOMING_SLOW_Y_MM_S  = 10.0f;    //const float HOMING_SLOW_Y_MM_S  = 10.0f;

const float HOMING_SPEED_C_MM_S = 20.0f;
const float HOMING_BACKOFF_C_MM = 0.0f;
const float HOMING_SLOW_C_MM_S  = 5.0f;

// ---------------- Grid ----------------

// Maximum grid dimensions (variable rows x columns)
int MAX_ROWS = 12;
int MAX_COLS = 7;

// Spacing between grid positions
float PITCH_X_MM   = 402.0f;   // Horizontal spacing
float PITCH_Y_MM   = 38.0f;    // Vertical spacing

// Offset from home position to first grid cell
float X0_OFFSET_MM = 185.0f;   //float X0_OFFSET_MM = 20.0f;
float Y0_OFFSET_MM = 5.0f;   //float Y0_OFFSET_MM = 20.0f;

// Camera preset positions
float C_IN_MM      = 10.0f;    // Camera extended position in cm and not in mm   //float C_IN_MM      = 80.0f;    // Camera extended position
float C_OUT_MM     = 0.0f;     // Camera fully retracted




void saveSettings() {

  Settings s;

  s.maxRows = MAX_ROWS;
  s.maxCols = MAX_COLS;

  s.pitchX = PITCH_X_MM;
  s.pitchY = PITCH_Y_MM;

  s.offsetX = X0_OFFSET_MM;
  s.offsetY = Y0_OFFSET_MM;

  s.camIn = C_IN_MM;
  s.camOut = C_OUT_MM;

  s.magic = EEPROM_MAGIC;

  EEPROM.put(EEPROM_ADDR, s);

  Serial.println("Settings saved (M500)");
}

void loadSettings() {

  Settings s;
  EEPROM.get(EEPROM_ADDR, s);

  if (s.magic != EEPROM_MAGIC) {
    Serial.println("No valid EEPROM data (using defaults)");
    return;
  }

  MAX_ROWS = s.maxRows;
  MAX_COLS = s.maxCols;

  PITCH_X_MM = s.pitchX;
  PITCH_Y_MM = s.pitchY;

  X0_OFFSET_MM = s.offsetX;
  Y0_OFFSET_MM = s.offsetY;

  C_IN_MM = s.camIn;
  C_OUT_MM = s.camOut;

  Serial.println("Settings loaded (M501)");
}


void resetSettings() {

  MAX_ROWS = 12;
  MAX_COLS = 7;

  PITCH_X_MM = 410.0f;
  PITCH_Y_MM = 36.0f;

  X0_OFFSET_MM = 90.0f;
  Y0_OFFSET_MM = 5.0f;

  C_IN_MM = 4.0f;
  C_OUT_MM = 0.0f;

  Serial.println("Settings reset to defaults (M502)");
}





// ---------------- State & Objects ----------------

// AccelStepper instance for X axis
AccelStepper stepperX(AccelStepper::DRIVER, X_STEP_PIN, X_DIR_PIN);

// AccelStepper instance for first Y motor
AccelStepper stepperY1(AccelStepper::DRIVER, Y1_STEP_PIN, Y1_DIR_PIN);

// AccelStepper instance for second Y motor
AccelStepper stepperY2(AccelStepper::DRIVER, Y2_STEP_PIN, Y2_DIR_PIN);

// AccelStepper instance for camera axis
AccelStepper stepperC(AccelStepper::DRIVER, C_STEP_PIN, C_DIR_PIN);

// Homing status flags
bool yHomed = false;   // TRUE if Y axis has been homed
bool xHomed = false;   // TRUE if X axis has been homed
bool cHomed = false;   // TRUE if camera axis has been homed

// Flag set by interrupt-style button logic
volatile bool homeBtnPressedFlag = false;

// TRUE while machine is executing motion
bool machineBusy = false;

// Motion mode: absolute (G90) or relative (G91)
bool  absoluteMode = true;

// Feedrate in mm/min for G1 moves


//float feedrate_mm_per_min = 6000;

float feedrate_X_mm_per_min = 6000;
float feedrate_Y_mm_per_min = 6000;
float feedrate_C_mm_per_min = 6000;



// Serial input line buffer
String serialBuf;


//Utilities: enable pins, LEDs, limits
// Enable or disable a stepper driver
void setEnablePin(int enPin, bool enable) {
  if (enPin < 0) return;                // Skip if pin not defined
  pinMode(enPin, OUTPUT);               // Configure pin as output
  int level =
    ENABLE_ACTIVE_LOW
      ? (enable ? LOW : HIGH)           // Active-low logic
      : (enable ? HIGH : LOW);          // Active-high logic
  digitalWrite(enPin, level);            // Apply enable state
}

// Enable or disable all motor drivers
void setAllEnabled(bool enable) {
  setEnablePin(X_EN_PIN, enable);        // X motor
  setEnablePin(Y1_EN_PIN, enable);       // Y motor 1
  setEnablePin(Y2_EN_PIN, enable);       // Y motor 2
  setEnablePin(C_EN_PIN, enable);        // Camera motor
}

// Turn status LED on or off
void setStatusLED(bool on) {
  if (STATUS_LED_PIN >= 0)
    digitalWrite(STATUS_LED_PIN, on ? HIGH : LOW);
}

// Update busy state and LED together
void setMachineBusy(bool busy) {
  machineBusy = busy;                   // Store busy flag
  setStatusLED(busy);                   // Reflect on LED
}

// Read and interpret a limit switch
bool isLimitTriggered(int pin) {
  if (pin < 0) return false;             // Ignore missing switches
  int v = digitalRead(pin);              // Read pin state
  return LIMIT_ACTIVE_LOW
           ? (v == LOW)                  // Active LOW logic
           : (v == HIGH);                // Active HIGH logic
}



//Stepper configuration, status, and E-stop
// Configure all GPIOs and stepper motion parameters
void configureSteppers() {

  // Configure Y-axis top limit switches as inputs with pullups
  if (Y1_TOP_PIN >= 0) pinMode(Y1_TOP_PIN, INPUT_PULLUP);
  if (Y2_TOP_PIN >= 0) pinMode(Y2_TOP_PIN, INPUT_PULLUP);

  // Configure X-axis limit switches
  if (X_MIN_PIN >= 0)  pinMode(X_MIN_PIN, INPUT_PULLUP);
  if (X_MAX_PIN >= 0)  pinMode(X_MAX_PIN, INPUT_PULLUP);

  // Configure camera axis limit switches
  if (C_MIN_PIN >= 0)  pinMode(C_MIN_PIN, INPUT_PULLUP);
  if (C_MAX_PIN >= 0)  pinMode(C_MAX_PIN, INPUT_PULLUP);

  // Configure manual home button input
  pinMode(HOME_BTN_PIN, INPUT_PULLUP);

  // Configure status LED output and ensure it's off
  if (STATUS_LED_PIN >= 0) {
    pinMode(STATUS_LED_PIN, OUTPUT);
    digitalWrite(STATUS_LED_PIN, LOW);
  }

  // Set X-axis max speed and acceleration (converted to steps/sec)
  stepperX.setMaxSpeed(MAX_SPEED_X_MM_S * stepsPerMM_X);
  stepperX.setAcceleration(ACCEL_X_MM_S2 * stepsPerMM_X);

  // Set Y-axis motors max speed
  stepperY1.setMaxSpeed(MAX_SPEED_Y_MM_S * stepsPerMM_Y);
  stepperY2.setMaxSpeed(MAX_SPEED_Y_MM_S * stepsPerMM_Y);     //stepperY1;

  // Set Y-axis motors acceleration
  stepperY1.setAcceleration(ACCEL_Y_MM_S2 * stepsPerMM_Y);
  stepperY2.setAcceleration(ACCEL_Y_MM_S2 * stepsPerMM_Y);

  // Set camera axis max speed and acceleration
  stepperC.setMaxSpeed(MAX_SPEED_C_MM_S * stepsPerMM_C);
  stepperC.setAcceleration(ACCEL_C_MM_S2 * stepsPerMM_C);

  // Enable all motor drivers
  setAllEnabled(true);
}










bool checkEmergencyStop() {

  while (Serial.available()) {

    char c = Serial.read();

    if (c == '!') {

      Serial.println("EMERGENCY STOP!");

      // Instant motor shutdown
      stepperY1.disableOutputs();
      stepperY2.disableOutputs();

      setAllEnabled(false);

      setMachineBusy(false);

      return true;
    }
  }

  return false;
}











//Position reporting and emergency stop
// Report current machine position in millimeters
void reportPosition() {

  // Convert X steps to millimeters
  float x_mm = stepperX.currentPosition() / stepsPerMM_X;

  // Average both Y motors and convert to millimeters
  float y_mm =
    ((stepperY1.currentPosition() + stepperY2.currentPosition()) * 0.5f)
    / stepsPerMM_Y;

  // Convert camera axis steps to millimeters
  float c_mm = stepperC.currentPosition() / stepsPerMM_C;

  // Print positions
  Serial.print("M114 X:");
  Serial.print(x_mm, 3);

  Serial.print(" Y:");
  Serial.print(y_mm, 3);

  Serial.print(" C:");
  Serial.print(c_mm, 3);

  // Print homing status flags
  Serial.print(" | homed: X=");
  Serial.print(xHomed ? "Y" : "N");

  Serial.print(" Y=");
  Serial.print(yHomed ? "Y" : "N");

  Serial.print(" C=");
  Serial.print(cHomed ? "Y" : "N");

  Serial.println();
}

// Immediate emergency stop
void eStop() {

  // Stop all motion immediately (decelerated)
  stepperX.stop();
  stepperY1.stop();
  stepperY2.stop();
  stepperC.stop();

  // Disable all motor drivers
  setAllEnabled(false);

  // Clear busy flag and LED
  setMachineBusy(false);

  // Notify host
  Serial.println("!! EMERGENCY STOP TRIGGERED");
}



//Generic homing helper (single axis)
// Home a single axis toward a minimum limit switch
void homeAxisToMin(
  AccelStepper &st,            // Stepper object to home
  int limitPin,                // Limit switch pin
  int dirSign,                 // Direction sign (+1 or -1)
  float stepsPerMM,            // Axis resolution
  float fast_mm_s,             // Fast homing speed
  float backoff_mm,            // Backoff distance
  float slow_mm_s              // Slow re-approach speed
) {

  // Abort if limit switch is missing
  if (limitPin < 0) {
    Serial.println("Home error: missing limit pin");
    return;
  }

  // Configure fast approach speed
  st.setMaxSpeed(fabsf(fast_mm_s * stepsPerMM));

  // Use X-axis acceleration scaling (safe for X and C)
  st.setAcceleration(ACCEL_X_MM_S2 * stepsPerMM);

  // Move far in homing direction
  long farTarget = st.currentPosition() + dirSign * 1000000L;
  st.moveTo(farTarget);

  // Run until limit switch is triggered
  while (!isLimitTriggered(limitPin))
    st.run();

  // Stop motion cleanly
  st.stop();
  while (st.isRunning())
    st.run();

  // Convert backoff distance to steps
  long backSteps = (long)lroundf(backoff_mm * stepsPerMM);

  // Move away from switch
  st.moveTo(st.currentPosition() - dirSign * backSteps);
  while (st.distanceToGo() != 0)
    st.run();

  // Set slow re-approach speed
  st.setMaxSpeed(fabsf(slow_mm_s * stepsPerMM));

  // Approach switch again slowly
  farTarget = st.currentPosition() + dirSign * 100000L;
  st.moveTo(farTarget);

  while (!isLimitTriggered(limitPin))
    st.run();

  // Final stop
  st.stop();
  while (st.isRunning())
    st.run();

  // Zero the axis position
  st.setCurrentPosition(0);
}


//Dual-motor Y-axis homing helper
// Home one Y motor independently to its top switch
void homeOneY(
  AccelStepper &st,            // Y motor stepper instance
  int topPin,                  // Top limit switch pin
  int dirSign,                 // Direction toward switch
  float stepsPerMM,            // Axis resolution
  float fast_mm_s,             // Fast homing speed
  float backoff_mm,            // Backoff distance
  float slow_mm_s              // Slow re-approach speed
) {

  // Set fast homing speed
  st.setMaxSpeed(fabsf(fast_mm_s * stepsPerMM));

  // Set Y-axis acceleration
  st.setAcceleration(ACCEL_Y_MM_S2 * stepsPerMM);

  // Move far toward top switch
  long farTarget = st.currentPosition() + dirSign * 1000000L;
  st.moveTo(farTarget);

  // Run until switch is hit
  while (!isLimitTriggered(topPin))
    st.run();

  // Stop motion
  st.stop();
  while (st.isRunning())
    st.run();

  // Convert backoff distance to steps
  long backSteps = (long)lroundf(backoff_mm * stepsPerMM);

  // Back away from switch
  st.moveTo(st.currentPosition() - dirSign * backSteps);
  while (st.distanceToGo() != 0)
    st.run();

  // Slow re-approach
  st.setMaxSpeed(fabsf(slow_mm_s * stepsPerMM));
  farTarget = st.currentPosition() + dirSign * 100000L;
  st.moveTo(farTarget);

  // Run until switch is hit again
  while (!isLimitTriggered(topPin))
    st.run();

  // Stop motion
  st.stop();
  while (st.isRunning())
    st.run();

  // Zero this motor
  st.setCurrentPosition(0);
}


void homeY_Dual() {

  Serial.println("Homing Y (dual synchronized)...");

  setAllEnabled(true);
  setMachineBusy(true);

  int dir = (HOMING_DIR_Y < 0) ? -1 : 1;

  // --- FAST APPROACH ---
  stepperY1.setMaxSpeed(fabsf(HOMING_SPEED_Y_MM_S * stepsPerMM_Y));
  stepperY2.setMaxSpeed(fabsf(HOMING_SPEED_Y_MM_S * stepsPerMM_Y));

  stepperY1.setAcceleration(ACCEL_Y_MM_S2 * stepsPerMM_Y);
  stepperY2.setAcceleration(ACCEL_Y_MM_S2 * stepsPerMM_Y);

  long farTarget1 = stepperY1.currentPosition() + dir * 1000000L;
  long farTarget2 = stepperY2.currentPosition() + dir * 1000000L;

  stepperY1.moveTo(farTarget1);
  stepperY2.moveTo(farTarget2);

  bool y1Done = false;
  bool y2Done = false;

 
 
 
 
 
 
 
 
 
 
  // --- MOVE BOTH UNTIL EACH HITS SWITCH ---
 
 








 
 
 
 
 
 
 
 
 
 
 
  while (!y1Done || !y2Done) {
if (checkEmergencyStop()) return;

    if (!y1Done) {
      if (isLimitTriggered(Y1_TOP_PIN)) {
        stepperY1.stop();
        y1Done = true;
        
      } else {
        //stepperY1.run();
        if (!y1Done) {
  stepperY1.run();
}
        
      }
    }


    if (!y2Done) {
      if (isLimitTriggered(Y2_TOP_PIN)) {
        stepperY2.stop();
        y2Done = true;
        
      } else {
        //stepperY2.run();
        if (!y2Done) {
        stepperY2.run();
        }
        
      }
    }
  }



  // Ensure both fully stopped
  
  while (stepperY1.isRunning() || stepperY2.isRunning()) {

    if (checkEmergencyStop()) return;

    stepperY1.run();
    stepperY2.run();
  }

  // --- BACKOFF ---
  long backSteps = (long)lroundf(HOMING_BACKOFF_Y_MM * stepsPerMM_Y);

  stepperY1.moveTo(stepperY1.currentPosition() - dir * backSteps);
  stepperY2.moveTo(stepperY2.currentPosition() - dir * backSteps);




  while (stepperY1.distanceToGo() != 0 || stepperY2.distanceToGo() != 0) {

if (checkEmergencyStop()) return;

    stepperY1.run();
    stepperY2.run();
  }

  // --- SLOW APPROACH ---
  stepperY1.setMaxSpeed(fabsf(HOMING_SLOW_Y_MM_S * stepsPerMM_Y));
  stepperY2.setMaxSpeed(fabsf(HOMING_SLOW_Y_MM_S * stepsPerMM_Y));

  farTarget1 = stepperY1.currentPosition() + dir * 100000L;
  farTarget2 = stepperY2.currentPosition() + dir * 100000L;

  stepperY1.moveTo(farTarget1);
  stepperY2.moveTo(farTarget2);

  y1Done = false;
  y2Done = false;









  while (!y1Done || !y2Done) {

if (checkEmergencyStop()) return;

    if (!y1Done) {
      if (isLimitTriggered(Y1_TOP_PIN)) {
        stepperY1.stop();
        y1Done = true;
        
      } else {
        //stepperY1.run();
        if (!y1Done) {
        stepperY1.run();
        }
        
      }
    }



    if (!y2Done) {
      if (isLimitTriggered(Y2_TOP_PIN)) {
        stepperY2.stop();
        y2Done = true;
        
      } else {
        //stepperY2.run();
        if (!y2Done) {
        stepperY2.run();
        }
        
      }
    }
  }






  while (stepperY1.isRunning() || stepperY2.isRunning()) {

    if (checkEmergencyStop()) return;
    
    stepperY1.run();
    stepperY2.run();
    
  }

  // --- ZERO BOTH ---
  stepperY1.setCurrentPosition(0);
  stepperY2.setCurrentPosition(0);

  yHomed = true;

  Serial.println("Y homed and squared.");

  setMachineBusy(false);
}


//X-axis homing wrapper
// Home X axis toward minimum switch
void homeX() {

  // Skip if no switch installed
  if (X_MIN_PIN < 0) {
    Serial.println("X home skipped: no switch");
    return;
  }

  // Inform host
  Serial.println("Homing X to MIN...");

  // Enable motors and set busy
  setAllEnabled(true);
  setMachineBusy(true);

  // Determine direction sign
  int dir = (HOMING_DIR_X < 0) ? -1 : 1;

  // Perform homing sequence
  homeAxisToMin(
    stepperX,                  // X stepper
    X_MIN_PIN,                 // Limit switch
    dir,                       // Direction
    stepsPerMM_X,              // Resolution
    HOMING_SPEED_X_MM_S,       // Fast speed
    HOMING_BACKOFF_X_MM,       // Backoff distance
    HOMING_SLOW_X_MM_S         // Slow approach speed
  );

  // Mark X as homed
  xHomed = true;

  // Report success
  Serial.println("X homed (left=0).");

  // Clear busy flag
  setMachineBusy(false);
}


//Camera (C axis) homing wrapper
// Home camera axis by retracting
void homeC() {

  // Skip if no switch installed
  if (C_MIN_PIN < 0) {
    Serial.println("C home skipped: no switch");
    return;
  }

  // Inform host
  Serial.println("Homing C (retract)...");

  // Enable motors and mark busy
  setAllEnabled(true);
  setMachineBusy(true);

  // Determine homing direction
  int dir = (HOMING_DIR_C < 0) ? -1 : 1;

  // Perform homing motion
  homeAxisToMin(
    stepperC,                  // Camera stepper
    C_MIN_PIN,                 // Retract switch
    dir,                       // Direction
    stepsPerMM_C,              // Resolution
    HOMING_SPEED_C_MM_S,       // Fast speed
    HOMING_BACKOFF_C_MM,       // Backoff distance
    HOMING_SLOW_C_MM_S         // Slow approach speed
  );

  // Mark camera as homed
  cHomed = true;

  // Report success
  Serial.println("C homed (retracted=0).");

  // Clear busy flag
  setMachineBusy(false);
}


//Full homing sequence (safe order)
// Perform full homing in safe mechanical order
void homeSequence_Full() {

  // Home camera first to retract out of the way
  if (C_MIN_PIN >= 0)
    homeC();

  // Home Y axis next (gantry alignment)
  homeY_Dual();

  // Home X axis last
  if (X_MIN_PIN >= 0)
    homeX();
}


//Coordinated motion scaling helper
// Scale max speed per axis so multi-axis moves finish together
void setScaledMax(
  AccelStepper &st,            // Stepper to configure
  float stepsPerMM,            // Axis resolution
  long dSteps,                 // Delta steps for this axis
  long maxdSteps,              // Largest delta among all axes
  float feed_mm_s,             // Requested feedrate
  float axisMaxMmS             // Axis maximum speed
) {

  // If no motion, use axis max speed
  if (maxdSteps == 0) {
    st.setMaxSpeed(axisMaxMmS * stepsPerMM);
    return;
  }

  // Calculate proportional share of motion
  float share = (float)labs(dSteps) / (float)maxdSteps;

  // Scale feedrate while respecting axis max
  float mm_s =
    (feed_mm_s < axisMaxMmS ? feed_mm_s : axisMaxMmS) * share;

  // Prevent zero-speed axis starvation
  if (mm_s < 1.0f)
    mm_s = 1.0f;

  // Apply scaled speed
  st.setMaxSpeed(mm_s * stepsPerMM);
}


//Coordinated linear motion (G0 / G1 engine)
// Perform a coordinated linear move in X, Y, and/or C
void moveLinear(
  float x_mm, bool hasX,        // Target X position and presence flag
  float y_mm, bool hasY,        // Target Y position and presence flag
  float c_mm, bool hasC,        // Target C position and presence flag
  bool rapid                    // TRUE for G0 (rapid), FALSE for G1
) {

  // Current target positions in steps
  long tx  = stepperX.currentPosition();
  long ty1 = stepperY1.currentPosition();
  long ty2 = stepperY2.currentPosition();
  long tc  = stepperC.currentPosition();

  // Convert requested X position to steps if present
  if (hasX)
    tx = (long)lroundf(x_mm * stepsPerMM_X);

  // Convert requested Y position to steps (both motors)
  if (hasY) {
    long ty = (long)lroundf(y_mm * stepsPerMM_Y);
    ty1 = ty;
    ty2 = ty;
  }

  // Convert requested C position to steps
  if (hasC)
    tc = (long)lroundf(c_mm * stepsPerMM_C);

  // Calculate deltas from current positions
  long dx = tx - stepperX.currentPosition();

  // Average Y motor positions for delta calculation
  long dy =
    ((ty1 + ty2) / 2) -
    ((stepperY1.currentPosition() + stepperY2.currentPosition()) / 2);

  // Camera delta
  long dc = tc - stepperC.currentPosition();

  // Determine the largest delta (for speed scaling)
  long maxd = 0;
  maxd = (labs(dx) > maxd ? labs(dx) : maxd);
  maxd = (labs(dy) > maxd ? labs(dy) : maxd);
  maxd = (labs(dc) > maxd ? labs(dc) : maxd);

  // Abort if no axis actually moves
  if (maxd == 0)
    return;

  // Determine feedrate in mm/s


float feedX = rapid ? MAX_SPEED_X_MM_S : (feedrate_X_mm_per_min / 60.0f);
float feedY = rapid ? MAX_SPEED_Y_MM_S : (feedrate_Y_mm_per_min / 60.0f);
float feedC = rapid ? MAX_SPEED_C_MM_S : (feedrate_C_mm_per_min / 60.0f);


setScaledMax(stepperX,  stepsPerMM_X, dx, maxd, feedX, MAX_SPEED_X_MM_S);
setScaledMax(stepperY1, stepsPerMM_Y, dy, maxd, feedY, MAX_SPEED_Y_MM_S);
setScaledMax(stepperY2, stepsPerMM_Y, dy, maxd, feedY, MAX_SPEED_Y_MM_S);
setScaledMax(stepperC,  stepsPerMM_C, dc, maxd, feedC, MAX_SPEED_C_MM_S);


  // Set final target positions
  stepperX.moveTo(tx);
  stepperY1.moveTo(ty1);
  stepperY2.moveTo(ty2);
  stepperC.moveTo(tc);

  // Enable motors and mark machine busy
  setAllEnabled(true);
  setMachineBusy(true);

  // Run all steppers until motion completes
  
  while (stepperX.distanceToGo() != 0 ||
         stepperY1.distanceToGo() != 0 ||
         stepperY2.distanceToGo() != 0 ||
         stepperC.distanceToGo() != 0) {

if (checkEmergencyStop()) return;

    stepperX.run();             // Advance X motor
    stepperY1.run();            // Advance Y motor 1
    stepperY2.run();            // Advance Y motor 2
    stepperC.run();             // Advance camera motor
  }

  // Clear busy flag
  setMachineBusy(false);

  // Report final position
  reportPosition();
}

//Home button debounce & trigger
// Button debounce time in milliseconds
const unsigned long HOME_DEBOUNCE_MS = 50;

// Poll and debounce the physical HOME button
void serviceHomeButton() {

  static unsigned long lastChange = 0;   // Last state change time
  static bool lastHigh = true;           // Previous stable state
  static bool pressed = false;           // Button currently pressed
  static unsigned long pressStart = 0;   // Time press began

  // Read button state (HIGH = idle, LOW = pressed)
  bool stateHigh = digitalRead(HOME_BTN_PIN);

  // Current time
  unsigned long now = millis();

  // Track any state change
  if (stateHigh != lastHigh) {
    lastChange = now;
    lastHigh = stateHigh;
  }

  // Check if state has been stable long enough
  if ((now - lastChange) >= HOME_DEBOUNCE_MS) {

    // Button just pressed
    if (!stateHigh && !pressed) {
      pressed = true;
      pressStart = now;
    }

    // Button just released
    else if (stateHigh && pressed) {
      unsigned long dur = now - pressStart;
      pressed = false;

      // Valid press detected
      if (dur >= HOME_DEBOUNCE_MS) {
        homeBtnPressedFlag = true;       // Queue full homing sequence
      }
    }
  }
}


//Serial parsing utilities
// Read one complete line from serial input
bool readLine(String &out) {

  // Process incoming serial characters
  while (Serial.available()) {

    char c = Serial.read();               // Read one character

    // End-of-line detected
    if (c == '\n' || c == '\r') {
      if (serialBuf.length() > 0) {
        out = serialBuf;                  // Return buffered line
        serialBuf = "";                   // Clear buffer
        return true;
      }
    }

    // Normal character
    else {

      // Semicolon starts comment until end of line
      if (c == ';') {
        while (Serial.available()) {
          char d = Serial.read();
          if (d == '\n' || d == '\r')
            break;
        }
        if (serialBuf.length() > 0) {
          out = serialBuf;
          serialBuf = "";
          return true;
        }
        return false;
      }

      // Parentheses comment (G-code style)
      else if (c == '(') {
        while (Serial.available()) {
          char d = Serial.read();
          if (d == ')')
            break;
        }
      }

      // Normal character, add to buffer
      else {
        serialBuf += c;
      }
    }
  }

  // No complete line yet
  return false;
}


//String cleanup and parsing helpers
// Trim whitespace, uppercase, and normalize spacing
String uptrim(const String &s) {
  String t = s;           // Copy string
  t.trim();               // Remove leading/trailing whitespace
  t.toUpperCase();        // Convert to uppercase
  t.replace("  ", " ");   // Collapse double spaces
  return t;
}

// Parse a floating-point word like X10.5 or F1200
bool parseWord(const String &line, char word, float &outVal) {

  // Locate the word character
  int idx = line.indexOf(word);
  if (idx < 0)
    return false;

  String num = "";        // Accumulate numeric characters

  // Parse characters following the word
  for (int i = idx + 1; i < (int)line.length(); ++i) {
    char c = line[i];

    if ((c >= '0' && c <= '9') || c == '-' || c == '+' || c == '.')
      num += c;
    else
      break;
  }

  // Fail if no number was found
  if (num.length() == 0)
    return false;

  // Convert to float
  outVal = num.toFloat();
  return true;
}

// Check if a command references an axis character
bool hasAxisChar(const String &line, char axis) {
  return line.indexOf(axis) >= 0;
}

// Parse a word like "FX200" or "FY150" from a line
bool parseWordStr(const String &line, const String &word, float &outVal) {
  int index = line.indexOf(word);
  if (index == -1) return false;
  
  int start = index + word.length();
  String valStr = "";
  
  while (start < line.length() && (isDigit(line[start]) || line[start] == '.' || line[start] == '-')) {
    valStr += line[start];
    start++;
  }
  
  if (valStr.length() == 0) return false;
  
  outVal = valStr.toFloat();
  return true;
}



// ---------- Motion Arrival Notification ----------
bool lastMotionState = false;  // remembers previous motion state

//Setup function
// Arduino setup routine
void setup() {


  // Initialize serial communication
  Serial.begin(115200);
  //delay(1000);

  // Wait for USB serial on some boards
  while (!Serial) {;}

  // Compute steps-per-mm values
  calcStepsPerMM_All();

  // Configure steppers and I/O
  configureSteppers();
  
  loadSettings();


  // Startup banner
  Serial.println("Rack Monitor G-code Ready");
  Serial.println(
    "Supported: G0/G1 X Y C F | "
    "G90/G91 | G28 [X][Y][C] | "
    "M114 | M17/M18 | "
    "M700 R C | M710/M711 | ! E-stop"
  );
}






//Main loop and command processing
// Arduino main loop
void loop() {

  // Poll home button
  serviceHomeButton();

  // Execute queued home request when machine is idle
  if (homeBtnPressedFlag && !machineBusy &&
      stepperX.distanceToGo() == 0 &&
      stepperY1.distanceToGo() == 0 &&
      stepperY2.distanceToGo() == 0 &&
      stepperC.distanceToGo() == 0) {

    homeBtnPressedFlag = false;
    homeSequence_Full();
  }

  String line;

  // Read one command line from serial
  if (!readLine(line))
    return;
    Serial.println("Yo! On my way!");


  // Normalize line
  line = uptrim(line);

  // Ignore empty lines
  if (line.length() == 0)
    return;

  // Emergency stop command
  if (line == "!") {
    eStop();
    return;
  }

  // Motion mode commands
  if (line.startsWith("G90")) {
    absoluteMode = true;
    Serial.println("ABS mode (G90)");
    return;
  }

  if (line.startsWith("G91")) {
    absoluteMode = false;
    Serial.println("REL mode (G91)");
    return;
  }

  // Homing command
  if (line.startsWith("G28")) {

    bool x = hasAxisChar(line, 'X');
    bool y = hasAxisChar(line, 'Y');
    bool c = hasAxisChar(line, 'C');

    // No axis specified = full homing
    if (!x && !y && !c) {
      homeSequence_Full();
    } else {
      if (c) homeC();
      if (y) homeY_Dual();
      if (x) homeX();
    }
    return;
  }

  // Status and motor enable commands
  if (line.startsWith("M114")) {
    reportPosition();
    return;
  }

  if (line.startsWith("M17")) {
    setAllEnabled(true);    //IS (true)
    Serial.println("Motors ENABLED");
    return;
  }

  if (line.startsWith("M18") || line.startsWith("M84")) {
    setAllEnabled(false);      //IS (false)
    Serial.println("Motors DISABLED");
    return;
  }

  // Grid move command
  if (line.startsWith("M700")) {

    float r = -1, c = -1;

    bool hasR = parseWord(line, 'R', r);
    bool hasCw = parseWord(line, 'C', c);

    if (!(hasR && hasCw)) {
      Serial.println("Err: M700 needs R and C");
      return;
    }

    int row = (int)r;
    int col = (int)c;

    if (row < 0 || row >= MAX_ROWS ||
        col < 0 || col >= MAX_COLS) {
      Serial.println("Err: M700 R/C out of range");
      return;
    }

    if (!yHomed)
      Serial.println("Warn: Y not homed; run G28 to zero at top.");

    if (!xHomed)
      Serial.println("Warn: X not homed; run G28 X.");

    float xTarget = X0_OFFSET_MM + col * PITCH_X_MM;
    float yTarget = Y0_OFFSET_MM + row * PITCH_Y_MM;

    moveLinear(xTarget, true, yTarget, true, 0, false, true);    //moveLinear(xTarget, true, yTarget, true, 0, false, false);
    return;
  }

  // Camera presets
  if (line.startsWith("M710")) {
    moveLinear(0, false, 0, false, C_IN_MM, true, false);
    return;
  }

  if (line.startsWith("M711")) {
    moveLinear(0, false, 0, false, C_OUT_MM, true, false);
    return;
  }


if (line.startsWith("M701")) {

  float r = -1, c = -1;
  bool hasR = parseWord(line, 'R', r);
  bool hasCw = parseWord(line, 'C', c);

  if (!(hasR && hasCw)) {
    Serial.println("Err: M701 needs R and C");
    return;
  }

  int newRows = (int)r;
  int newCols = (int)c;

  // 🔒 SAFETY LIMITS (important)
  if (newRows <= 0 || newRows > 100) {
    Serial.println("Err: Invalid ROWS (1–100)");
    return;
  }

  if (newCols <= 0 || newCols > 100) {
    Serial.println("Err: Invalid COLS (1–100)");
    return;
  }

  MAX_ROWS = newRows;
  MAX_COLS = newCols;

  Serial.print("Grid updated: ROWS=");
  Serial.print(MAX_ROWS);
  Serial.print(" COLS=");
  Serial.println(MAX_COLS);

  return;
}


if (line.startsWith("M702")) {

  float px = -1, py = -1;
  bool hasPX = parseWord(line, 'X', px);
  bool hasPY = parseWord(line, 'Y', py);

  if (!(hasPX || hasPY)) {
    Serial.println("Err: M702 needs X and/or Y");
    return;
  }

  // 🔒 Safety limits (VERY important)
  if (hasPX) {
    if (px <= 0 || px > 1000) {
      Serial.println("Err: Invalid X pitch (0–1000 mm)");
      return;
    }
    PITCH_X_MM = px;
  }

  if (hasPY) {
    if (py <= 0 || py > 1000) {
      Serial.println("Err: Invalid Y pitch (0–1000 mm)");
      return;
    }
    PITCH_Y_MM = py;
  }

  Serial.print("Pitch updated: X=");
  Serial.print(PITCH_X_MM);
  Serial.print(" Y=");
  Serial.println(PITCH_Y_MM);

  return;
}


if (line.startsWith("M703")) {

  float ox = 0, oy = 0;
  bool hasOX = parseWord(line, 'X', ox);
  bool hasOY = parseWord(line, 'Y', oy);

  if (!(hasOX || hasOY)) {
    Serial.println("Err: M703 needs X and/or Y");
    return;
  }

  // 🔒 Safety limits (adjust if needed)
  if (hasOX) {
    if (ox < -1000 || ox > 1000) {
      Serial.println("Err: Invalid X offset (-1000 to 1000 mm)");
      return;
    }
    X0_OFFSET_MM = ox;
  }

  if (hasOY) {
    if (oy < -1000 || oy > 1000) {
      Serial.println("Err: Invalid Y offset (-1000 to 1000 mm)");
      return;
    }
    Y0_OFFSET_MM = oy;
  }

  Serial.print("Offsets updated: X0=");
  Serial.print(X0_OFFSET_MM);
  Serial.print(" Y0=");
  Serial.println(Y0_OFFSET_MM);

  return;
}


if (line.startsWith("M704")) {

  float inPos = 0, outPos = 0;
  bool hasI = parseWord(line, 'I', inPos);
  bool hasO = parseWord(line, 'O', outPos);

  if (!(hasI || hasO)) {
    Serial.println("Err: M704 needs I and/or O");
    return;
  }

  // 🔒 Safety limits (adjust to your machine travel)
  if (hasI) {
    if (inPos < 0 || inPos > 200) {
      Serial.println("Err: Invalid C_IN (0–200 mm)");
      return;
    }
    C_IN_MM = inPos;
  }

  if (hasO) {
    if (outPos < 0 || outPos > 200) {
      Serial.println("Err: Invalid C_OUT (0–200 mm)");
      return;
    }
    C_OUT_MM = outPos;
  }

  Serial.print("Camera positions updated: IN=");
  Serial.print(C_IN_MM);
  Serial.print(" OUT=");
  Serial.println(C_OUT_MM);

  return;
}


if (line.startsWith("M705")) {
  Serial.print("ROWS=");
  Serial.print(MAX_ROWS);
  Serial.print(" COLS=");
  Serial.println(MAX_COLS);
  return;
}


if (line.startsWith("M706")) {
  Serial.print("Pitch X=");
  Serial.print(PITCH_X_MM);
  Serial.print(" Y=");
  Serial.println(PITCH_Y_MM);
  return;
}


if (line.startsWith("M707")) {
  Serial.print("Offsets X0=");
  Serial.print(X0_OFFSET_MM);
  Serial.print(" Y0=");
  Serial.println(Y0_OFFSET_MM);
  return;
}


if (line.startsWith("M708")) {
  Serial.print("Camera IN=");
  Serial.print(C_IN_MM);
  Serial.print(" OUT=");
  Serial.println(C_OUT_MM);
  return;
}


if (line.startsWith("M709")) {
  Serial.print("GRID R=");
  Serial.print(MAX_ROWS);
  Serial.print(" C=");
  Serial.print(MAX_COLS);

  Serial.print(" | PITCH X=");
  Serial.print(PITCH_X_MM);
  Serial.print(" Y=");
  Serial.print(PITCH_Y_MM);

  Serial.print(" | OFFSET X0=");
  Serial.print(X0_OFFSET_MM);
  Serial.print(" Y0=");
  Serial.print(Y0_OFFSET_MM);

  Serial.print("Camera IN=");
  Serial.print(C_IN_MM);
  Serial.print(" OUT=");
  Serial.println(C_OUT_MM);
  return;
}


if (line.startsWith("M500")) {
  saveSettings();
  return;
}

if (line.startsWith("M501")) {
  loadSettings();
  return;
}

if (line.startsWith("M502")) {
  resetSettings();
  return;
}


  // Linear motion commands
  bool isG0 = line.startsWith("G0") || line.startsWith("G00");
  bool isG1 = line.startsWith("G1") || line.startsWith("G01");

  if (isG0 || isG1) {


float x = 0, y = 0, c = 0;
bool hasX = parseWord(line, 'X', x);
bool hasY = parseWord(line, 'Y', y);
bool hasC = parseWord(line, 'C', c);


float fx = 0, fy = 0, fc = 0;
bool hasFX = parseWordStr(line, "FX", fx);
bool hasFY = parseWordStr(line, "FY", fy);
bool hasFC = parseWordStr(line, "FC", fc);

if (hasFX) feedrate_X_mm_per_min = fx;
if (hasFY) feedrate_Y_mm_per_min = fy;
if (hasFC) feedrate_C_mm_per_min = fc;


    // Current positions in mm
    float tx = stepperX.currentPosition() / stepsPerMM_X;
    float ty =
      ((stepperY1.currentPosition() + stepperY2.currentPosition()) * 0.5f)
      / stepsPerMM_Y;
    float tc = stepperC.currentPosition() / stepsPerMM_C;

    // Apply absolute or relative motion
    if (hasX) tx = absoluteMode ? x : (tx + x);
    if (hasY) ty = absoluteMode ? y : (ty + y);
    if (hasC) tc = absoluteMode ? c : (tc + c);

    // Homing warnings
    if (hasY && !yHomed)
      Serial.println("Warn: Y not homed; run G28 to zero at top.");

    if (hasX && !xHomed)
      Serial.println("Warn: X not homed; run G28 X.");

    if (hasC && !cHomed)
      Serial.println("Warn: C not homed; run G28 C.");

    // Execute motion
    moveLinear(tx, hasX, ty, hasY, tc, hasC, isG0);
    return;
  }

  // Unknown command handler
  Serial.print("Unknown/unsupported: ");
  Serial.println(line);
}



