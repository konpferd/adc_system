#include <string.h>
#include <stdlib.h>
#include "lwip/sockets.h"
#include "xil_printf.h"
#include "FreeRTOS.h"
#include "task.h"
#include "semphr.h"
#include "adc_hw.h"
#include "console.h"
#include "led_status.h"
#include "ads127l14_spi.h"
#include "device_gpio.h"
#include "sleep.h"

#define THREAD_STACKSIZE 2048

static SemaphoreHandle_t g_cfg_mutex;

static void send_str(int sd, const char *s)
{
    lwip_write(sd, s, strlen(s));
    eth_led_status_note_activity();
}

/* Very small line-based tokenizer; replace with something more robust
 * once the real command set is finalized. */
static void handle_line(int sd, char *line)
{
    char *cmd = strtok(line, " \t\r\n");
    if (!cmd) return;

    xSemaphoreTake(g_cfg_mutex, portMAX_DELAY);

    if (!strcasecmp(cmd, "HELP")) {
        xSemaphoreGive(g_cfg_mutex);
        send_str(sd, "PRINT HELP INFO - PROJECT NOT COMPLETED - PROTOTYPE PHASE");
        return;
    } else if (!strcasecmp(cmd, "ANALOG_PWR")) {
    	char *sub = strtok(NULL, " \t\r\n");
		if (sub && !strcasecmp(sub, "ON")) {
			device_gpio_write_pin(EN_A_PWR_PIN, 0);
			xSemaphoreGive(g_cfg_mutex);
			send_str(sd, "Analog PWR ON\r\n");
			return;
		} else if (sub && !strcasecmp(sub, "OFF")) {
			device_gpio_write_pin(EN_A_PWR_PIN, 1);
			xSemaphoreGive(g_cfg_mutex);
			send_str(sd, "Analog PWR OFF\r\n");
			return;
		}
		xSemaphoreGive(g_cfg_mutex);
		send_str(sd, "ERR unknown ANALOG_PWR sub-command\r\n");
		return;
    } else if (!strcasecmp(cmd, "START")) {
        xSemaphoreGive(g_cfg_mutex);
        send_str(sd, "OK\r\n");
        return;
    } else if (!strcasecmp(cmd, "STOP")) {
        xSemaphoreGive(g_cfg_mutex);
        send_str(sd, "OK\r\n");
        return;
    } else if (!strcasecmp(cmd, "ADC")) {
        char *sub = strtok(NULL, " \t\r\n");
        if (sub && !strcasecmp(sub, "READ_ID")) {
            uint8_t device_id = 0, rev_id = 0;
            uint8_t spi_err;
            char buf[64];

            spi_err = ads127l14_reg_read(0x00, &device_id);
            if(spi_err){
            	snprintf(buf, sizeof(buf), "ERR SPI transaction failed [ERR: 0x%02X]\r\n", spi_err);
				xSemaphoreGive(g_cfg_mutex);
				send_str(sd, buf);
				return;
            }

            spi_err = ads127l14_reg_read(0x01, &rev_id);
            if(spi_err){
				snprintf(buf, sizeof(buf), "ERR SPI transaction failed [ERR: 0x%02X]\r\n", spi_err);
				xSemaphoreGive(g_cfg_mutex);
				send_str(sd, buf);
				return;
			}

            snprintf(buf, sizeof(buf), "DEVICE_ID=0x%02X REV_ID=0x%02X\r\n", device_id, rev_id);

            xSemaphoreGive(g_cfg_mutex);
            send_str(sd, buf);
            return;
        } else if(sub && !strcasecmp(sub, "RESET")){
        	adc_hw_disable_stream();
        	device_gpio_write_pin(ADC_RESET_PIN, 0);
        	usleep(100);
        	device_gpio_write_pin(ADC_RESET_PIN, 1);
        	adc_hw_enable_stream();
        	xSemaphoreGive(g_cfg_mutex);
        	send_str(sd, "ADC reset done\r\n");
			return;
        } else if(sub && !strcasecmp(sub, "START")){
        	device_gpio_write_pin(ADC_START_PIN, 0);
        	usleep(250);
        	device_gpio_write_pin(ADC_START_PIN, 1);
        	xSemaphoreGive(g_cfg_mutex);
        	adc_hw_enable_stream();
        	send_str(sd, "ADC sampling started ...\r\n");
			return;
        } else if(sub && !strcasecmp(sub, "STOP")){
        	device_gpio_write_pin(ADC_START_PIN, 0);
        	xSemaphoreGive(g_cfg_mutex);
        	adc_hw_disable_stream();
        	send_str(sd, "ADC sampling stopped\r\n");
			return;
        } else if(sub && !strcasecmp(sub, "INIT")){
        	ads127114_spi_system_config();
			xSemaphoreGive(g_cfg_mutex);
			send_str(sd, "ADC initialized\r\n");
			return;
        } else {
			xSemaphoreGive(g_cfg_mutex);
			send_str(sd, "ERR unknown ADC sub-command\r\n");
			return;
        }
    }else {
        xSemaphoreGive(g_cfg_mutex);
        send_str(sd, "ERR unknown command\r\n");
        return;
    }
    xSemaphoreGive(g_cfg_mutex);
}

