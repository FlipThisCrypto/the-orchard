// SPDX-License-Identifier: Apache-2.0
//
// Default pin allocation for the classic ESP32 Tree.
//
// The ESP32-S3 PlatformIO environments override pins that are unavailable
// on that module. All pins are configurable at build time via
// `-D ORCHARD_PIN_*` flags in platformio.ini.

#pragma once

// --- I2C bus (shared by AHT20, BMP280, BH1750 when added) -----------
#ifndef ORCHARD_PIN_I2C_SDA
#define ORCHARD_PIN_I2C_SDA  21
#endif
#ifndef ORCHARD_PIN_I2C_SCL
#define ORCHARD_PIN_I2C_SCL  22
#endif

// --- GPS NEO module on UART1 (confirmed working) --------------------
#ifndef ORCHARD_PIN_GPS_RX        // ESP32 RX  <- GPS TX
#define ORCHARD_PIN_GPS_RX   4
#endif
#ifndef ORCHARD_PIN_GPS_TX        // ESP32 TX  -> GPS RX
#define ORCHARD_PIN_GPS_TX   5
#endif

// --- PMS5003 on UART2 (when added) ----------------------------------
#ifndef ORCHARD_PIN_PMS_RX
#define ORCHARD_PIN_PMS_RX   16
#endif
#ifndef ORCHARD_PIN_PMS_TX
#define ORCHARD_PIN_PMS_TX   17
#endif

// --- MQ-135 analog (classic ESP32 ADC1_CH6, input only) -------------
// The S3 environment overrides this to GPIO 7 (ADC1).
#ifndef ORCHARD_PIN_MQ135_ADC
#define ORCHARD_PIN_MQ135_ADC  34
#endif

// --- DS18B20 1-Wire temperature probe -------------------------------
// GPIO 25 on WROOM-32U: regular bidirectional GPIO, not a strapping
// pin or input-only. The S3 environment overrides this to GPIO 10.
//
// REQUIRED EXTERNAL PARTS: 4.7 kΩ pull-up resistor between this pin
// and 3.3V. Waterproof DS18B20 probe kits usually include one in a
// heat-shrink tube near the connector; bare TO-92 chips do NOT.
// Without the pull-up the sensor never responds.
#ifndef ORCHARD_PIN_DS18B20_DATA
#define ORCHARD_PIN_DS18B20_DATA  25
#endif

// --- Status LED (overridden per board in platformio.ini) ------------
#ifndef ORCHARD_PIN_STATUS_LED
#define ORCHARD_PIN_STATUS_LED  48
#endif
