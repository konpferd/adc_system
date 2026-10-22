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

#define THREAD_STACKSIZE    4096
#define IDLE_FLUSH_MS       200
#define CONSOLE_RX_BUF_SIZE 256
#define CONSOLE_TX_BUF_SIZE 256
#define CONSOLE_MAX_ARGS    8
#define CONSOLE_PROMPT      "> "

#define CONSOLE_OUT_BUF_SIZE    2048
#define CONSOLE_SEND_TIMEOUT_MS 1000
#define CONSOLE_CFG_LOCK_MS     2000

#define ADC_RESET_PULSE_US      100     /* RESET low time                       */
#define ADC_RESET_RECOVERY_US   1000    /* wait after RESET before SPI access   */
#define ADC_START_PULSE_US      250     /* START low pulse that restarts the ADC */

typedef struct {
    int   sd;
    char *out;
    int   out_len;
    bool  dead;
} console_ctx_t;

static char g_out_buf[CONSOLE_MAX_CLIENTS][CONSOLE_OUT_BUF_SIZE];

extern struct console_init_report main_init_report;

static SemaphoreHandle_t g_cfg_mutex;
static SemaphoreHandle_t g_clients_mutex;

static int          g_clients[CONSOLE_MAX_CLIENTS];
static volatile int g_client_count;
static char         g_tx_buf[CONSOLE_TX_BUF_SIZE];

static const char *mc_str(char *buf, size_t len, int32_t mc)
{
	int32_t a = mc < 0 ? -mc : mc;
	snprintf(buf, len, "%s%ld.%03ld", mc < 0 ? "-" : "", (long)(a / 1000), (long)(a % 1000));
	return buf;
}

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

static void client_remove(int slot)
{
    xSemaphoreTake(g_clients_mutex, portMAX_DELAY);
    if (g_clients[slot] >= 0) {
        g_clients[slot] = -1;
        g_client_count--;
    }
    xSemaphoreGive(g_clients_mutex);
}