/* One thread per connected console client -- direct analogue of
 * process_echo_request(), but line-buffered and command-parsing.
 *
 * IMPORTANT: don't assume every client sends a trailing '\n'. Generic raw
 * TCP test tools frequently send exactly the bytes you typed with no line
 * ending at all. Two defenses here:
 *   1. Treat '\r' OR '\n' as a terminator (strpbrk, not strchr on '\n' only).
 *   2. Use select() with a timeout to detect an idle gap after partial data
 *      has arrived, and flush whatever's buffered as a command anyway.
 *
 * NOTE: this deliberately uses select() with a struct timeval instead of
 * SO_RCVTIMEO. SO_RCVTIMEO on lwIP sockets requires LWIP_SO_SNDRCVTIMEO
 * (or LWIP_SO_RCVTIMEO, depending on lwIP version) to be enabled in
 * lwipopts.h -- it is NOT enabled in the stock Xilinx lwIP echo-example
 * BSP config. If it's off, setsockopt(SO_RCVTIMEO) silently does nothing
 * and lwip_read() blocks forever, which looks exactly like "no response,
 * ever" from the client's point of view. select() has no such dependency:
 * it only requires LWIP_SOCKET (already required for the rest of this
 * file to compile), so it works regardless of that BSP setting. */
#define IDLE_FLUSH_MS 200

static void console_client_thread(void *p)
{
    int sd = *(int *)p;
    char buf[256];
    int fill = 0;
    int n;

    send_str(sd, "ADC console ready. Type HELP.\r\n> ");
    xil_printf("console: client accepted, sd=%d\r\n", sd);

    while (1) {
        fd_set rfds;
        struct timeval tv;
        FD_ZERO(&rfds);
        FD_SET(sd, &rfds);
        tv.tv_sec = 0;
        tv.tv_usec = IDLE_FLUSH_MS * 1000;

        int sel = lwip_select(sd + 1, &rfds, NULL, NULL, &tv);

        if (sel < 0) break; /* socket error */

        if (sel == 0) {
            /* Timed out with no new data. If we have a partial, unterminated
             * command sitting in the buffer, treat it as complete now. */
            if (fill > 0) {
                buf[fill] = 0;
                handle_line(sd, buf);
                send_str(sd, "> ");
                fill = 0;
            }
            continue;
        }

        /* sel > 0: socket is readable */
        n = lwip_read(sd, buf + fill, sizeof(buf) - fill - 1);
        if (n <= 0) break; /* 0 = orderly close, <0 = real error */
        eth_led_status_note_activity();

        fill += n;
        buf[fill] = 0;

        char *term;
        char *start = buf;
        while ((term = strpbrk(start, "\r\n")) != NULL) {
            *term = 0;
            if (term != start) { /* skip empty lines from \r\n pairs */
                handle_line(sd, start);
                send_str(sd, "> ");
            }
            start = term + 1;
        }
        /* shift any partial line to the front of buf */
        int remaining = fill - (start - buf);
        memmove(buf, start, remaining);
        fill = remaining;
        if (fill >= (int)sizeof(buf) - 1) fill = 0; /* overflow guard */
    }

    close(sd);
    vTaskDelete(NULL);
}

void console_thread(void *p)
{
    int listen_sd;
    struct sockaddr_in addr, remote;
    socklen_t remote_len;
    static int client_sd[CONSOLE_MAX_CLIENTS];

    g_cfg_mutex = xSemaphoreCreateMutex();

    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(CONSOLE_PORT);
    addr.sin_addr.s_addr = INADDR_ANY;

    listen_sd = lwip_socket(AF_INET, SOCK_STREAM, 0);
    lwip_bind(listen_sd, (struct sockaddr *)&addr, sizeof(addr));
    lwip_listen(listen_sd, CONSOLE_MAX_CLIENTS);

    xil_printf("Console server listening on port %d\r\n", CONSOLE_PORT);

    int idx = 0;
    while (1) {
        remote_len = sizeof(remote);
        client_sd[idx] = lwip_accept(listen_sd, (struct sockaddr *)&remote, &remote_len);
        if (client_sd[idx] > 0) {
            sys_thread_new("console_c", console_client_thread,
                            (void *)&client_sd[idx],
                            THREAD_STACKSIZE, DEFAULT_THREAD_PRIO);
            idx = (idx + 1) % CONSOLE_MAX_CLIENTS;
        }
    }
}
