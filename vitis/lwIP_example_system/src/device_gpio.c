/**
 * @file device_gpio.c
 * @brief Board signals on the PS GPIO EMIO bank.
 */

#include "xgpiops.h"
#include "xparameters.h"

#include "device_gpio.h"

static XGpioPs g_gpio; /**< PS GPIO driver instance. */

/**
 * @brief Configure a pin as output and drive a level.
 *
 * @param _pin XGpioPs pin number.
 * @param _state Initial level.
 */
#define GPIO_PIN_CONFIGURE_OUT(_pin, _state)                                                       \
	do {                                                                                       \
		XGpioPs_SetDirectionPin(&g_gpio, (_pin), 1);                                       \
		XGpioPs_SetOutputEnablePin(&g_gpio, (_pin), 1);                                    \
		XGpioPs_WritePin(&g_gpio, (_pin), (_state));                                       \
	} while (0)

/**
 * @brief Configure a pin as input.
 *
 * @param _pin XGpioPs pin number.
 */
#define GPIO_PIN_CONFIGURE_IN(_pin)                                                                \
	do {                                                                                       \
		XGpioPs_SetDirectionPin(&g_gpio, (_pin), 0);                                       \
		XGpioPs_SetOutputEnablePin(&g_gpio, (_pin), 0);                                    \
	} while (0)

/**
 * @brief Configure the direction and initial level of all board pins.
 *
 * @retval 0 Always.
 */
int device_gpio_init(void)
{
	XGpioPs_Config *cfg = XGpioPs_LookupConfig(XPAR_XGPIOPS_0_DEVICE_ID);
	XGpioPs_CfgInitialize(&g_gpio, cfg, cfg->BaseAddr);

	GPIO_PIN_CONFIGURE_OUT(ETH_LED_0_PIN, 0);
	GPIO_PIN_CONFIGURE_OUT(ETH_LED_1_PIN, 0);
	GPIO_PIN_CONFIGURE_OUT(ETH_LED_2_PIN, 0);
	GPIO_PIN_CONFIGURE_OUT(ETH_LED_3_PIN, 0);

	GPIO_PIN_CONFIGURE_OUT(EN_CH_0_PIN, 1);
	GPIO_PIN_CONFIGURE_OUT(EN_CH_1_PIN, 1);
	GPIO_PIN_CONFIGURE_OUT(EN_CH_2_PIN, 1);
	GPIO_PIN_CONFIGURE_OUT(EN_CH_3_PIN, 1);

	GPIO_PIN_CONFIGURE_OUT(RST_PGA_0_PIN, 0);
	GPIO_PIN_CONFIGURE_OUT(RST_PGA_1_PIN, 0);

	GPIO_PIN_CONFIGURE_IN(ADC_GPIO_0_PIN);
	GPIO_PIN_CONFIGURE_IN(ADC_GPIO_1_PIN);
	GPIO_PIN_CONFIGURE_IN(ADC_GPIO_4_PIN);
	GPIO_PIN_CONFIGURE_IN(ADC_GPIO_5_PIN);
	GPIO_PIN_CONFIGURE_IN(ADC_GPIO_6_PIN);
	GPIO_PIN_CONFIGURE_IN(ADC_GPIO_7_PIN);

	GPIO_PIN_CONFIGURE_OUT(EN_A_PWR_PIN, 1);

	GPIO_PIN_CONFIGURE_OUT(ADC_START_PIN, 1);
	GPIO_PIN_CONFIGURE_OUT(ADC_RESET_PIN, 0);

	GPIO_PIN_CONFIGURE_IN(MIC_IN_0_PIN);
	GPIO_PIN_CONFIGURE_IN(MIC_IN_1_PIN);
	GPIO_PIN_CONFIGURE_IN(MIC_IN_2_PIN);
	GPIO_PIN_CONFIGURE_IN(MIC_IN_3_PIN);

	GPIO_PIN_CONFIGURE_IN(ADC_ERROR_PIN);

	GPIO_PIN_CONFIGURE_OUT(USER_LED_0_PIN, 0);
	GPIO_PIN_CONFIGURE_OUT(USER_LED_1_PIN, 0);
	GPIO_PIN_CONFIGURE_OUT(USER_LED_2_PIN, 0);
	GPIO_PIN_CONFIGURE_OUT(USER_LED_3_PIN, 0);

	return 0;
}

/**
 * @brief Drive an output pin.
 *
 * @param pin XGpioPs pin number.
 * @param state Level to drive, 0 or 1.
 *
 * @retval 0 Always.
 */
int device_gpio_write_pin(uint32_t pin, uint8_t state)
{
	XGpioPs_WritePin(&g_gpio, pin, state);
	return 0;
}

/**
 * @brief Read a pin.
 *
 * @param pin XGpioPs pin number.
 *
 * @return Pin level, 0 or 1.
 */
int device_gpio_read_pin(uint32_t pin)
{
	return XGpioPs_ReadPin(&g_gpio, pin);
}
