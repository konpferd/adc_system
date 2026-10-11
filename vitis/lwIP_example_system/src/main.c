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

extern XScuGic xInterruptController;

int main_thread();
void print_echo_app_header();
void echo_application_thread(void *);

void lwip_init();

#define THREAD_STACKSIZE 1024

static struct netif server_netif;
struct netif *echo_netif;

void print_ip(char *msg, ip_addr_t *ip)
{
	xil_printf(msg);
	xil_printf("%d.%d.%d.%d\n\r", ip4_addr1(ip), ip4_addr2(ip),
			ip4_addr3(ip), ip4_addr4(ip));
}

void print_ip_settings(ip_addr_t *ip, ip_addr_t *mask, ip_addr_t *gw)
{

	print_ip("Board IP: ", ip);
	print_ip("Netmask : ", mask);
	print_ip("Gateway : ", gw);
}

int main()
{
	sys_thread_new("main_thrd", (void(*)(void*))main_thread, 0,
	                THREAD_STACKSIZE,
	                DEFAULT_THREAD_PRIO);
	vTaskStartScheduler();
	while(1);
	return 0;
}

void network_thread(void *p)
{
    struct netif *netif;
    /* the mac address of the board. this should be unique per board */
    unsigned char mac_ethernet_address[] = { 0x00, 0x0a, 0x35, 0x00, 0x01, 0x02 };
    ip_addr_t ipaddr, netmask, gw;

    netif = &server_netif;

    xil_printf("\r\n\r\n");
    xil_printf("-----lwIP ADC Prototype Application ------\r\n");

    /* initialize IP addresses to be used */
    IP4_ADDR(&ipaddr,  192, 168, 1, 10);
    IP4_ADDR(&netmask, 255, 255, 255,  0);
    IP4_ADDR(&gw,      192, 168, 1, 1);

    /* print out IP settings of the board */

    print_ip_settings(&ipaddr, &netmask, &gw);
    /* print all application headers */

    /* Add network interface to the netif_list, and set it as default */
    if (!xemac_add(netif, &ipaddr, &netmask, &gw, mac_ethernet_address, PLATFORM_EMAC_BASEADDR)) {
		xil_printf("Error adding N/W interface\r\n");
		return;
    }

    netif_set_default(netif);

    /* specify that the network if is up */
    netif_set_up(netif);

    /* start packet receive thread - required for lwIP operation */
    sys_thread_new("xemacif_input_thread", (void(*)(void*))xemacif_input_thread, netif,
            THREAD_STACKSIZE,
            DEFAULT_THREAD_PRIO);

    int init_res;

    xil_printf("Initializing ADC DMA ...");
    init_res = adc_hw_init(&xInterruptController);
    if(init_res == XST_SUCCESS)
		xil_printf(" Success\r\n");
	else
		for (;;) {
			xil_printf("ADC DMA FATAL ERROR [ERR: %02X]\r\n", init_res);
			vTaskDelay(pdMS_TO_TICKS(10000));
		}

    xil_printf("Initializing GPIO pins ...");
	init_res = device_gpio_init();
	if(init_res == 0)
		xil_printf(" Success\r\n");
	else
		xil_printf(" Fail [ERR: %02X]\r\n", init_res);

    xil_printf("Initializing ADS127l14 SPI communication...");
    init_res = ads127l14_spi_init();
    if(init_res == 0)
    	xil_printf(" Success\r\n");
    else
    	xil_printf(" Fail [ERR: %02X]\r\n", init_res);

    xil_printf("Initializing PGA SPI communication...");
        init_res = pga_spi_init();
        if(init_res == 0)
        	xil_printf(" Success\r\n");
        else
        	xil_printf(" Fail [ERR: %02X]\r\n", init_res);

    ads127114_spi_system_config();

    xil_printf("\r\n");
    xil_printf("%20s %6s %s\r\n", "Server", "Port", "Connect With..");
    xil_printf("%20s %6s %s\r\n", "--------------------", "------", "--------------------");

	xil_printf("\r\n");
	xil_printf("ADC streaming server : port %d\r\n", ADC_STREAM_PORT);
	xil_printf("Console server        : port %d\r\n", CONSOLE_PORT);
	xil_printf("\r\n");

	usleep(10000);

	sys_thread_new("adc_stream", adc_stream_thread, 0,
		THREAD_STACKSIZE * 4, DEFAULT_THREAD_PRIO + 2);

	sys_thread_new("console", console_thread, 0,
		THREAD_STACKSIZE * 1, DEFAULT_THREAD_PRIO + 1);

	sys_thread_new("eth_led_stat", eth_led_status_thread, 0,
		THREAD_STACKSIZE / 2, DEFAULT_THREAD_PRIO);

	vTaskDelete(NULL);
    return;
}

int main_thread()
{
	/* initialize lwIP before calling sys_thread_new */
    lwip_init();

    /* any thread using lwIP should be created using sys_thread_new */
    sys_thread_new("NW_THRD", network_thread, NULL,
		THREAD_STACKSIZE,
            DEFAULT_THREAD_PRIO);

    vTaskDelete(NULL);
    return 0;
}