int console_vprintf(const char *fmt, va_list ap)
{
    if (g_client_count == 0 || g_clients_mutex == NULL)
        return 0;

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

int console_printf(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    int n = console_vprintf(fmt, ap);
    va_end(ap);
    return n;
}

static void reply(console_ctx_t *ctx, const char *fmt, ...)
{
    int room = CONSOLE_OUT_BUF_SIZE - ctx->out_len;
    if (room <= 1)
        return;

    va_list ap;
    va_start(ap, fmt);
    int len = vsnprintf(ctx->out + ctx->out_len, (size_t)room, fmt, ap);
    va_end(ap);

    if (len > 0)
        ctx->out_len += (len < room) ? len : room - 1;
}

static void console_flush(console_ctx_t *ctx)
{
    if (ctx->out_len > 0 && !ctx->dead) {
        if (net_send_all(ctx->sd, ctx->out, (size_t)ctx->out_len, CONSOLE_SEND_TIMEOUT_MS) != 0)
            ctx->dead = true;
    }
    ctx->out_len = 0;
}

#define reply_ok(ctx, ...)   reply((ctx), __VA_ARGS__)
#define reply_err(ctx, ...)  reply((ctx), __VA_ARGS__)

typedef void (*cmd_fn_t)(console_ctx_t *ctx, int argc, char **argv);

static void cmd_help(console_ctx_t *ctx, int argc, char **argv);

static int parse_int_range(const char *s, long min, long max, long *out)
{
    char *end;
    long v = strtol(s, &end, 0);

    if (end == s || *end != '\0' || v < min || v > max)
        return -1;

    *out = v;
    return 0;
}

/* --- ROOT_CMDS -------------------------------------------------------- */

static void ch_status(console_ctx_t *ctx, int argc, char **argv)
{
    (void)argc; (void)argv;;
    reply_ok(ctx, "OK Channel status: %d, %d, %d, %d\r\n", 1, 1, 1, 1);
}

static void cmd_device_temp(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc; (void)argv;
	int32_t now, min, max;
	char b0[16], b1[16], b2[16];

	if (device_xadc_read_temp_mc(&now, &min, &max)) {
		reply_err(ctx, "ERR XADC not available\r\n");
		return;
	}

	reply_ok(ctx, "OK DEVICE_TEMP=%s MIN=%s MAX=%s DEVICE_WRN_LVL=%d\r\n",
			mc_str(b0, sizeof b0, now), mc_str(b1, sizeof b1, min), mc_str(b2, sizeof b2, max), DEVICE_TEMP_C_WRN_LVL);
}

/* --- ANALOG_PWR ------------------------------------------------------- */
static void cmd_analog_pwr_on(console_ctx_t *ctx, int argc, char **argv)
{
    (void)argc; (void)argv;
    device_gpio_write_pin(EN_A_PWR_PIN, 0);
    reply_ok(ctx, "OK Analog PWR ON\r\n");
}

static void cmd_analog_pwr_off(console_ctx_t *ctx, int argc, char **argv)
{
    (void)argc; (void)argv;
    adc_stream_set_run(false);
    device_gpio_write_pin(ADC_START_PIN, 0);
	device_gpio_write_pin(EN_A_PWR_PIN, 1);
    reply_ok(ctx, "OK Analog PWR OFF\r\n");
}

/* --- ADC -------------------------------------------------------------- */
static void cmd_adc_read_id(console_ctx_t *ctx, int argc, char **argv)
{
    (void)argc; (void)argv;
    uint8_t device_id = 0, rev_id = 0;
    int adc_error;

    adc_error = ads127l14_spi_read_device_data(&device_id, &rev_id);
    if (adc_error) {
        reply_err(ctx, "ERR ADC data read fail [ERR: %d]\r\n", adc_error);
        return;
    }

    reply_ok(ctx, "OK DEVICE_ID=0x%02X REV_ID=0x%02X\r\n", device_id, rev_id);
}

static void cmd_adc_reset(console_ctx_t *ctx, int argc, char **argv)
{
    (void)argc; (void)argv;
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

static void cmd_adc_start(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc; (void)argv;
	device_gpio_write_pin(ADC_START_PIN, 0);
	os_delay_us(ADC_START_PULSE_US);
	device_gpio_write_pin(ADC_START_PIN, 1);
	adc_stream_set_run(true);
	reply_ok(ctx, "OK ADC sampling started ...\r\n");
}

static void cmd_adc_stop(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc; (void)argv;
	device_gpio_write_pin(ADC_START_PIN, 0);
	adc_stream_set_run(false);
	reply_ok(ctx, "OK ADC sampling stopped\r\n");
}

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

    clk_div_setting = (clk_div < 5) ? clk_div-1 : 4;
    adc_error = ads127l14_spi_set_clk_div(clk_div_setting);
	if (adc_error) {
		reply_err(ctx, "ERR ADC clk_div update fail [ERR: %d]\r\n", adc_error);
		return;
	}

    reply_ok(ctx, "OK CLK_DIV=%ld\r\n", clk_div);
}

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
		reply_err(ctx, "ERR ADC digital filtration mode update fail [ERR: %d]\r\n", adc_error);
		return;
	}

    reply_ok(ctx, "OK FLTR=%ld\r\n", fltr);
}

static void cmd_adc_status(console_ctx_t *ctx, int argc, char **argv)
{
    (void)argc; (void)argv;
    uint8_t status = 0;
    int adc_error;

    adc_error = ads127l14_spi_read_status(&status);
    if (adc_error) {
        reply_err(ctx, "ERR ADC status read fail [ERR: %d]\r\n", adc_error);
        return;
    }

    reply_ok(ctx, "OK STATUS=0x%02X\r\n", status);
}

static void cmd_adc_clear_status(console_ctx_t *ctx, int argc, char **argv)
{
    (void)argc; (void)argv;
    int adc_error;

    adc_error = ads127l14_spi_clear_status();
    if (adc_error) {
        reply_err(ctx, "ERR ADC status clear fail [ERR: %d]\r\n", adc_error);
        return;
    }

    reply_ok(ctx, "OK STATUS register cleared\r\n");
}

static void cmd_adc_settings(console_ctx_t *ctx, int argc, char **argv)
{
    (void)argc; (void)argv;
    ads127l14_spi_display_settings();
    reply_ok(ctx, "OK ADC DISPLAY_SETTINGS completed\r\n");
}

/* --- PGA -------------------------------------------------------------- */
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

    reply_ok(ctx, "OK CH0=0x%02X, CH1=0x%02X, CH2=0x%02X, CH3=0x%02X\r\n",
             gain[0], gain[1], gain[2], gain[3]);
}

static void cmd_pga_settings(console_ctx_t *ctx, int argc, char **argv)
{
    (void)argc; (void)argv;
    pga_spi_display_settings();
    reply_ok(ctx, "OK PGA DISPLAY_SETTINGS completed\r\n");
}

