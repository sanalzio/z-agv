#include <EEPROM.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

constexpr uint32_t BT_BAUD = 9600UL;
constexpr uint32_t USB_BAUD = 115200UL;

// Flip to LOW if your 8-channel IR bar uses active-low LEDON control
#define IR_LED_ON HIGH

// Verified Arduino Leonardo (ATmega32U4) pin map
constexpr uint8_t PIN_IR_LED = 2;
constexpr uint8_t SENSOR_PINS[8] = { A0, A1, A2, A3, A4, A5, A6, A8 };
constexpr uint8_t PIN_ENA = 5;
constexpr uint8_t PIN_IN1 = 6;
constexpr uint8_t PIN_IN2 = 7;
constexpr uint8_t PIN_IN3 = 11;
constexpr uint8_t PIN_IN4 = 12;
constexpr uint8_t PIN_ENB = 9;

// Control & safety timing
constexpr uint32_t CONTROL_INTERVAL_MS = 10UL;
constexpr uint32_t MANUAL_WATCHDOG_MS = 800UL;
constexpr uint32_t AUTO_WATCHDOG_MS = 1500UL;
constexpr uint32_t TURN_TIMEOUT_MS = 3000UL;
constexpr uint32_t TURN_EXIT_MIN_MS = 220UL;
constexpr uint32_t LINE_LOST_TIMEOUT_MS = 1500UL;
constexpr int16_t TURN_PWM = 140;

constexpr uint16_t CAL_MAGIC = 0x4147U;
constexpr int8_t SENSOR_WEIGHTS[8] = { -100, -71, -43, -14, 14, 43, 71, 100 };

enum RobotMode : uint8_t {
  MODE_IDLE = 0,
  MODE_LINE,
  MODE_TURN180,
  MODE_MANUAL,
  MODE_ESTOP
};

struct CalData {
  uint16_t magic;
  uint16_t white[8];
  uint16_t black[8];
  uint16_t checksum;
};

struct CmdReader {
  Stream *io;
  char buf[32];
  uint8_t len;
  bool overflow;
};

CalData cal;
RobotMode mode = MODE_IDLE;
char manualDir = 'S';

int16_t lineBaseSpeed = 150;
int16_t manualSpeed = 160;
int16_t motorTrim = 0;
float kp = 1.20f;
float kd = 0.85f;

int16_t curLeftPwm = 0;
int16_t curRightPwm = 0;
uint8_t normSensor[8] = { 0 };
int16_t lineError = 0;
int16_t lastError = 0;
int16_t telError = 999;

uint32_t lastCmdMs = 0;
uint32_t lastControlMs = 0;
uint32_t turnStartMs = 0;
uint32_t lineLostSinceMs = 0;
uint8_t turnStage = 0;

Stream *activeLink = &Serial1;
CmdReader btReader = { &Serial1, { 0 }, 0, false };
CmdReader usbReader = { &Serial, { 0 }, 0, false };

const char *modeName(RobotMode m) {
  switch (m) {
    case MODE_LINE: return "LINE";
    case MODE_TURN180: return "TURN180";
    case MODE_MANUAL: return "MANUAL";
    case MODE_ESTOP: return "ESTOP";
    default: return "IDLE";
  }
}

uint16_t calcChecksum(const CalData &d) {
  uint16_t sum = d.magic;
  for (uint8_t i = 0; i < 8; i++) {
    sum = (uint16_t)(sum * 31U + d.white[i]);
    sum = (uint16_t)(sum * 31U + d.black[i]);
  }
  return (uint16_t)(sum ^ 0xA55AU);
}

void loadCalibration() {
  EEPROM.get(0, cal);
  if (cal.magic == CAL_MAGIC && cal.checksum == calcChecksum(cal)) {
    return;
  }
  // Keep defaults in RAM only so uncalibrated boots do not wear EEPROM
  cal.magic = CAL_MAGIC;
  for (uint8_t i = 0; i < 8; i++) {
    cal.white[i] = 150;
    cal.black[i] = 800;
  }
  cal.checksum = calcChecksum(cal);
}

void driveChannel(uint8_t pinA, uint8_t pinB, uint8_t pinEn, int16_t pwm) {
  if (pwm > 0) {
    digitalWrite(pinA, HIGH);
    digitalWrite(pinB, LOW);
    analogWrite(pinEn, (uint8_t)pwm);
  } else if (pwm < 0) {
    digitalWrite(pinA, LOW);
    digitalWrite(pinB, HIGH);
    analogWrite(pinEn, (uint8_t)(-pwm));
  } else {
    digitalWrite(pinA, LOW);
    digitalWrite(pinB, LOW);
    analogWrite(pinEn, 0);
  }
}

