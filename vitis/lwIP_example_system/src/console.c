/**
 * @file console.c
 * @brief TCP command console.
 *
 * Commands are words separated by spaces, case-insensitive, one per line.
 * They form a tree (see ::root_cmds); each client runs in its own task and
 * commands are serialised by ::g_cfg_mutex.
 */

#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <stdbool.h>

#include "lwip/sockets.h"
#include "lwip/sys.h"
#include "xil_printf.h"
#include "FreeRTOS.h"
#include "task.h"
#include "semphr.h"
#include "sleep.h"
#include "os_delay.h"

#include "adc_hw.h"
#include "console.h"
#include "led_status.h"
#include "ads127l14_spi.h"
#include "pga_spi.h"
#include "device_gpio.h"
#include "net_util.h"
#include "adc_stream.h"
#include "device_xadc.h"

#define THREAD_STACKSIZE    4096 /**< Stack of a client task, in words. */
#define IDLE_FLUSH_MS       200  /**< Idle time before a partial line runs. */
#define CONSOLE_RX_BUF_SIZE 256  /**< Receive line buffer size. */
#define CONSOLE_TX_BUF_SIZE 256  /**< Broadcast buffer size. */
#define CONSOLE_MAX_ARGS    8    /**< Maximum words in a command line. */
#define CONSOLE_PROMPT      "> " /**< Prompt sent after every reply. */

#define CONSOLE_OUT_BUF_SIZE    2048 /**< Reply buffer size per client. */
#define CONSOLE_SEND_TIMEOUT_MS 1000 /**< Longest stall when sending a reply. */
#define CONSOLE_CFG_LOCK_MS     2000 /**< Longest wait for the command lock. */

#define ADC_RESET_PULSE_US    100  /**< ADC RESET low time, in us. */
#define ADC_RESET_RECOVERY_US 1000 /**< Wait after RESET before SPI access, in us. */
#define ADC_START_PULSE_US    250  /**< START low pulse that restarts the ADC, in us. */

/** @brief Per-client state. */
typedef struct {
	int sd;      /**< Client socket. */
	char *out;   /**< Reply buffer. */
	int out_len; /**< Bytes queued in the reply buffer. */
	bool dead;   /**< Send failed; the client is closing. */
} console_ctx_t;

/** Reply buffer of each client slot. */
static char g_out_buf[CONSOLE_MAX_CLIENTS][CONSOLE_OUT_BUF_SIZE];

extern struct console_init_report main_init_report;

static SemaphoreHandle_t g_cfg_mutex;     /**< Serialises command execution. */
static SemaphoreHandle_t g_clients_mutex; /**< Protects the client table. */

static int g_clients[CONSOLE_MAX_CLIENTS]; /**< Socket of each slot, -1 if free. */
static volatile int g_client_count;        /**< Number of connected clients. */
static char g_tx_buf[CONSOLE_TX_BUF_SIZE]; /**< Broadcast buffer of console_vprintf(). */

/**
 * @brief Format millidegrees as degrees with three decimals.
 *
 * @param buf Output buffer.
 * @param len Size of @p buf.
 * @param mc Temperature, in millidegrees Celsius.
 *
 * @return @p buf.
 */
static const char *mc_str(char *buf, size_t len, int32_t mc)
{
	int32_t a = mc < 0 ? -mc : mc;
	snprintf(buf, len, "%s%ld.%03ld", mc < 0 ? "-" : "", (long)(a / 1000), (long)(a % 1000));
	return buf;
}

/**
 * @brief Register a client socket in a free slot.
 *
 * @param sd Client socket.
 *
 * @return Slot index, -1 if all slots are taken.
 */
static int client_add(int sd)
{
	int slot = -1;

	xSemaphoreTake(g_clients_mutex, portMAX_DELAY);
	for (int i = 0; i < CONSOLE_MAX_CLIENTS; i++) {
		if (g_clients[i] < 0) {
			g_clients[i] = sd;
			g_client_count++;
			slot = i;
			break;
		}
	}
	xSemaphoreGive(g_clients_mutex);
	return slot;
}

/**
 * @brief Free a client slot.
 *
 * @param slot Slot index.
 */
