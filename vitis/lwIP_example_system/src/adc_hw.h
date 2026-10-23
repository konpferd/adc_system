#ifndef ADC_HW_H
#define ADC_HW_H

#include <stdint.h>
#include "xscugic.h"

#define ADC_NUM_CHANNELS 			4
#define ADC_MAX_SAMPLE_RATE_HZ		1365300
#define ADC_ENTRY_SIZE				16
#define ADC_MAX_WORDS_PER_BATCH		4095

#define ADC_WORDS_PER_BATCH      	2048		// System default

#define ADC_DMA_BUF_BASE_ADDR   0xFFFF0000u

int adc_hw_init(XScuGic *IntcInstance);

uint32_t adc_hw_read(uint8_t *adc_data, uint32_t max_batch_length);

int adc_hw_set_batch_size(uint16_t entries);
int  adc_hw_start_stream(void);
int  adc_hw_disable_stream(void);
void adc_hw_abort_read(void);

#endif	/* ADC_WH_H */
