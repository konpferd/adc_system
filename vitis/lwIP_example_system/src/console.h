/**
 * @file console.h
 * @brief TCP command console.
 *
 * @defgroup console Command console
 * @{
 */

#ifndef CONSOLE_H
#define CONSOLE_H

#include <stdarg.h>

#define CONSOLE_PORT        5000 /**< TCP port of the console server. */
#define CONSOLE_MAX_CLIENTS 4    /**< Maximum number of simultaneous clients. */

/** Die temperature warning level reported by DEVICE_TEMP, in degC. */
#define DEVICE_TEMP_C_WRN_LVL 75

/** @brief Start-up initialization results, shown to every new client. */
struct console_init_report {
	int dma_init_res;         /**< adc_hw_init() result. */
	int gpio_init_res;        /**< device_gpio_init() result. */
	int ads127l14_init_res;   /**< ads127l14_spi_init() result. */
	int pga_init_res;         /**< pga_spi_init() result. */
	int xadc_init_res;        /**< device_xadc_init() result. */
	int ads127l14_config_res; /**< ads127l14_spi_system_config() result. */
};

/**
 * @brief Console server task.
 *
 * Accepts clients on CONSOLE_PORT and starts one task per client.
 *
 * @param p Unused.
 */
void console_thread(void *p);

/**
 * @brief Print to all connected clients.
 *
 * Output longer than 255 characters is truncated. Sending never blocks.
 *
 * @param fmt printf-style format string.
 * @param ... Format arguments.
 *
 * @return Number of characters sent; 0 if no client is connected.
 */
int console_printf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

/**
 * @brief Print to all connected clients, va_list variant.
 *
 * @param fmt printf-style format string.
 * @param ap Format arguments.
 *
 * @return Number of characters sent; 0 if no client is connected.
 */
int console_vprintf(const char *fmt, va_list ap);

/** @} */

#endif /* CONSOLE_H */