static void client_remove(int slot)
{
	xSemaphoreTake(g_clients_mutex, portMAX_DELAY);
	if (g_clients[slot] >= 0) {
		g_clients[slot] = -1;
		g_client_count--;
	}
	xSemaphoreGive(g_clients_mutex);
}

/**
 * @brief Print to all connected clients, va_list variant.
 *
 * @param fmt printf-style format string.
 * @param ap Format arguments.
 *
 * @return Number of characters sent; 0 if no client is connected.
 */
int console_vprintf(const char *fmt, va_list ap)
{
	if (g_client_count == 0 || g_clients_mutex == NULL) {
		return 0;
	}

	xSemaphoreTake(g_clients_mutex, portMAX_DELAY);

	int len = 0;
	if (g_client_count > 0) {
		len = vsnprintf(g_tx_buf, sizeof(g_tx_buf), fmt, ap);
		if (len < 0) {
			len = 0;
		} else if (len >= (int)sizeof(g_tx_buf)) {
			len = sizeof(g_tx_buf) - 1;
		}

		if (len > 0) {
			for (int i = 0; i < CONSOLE_MAX_CLIENTS; i++) {
				if (g_clients[i] >= 0) {
					lwip_send(g_clients[i], g_tx_buf, len, MSG_DONTWAIT);
				}
			}
		}
	}

	xSemaphoreGive(g_clients_mutex);
	return len;
}

/**
 * @brief Print to all connected clients.
 *
 * @param fmt printf-style format string.
 * @param ... Format arguments.
 *
 * @return Number of characters sent; 0 if no client is connected.
 */
int console_printf(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	int n = console_vprintf(fmt, ap);
	va_end(ap);
	return n;
}

/**
 * @brief Append formatted text to the reply buffer of a client.
 *
 * Text that does not fit is truncated.
 *
 * @param ctx Client context.
 * @param fmt printf-style format string.
 * @param ... Format arguments.
 */
static void reply(console_ctx_t *ctx, const char *fmt, ...)
{
	int room = CONSOLE_OUT_BUF_SIZE - ctx->out_len;
	if (room <= 1) {
		return;
	}

	va_list ap;
	va_start(ap, fmt);
	int len = vsnprintf(ctx->out + ctx->out_len, (size_t)room, fmt, ap);
	va_end(ap);

	if (len > 0) {
		ctx->out_len += (len < room) ? len : room - 1;
	}
}

/**
 * @brief Send the queued reply; marks the client dead on failure.
 *
 * @param ctx Client context.
 */
static void console_flush(console_ctx_t *ctx)
{
	if (ctx->out_len > 0 && !ctx->dead) {
		if (net_send_all(ctx->sd, ctx->out, (size_t)ctx->out_len,
				 CONSOLE_SEND_TIMEOUT_MS) != 0) {
			ctx->dead = true;
		}
	}
	ctx->out_len = 0;
}

/**
 * @brief Queue a success reply.
 *
 * @param ctx Client context.
 */
#define reply_ok(ctx, ...) reply((ctx), __VA_ARGS__)

/**
 * @brief Queue an error reply.
 *
 * @param ctx Client context.
 */
#define reply_err(ctx, ...) reply((ctx), __VA_ARGS__)

/**
 * @brief Command handler.
 *
 * @param ctx Client context.
 * @param argc Number of arguments after the command words.
 * @param argv Arguments.
 */
typedef void (*cmd_fn_t)(console_ctx_t *ctx, int argc, char **argv);

static void cmd_help(console_ctx_t *ctx, int argc, char **argv);

/**
 * @brief Parse an integer (decimal, 0x hex or 0 octal) and check its range.
 *
 * @param s Text to parse.
 * @param min Lowest accepted value.
 * @param max Highest accepted value.
 * @param out Parsed value.
 *
 * @retval 0 Value parsed and in range.
 * @retval -1 Not a number or out of range.
 */
static int parse_int_range(const char *s, long min, long max, long *out)
{
	char *end;
	long v = strtol(s, &end, 0);

	if (end == s || *end != '\0' || v < min || v > max) {
		return -1;
	}

	*out = v;
	return 0;
}

/**
 * @brief CH_STATUS: report the channel status.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Unused.
 */
static void ch_status(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc;
	(void)argv;
	reply_ok(ctx, "OK Channel status: %d, %d, %d, %d\r\n", 1, 1, 1, 1);
}

