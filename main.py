# Air Quality Monitor (NAQI-style gauge UI) - Raspberry Pi Pico (Pico or Pico W), MicroPython
# MQ135 (GP26 via divider) + BMP280 (I2C0 GP4/GP5) + SSD1306 yellow/blue OLED + active buzzer GP15
# Button GP14 -> GND:  short press = snooze 5 min on/off,  hold 2 s = calibrate R0 (FRESH AIR only)
import time, framebuf, struct, math
from machine import Pin, I2C, ADC

# ---------------- CONFIG (tune these) ----------------
R_TOP, R_BOT = 1000, 1000          # divider: AO -> R_TOP -> GP26 -> R_BOT -> GND (1k+1k = half, 5 V -> 2.5 V)
VC = 5.0                           # MQ135 supply (VBUS is often 4.6-5.0 V; calibration cancels most of it)
RL = 10000                         # module load resistor (value cancels out in R0/Rs ratio)
ASSUMED_RH = 50.0                  # BMP280 has NO humidity -> fixed guess used in MQ135 correction
TEMP_OFFSET = 0.0                  # subtract self-heating error from MQ135 (measure vs a reference)
WARMUP_S = 180                     # alerts suppressed during warm-up (first-ever use: burn in ~24 h)
CAL_NAQI = 40                      # the air you calibrate in is treated as NAQI 40. PLACEHOLDER - NOT a measurement
TH = (101, 201)                    # buzzer: slow beep from NAQI 101 (Moderate), fast beep from 201 (Poor)
GAUGE_MAX = 300                    # gauge arc is full at this NAQI (scale itself is 0-500)
HYST = 0.90                        # clear alarm at 90% of trigger value
SNOOZE_S = 300
SAMPLE_MS = 2000
OLED_ADDR = 0x3C

# ---------------- hardware ----------------
i2c = I2C(0, sda=Pin(4), scl=Pin(5), freq=100000)   # 100 kHz: tolerant of breadboard wiring / weak pull-ups
time.sleep_ms(200)                                   # let OLED and BMP280 finish power-up
adc = ADC(26)
buzzer = Pin(15, Pin.OUT, value=0)
btn = Pin(14, Pin.IN, Pin.PULL_UP)

class OLED(framebuf.FrameBuffer):
    def __init__(self, i2c, addr=0x3C):
        self.i2c, self.addr = i2c, addr
        self.buf = bytearray(1024)
        super().__init__(self.buf, 128, 64, framebuf.MONO_VLSB)
        for c in (0xAE,0x20,0x02,0x40,0xA1,0xA8,0x3F,0xC8,0xD3,0x00,0xDA,0x12,
                  0xD5,0x80,0xD9,0xF1,0xDB,0x30,0x81,0xCF,0xA4,0xA6,0x8D,0x14,0xAF):
            self.i2c.writeto(addr, bytes((0x80, c)))
    def show(self):
        # send 8 small page-sized writes instead of one 1 KB write; retry on bus glitches
        mv = memoryview(self.buf)
        for page in range(8):
            for _ in range(3):
                try:
                    self.i2c.writeto(self.addr, bytes((0x00, 0xB0 | page, 0x00, 0x10)))
                    self.i2c.writeto(self.addr, b'\x40' + bytes(mv[page * 128:(page + 1) * 128]))
                    break
                except OSError:
                    time.sleep_ms(20)

class BMP280:
    def __init__(self, i2c):
        self.i2c = i2c
        found = i2c.scan()
        self.addr = 0x76 if 0x76 in found else 0x77 if 0x77 in found else None
        if self.addr is None:
            raise OSError("BMP280 not found (0x76/0x77)")
        self.chip = i2c.readfrom_mem(self.addr, 0xD0, 1)[0]
        if self.chip == 0x60:
            print("NOTE: chip ID 0x60 = BME280 (it has humidity). This code ignores it.")
        elif self.chip not in (0x56, 0x57, 0x58):
            raise OSError("Unknown chip ID 0x%02X" % self.chip)
        c = struct.unpack('<HhhHhhhhhhhh', i2c.readfrom_mem(self.addr, 0x88, 24))
        (self.T1,self.T2,self.T3,self.P1,self.P2,self.P3,self.P4,
         self.P5,self.P6,self.P7,self.P8,self.P9) = c
        i2c.writeto_mem(self.addr, 0xF5, b'\x88')   # standby 500 ms, IIR filter 4
        i2c.writeto_mem(self.addr, 0xF4, b'\x57')   # T x2, P x16, normal mode
        time.sleep_ms(100)
    def read(self):
        d = self.i2c.readfrom_mem(self.addr, 0xF7, 6)
        adc_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        adc_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
        # integer maths from Bosch datasheet (Pico floats are only 32-bit, so avoid float version)
        v1 = (((adc_t >> 3) - (self.T1 << 1)) * self.T2) >> 11
        v2 = (((((adc_t >> 4) - self.T1) * ((adc_t >> 4) - self.T1)) >> 12) * self.T3) >> 14
        tf = v1 + v2
        temp = ((tf * 5 + 128) >> 8) / 100.0
        v1 = tf - 128000
        v2 = v1 * v1 * self.P6
        v2 += (v1 * self.P5) << 17
        v2 += self.P4 << 35
        v1 = ((v1 * v1 * self.P3) >> 8) + ((v1 * self.P2) << 12)
        v1 = (((1 << 47) + v1) * self.P1) >> 33
        if v1 == 0:
            return temp, 0.0
        p = 1048576 - adc_p
        p = (((p << 31) - v2) * 3125) // v1
        v1 = (self.P9 * (p >> 13) * (p >> 13)) >> 25
        v2 = (self.P8 * p) >> 19
        p = ((p + v1 + v2) >> 8) + (self.P7 << 4)
        return temp, p / 256.0 / 100.0   # degC, hPa

