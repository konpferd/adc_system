#ifndef ADC_STREAM_H
#define ADC_STREAM_H

#include <stdbool.h>

#define ADC_STREAM_PORT 5001

void adc_stream_thread(void *p);

void adc_stream_set_run(bool run);
bool adc_stream_get_run(void);

#endif	/* ADC_STREAM_H */