/**
 * @brief DEVICE_TEMP: report the die temperature and warning level.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Unused.
 */
static void cmd_device_temp(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc;
	(void)argv;
	int32_t now, min, max;
	char b0[16], b1[16], b2[16];

	if (device_xadc_read_temp_mc(&now, &min, &max)) {
		reply_err(ctx, "ERR XADC not available\r\n");
		return;
	}

	reply_ok(ctx, "OK DEVICE_TEMP=%s MIN=%s MAX=%s DEVICE_WRN_LVL=%d\r\n",
		 mc_str(b0, sizeof b0, now), mc_str(b1, sizeof b1, min), mc_str(b2, sizeof b2, max),
		 DEVICE_TEMP_C_WRN_LVL);
}

/**
 * @brief ANALOG_PWR ON: switch the analog supply on.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Unused.
 */
static void cmd_analog_pwr_on(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc;
	(void)argv;
	device_gpio_write_pin(EN_A_PWR_PIN, 0);
	reply_ok(ctx, "OK Analog PWR ON\r\n");
}

/**
 * @brief ANALOG_PWR OFF: stop sampling and switch the analog supply off.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Unused.
 */
static void cmd_analog_pwr_off(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc;
	(void)argv;
	adc_stream_set_run(false);
	device_gpio_write_pin(ADC_START_PIN, 0);
	device_gpio_write_pin(EN_A_PWR_PIN, 1);
	reply_ok(ctx, "OK Analog PWR OFF\r\n");
}

/**
 * @brief ADC READ_ID: report DEV_ID and REV_ID.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Unused.
 */
static void cmd_adc_read_id(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc;
	(void)argv;
	uint8_t device_id = 0, rev_id = 0;
	int adc_error;

	adc_error = ads127l14_spi_read_device_data(&device_id, &rev_id);
	if (adc_error) {
		reply_err(ctx, "ERR ADC data read fail [ERR: %d]\r\n", adc_error);
		return;
	}

	reply_ok(ctx, "OK DEVICE_ID=0x%02X REV_ID=0x%02X\r\n", device_id, rev_id);
}

/**
 * @brief ADC RESET: reset and reconfigure the ADC, keeping the run state.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Unused.
 */
static void cmd_adc_reset(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc;
	(void)argv;
	bool was_running = adc_stream_get_run();

	adc_stream_set_run(false);
	device_gpio_write_pin(ADC_START_PIN, 0);
	device_gpio_write_pin(ADC_RESET_PIN, 0);
	os_delay_us(ADC_RESET_PULSE_US);
	device_gpio_write_pin(ADC_RESET_PIN, 1);
	os_delay_us(ADC_RESET_RECOVERY_US);

	int adc_error = ads127l14_spi_system_config();
	if (adc_error) {
		reply_err(ctx, "ERR ADC reconfiguration failed [ERR: %d]\r\n", adc_error);
		return;
	}

	if (was_running) {
		device_gpio_write_pin(ADC_START_PIN, 1);
		adc_stream_set_run(true);
	}
	reply_ok(ctx, "OK ADC reset done\r\n");
}

/**
 * @brief ADC START: pulse START and start streaming.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Unused.
 */
static void cmd_adc_start(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc;
	(void)argv;
	device_gpio_write_pin(ADC_START_PIN, 0);
	os_delay_us(ADC_START_PULSE_US);
	device_gpio_write_pin(ADC_START_PIN, 1);
	adc_stream_set_run(true);
	reply_ok(ctx, "OK ADC sampling started ...\r\n");
}

/**
 * @brief ADC STOP: stop sampling and streaming.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Unused.
 */
static void cmd_adc_stop(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc;
	(void)argv;
	device_gpio_write_pin(ADC_START_PIN, 0);
	adc_stream_set_run(false);
	reply_ok(ctx, "OK ADC sampling stopped\r\n");
}

/**
 * @brief ADC SET SPEED_MODE: set the speed mode.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Speed mode, 0 to 3.
 */
