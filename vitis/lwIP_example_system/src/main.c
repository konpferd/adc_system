/**
 * @file main.c
 * @brief Start-up: lwIP, board hardware and the server tasks.
 *
 * Network settings are static: 192.168.1.10/24, gateway 192.168.1.1.
 */

#include <stdio.h>
#include "xparameters.h"
#include "netif/xadapter.h"
#include "platform_config.h"
#include "xil_printf.h"

#include "console.h"
#include "adc_stream.h"
#include "led_status.h"
#include "xscugic.h"
#include "adc_hw.h"
#include "device_gpio.h"
#include "ads127l14_spi.h"
#include "pga_spi.h"
#include "sleep.h"
#include "os_delay.h"
#include "device_xadc.h"

extern XScuGic xInterruptController;

int main_thread();

void lwip_init();

#define THREAD_STACKSIZE  1024  /**< Base task stack size, in words. */
#define STARTUP_SETTLE_US 10000 /**< Delay before the servers start, in us. */

static struct netif server_netif;            /**< lwIP interface of GEM0. */
struct console_init_report main_init_report; /**< Start-up results for the console. */

/**
 * @brief Print a label and an IPv4 address.
 *
 * @param msg Label.
 * @param ip Address.
 */
void print_ip(char *msg, ip_addr_t *ip)
{
	xil_printf(msg);
	xil_printf("%d.%d.%d.%d\n\r", ip4_addr1(ip), ip4_addr2(ip), ip4_addr3(ip), ip4_addr4(ip));
}

/**
 * @brief Print the address, netmask and gateway.
 *
 * @param ip Address.
 * @param mask Netmask.
 * @param gw Gateway.
 */
void print_ip_settings(ip_addr_t *ip, ip_addr_t *mask, ip_addr_t *gw)
{
	print_ip("Board IP: ", ip);
	print_ip("Netmask : ", mask);
	print_ip("Gateway : ", gw);
}

/**
 * @brief Create the start-up task and run the scheduler.
 *
 * @return Does not return.
 */
int main()
{
	sys_thread_new("main_thrd", (void (*)(void *))main_thread, 0, THREAD_STACKSIZE,
		       DEFAULT_THREAD_PRIO);
	vTaskStartScheduler();
	while (1) {
	}
	return 0;
}

/**
 * @brief Bring up the network interface and the hardware, then start the servers.
 *
 * User LEDs 0 to 3 light up as the GPIO, DMA, ADC and PGA steps complete.
 *
 * @param p Unused.
 */
void network_thread(void *p)
{
	struct netif *netif;

	unsigned char mac_ethernet_address[] = {0x02, 0x00, 0x00, 0xAD, 0xC0, 0x01};
	ip_addr_t ipaddr, netmask, gw;

	netif = &server_netif;

	xil_printf("\r\n\r\n");
	xil_printf("-----lwIP ADC Prototype Application ------\r\n");

	IP4_ADDR(&ipaddr, 192, 168, 1, 10);
	IP4_ADDR(&netmask, 255, 255, 255, 0);
	IP4_ADDR(&gw, 192, 168, 1, 1);

	print_ip_settings(&ipaddr, &netmask, &gw);

	if (!xemac_add(netif, &ipaddr, &netmask, &gw, mac_ethernet_address,
		       PLATFORM_EMAC_BASEADDR)) {
		xil_printf("Error adding N/W interface\r\n");
		return;
	}

	netif_set_default(netif);

	netif_set_up(netif);

	sys_thread_new("xemacif_input_thread", (void (*)(void *))xemacif_input_thread, netif,
		       THREAD_STACKSIZE, DEFAULT_THREAD_PRIO);

	int init_res;

	xil_printf("Initializing GPIO pins ...");
	init_res = device_gpio_init();
	main_init_report.gpio_init_res = init_res;
	if (init_res == 0) {
		xil_printf(" Success\r\n");
	} else {
		xil_printf(" Fail [ERR: %02X]\r\n", init_res);
	}
	device_gpio_write_pin(USER_LED_0_PIN, 1);
	vTaskDelay(100);

	xil_printf("Initializing ADC DMA ...");
	init_res = adc_hw_init(&xInterruptController);
	main_init_report.dma_init_res = init_res;
	if (init_res == XST_SUCCESS) {
		xil_printf(" Success\r\n");
	} else {
		xil_printf("ADC DMA FATAL ERROR [ERR: %02X]\r\n", init_res);
	}
	device_gpio_write_pin(USER_LED_1_PIN, 1);
	vTaskDelay(100);

	xil_printf("Initializing ADS127l14 SPI communication...");
	init_res = ads127l14_spi_init();
	main_init_report.ads127l14_init_res = init_res;
	if (init_res == 0) {
		xil_printf(" Success\r\n");
	} else {
		xil_printf(" Fail [ERR: %02X]\r\n", init_res);
	}
	device_gpio_write_pin(USER_LED_2_PIN, 1);
	vTaskDelay(100);

	xil_printf("Initializing PGA SPI communication...");
	init_res = pga_spi_init();
	main_init_report.pga_init_res = init_res;
	if (init_res == 0) {
		xil_printf(" Success\r\n");
	} else {
		xil_printf(" Fail [ERR: %02X]\r\n", init_res);
	}
	device_gpio_write_pin(USER_LED_3_PIN, 1);
	vTaskDelay(100);

	xil_printf("Initializing XADC system...");
	init_res = device_xadc_init();
	main_init_report.xadc_init_res = init_res;
	if (init_res == 0) {
		xil_printf(" Success\r\n");
	} else {
		xil_printf(" Fail [ERR: %02X]\r\n", init_res);
	}

	init_res = ads127l14_spi_system_config();
	main_init_report.ads127l14_config_res = init_res;
	if (init_res != 0) {
		xil_printf("ADC configuration Fail [ERR: %d]\r\n", init_res);
	}

	xil_printf("\r\n");
	xil_printf("%20s %6s %s\r\n", "Server", "Port", "Connect With..");
	xil_printf("%20s %6s %s\r\n", "--------------------", "------", "--------------------");

	xil_printf("\r\n");
	xil_printf("ADC streaming server : port %d\r\n", ADC_STREAM_PORT);
	xil_printf("Console server       : port %d\r\n", CONSOLE_PORT);
	xil_printf("\r\n");

	os_delay_us(STARTUP_SETTLE_US);

	if (main_init_report.dma_init_res == XST_SUCCESS) {
		sys_thread_new("adc_stream", adc_stream_thread, 0, THREAD_STACKSIZE * 4, 3);
	}

	sys_thread_new("console", console_thread, 0, THREAD_STACKSIZE * 2, 2);

	sys_thread_new("eth_led_stat", eth_led_status_thread, 0, THREAD_STACKSIZE, 1);

	vTaskDelete(NULL);
	return;
}

/**
 * @brief Initialise lwIP and start the network thread.
 *
 * @return 0; the task deletes itself first.
 */
int main_thread()
{
	lwip_init();

	sys_thread_new("NW_THRD", network_thread, NULL, THREAD_STACKSIZE * 2, DEFAULT_THREAD_PRIO);

	vTaskDelete(NULL);
	return 0;
}
