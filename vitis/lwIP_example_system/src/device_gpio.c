#include "xgpiops.h"
#include "xparameters.h"

#include "device_gpio.h"

static XGpioPs g_gpio;

#define GPIO_PIN_CONFIGURE_OUT(_pin, _state)      		\
    do {                                          		\
        XGpioPs_SetDirectionPin(&g_gpio, (_pin), 1); 	\
        XGpioPs_SetOutputEnablePin(&g_gpio, (_pin), 1); \
        XGpioPs_WritePin(&g_gpio, (_pin), (_state)); 	\
    } while (0)

#define GPIO_PIN_CONFIGURE_IN(_pin)               		\
    do {                                          		\
        XGpioPs_SetDirectionPin(&g_gpio, (_pin), 0); 	\
        XGpioPs_SetOutputEnablePin(&g_gpio, (_pin), 0); \
    } while (0)

int device_gpio_init(void){
	XGpioPs_Config *cfg = XGpioPs_LookupConfig(XPAR_XGPIOPS_0_DEVICE_ID);
	XGpioPs_CfgInitialize(&g_gpio, cfg, cfg->BaseAddr);

	GPIO_PIN_CONFIGURE_OUT(ETH_LED_0_PIN, 0);
	GPIO_PIN_CONFIGURE_OUT(ETH_LED_1_PIN, 0);
	GPIO_PIN_CONFIGURE_OUT(ETH_LED_2_PIN, 0);
	GPIO_PIN_CONFIGURE_OUT(ETH_LED_3_PIN, 0);

	GPIO_PIN_CONFIGURE_OUT(EN_CH_0_PIN, 0);
	GPIO_PIN_CONFIGURE_OUT(EN_CH_1_PIN, 0);
	GPIO_PIN_CONFIGURE_OUT(EN_CH_2_PIN, 0);
	GPIO_PIN_CONFIGURE_OUT(EN_CH_3_PIN, 0);

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
	GPIO_PIN_CONFIGURE_IN(MIC_IN_0_PIN);
	GPIO_PIN_CONFIGURE_IN(MIC_IN_0_PIN);
	GPIO_PIN_CONFIGURE_IN(MIC_IN_0_PIN);

	return 0;
}

int device_gpio_write_pin(uint32_t pin, uint8_t state){
	XGpioPs_WritePin(&g_gpio, pin, state);
	return 0;
}

int device_gpio_read_pin(uint32_t pin){
	return XGpioPs_ReadPin(&g_gpio, pin);
}