static void cmd_adc_set_speed_mode(console_ctx_t *ctx, int argc, char **argv)
{
	if (argc < 1) {
		reply_err(ctx, "ERR usage: ADC SET SPEED_MODE <0-3>\r\n");
		return;
	}

	char *end;
	int adc_error;
	long mode = strtol(argv[0], &end, 0);
	if (*end != '\0' || mode < 0 || mode > 3) {
		reply_err(ctx, "ERR SPEED_MODE must be <0-3>\r\n");
		return;
	}

	adc_error = ads127l14_spi_set_speed_mode((uint8_t)mode);
	if (adc_error) {
		reply_err(ctx, "ERR ADC speed mode update fail [ERR: %d]\r\n", adc_error);
		return;
	}

	reply_ok(ctx, "OK SPEED_MODE=%ld\r\n", mode);
}

/**
 * @brief ADC SET DCLK_DIV: set the DCLK divider.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Divider: 1, 2, 4 or 8.
 */
static void cmd_adc_set_dclk_div(console_ctx_t *ctx, int argc, char **argv)
{
	if (argc < 1) {
		reply_err(ctx, "ERR usage: ADC SET DCLK_DIV <1|2|4|8>\r\n");
		return;
	}

	char *end;
	int adc_error;
	uint8_t dclk_div_setting = 0;
	long dclk_div = strtol(argv[0], &end, 10);
	if (*end != '\0' || (dclk_div != 1 && dclk_div != 2 && dclk_div != 4 && dclk_div != 8)) {
		reply_err(ctx, "ERR DCLK_DIV must be one of following values <1|2|4|8>\r\n");
		return;
	}

	long tmp = dclk_div;
	while (tmp > 1) {
		tmp >>= 1;
		dclk_div_setting++;
	}

	adc_error = ads127l14_spi_set_dclk_div(dclk_div_setting);
	if (adc_error) {
		reply_err(ctx, "ERR ADC dclk_div update fail [ERR: %d]\r\n", adc_error);
		return;
	}

	reply_ok(ctx, "OK DCLK_DIV=%ld\r\n", dclk_div);
}

/**
 * @brief ADC SET CLK_DIV: set the CLK divider.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Divider: 1, 2, 3, 4 or 8.
 */
static void cmd_adc_set_clk_div(console_ctx_t *ctx, int argc, char **argv)
{
	if (argc < 1) {
		reply_err(ctx, "ERR usage: ADC SET CLK_DIV <1|2|3|4|8>\r\n");
		return;
	}

	char *end;
	int adc_error;
	uint8_t clk_div_setting;
	long clk_div = strtol(argv[0], &end, 10);
	if (*end != '\0' || clk_div < 1 || (clk_div > 4 && clk_div != 8)) {
		reply_err(ctx, "ERR CLK_DIV must be one of following values <1|2|3|4|8>\r\n");
		return;
	}

	clk_div_setting = (clk_div < 5) ? clk_div - 1 : 4;
	adc_error = ads127l14_spi_set_clk_div(clk_div_setting);
	if (adc_error) {
		reply_err(ctx, "ERR ADC clk_div update fail [ERR: %d]\r\n", adc_error);
		return;
	}

	reply_ok(ctx, "OK CLK_DIV=%ld\r\n", clk_div);
}

/**
 * @brief ADC SET FLTR: set the digital filter.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Filter code, 0 to 31.
 */
static void cmd_adc_set_fltr(console_ctx_t *ctx, int argc, char **argv)
{
	if (argc < 1) {
		reply_err(ctx, "ERR usage: ADC SET FLTR <0-31>\r\n");
		return;
	}

	char *end;
	int adc_error;
	long fltr = strtol(argv[0], &end, 0);
	if (*end != '\0' || fltr < 0 || fltr > 31) {
		reply_err(ctx, "ERR FLTR must be <0-31>\r\n");
		return;
	}

	adc_error = ads127l14_spi_set_fltr((uint8_t)fltr);
	if (adc_error) {
		reply_err(ctx, "ERR ADC digital filtration mode update fail [ERR: %d]\r\n",
			  adc_error);
		return;
	}

	reply_ok(ctx, "OK FLTR=%ld\r\n", fltr);
}

/**
 * @brief ADC STATUS: report the STATUS register.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Unused.
 */
