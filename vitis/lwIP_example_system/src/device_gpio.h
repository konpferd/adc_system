/**
 * @file device_gpio.h
 * @brief Board signals on the PS GPIO EMIO bank.
 *
 * @defgroup device_gpio Board GPIO
 * @{
 */

#ifndef DEVICE_GPIO_H
#define DEVICE_GPIO_H
#include <stdint.h>

#define EMIO_BASE_PIN 54 /**< XGpioPs pin number of EMIO 0 (bank 2). */

#define ETH_LED_0_PIN (EMIO_BASE_PIN + 0) /**< EMIO0, out: Ethernet LED 11, left pair. */
#define ETH_LED_1_PIN (EMIO_BASE_PIN + 1) /**< EMIO1, out: Ethernet LED 12, left pair. */
#define ETH_LED_2_PIN (EMIO_BASE_PIN + 2) /**< EMIO2, out: Ethernet LED 21, right pair. */
#define ETH_LED_3_PIN (EMIO_BASE_PIN + 3) /**< EMIO3, out: Ethernet LED 22, right pair. */

#define EN_CH_0_PIN (EMIO_BASE_PIN + 4) /**< EMIO4, out: enable analog channel 0. */
#define EN_CH_1_PIN (EMIO_BASE_PIN + 5) /**< EMIO5, out: enable analog channel 1. */
#define EN_CH_2_PIN (EMIO_BASE_PIN + 6) /**< EMIO6, out: enable analog channel 2. */
#define EN_CH_3_PIN (EMIO_BASE_PIN + 7) /**< EMIO7, out: enable analog channel 3. */

#define RST_PGA_0_PIN (EMIO_BASE_PIN + 8) /**< EMIO8, out: reset PGA 0. */
#define RST_PGA_1_PIN (EMIO_BASE_PIN + 9) /**< EMIO9, out: reset PGA 1. */

#define ADC_GPIO_0_PIN (EMIO_BASE_PIN + 10) /**< EMIO10, in/out: ADC GPIO 0. */
#define ADC_GPIO_1_PIN (EMIO_BASE_PIN + 11) /**< EMIO11, in/out: ADC GPIO 1. */
#define ADC_GPIO_4_PIN (EMIO_BASE_PIN + 12) /**< EMIO12, in/out: ADC GPIO 4. */
#define ADC_GPIO_5_PIN (EMIO_BASE_PIN + 13) /**< EMIO13, in/out: ADC GPIO 5. */
#define ADC_GPIO_6_PIN (EMIO_BASE_PIN + 14) /**< EMIO14, in/out: ADC GPIO 6. */
#define ADC_GPIO_7_PIN (EMIO_BASE_PIN + 15) /**< EMIO15, in/out: ADC GPIO 7. */

#define EN_A_PWR_PIN (EMIO_BASE_PIN + 16) /**< EMIO16, out: analog 5 V supply, active low. */

#define ADC_START_PIN (EMIO_BASE_PIN + 17) /**< EMIO17, out: ADC START. */
#define ADC_RESET_PIN (EMIO_BASE_PIN + 18) /**< EMIO18, out: ADC RESET, active low. */

#define MIC_IN_0_PIN (EMIO_BASE_PIN + 19) /**< EMIO19, in: microphone 0 connected. */
#define MIC_IN_1_PIN (EMIO_BASE_PIN + 20) /**< EMIO20, in: microphone 1 connected. */
#define MIC_IN_2_PIN (EMIO_BASE_PIN + 21) /**< EMIO21, in: microphone 2 connected. */
#define MIC_IN_3_PIN (EMIO_BASE_PIN + 22) /**< EMIO22, in: microphone 3 connected. */

#define ADC_ERROR_PIN (EMIO_BASE_PIN + 23) /**< EMIO23, in: ADC error state. */

#define USER_LED_0_PIN (EMIO_BASE_PIN + 24) /**< EMIO24, out: user LED 0. */
#define USER_LED_1_PIN (EMIO_BASE_PIN + 25) /**< EMIO25, out: user LED 1. */
#define USER_LED_2_PIN (EMIO_BASE_PIN + 26) /**< EMIO26, out: user LED 2. */
#define USER_LED_3_PIN (EMIO_BASE_PIN + 27) /**< EMIO27, out: user LED 3. */

/**
 * @brief Configure the direction and initial level of all board pins.
 *
 * @retval 0 Always.
 */
int device_gpio_init(void);

/**
 * @brief Drive an output pin.
 *
 * @param pin XGpioPs pin number.
 * @param state Level to drive, 0 or 1.
 *
 * @retval 0 Always.
 */
int device_gpio_write_pin(uint32_t pin, uint8_t state);

/**
 * @brief Read a pin.
 *
 * @param pin XGpioPs pin number.
 *
 * @return Pin level, 0 or 1.
 */
int device_gpio_read_pin(uint32_t pin);

/** @} */

#endif /* DEVICE_GPIO_H */
