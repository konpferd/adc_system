#ifndef DEVICE_XADC_H
#define DEVICE_XADC_H
#include <stdint.h>

int device_xadc_init(void);

int device_xadc_read_temp_mc(int32_t *now_mc, int32_t *min_mc, int32_t *max_mc);

#endif