static void cmd_adc_status(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc;
	(void)argv;
	uint8_t status = 0;
	int adc_error;

	adc_error = ads127l14_spi_read_status(&status);
	if (adc_error) {
		reply_err(ctx, "ERR ADC status read fail [ERR: %d]\r\n", adc_error);
		return;
	}

	reply_ok(ctx, "OK STATUS=0x%02X\r\n", status);
}

/**
 * @brief ADC CLEAR_STATUS: clear the STATUS flags.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Unused.
 */
static void cmd_adc_clear_status(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc;
	(void)argv;
	int adc_error;

	adc_error = ads127l14_spi_clear_status();
	if (adc_error) {
		reply_err(ctx, "ERR ADC status clear fail [ERR: %d]\r\n", adc_error);
		return;
	}

	reply_ok(ctx, "OK STATUS register cleared\r\n");
}

/**
 * @brief ADC DISPLAY_SETTINGS: print the ADC configuration.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Unused.
 */
static void cmd_adc_settings(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc;
	(void)argv;
	ads127l14_spi_display_settings();
	reply_ok(ctx, "OK ADC DISPLAY_SETTINGS completed\r\n");
}

/**
 * @brief PGA SET_GAIN: set the wiper codes of all channels.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Wiper codes of channels 0 to 3, 0x00 to 0x7F.
 */
static void cmd_pga_set_gain(console_ctx_t *ctx, int argc, char **argv)
{
	if (argc != 4) {
		reply_err(ctx, "ERR usage: PGA SET_GAIN <ch0> <ch1> <ch2> <ch3> (0x00-0x7F)\r\n");
		return;
	}

	uint8_t gain[4];

	for (int i = 0; i < 4; i++) {
		long v;
		if (parse_int_range(argv[i], 0x00, 0x7F, &v)) {
			reply_err(ctx, "ERR CH%d gain must be 0x00-0x7F\r\n", i);
			return;
		}
		gain[i] = (uint8_t)v;
	}

	int pga_error = pga_spi_set_rdac(gain[3], gain[2], gain[1], gain[0]);
	if (pga_error) {
		reply_err(ctx, "ERR PGA channel gain update fail [ERR: %d]\r\n", pga_error);
		return;
	}

	reply_ok(ctx, "OK CH0=0x%02X, CH1=0x%02X, CH2=0x%02X, CH3=0x%02X\r\n", gain[0], gain[1],
		 gain[2], gain[3]);
}

/**
 * @brief PGA DISPLAY_SETTINGS: print the PGA codes.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Unused.
 */
static void cmd_pga_settings(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc;
	(void)argv;
	pga_spi_display_settings();
	reply_ok(ctx, "OK PGA DISPLAY_SETTINGS completed\r\n");
}

/**
 * @brief PGA READ_GAIN: report the wiper codes.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Unused.
 */
static void cmd_pga_read_gain(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc;
	(void)argv;
	uint8_t gain[4];
	int pga_error;

	pga_error = pga_spi_read_rdac(&gain[3], &gain[2], &gain[1], &gain[0]);
	if (pga_error) {
		reply_err(ctx, "ERR PGA channel gain read fail [ERR: %d]\r\n", pga_error);
		return;
	}

	reply_ok(ctx, "OK CH0=0x%02X, CH1=0x%02X, CH2=0x%02X, CH3=0x%02X\r\n", gain[0], gain[1],
		 gain[2], gain[3]);
}

/**
 * @brief PGA RESET: reset both PGAs.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Unused.
 */
static void cmd_pga_reset(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc;
	(void)argv;
	int pga_error;

	pga_error = pga_spi_system_reset();
	if (pga_error) {
		reply_err(ctx, "ERR PGA reset fail [ERR: %d]\r\n", pga_error);
		return;
	}

	reply_ok(ctx, "OK PGA reset completed\r\n");
}

/**
 * @brief PGA SAVE_EEPROM: store the wiper codes in EEPROM.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Unused.
 */
static void cmd_pga_eeprom_save(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc;
	(void)argv;
	int pga_error;

	pga_error = pga_spi_eeprom_save_rdac();
	if (pga_error) {
		reply_err(ctx, "ERR PGA save to eeprom fail [ERR: %d]\r\n", pga_error);
		return;
	}

	reply_ok(ctx, "OK PGA eeprom save completed\r\n");
}