void setMotors(int16_t left, int16_t right) {
  left = constrain(left, -255, 255);
  right = constrain(right, -255, 255);
  curLeftPwm = left;
  curRightPwm = right;
  driveChannel(PIN_IN1, PIN_IN2, PIN_ENA, left);
  driveChannel(PIN_IN3, PIN_IN4, PIN_ENB, right);
}

void applyManualMotion() {
  switch (manualDir) {
    case 'F': setMotors(manualSpeed + motorTrim, manualSpeed - motorTrim); break;
    case 'B': setMotors(-(manualSpeed + motorTrim), -(manualSpeed - motorTrim)); break;
    case 'A': setMotors(-manualSpeed, manualSpeed); break;
    case 'D': setMotors(manualSpeed, -manualSpeed); break;
    default: setMotors(0, 0); break;
  }
}

void readSensors() {
  int32_t weightedSum = 0;
  int32_t totalWeight = 0;

  for (uint8_t i = 0; i < 8; i++) {
    uint16_t raw = (uint16_t)analogRead(SENSOR_PINS[i]);
    int32_t span = (int32_t)cal.black[i] - (int32_t)cal.white[i];
    if (span > -20L && span < 20L) {
      normSensor[i] = 0;
    } else {
      // 32-bit fixed-point scaling avoids 16-bit AVR overflow and handles inverted polarity
      int32_t scaled = ((int32_t)raw - (int32_t)cal.white[i]) * 90L / span;
      int32_t digit = (scaled + (scaled >= 0 ? 5L : -5L)) / 10L;
      normSensor[i] = (uint8_t)constrain(digit, 0L, 9L);
    }
    if (normSensor[i] >= 3) {
      weightedSum += (int32_t)normSensor[i] * (int32_t)SENSOR_WEIGHTS[i];
      totalWeight += (int32_t)normSensor[i];
    }
  }

  if (totalWeight > 0) {
    telError = (int16_t)(weightedSum / totalWeight);
    lineError = telError;
    lineLostSinceMs = 0;
  } else {
    telError = 999;
    lineError = (lastError < 0) ? -100 : 100;
  }
}

void sendLine(Stream &io, const char *msg) {
  io.print(msg);
  io.print('\n');
}

void sendTelemetry(Stream &io) {
  char out[48];
  int len = snprintf(
    out, sizeof(out),
    "TEL:%s,%d,%d,%u%u%u%u%u%u%u%u,%d\n",
    modeName(mode),
    (int)curLeftPwm,
    (int)curRightPwm,
    (unsigned)normSensor[0], (unsigned)normSensor[1], (unsigned)normSensor[2], (unsigned)normSensor[3],
    (unsigned)normSensor[4], (unsigned)normSensor[5], (unsigned)normSensor[6], (unsigned)normSensor[7],
    (int)telError
  );
  // Drop frame rather than block if TX buffer is saturated
  if (len > 0 && io.availableForWrite() >= len) {
    io.write((const uint8_t *)out, (size_t)len);
  }
}

bool parseIntStrict(const char *s, int16_t &out) {
  if (!s || *s == '\0') return false;
  char *end = nullptr;
  long val = strtol(s, &end, 10);
  if (end == s || *end != '\0' || val < -32768L || val > 32767L) return false;
  out = (int16_t)val;
  return true;
}

bool parseFloatStrict(const char *s, float &out) {
  if (!s || *s == '\0') return false;
  char *end = nullptr;
  double val = strtod(s, &end);
  if (end == s || *end != '\0' || isnan(val) || isinf(val)) return false;
  out = (float)val;
  return true;
}

void calibrateSurface(Stream &io, bool isBlack) {
  if (mode != MODE_IDLE && mode != MODE_ESTOP) {
    sendLine(io, "ERR:STOP_FIRST");
    return;
  }
  uint32_t acc[8] = { 0 };
  constexpr uint8_t SAMPLES = 10;
  for (uint8_t s = 0; s < SAMPLES; s++) {
    for (uint8_t i = 0; i < 8; i++) {
      acc[i] += (uint16_t)analogRead(SENSOR_PINS[i]);
    }
    delay(2);
  }
  for (uint8_t i = 0; i < 8; i++) {
    uint16_t avg = (uint16_t)(acc[i] / SAMPLES);
    if (isBlack) cal.black[i] = avg;
    else cal.white[i] = avg;
  }
  cal.magic = CAL_MAGIC;
  cal.checksum = calcChecksum(cal);
  // EEPROM.put() writes only changed bytes
  EEPROM.put(0, cal);
  readSensors();
  sendLine(io, isBlack ? "ACK:CB" : "ACK:CW");
  sendTelemetry(io);
}

