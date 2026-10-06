# `<Sensor Name>` wiring

> Copy this file into `docs/wiring/`, rename it to match your sensor (e.g. `sht41.md`), and fill in the sections. Delete the instructional notes once filled in.
>
> The Orchard supports a classic ESP32 WROOM and an ESP32-S3. Choose the pin for the board and PlatformIO environment you are actually using; do not assume a GPIO number is valid on both chips. Current project assignments are listed in [`docs/wiring/README.md`](../../docs/wiring/README.md).

## What it measures

> 1-2 sentences. What does this sensor do? What units does it report? Typical use cases.

## Module variants

> Sensors often ship as multiple breakout-board variants. List the ones you've tested. Include vendor names and any pin differences.

| Variant       | Vendor   | Pin labels        | Notes                       |
|---------------|----------|-------------------|-----------------------------|
| `<name>`      | `<...>`  | `VCC GND SDA SCL` | `<exact board/module tested, or "not yet tested">` |

## Pin-by-pin wiring

List the board profile and PlatformIO environment used for each pin map. These are the current project defaults; custom `ORCHARD_PIN_*` build flags can change them.

| Sensor pin | Classic ESP32 WROOM (`freenove_esp32_wroom`) | ESP32-S3 (`freenove_esp32s3`) | Notes |
|------------|----------------------------------------------|-------------------------------|-------|
| VCC        | `<module-specific voltage>`                 | `<module-specific voltage>`   | Check the exact breakout's specifications; do not assume its VCC or logic-level tolerance. |
| GND        | GND                                          | GND                           | Share ground with the ESP32. |
| SDA        | GPIO 21                                     | GPIO 21                       | Shared I2C bus. |
| SCL        | GPIO 22                                     | GPIO 9                        | Shared I2C bus; the S3 profile remaps SCL because GPIO 22 is not available on the module. |

> ESP32 GPIOs are **not 5 V tolerant**. Verify the sensor's signal voltage as well as its supply voltage. Use a suitable level shifter or divider if a sensor can drive a signal above 3.3 V; do not rely on a module accepting 5 V power to mean its I/O is 3.3 V safe. For I2C, ensure pull-ups go to 3.3 V.

> For UART sensors: list TX/RX and required baud rate. Remember TX on sensor goes to RX on ESP32 (and vice versa).
>
> For analog sensors: list the board-specific ADC pin and its valid input-voltage range. Classic ESP32 ADC1 pins are GPIO 32–39; ESP32-S3 ADC1 pins are GPIO 1–10. Check the board profile for exposed pins, use ADC1, and add a divider when needed to keep the input within the chip's range.

## Protocol details

> **I2C devices:** address (default + alt), required pull-up resistors and their voltage.
>
> **UART devices:** baud rate, frame format, sentence/packet structure.
>
> **Analog devices:** voltage range, reference voltage, calibration curve.

## Calibration notes

> Warm-up time, drift behavior, known quirks. Anything a first-time user might trip over.

## Bring-up checklist

> Steps to confirm the sensor is alive before integrating into the firmware.

1. Wire as above.
2. Run an I2C bus scan / UART monitor / multimeter check. Expected output:

```
<paste expected output>
```

3. Load the driver. Confirm `name()` appears in the dashboard Scan page.

## Known issues

> Anything that has bitten you. Future-you will appreciate it.

## References

- Datasheet: `<link>`
- Library used: `<link>`