/** @brief Command table entry. */
typedef struct cmd_entry {
	const char *name;            /**< Command word. */
	const char *help;            /**< Help text. */
	cmd_fn_t fn;                 /**< Handler, NULL for a group. */
	const struct cmd_entry *sub; /**< Sub-commands, NULL for a leaf. */
} cmd_entry_t;

/** ANALOG_PWR sub-commands. */
static const cmd_entry_t analog_pwr_cmds[] = {
	{"ON", "enable analog power", cmd_analog_pwr_on, NULL},
	{"OFF", "disable analog power", cmd_analog_pwr_off, NULL},
	{NULL},
};

/** ADC SET sub-commands. */
static const cmd_entry_t adc_set_cmds[] = {
	{"SPEED_MODE", "set ADC speed mode <0-3>", cmd_adc_set_speed_mode, NULL},
	{"DCLK_DIV", "set DCLK divider <1|2|4|8>", cmd_adc_set_dclk_div, NULL},
	{"CLK_DIV", "set CLK divider <1|2|3|4|8>", cmd_adc_set_clk_div, NULL},
	{"FLTR", "set ADC digital filter <0-31>", cmd_adc_set_fltr, NULL},
	{NULL},
};

/** ADC sub-commands. */
static const cmd_entry_t adc_cmds[] = {
	{"READ_ID", "read DEVICE_ID / REV_ID registers", cmd_adc_read_id, NULL},
	{"RESET", "hardware reset of the ADC", cmd_adc_reset, NULL},
	{"START", "pulse START and enable streaming", cmd_adc_start, NULL},
	{"STOP", "stop sampling and streaming", cmd_adc_stop, NULL},
	{"STATUS", "get ADC status register", cmd_adc_status, NULL},
	{"CLEAR_STATUS", "clear ADC status register", cmd_adc_clear_status, NULL},
	{"DISPLAY_SETTINGS", "display ADC settings", cmd_adc_settings, NULL},
	{"SET", "set ADC parameters", NULL, adc_set_cmds},
	{NULL},
};

/** PGA sub-commands. */
static const cmd_entry_t pga_cmds[] = {
	{"SET_GAIN", "set PGA gain on channels 0-3", cmd_pga_set_gain, NULL},
	{"READ_GAIN", "read PGA gain on channels 0-3", cmd_pga_read_gain, NULL},
	{"DISPLAY_SETTINGS", "display PGA settings", cmd_pga_settings, NULL},
	{"RESET", "hardware reset of the PGA", cmd_pga_reset, NULL},
	{"SAVE_EEPROM", "set current settings to PGA EEPROM", cmd_pga_eeprom_save, NULL},
	{NULL},
};

/** Top-level commands. */
static const cmd_entry_t root_cmds[] = {
	{"HELP", "list commands", cmd_help, NULL},
	{"ANALOG_PWR", "analog power control", NULL, analog_pwr_cmds},
	{"ADC", "ADC control", NULL, adc_cmds},
	{"PGA", "PGA control", NULL, pga_cmds},
	{"CH_STATUS", "channel status", ch_status, NULL},
	{"DEVICE_TEMP", "device internal temperature", cmd_device_temp, NULL},
	{NULL},
};

/**
 * @brief Append the help lines of a command table and its sub-tables.
 *
 * @param ctx Client context.
 * @param table Command table.
 * @param prefix Command words leading to @p table.
 */
static void print_help_table(console_ctx_t *ctx, const cmd_entry_t *table, const char *prefix)
{
	for (const cmd_entry_t *e = table; e->name; e++) {
		if (e->sub) {
			char child[48];
			snprintf(child, sizeof(child), "%s%s ", prefix, e->name);
			print_help_table(ctx, e->sub, child);
		} else {
			reply(ctx, "  %s%-*s - %s\r\n", prefix, (int)(14 - strlen(prefix)), e->name,
			      e->help ? e->help : "");
		}
	}
}

/**
 * @brief HELP: list all commands.
 *
 * @param ctx Client context.
 * @param argc Number of arguments.
 * @param argv Unused.
 */
static void cmd_help(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc;
	(void)argv;
	reply(ctx, "Commands (case-insensitive) - PROTOTYPE PHASE:\r\n");
	print_help_table(ctx, root_cmds, "");
}

