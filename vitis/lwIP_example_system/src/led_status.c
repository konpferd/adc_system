#include "FreeRTOS.h"
#include "task.h"
#include "lwip/netif.h"
#include "xparameters.h"
#include "xemacps_hw.h"
#include "xil_io.h"
#include "led_status.h"
#include "device_gpio.h"
#include "lwip/stats.h"

#define LED_POLL_MS       20
#define LED_HOLD_TICKS     3            /* keep a short burst visible ~60 ms */
#define GEM_BASE          XPAR_XEMACPS_0_BASEADDR

/* Drive one anti-parallel LED pair: +1 = colour A, -1 = colour B, 0 = off. */
static void led_pair(uint32_t pin_a, uint32_t pin_b, int state)
{
    device_gpio_write_pin(pin_a, state > 0);
    device_gpio_write_pin(pin_b, state < 0);
}

void eth_led_status_thread(void *p)
{
    (void)p;
    uint8_t tx_hold = 0, rx_hold = 0, toggle = 0;

    /* Activity comes from lwIP's software frame counters (incremented by the
     * Xilinx adapter in low_level_output / xemacpsif_input). Never read the
     * GEM statistics registers here: they are clear-on-read and the driver's
     * RX-hang workaround (resetrx_on_no_rxdata) depends on XEMACPS_RXCNT_OFFSET. */
    u32_t last_tx = lwip_stats.link.xmit;
    u32_t last_rx = lwip_stats.link.recv;

    while (1) {
        /* ---- link + speed */
        /* netif_default is set by netif_set_default() in main.c */
        int link = netif_default ? netif_is_link_up(netif_default) : 0;
        int link_state = 0;
        if (link) {
            uint32_t cfg = Xil_In32(GEM_BASE + XEMACPS_NWCFG_OFFSET);   /* config register: read-only use is safe */
            link_state = (cfg & XEMACPS_NWCFG_1000_MASK) ? +1 : -1;
        }
        led_pair(ETH_LED_0_PIN, ETH_LED_1_PIN, link_state);

        /* ---- activity: any frame sent/received since the last poll */
        u32_t tx = lwip_stats.link.xmit;
        u32_t rx = lwip_stats.link.recv;
        if (tx != last_tx) tx_hold = LED_HOLD_TICKS;
        if (rx != last_rx) rx_hold = LED_HOLD_TICKS;
        last_tx = tx;
        last_rx = rx;

        int act = 0;
        if (tx_hold && rx_hold) act = (toggle ^= 1) ? +1 : -1;
        else if (tx_hold)       act = +1;
        else if (rx_hold)       act = -1;
        led_pair(ETH_LED_2_PIN, ETH_LED_3_PIN, link ? act : 0);

        if (tx_hold) tx_hold--;
        if (rx_hold) rx_hold--;

        device_gpio_write_pin(USER_LED_0_PIN, device_gpio_read_pin(MIC_IN_0_PIN));
        device_gpio_write_pin(USER_LED_1_PIN, device_gpio_read_pin(MIC_IN_1_PIN));
        device_gpio_write_pin(USER_LED_2_PIN, device_gpio_read_pin(MIC_IN_2_PIN));
        device_gpio_write_pin(USER_LED_3_PIN, device_gpio_read_pin(MIC_IN_3_PIN));

        vTaskDelay(pdMS_TO_TICKS(LED_POLL_MS));
    }
}