static void cmd_pga_read_gain(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc; (void)argv;
	uint8_t gain[4];
	int pga_error;

	pga_error = pga_spi_read_rdac(&gain[3], &gain[2], &gain[1], &gain[0]);
	if (pga_error) {
		reply_err(ctx, "ERR PGA channel gain read fail [ERR: %d]\r\n", pga_error);
		return;
	}

    reply_ok(ctx, "OK CH0=0x%02X, CH1=0x%02X, CH2=0x%02X, CH3=0x%02X\r\n",
             gain[0], gain[1], gain[2], gain[3]);
}

static void cmd_pga_reset(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc; (void)argv;
	int pga_error;

	pga_error = pga_spi_system_reset();
	if (pga_error) {
		reply_err(ctx, "ERR PGA reset fail [ERR: %d]\r\n", pga_error);
		return;
	}

    reply_ok(ctx, "OK PGA reset completed\r\n");
}

static void cmd_pga_eeprom_save(console_ctx_t *ctx, int argc, char **argv)
{
	(void)argc; (void)argv;
	int pga_error;

	pga_error = pga_spi_eeprom_save_rdac();
	if (pga_error) {
		reply_err(ctx, "ERR PGA save to eeprom fail [ERR: %d]\r\n", pga_error);
		return;
	}

    reply_ok(ctx, "OK PGA eeprom save completed\r\n");
}

/* ========================================================================
 *  Command tables
 *
 *  To add a command: write a handler above and add one line below.
 *  To add a sub-command group: create a cmd_entry_t[] and point `sub` at it.
 *  Tables end with a {NULL} sentinel. HELP output is generated from these.
 * ====================================================================== */

typedef struct cmd_entry {
    const char              *name;
    const char              *help;
    cmd_fn_t                 fn;
    const struct cmd_entry  *sub;
} cmd_entry_t;

static const cmd_entry_t analog_pwr_cmds[] = {
    { "ON",      "enable analog power",  cmd_analog_pwr_on,  NULL },
    { "OFF",     "disable analog power", cmd_analog_pwr_off, NULL },
    { NULL }
};

static const cmd_entry_t adc_set_cmds[] = {
    { "SPEED_MODE", "set ADC speed mode <0-3>",			cmd_adc_set_speed_mode, NULL },
	{ "DCLK_DIV", 	"set DCLK divider <1|2|4|8>", 		cmd_adc_set_dclk_div, NULL },
	{ "CLK_DIV", 	"set CLK divider <1|2|3|4|8>", 		cmd_adc_set_clk_div, NULL },
	{ "FLTR", 		"set ADC digital filter <0-31>", 	cmd_adc_set_fltr, NULL },
    { NULL }
};

static const cmd_entry_t adc_cmds[] = {
    { "READ_ID", 			"read DEVICE_ID / REV_ID registers", 	cmd_adc_read_id, 		NULL },
    { "RESET",   			"hardware reset of the ADC",         	cmd_adc_reset,   		NULL },
    { "START",   			"pulse START and enable streaming",  	cmd_adc_start,   		NULL },
    { "STOP",    			"stop sampling and streaming",       	cmd_adc_stop,    		NULL },
	{ "STATUS",  			"get ADC status register",       		cmd_adc_status,  		NULL },
	{ "CLEAR_STATUS", 		"clear ADC status register",			cmd_adc_clear_status, 	NULL },
	{ "DISPLAY_SETTINGS",	"display ADC settings",					cmd_adc_settings, 		NULL },
	{ "SET",     			"set ADC parameters",               	NULL,            		adc_set_cmds },
    { NULL }
};

static const cmd_entry_t pga_cmds[] = {
	{ "SET_GAIN", 			"set PGA gain on channels 0-3", 		cmd_pga_set_gain, 		NULL },
	{ "READ_GAIN", 			"read PGA gain on channels 0-3", 		cmd_pga_read_gain, 		NULL },
	{ "DISPLAY_SETTINGS",	"display PGA settings",					cmd_pga_settings, 		NULL },
	{ "RESET",   			"hardware reset of the PGA",         	cmd_pga_reset,   		NULL },
	{ "SAVE_EEPROM",   		"set current settings to PGA EEPROM",   cmd_pga_eeprom_save,   	NULL },
    { NULL }
};