/**
 * @brief Find a command word in a table, ignoring case.
 *
 * @param table Command table.
 * @param name Command word.
 *
 * @return Matching entry, NULL if none.
 */
static const cmd_entry_t *find_entry(const cmd_entry_t *table, const char *name)
{
	for (const cmd_entry_t *e = table; e->name; e++) {
		if (!strcasecmp(e->name, name)) {
			return e;
		}
	}
	return NULL;
}

/**
 * @brief Split a line into words in place.
 *
 * @param line Line to split; modified.
 * @param argv Word pointers.
 * @param max_args Capacity of @p argv.
 *
 * @return Number of words.
 */
static int tokenize(char *line, char **argv, int max_args)
{
	int argc = 0;
	char *save = NULL;
	char *tok = strtok_r(line, " \t\r\n", &save);

	while (tok && argc < max_args) {
		argv[argc++] = tok;
		tok = strtok_r(NULL, " \t\r\n", &save);
	}
	return argc;
}

/**
 * @brief Walk the command tree and run the matching handler.
 *
 * @param ctx Client context.
 * @param argc Number of words.
 * @param argv Words.
 */
static void dispatch(console_ctx_t *ctx, int argc, char **argv)
{
	const cmd_entry_t *table = root_cmds;
	const char *parent = NULL;
	int depth = 0;

	while (depth < argc) {
		const cmd_entry_t *e = find_entry(table, argv[depth]);
		if (!e) {
			if (parent) {
				reply_err(ctx, "ERR unknown %s sub-command\r\n", parent);
			} else {
				reply_err(ctx, "ERR unknown command\r\n");
			}
			return;
		}
		depth++;

		if (e->sub) {
			if (depth >= argc) {
				reply_err(ctx, "ERR missing %s sub-command\r\n", e->name);
				return;
			}
			table = e->sub;
			parent = e->name;
			continue;
		}

		e->fn(ctx, argc - depth, argv + depth);
		return;
	}
}

/**
 * @brief Run one command line under the command lock.
 *
 * @param ctx Client context.
 * @param line Command line; modified.
 */
static void handle_line(console_ctx_t *ctx, char *line)
{
	char *argv[CONSOLE_MAX_ARGS];
	int argc = tokenize(line, argv, CONSOLE_MAX_ARGS);

	if (argc == 0) {
		return;
	}

	if (xSemaphoreTake(g_cfg_mutex, pdMS_TO_TICKS(CONSOLE_CFG_LOCK_MS)) != pdTRUE) {
		reply(ctx, "ERR device busy, try again\r\n");
		return;
	}
	dispatch(ctx, argc, argv);
	xSemaphoreGive(g_cfg_mutex);
}

/**
 * @brief Client task: sends the start-up summary, then runs commands.
 *
 * @param p Client slot index, cast to a pointer.
 */