def read_rs():
    s = 0
    for _ in range(64):
        s += adc.read_u16()
        time.sleep_us(300)
    v_pin = (s / 64) * 3.3 / 65535
    v_ao = v_pin * (R_TOP + R_BOT) / R_BOT
    if v_ao < 0.05:
        return None                       # sensor not connected / not powered
    return RL * (VC - v_ao) / v_ao

def temp_factor(t, h=ASSUMED_RH):
    # Approximation used by common MQ135 libraries (hand-fitted, NOT a datasheet formula)
    if t < 20:
        return 0.00035 * t * t - 0.02718 * t + 1.39538 - (h - 33.0) * 0.0018
    return -0.003333333 * t - 0.001923077 * h + 1.130128205

def load_r0():
    try:
        with open("r0.txt") as f:
            return float(f.read())
    except Exception:
        return None

def save_r0(v):
    with open("r0.txt", "w") as f:
        f.write(str(v))

oled = OLED(i2c, OLED_ADDR)
sensor = BMP280(i2c)

def screen(y1, y2, lines):
    oled.fill(0)
    oled.text(y1, 0, 0)                  # yellow band: rows 0-15
    oled.text(y2, 0, 8)
    for i, l in enumerate(lines):        # blue area: rows 16-63
        oled.text(l, 0, 20 + i * 11)
    oled.show()

# --- UI begin ---
CX, CY, RO, RI = 64, 48, 32, 27          # gauge centre and outer/inner radius
NAMES = ("GOOD", "SATISFACT.", "MODERATE", "POOR", "VERY POOR", "SEVERE")

def category(n):
    for i, lim in enumerate((50, 100, 200, 300, 400)):
        if n <= lim:
            return NAMES[i]
    return NAMES[5]

def deg(x, y):                           # 8x8 font has no degree sign
    for dx, dy in ((1, 0), (0, 1), (2, 1), (1, 2)):
        oled.pixel(x + dx, y + dy, 1)

def big(txt, x, y):                      # 2x scaled text
    w = 8 * len(txt)
    tmp = framebuf.FrameBuffer(bytearray(w), w, 8, framebuf.MONO_VLSB)
    tmp.text(txt, 0, 0, 1)
    for j in range(8):
        for i in range(w):
            if tmp.pixel(i, j):
                oled.fill_rect(x + 2 * i, y + 2 * j, 2, 2, 1)

def gauge(naqi):
    f = min(naqi, GAUGE_MAX) / GAUGE_MAX
    n = 120
    for k in range(n + 1):
        a = math.pi * (1 - k / n)        # sweep from left, over the top, to right
        c, s = math.cos(a), math.sin(a)
        full = (k / n) <= f
        for r in range(RI, RO + 1):
            if full or r == RI or r == RO:
                oled.pixel(int(CX + r * c + 0.5), int(CY - r * s + 0.5), 1)
    for r in range(RI, RO + 1):          # end caps
        oled.pixel(CX - r, CY, 1)
        oled.pixel(CX + r, CY, 1)

