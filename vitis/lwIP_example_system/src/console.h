#ifndef CONSOLE_H
#define CONSOLE_H

#include <stdarg.h>

#define CONSOLE_PORT        5000
#define CONSOLE_MAX_CLIENTS 4

#define DEVICE_TEMP_C_WRN_LVL 75

struct console_init_report{
	int dma_init_res;
	int gpio_init_res;
	int ads127l14_init_res;
	int pga_init_res;
	int xadc_init_res;
	int ads127l14_config_res;
};

void console_thread(void *p);

int console_printf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
int console_vprintf(const char *fmt, va_list ap);

#endif	/* CONSOLE_H */
