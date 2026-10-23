#ifndef DEVICE_GPIO_H
#define DEVICE_GPIO_H
#include <stdint.h>

/* EMIO[3:0] on GPIO_0 == XGpioPs pin numbers 54..57 (bank2, EMIO bank).
 * Bank0 = MIO[31:0]  -> pins 0-31
 * Bank1 = MIO[53:32] -> pins 32-53
 * Bank2 = EMIO[31:0] -> pins 54-85   <-- this is where EMIO[3:0] live
 * Bank3 = EMIO[63:32]-> pins 86-117
 */
#define EMIO_BASE_PIN	54

// ETH LEDs
#define ETH_LED_0_PIN	(EMIO_BASE_PIN + 0)  /* EMIO0	| OUT	: ETH LED 11 - Pair L */
#define ETH_LED_1_PIN	(EMIO_BASE_PIN + 1)  /* EMIO1	| OUT	: ETH LED 12 - Pair L */
#define ETH_LED_2_PIN	(EMIO_BASE_PIN + 2)  /* EMIO2	| OUT	: ETH LED 21 - Pair R */
#define ETH_LED_3_PIN	(EMIO_BASE_PIN + 3)  /* EMIO3	| OUT	: ETH LED 22 - Pair R */

// Analog channel enable
#define EN_CH_0_PIN		(EMIO_BASE_PIN + 4)  /* EMIO4	| OUT	: Enable analog channel 0 */
#define EN_CH_1_PIN		(EMIO_BASE_PIN + 5)  /* EMIO5	| OUT	: Enable analog channel 1 */
#define EN_CH_2_PIN		(EMIO_BASE_PIN + 6)  /* EMIO6	| OUT	: Enable analog channel 2 */
#define EN_CH_3_PIN		(EMIO_BASE_PIN + 7)  /* EMIO7	| OUT	: Enable analog channel 3 */

// Reset Programmable Gain Amplifiers
#define RST_PGA_0_PIN	(EMIO_BASE_PIN + 8)  /* EMIO8	| OUT	: Reset PGA 0 */
#define RST_PGA_1_PIN	(EMIO_BASE_PIN + 9)  /* EMIO9	| OUT	: Reset PGA 1 */

// ADC GPIO pins
#define ADC_GPIO_0_PIN	(EMIO_BASE_PIN + 10) /* EMIO10	| GPIO	: ADC GPIO pin 0 */
#define ADC_GPIO_1_PIN	(EMIO_BASE_PIN + 11) /* EMIO11	| GPIO	: ADC GPIO pin 1 */
#define ADC_GPIO_4_PIN	(EMIO_BASE_PIN + 12) /* EMIO12	| GPIO	: ADC GPIO pin 4 */
#define ADC_GPIO_5_PIN	(EMIO_BASE_PIN + 13) /* EMIO13	| GPIO	: ADC GPIO pin 5 */
#define ADC_GPIO_6_PIN	(EMIO_BASE_PIN + 14) /* EMIO14	| GPIO	: ADC GPIO pin 6 */
#define ADC_GPIO_7_PIN	(EMIO_BASE_PIN + 15) /* EMIO15	| GPIO	: ADC GPIO pin 7 */

// Enable Analog Power
#define EN_A_PWR_PIN	(EMIO_BASE_PIN + 16) /* EMIO16	| OUT	: Enable analog power 5V */

// ADC control pins
#define ADC_START_PIN	(EMIO_BASE_PIN + 17)  /* EMIO17	| OUT	: ADC start conversion */
#define ADC_RESET_PIN	(EMIO_BASE_PIN + 18)  /* EMIO18	| OUT	: Reset ADC device */

// MIC connected sense
#define MIC_IN_0_PIN	(EMIO_BASE_PIN + 19) /* EMIO19	| IN	: Sense MIC connected 0 */
#define MIC_IN_1_PIN	(EMIO_BASE_PIN + 20) /* EMIO20	| IN	: Sense MIC connected 1 */
#define MIC_IN_2_PIN	(EMIO_BASE_PIN + 21) /* EMIO21	| IN	: Sense MIC connected 2 */
#define MIC_IN_3_PIN	(EMIO_BASE_PIN + 22) /* EMIO22	| IN	: Sense MIC connected 3 */

// ADC error pin
#define ADC_ERROR_PIN	(EMIO_BASE_PIN + 23) /* EMIO23	| IN	: Check the error state of ADC */

// User LEDs
#define USER_LED_0_PIN	(EMIO_BASE_PIN + 24) /* EMIO24	| OUT	: User LED 0 */
#define USER_LED_1_PIN	(EMIO_BASE_PIN + 25) /* EMIO25	| OUT	: User LED 1 */
#define USER_LED_2_PIN	(EMIO_BASE_PIN + 26) /* EMIO26	| OUT	: User LED 2 */
#define USER_LED_3_PIN	(EMIO_BASE_PIN + 27) /* EMIO27	| OUT	: User LED 3 */

int device_gpio_init(void);

int device_gpio_write_pin(uint32_t pin, uint8_t state);
int device_gpio_read_pin(uint32_t pin);

#endif	/* DEVICE_GPIO_H */