def gauge_screen(naqi, t, tmin, tmax, nmin, nmax, label):
    oled.fill(0)
    # yellow band (rows 0-15): min temp | current temp | max temp
    oled.text("min", 0, 0)
    oled.text("max", 104, 0)
    cur = "%d" % round(t)
    x = (128 - (8 * len(cur) + 4)) // 2
    oled.text(cur, x, 4)
    deg(x + 8 * len(cur) + 1, 4)
    lo = "%d" % round(tmin)
    oled.text(lo, 0, 8)
    deg(8 * len(lo) + 1, 8)
    hi = "%d" % round(tmax)
    oled.text(hi, 104, 8)
    deg(104 + 8 * len(hi) + 1, 8)
    # blue area: gauge, big NAQI number, label, min/max NAQI
    gauge(naqi)
    num = "%d" % naqi
    big(num, 64 - 8 * len(num), 36)
    oled.text(label, (128 - 8 * len(label)) // 2, 54)
    oled.text("min", 0, 47)
    oled.text("max", 104, 47)
    lo = "--" if nmin > nmax else "%d" % nmin
    hi = "--" if nmin > nmax else "%d" % nmax
    oled.text(lo, 0, 55)
    oled.text(hi, 128 - 8 * len(hi), 55)
    oled.show()
# --- UI end ---

def calibrate(temp):
    vals = []
    for i in range(30):
        screen("CALIBRATING", "FRESH AIR! %ds" % (30 - i), ["Keep still,", "don't breathe on", "the sensor"])
        rs = read_rs()
        if rs:
            vals.append(rs / temp_factor(temp))
        time.sleep_ms(600)
    if vals:
        r0 = sum(vals) / len(vals)
        save_r0(r0)
        return r0
    return None

# ---------------- state ----------------
r0 = load_r0()
boot = time.ticks_ms()
level, naqi, tick = 0, 0, 0
last_sample = -SAMPLE_MS
t_now, p_now = sensor.read()
tmin = tmax = t_now
nmin, nmax = 999, 0
snooze_until = 0
press_t = None
LABEL = ("GOOD", "MODERATE", "DANGER!")

def buzz(now):
    if time.ticks_diff(now, snooze_until) < 0:
        return 0
    if level == 1:
        return 1 if now % 2000 < 80 else 0
    if level == 2:
        return 1 if now % 240 < 120 else 0
    return 0

while True:
    now = time.ticks_ms()
    elapsed = time.ticks_diff(now, boot) // 1000
    warm = elapsed < WARMUP_S

    # ---- button ----
    if btn.value() == 0 and press_t is None:
        press_t = now
    elif btn.value() == 1 and press_t is not None:
        held = time.ticks_diff(now, press_t)
        press_t = None
        if held > 40:
            if held >= 2000 and not warm:
                buzzer.value(0)
                new = calibrate(t_now - TEMP_OFFSET)
                if new:
                    r0 = new
                nmin, nmax = 999, 0
                last_sample = -SAMPLE_MS
            elif held < 2000:
                snooze_until = now + SNOOZE_S * 1000 if time.ticks_diff(now, snooze_until) >= 0 else now

    # ---- sample ----
    if time.ticks_diff(now, last_sample) >= SAMPLE_MS:
        last_sample = now
        try:
            t_now, p_now = sensor.read()
        except OSError:
            print("BMP280 read error")
            time.sleep_ms(200)
            continue
        tmin, tmax = min(tmin, t_now), max(tmax, t_now)
        rs = read_rs()
        if rs is None:
            screen("MQ135 ERROR", "check wiring", ["T %.1fC" % t_now, "P %.0fhPa" % p_now])
        elif warm:
            screen("WARMING UP", "%ds left" % (WARMUP_S - elapsed),
                   ["T %.1f C" % t_now, "P %.0f hPa" % p_now, "Alerts off"])
        elif r0 is None:
            screen("NOT CALIBRATED", "Hold btn 2s", ["Fresh air only", "T %.1f C" % t_now, "P %.0f hPa" % p_now])
            level = 0
        else:
            rs_c = rs / temp_factor(t_now - TEMP_OFFSET)
            naqi = min(500, int(CAL_NAQI * r0 / rs_c))   # ESTIMATE scaled from your calibration, not a true NAQI
            nmin, nmax = min(nmin, naqi), max(nmax, naqi)
            while level < 2 and naqi >= TH[level]:
                level += 1
            while level > 0 and naqi < TH[level - 1] * HYST:
                level -= 1
            tick += 1
            snz = time.ticks_diff(now, snooze_until) < 0
            label = "NAQI"
            if tick % 2 and snz:
                label = "SNOOZED"
            elif tick % 2 and level > 0:
                label = category(naqi)
            gauge_screen(naqi, t_now, tmin, tmax, nmin, nmax, label)
        print(elapsed, "T=%.2f P=%.1f Rs=%s NAQI~%d lvl=%d" % (t_now, p_now, rs and int(rs), naqi, level))

    buzzer.value(0 if warm or r0 is None else buzz(now))
    time.sleep_ms(20)