static const cmd_entry_t root_cmds[] = {
    { "HELP",       	"list commands",        		cmd_help,  			NULL           	},
    { "ANALOG_PWR", 	"analog power control", 		NULL,      			analog_pwr_cmds	},
    { "ADC",        	"ADC control",          		NULL,      			adc_cmds       	},
	{ "PGA",        	"PGA control",          		NULL,      			pga_cmds       	},
	{ "CH_STATUS",  	"channel status",       		ch_status, 			NULL       		},
	{ "DEVICE_TEMP",	"device internal temperature",	cmd_device_temp,	NULL       		},
    { NULL }
};

static void print_help_table(console_ctx_t *ctx, const cmd_entry_t *table, const char *prefix)
{
    for (const cmd_entry_t *e = table; e->name; e++) {
        if (e->sub) {
            char child[48];
            snprintf(child, sizeof(child), "%s%s ", prefix, e->name);
            print_help_table(ctx, e->sub, child);
        } else {
            reply(ctx, "  %s%-*s - %s\r\n", prefix,
                  (int)(14 - strlen(prefix)), e->name, e->help ? e->help : "");
        }
    }
}

static void cmd_help(console_ctx_t *ctx, int argc, char **argv)
{
    (void)argc; (void)argv;
    reply(ctx, "Commands (case-insensitive) - PROTOTYPE PHASE:\r\n");
    print_help_table(ctx, root_cmds, "");
}

/* ========================================================================
 *  Dispatcher
 * ====================================================================== */

static const cmd_entry_t *find_entry(const cmd_entry_t *table, const char *name)
{
    for (const cmd_entry_t *e = table; e->name; e++) {
        if (!strcasecmp(e->name, name))
            return e;
    }
    return NULL;
}

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

static void dispatch(console_ctx_t *ctx, int argc, char **argv)
{
    const cmd_entry_t *table  = root_cmds;
    const char        *parent = NULL;
    int                depth  = 0;

    while (depth < argc) {
        const cmd_entry_t *e = find_entry(table, argv[depth]);
        if (!e) {
            if (parent) reply_err(ctx, "ERR unknown %s sub-command\r\n", parent);
            else        reply_err(ctx, "ERR unknown command\r\n");
            return;
        }
        depth++;

        if (e->sub) {                       /* group: descend */
            if (depth >= argc) {
                reply_err(ctx, "ERR missing %s sub-command\r\n", e->name);
                return;
            }
            table  = e->sub;
            parent = e->name;
            continue;
        }

        e->fn(ctx, argc - depth, argv + depth);   /* leaf: run it */
        return;
    }
}

static void handle_line(console_ctx_t *ctx, char *line)
{
    char *argv[CONSOLE_MAX_ARGS];
    int argc = tokenize(line, argv, CONSOLE_MAX_ARGS);

    if (argc == 0)
        return;

    if (xSemaphoreTake(g_cfg_mutex, pdMS_TO_TICKS(CONSOLE_CFG_LOCK_MS)) != pdTRUE) {
		reply(ctx, "ERR device busy, try again\r\n");
		return;
	}
	dispatch(ctx, argc, argv);
	xSemaphoreGive(g_cfg_mutex);
}

/* ========================================================================
 *  Per-client thread
 *
 *  Input handling notes:
 *   - '\r' or '\n' terminates a command.
 *   - Some raw TCP tools send no line ending at all, so after IDLE_FLUSH_MS
 *     of silence any buffered partial line is executed as a command.
 *   - select() (not SO_RCVTIMEO) is used for the timeout because
 *     SO_RCVTIMEO needs LWIP_SO_RCVTIMEO enabled in lwipopts.h, which the
 *     stock Xilinx BSP does not do.
 * ====================================================================== */