void handleCommand(Stream &io, const char *cmd) {
  activeLink = &io;
  lastCmdMs = millis();

  if (strcmp(cmd, "P") == 0) {
    sendTelemetry(io);
    return;
  }
  if (strcmp(cmd, "E") == 0) {
    setMotors(0, 0);
    mode = MODE_ESTOP;
    manualDir = 'S';
    sendLine(io, "ACK:ESTOP");
    sendTelemetry(io);
    return;
  }
  if (strcmp(cmd, "R") == 0) {
    setMotors(0, 0);
    mode = MODE_IDLE;
    manualDir = 'S';
    sendLine(io, "ACK:RESET");
    sendTelemetry(io);
    return;
  }
  if (strcmp(cmd, "S") == 0) {
    setMotors(0, 0);
    manualDir = 'S';
    if (mode != MODE_ESTOP) mode = MODE_IDLE;
    sendLine(io, "ACK:S");
    sendTelemetry(io);
    return;
  }
  if (strcmp(cmd, "CW") == 0) {
    calibrateSurface(io, false);
    return;
  }
  if (strcmp(cmd, "CB") == 0) {
    calibrateSurface(io, true);
    return;
  }

  // Movement & mode transitions are strictly blocked while E-STOP is latched
  if (cmd[0] != '\0' && cmd[1] == '\0' && (cmd[0] == 'L' || cmd[0] == 'U' || cmd[0] == 'F' || cmd[0] == 'B' || cmd[0] == 'A' || cmd[0] == 'D')) {
    if (mode == MODE_ESTOP) {
      sendLine(io, "ERR:ESTOP_LOCKED");
      return;
    }
    if (cmd[0] == 'L') {
      mode = MODE_LINE;
      lastError = 0;
      lineLostSinceMs = 0;
      sendLine(io, "ACK:L");
      sendTelemetry(io);
    } else if (cmd[0] == 'U') {
      mode = MODE_TURN180;
      turnStage = 0;
      turnStartMs = millis();
      setMotors(-TURN_PWM, TURN_PWM);
      sendLine(io, "ACK:U");
      sendTelemetry(io);
    } else {
      mode = MODE_MANUAL;
      manualDir = cmd[0];
      applyManualMotion();
      char ack[8] = "ACK:X";
      ack[4] = cmd[0];
      sendLine(io, ack);
      sendTelemetry(io);
    }
    return;
  }

  int16_t iv = 0;
  float fv = 0.0f;
  char reply[24];
  char fbuf[12];

  if (strncmp(cmd, "VL:", 3) == 0) {
    if (!parseIntStrict(cmd + 3, iv)) { sendLine(io, "ERR:BAD_ARG"); return; }
    lineBaseSpeed = constrain(iv, 90, 255);
    snprintf(reply, sizeof(reply), "ACK:VL:%d", (int)lineBaseSpeed);
    sendLine(io, reply);
    return;
  }
  if (strncmp(cmd, "VM:", 3) == 0) {
    if (!parseIntStrict(cmd + 3, iv)) { sendLine(io, "ERR:BAD_ARG"); return; }
    manualSpeed = constrain(iv, 90, 255);
    if (mode == MODE_MANUAL) applyManualMotion();
    snprintf(reply, sizeof(reply), "ACK:VM:%d", (int)manualSpeed);
    sendLine(io, reply);
    return;
  }
  if (strncmp(cmd, "T:", 2) == 0) {
    if (!parseIntStrict(cmd + 2, iv)) { sendLine(io, "ERR:BAD_ARG"); return; }
    motorTrim = constrain(iv, -30, 30);
    if (mode == MODE_MANUAL) applyManualMotion();
    snprintf(reply, sizeof(reply), "ACK:T:%d", (int)motorTrim);
    sendLine(io, reply);
    return;
  }
  if (strncmp(cmd, "KP:", 3) == 0) {
    if (!parseFloatStrict(cmd + 3, fv) || fv < 0.0f || fv > 50.0f) { sendLine(io, "ERR:BAD_ARG"); return; }
    kp = fv;
    dtostrf(kp, 1, 2, fbuf);
    snprintf(reply, sizeof(reply), "ACK:KP:%s", fbuf);
    sendLine(io, reply);
    return;
  }
  if (strncmp(cmd, "KD:", 3) == 0) {
    if (!parseFloatStrict(cmd + 3, fv) || fv < 0.0f || fv > 50.0f) { sendLine(io, "ERR:BAD_ARG"); return; }
    kd = fv;
    dtostrf(kd, 1, 2, fbuf);
    snprintf(reply, sizeof(reply), "ACK:KD:%s", fbuf);
    sendLine(io, reply);
    return;
  }

  sendLine(io, "ERR:UNKNOWN_CMD");
}

