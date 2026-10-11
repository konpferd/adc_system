#include "FreeRTOS.h"
#include "task.h"
#include "lwip/netif.h"
#include "xgpiops.h"
#include "xparameters.h"
#include "led_status.h"
#include "device_gpio.h"

#define LED_POLL_MS        20   /* thread tick */
#define LED_MIN_ON_MS       60  /* minimum visible pulse width */
#define LED_MIN_ON_TICKS   (LED_MIN_ON_MS / LED_POLL_MS)

extern struct netif *echo_netif;   /* already exported from main.c */

static volatile uint8_t eth_act_pending;

void eth_led_status_note_activity(void) { eth_act_pending = 1; }

void eth_led_status_thread(void *p)
{
	uint16_t on_ticks;

    device_gpio_write_pin(ETH_LED_1_PIN, 0);
    device_gpio_write_pin(ETH_LED_2_PIN, 0);

    xil_printf("ETH LEDs thread ready\r\n");

    while (1) {
        int link_up = echo_netif ? netif_is_link_up(echo_netif) : 0;
        device_gpio_write_pin(ETH_LED_0_PIN, link_up ? 1 : 0);

        if (eth_act_pending) {
        	eth_act_pending = 0;
            on_ticks = LED_MIN_ON_TICKS;
        }

        device_gpio_write_pin(ETH_LED_3_PIN, on_ticks > 0 ? 1 : 0);

        if (on_ticks > 0) on_ticks--;

        vTaskDelay(pdMS_TO_TICKS(LED_POLL_MS));
    }
}