static void console_client_thread(void *p)
{
    int slot = (int)(intptr_t)p;
    console_ctx_t ctx = { .sd = g_clients[slot], .out = g_out_buf[slot], .out_len = 0, .dead = false };
    char buf[CONSOLE_RX_BUF_SIZE];
    int fill = 0;

    reply(&ctx, "System initialization summary\r\n" CONSOLE_PROMPT);
    if (main_init_report.dma_init_res) reply(&ctx, "DMA initialization critical error (system inactive) [ERR %d]\r\n", main_init_report.dma_init_res);
    else reply(&ctx, "DMA ready\r\n");
    if (main_init_report.gpio_init_res) reply(&ctx, "GPIO initialization error [ERR %d]\r\n", main_init_report.gpio_init_res);
	else reply(&ctx, "GPIO ready\r\n");
    if (main_init_report.ads127l14_init_res) reply(&ctx, "ADS127l14 initialization error [ERR %d]\r\n", main_init_report.ads127l14_init_res);
	else reply(&ctx, "ADS127l14 ready\r\n");
    if (main_init_report.pga_init_res) reply(&ctx, "PGA initialization error [ERR %d]\r\n", main_init_report.pga_init_res);
	else reply(&ctx, "PGA ready\r\n");
    if (main_init_report.xadc_init_res) reply(&ctx, "XADC initialization error [ERR %d]\r\n", main_init_report.xadc_init_res);
	else reply(&ctx, "XADC ready\r\n");
    if (main_init_report.ads127l14_config_res) reply(&ctx, "ADS127l14 configuration error [ERR %d]\r\n", main_init_report.ads127l14_config_res);
	else reply(&ctx, "ADS127l14 configured\r\n");

    reply(&ctx, "ADC console ready. Type HELP.\r\n" CONSOLE_PROMPT);
    console_flush(&ctx);
    xil_printf("console: client accepted, sd=%d slot=%d\r\n", ctx.sd, slot);

    while (!ctx.dead) {
        fd_set rfds;
        struct timeval tv = { .tv_sec = 0, .tv_usec = IDLE_FLUSH_MS * 1000 };

        FD_ZERO(&rfds);
        FD_SET(ctx.sd, &rfds);

        int sel = lwip_select(ctx.sd + 1, &rfds, NULL, NULL, &tv);
        if (sel < 0) break;                         /* socket error */

        if (sel == 0) {                             /* idle: flush partial line */
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
        if (n <= 0) break;                          /* 0 = closed, <0 = error */

        fill += n;
        buf[fill] = 0;

        /* Execute every complete line in the buffer. */
        char *start = buf;
        char *term;
        while ((term = strpbrk(start, "\r\n")) != NULL) {
            *term = 0;
            if (term != start) {                    /* skip empty lines (\r\n pairs) */
                handle_line(&ctx, start);
                reply(&ctx, CONSOLE_PROMPT);
                console_flush(&ctx);
            }
            start = term + 1;
        }

        /* Keep any trailing partial line. */
        int remaining = fill - (int)(start - buf);
        memmove(buf, start, remaining);
        fill = remaining;

        if (fill >= (int)sizeof(buf) - 1) {         /* line too long: discard */
            fill = 0;
            reply(&ctx, "ERR line too long\r\n" CONSOLE_PROMPT);
            console_flush(&ctx);
        }
    }

    xil_printf("console: client closed, sd=%d\r\n", ctx.sd);
    client_remove(slot);        /* unregister first, then close */
    close(ctx.sd);
    vTaskDelete(NULL);
}

/* ========================================================================
 *  Listener thread (spawned from main.c)
 * ====================================================================== */

void console_thread(void *p)
{
    (void)p;
    int listen_sd;
    struct sockaddr_in addr, remote;
    socklen_t remote_len;

    g_cfg_mutex     = xSemaphoreCreateMutex();
    g_clients_mutex = xSemaphoreCreateMutex();
    for (int i = 0; i < CONSOLE_MAX_CLIENTS; i++)
        g_clients[i] = -1;

    memset(&addr, 0, sizeof(addr));
    addr.sin_family      = AF_INET;
    addr.sin_port        = htons(CONSOLE_PORT);
    addr.sin_addr.s_addr = INADDR_ANY;

    listen_sd = lwip_socket(AF_INET, SOCK_STREAM, 0);
    if (listen_sd < 0 ||
        lwip_bind(listen_sd, (struct sockaddr *)&addr, sizeof(addr)) < 0 ||
        lwip_listen(listen_sd, CONSOLE_MAX_CLIENTS) < 0) {
        xil_printf("console: failed to set up listening socket\r\n");
        vTaskDelete(NULL);
        return;
    }

    xil_printf("Console server listening on port %d\r\n", CONSOLE_PORT);

    while (1) {
        remote_len = sizeof(remote);
        int sd = lwip_accept(listen_sd, (struct sockaddr *)&remote, &remote_len);
        if (sd < 0)
            continue;

        net_enable_keepalive(sd);
        int slot = client_add(sd);
        if (slot < 0) {                             /* all slots busy */
            static const char busy[] = "ERR console busy\r\n";
            lwip_write(sd, busy, sizeof(busy) - 1);
            close(sd);
            continue;
        }

        if (sys_thread_new("console_c", console_client_thread,
                           (void *)(intptr_t)slot,
                           THREAD_STACKSIZE, 2) == NULL) {
            client_remove(slot);
            close(sd);
        }
    }
}