void pollReader(CmdReader &r) {
  while (r.io->available() > 0) {
    char c = (char)r.io->read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (r.overflow) {
        r.len = 0;
        r.overflow = false;
        sendLine(*r.io, "ERR:BAD_ARG");
      } else if (r.len > 0) {
        r.buf[r.len] = '\0';
        r.len = 0;
        handleCommand(*r.io, r.buf);
      }
      continue;
    }
    if ((size_t)r.len + 1U < sizeof(r.buf)) {
      if (c >= 'a' && c <= 'z') c = (char)(c - 'a' + 'A');
      r.buf[r.len++] = c;
    } else {
      r.overflow = true;
    }
  }
}

void updateControl(uint32_t now) {
  readSensors();

  if (mode == MODE_LINE) {
    if (telError == 999) {
      if (lineLostSinceMs == 0) lineLostSinceMs = now;
      else if (now - lineLostSinceMs > LINE_LOST_TIMEOUT_MS) {
        setMotors(0, 0);
        mode = MODE_IDLE;
        sendLine(*activeLink, "ERR:LINE_LOST");
        sendTelemetry(*activeLink);
        return;
      }
    }
    float speedFactor = (float)lineBaseSpeed / 150.0f;
    float corrF = (kp * (float)lineError + kd * (float)(lineError - lastError)) * speedFactor;
    lastError = lineError;
    int16_t corr = (int16_t)corrF;
    int16_t left = constrain(lineBaseSpeed + corr + motorTrim, -120, 255);
    int16_t right = constrain(lineBaseSpeed - corr - motorTrim, -120, 255);
    setMotors(left, right);
    return;
  }

  if (mode == MODE_TURN180) {
    uint32_t elapsed = now - turnStartMs;
    if (elapsed > TURN_TIMEOUT_MS) {
      setMotors(0, 0);
      mode = MODE_IDLE;
      sendLine(*activeLink, "ERR:TURN_TIMEOUT");
      sendTelemetry(*activeLink);
      return;
    }
    if (turnStage == 0) {
      setMotors(-TURN_PWM, TURN_PWM);
      if (elapsed >= TURN_EXIT_MIN_MS && normSensor[3] < 4 && normSensor[4] < 4) {
        turnStage = 1;
      }
    } else {
      setMotors(-TURN_PWM, TURN_PWM);
      if (normSensor[3] >= 5 || normSensor[4] >= 5) {
        setMotors(0, 0);
        lastError = 0;
        lineLostSinceMs = 0;
        mode = MODE_LINE;
        sendLine(*activeLink, "ACK:TURN_LOCKED");
        sendTelemetry(*activeLink);
      }
    }
    return;
  }

  if (mode == MODE_IDLE || mode == MODE_ESTOP) {
    if (curLeftPwm != 0 || curRightPwm != 0) setMotors(0, 0);
  }
}

void setup() {
  // Lock motor outputs LOW immediately on boot to prevent power-on twitch
  pinMode(PIN_ENA, OUTPUT);
  pinMode(PIN_IN1, OUTPUT);
  pinMode(PIN_IN2, OUTPUT);
  pinMode(PIN_IN3, OUTPUT);
  pinMode(PIN_IN4, OUTPUT);
  pinMode(PIN_ENB, OUTPUT);
  setMotors(0, 0);

  pinMode(PIN_IR_LED, OUTPUT);
  digitalWrite(PIN_IR_LED, IR_LED_ON);

  loadCalibration();

  // Never use while(!Serial) — Leonardo must boot standalone on battery
  Serial1.begin(BT_BAUD);
  Serial.begin(USB_BAUD);

  lastCmdMs = millis();
  lastControlMs = lastCmdMs;
}

void loop() {
  pollReader(btReader);
  if (Serial) pollReader(usbReader);

  uint32_t now = millis();

  if (mode == MODE_LINE || mode == MODE_TURN180 || mode == MODE_MANUAL) {
    uint32_t timeout = (mode == MODE_MANUAL) ? MANUAL_WATCHDOG_MS : AUTO_WATCHDOG_MS;
    if (now - lastCmdMs > timeout) {
      setMotors(0, 0);
      mode = MODE_IDLE;
      manualDir = 'S';
      sendLine(*activeLink, "ERR:WATCHDOG_TIMEOUT");
      sendTelemetry(*activeLink);
    }
  }

  if (now - lastControlMs >= CONTROL_INTERVAL_MS) {
    lastControlMs = now;
    updateControl(now);
  }
}
