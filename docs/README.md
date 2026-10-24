# AQMAS - Air Quality Monitoring and Alert System

A small Raspberry Pi Pico (MicroPython) project that reads an MQ135 gas sensor and a BMP280,
shows an air quality gauge on a yellow/blue OLED, and beeps a buzzer when the air gets worse.

Tested on my prototype and working. Please read the [Concerns](#concerns) before building it.

## Parts

- Raspberry Pi Pico (MicroPython)
- MQ135 gas sensor module (analog output)
- BMP280 module (HW-611) for temperature and pressure. It has **no humidity**
- 0.96" SSD1306 OLED, yellow/blue, I2C
- Active buzzer
- 2 x 1 k resistors (voltage divider for the MQ135 output)
- Push button (GP14 to GND)

## Circuit

![Circuit](docs/circuit.png)

Notes on the diagram:
- The push button goes between **GP14 (pin 19)** and **GND**. Its other leg must land on a GND pin
  (for example pin 18 or 23). Double-check this on the real board.

| Part | Pico pin |
|---|---|
| OLED + BMP280 SDA | GP4 (pin 6) |
| OLED + BMP280 SCL | GP5 (pin 7) |
| OLED + BMP280 VCC | 3V3 (pin 36) |
| MQ135 VCC | VBUS 5 V (pin 40) |
| MQ135 AO | GP26 (pin 31) through the divider |
| Buzzer + / - | GP15 (pin 20) / GND (pin 18) |
| Button | GP14 (pin 19) to GND (pin 18 or 23) |

The MQ135 output can reach 5 V and the Pico ADC only tolerates 3.3 V, so AO goes through two 1 k
resistors (5 V becomes 2.5 V). The code has the same values: `R_TOP, R_BOT = 1000, 1000`.
If you use different resistors, change those numbers and recalibrate.

## How to run it

1. Install MicroPython on the Pico.
2. Wire it up.
3. Copy `main.py` to the Pico (Thonny: Save as > Raspberry Pi Pico > `main.py`).
4. Power it. It warms up for 3 minutes.
5. In **fresh air**, hold the button for 2 seconds to calibrate. The baseline is saved to `r0.txt`.

Button: short press = snooze the buzzer for 5 minutes, hold 2 s = recalibrate.

A new MQ135 needs roughly 24 hours powered before its readings settle, so recalibrate after that.

## How the number is made

The MQ135's resistance (Rs) drops when gases like ammonia, alcohol, smoke and CO2 go up.
The code compares the current Rs with the Rs measured in clean air during calibration (R0),
after a small temperature correction using the BMP280:

```
NAQI estimate = CAL_NAQI x R0 / Rs
```

`CAL_NAQI` (40) is a number I picked: it means "the air I calibrated in counts as NAQI 40".
It is **not measured**. The buzzer follows the CPCB category limits: slow beep from 101,
fast beep from 201, and it clears at 90 % of the trigger so it doesn't chatter.

OLED layout: temperature min / now / max in the yellow strip; a gauge with the big number,
the label "NAQI" and the min/max since boot in the blue area.

## Concerns

1. **It is a 5 V sensor.** The MQ135 heater needs 5 V (about 150 mA), so the module is powered from the Pico's VBUS pin, not 3V3. At 5 V its analog output can also reach about 5 V, which is too high for the Pico's 3.3 V ADC pin. Give it a good USB supply.
2. **Voltage divider (hardware).** Put two equal 1 k resistors between the MQ135 AO pin and GND and take the Pico's GP26 from the midpoint. This halves the voltage, so the 5 V maximum becomes **2.5 V** at the Pico. Keep these wires short, and a 100 nF capacitor from GP26 to GND helps with noise.
3. **BMP280 vs BMP180.** The simulator has no BMP280 part, so the circuit diagram uses a BMP180 symbol for reference only. The real prototype uses a BMP280. They are different chips, so buy and use a BMP280.
4. **Two-colour display.** The OLED is yellow in the top 16 pixel rows and blue below. This is fixed in the hardware, so the layout puts temperature in the yellow band and the gauge in the blue area.
5. **NAQI is not real.** The value shown is an estimate from the MQ135, not an official NAQI.

## Files

```
main.py        firmware for the Pico
docs/circuit.png   wiring diagram
```

## Credits

The firmware (`main.py`) was written by **Claude**, an AI assistant made by Anthropic.
The hardware build, wiring and testing are mine.

## Disclaimer

Learning project. Readings are only indicative.