static void console_client_thread(void *p)
{
	int slot = (int)(intptr_t)p;
	console_ctx_t ctx = {
		.sd = g_clients[slot], .out = g_out_buf[slot], .out_len = 0, .dead = false};
	char buf[CONSOLE_RX_BUF_SIZE];
	int fill = 0;

	reply(&ctx, "System initialization summary\r\n" CONSOLE_PROMPT);
	if (main_init_report.dma_init_res) {
		reply(&ctx, "DMA initialization critical error (system inactive) [ERR %d]\r\n",
		      main_init_report.dma_init_res);
	} else {
		reply(&ctx, "DMA ready\r\n");
	}
	if (main_init_report.gpio_init_res) {
		reply(&ctx, "GPIO initialization error [ERR %d]\r\n",
		      main_init_report.gpio_init_res);
	} else {
		reply(&ctx, "GPIO ready\r\n");
	}
	if (main_init_report.ads127l14_init_res) {
		reply(&ctx, "ADS127l14 initialization error [ERR %d]\r\n",
		      main_init_report.ads127l14_init_res);
	} else {
		reply(&ctx, "ADS127l14 ready\r\n");
	}
	if (main_init_report.pga_init_res) {
		reply(&ctx, "PGA initialization error [ERR %d]\r\n", main_init_report.pga_init_res);
	} else {
		reply(&ctx, "PGA ready\r\n");
	}
	if (main_init_report.xadc_init_res) {
		reply(&ctx, "XADC initialization error [ERR %d]\r\n",
		      main_init_report.xadc_init_res);
	} else {
		reply(&ctx, "XADC ready\r\n");
	}
	if (main_init_report.ads127l14_config_res) {
		reply(&ctx, "ADS127l14 configuration error [ERR %d]\r\n",
		      main_init_report.ads127l14_config_res);
	} else {
		reply(&ctx, "ADS127l14 configured\r\n");
	}

	reply(&ctx, "ADC console ready. Type HELP.\r\n" CONSOLE_PROMPT);
	console_flush(&ctx);
	xil_printf("console: client accepted, sd=%d slot=%d\r\n", ctx.sd, slot);

	while (!ctx.dead) {
		fd_set rfds;
		struct timeval tv = {.tv_sec = 0, .tv_usec = IDLE_FLUSH_MS * 1000};

		FD_ZERO(&rfds);
		FD_SET(ctx.sd, &rfds);

		int sel = lwip_select(ctx.sd + 1, &rfds, NULL, NULL, &tv);
		if (sel < 0) {
			break;
		}

		if (sel == 0) {
			if (fill > 0) {
				buf[fill] = 0;
				handle_line(&ctx, buf);
				reply(&ctx, CONSOLE_PROMPT);
				console_flush(&ctx);
				fill = 0;
			}
			continue;
		}

		int n = lwip_read(ctx.sd, buf + fill, sizeof(buf) - fill - 1);
		if (n <= 0) {
			break;
		}

		fill += n;
		buf[fill] = 0;

		char *start = buf;
		char *term;
		while ((term = strpbrk(start, "\r\n")) != NULL) {
			*term = 0;
			if (term != start) {
				handle_line(&ctx, start);
				reply(&ctx, CONSOLE_PROMPT);
				console_flush(&ctx);
			}
			start = term + 1;
		}

		int remaining = fill - (int)(start - buf);
		memmove(buf, start, remaining);
		fill = remaining;

		if (fill >= (int)sizeof(buf) - 1) {
			fill = 0;
			reply(&ctx, "ERR line too long\r\n" CONSOLE_PROMPT);
			console_flush(&ctx);
		}
	}

	xil_printf("console: client closed, sd=%d\r\n", ctx.sd);
	client_remove(slot);
	close(ctx.sd);
	vTaskDelete(NULL);
}

/**
 * @brief Console server task.
 *
 * @param p Unused.
 */
void console_thread(void *p)
{
	(void)p;
	int listen_sd;
	struct sockaddr_in addr, remote;
	socklen_t remote_len;

	g_cfg_mutex = xSemaphoreCreateMutex();
	g_clients_mutex = xSemaphoreCreateMutex();
	for (int i = 0; i < CONSOLE_MAX_CLIENTS; i++) {
		g_clients[i] = -1;
	}

	memset(&addr, 0, sizeof(addr));
	addr.sin_family = AF_INET;
	addr.sin_port = htons(CONSOLE_PORT);
	addr.sin_addr.s_addr = INADDR_ANY;

	listen_sd = lwip_socket(AF_INET, SOCK_STREAM, 0);
	if (listen_sd < 0 || lwip_bind(listen_sd, (struct sockaddr *)&addr, sizeof(addr)) < 0 ||
	    lwip_listen(listen_sd, CONSOLE_MAX_CLIENTS) < 0) {
		xil_printf("console: failed to set up listening socket\r\n");
		vTaskDelete(NULL);
		return;
	}

	xil_printf("Console server listening on port %d\r\n", CONSOLE_PORT);

	while (1) {
		remote_len = sizeof(remote);
		int sd = lwip_accept(listen_sd, (struct sockaddr *)&remote, &remote_len);
		if (sd < 0) {
			continue;
		}

		net_enable_keepalive(sd);
		int slot = client_add(sd);
		if (slot < 0) {
			static const char busy[] = "ERR console busy\r\n";
			lwip_write(sd, busy, sizeof(busy) - 1);
			close(sd);
			continue;
		}

		if (sys_thread_new("console_c", console_client_thread, (void *)(intptr_t)slot,
				   THREAD_STACKSIZE, 2) == NULL) {
			client_remove(slot);
			close(sd);
		}
	}
}
