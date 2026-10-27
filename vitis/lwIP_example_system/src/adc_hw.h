/**
 * @file adc_hw.h
 * @brief AXI DMA receive path for the ADS127L14 sample stream.
 *
 * @defgroup adc_hw ADC DMA receive path
 * @{
 */

#ifndef ADC_HW_H
#define ADC_HW_H

#include <stdint.h>
#include "xscugic.h"

/** Number of ADC channels in one stream entry. */
#define ADC_NUM_CHANNELS        4
/** Highest per-channel sample rate, in Hz. */
#define ADC_MAX_SAMPLE_RATE_HZ  1365300
/** Bytes per entry: 4 x 24-bit samples and a 32-bit frame counter. */
#define ADC_ENTRY_SIZE          16
/** Largest DMA batch, in entries (16-bit DMA length). */
#define ADC_MAX_WORDS_PER_BATCH 4095

#define ADC_WORDS_PER_BATCH 2048 /**< Default DMA batch size, in entries. */

/**
 * @brief Initialize the batch control GPIO, the AXI DMA and its interrupt.
 *
 * Arms the first S2MM transfer. The PL stream stays disabled until
 * adc_hw_start_stream() is called.
 *
 * @param IntcInstance Interrupt controller the S2MM interrupt is connected to.
 *
 * @retval XST_SUCCESS Initialization completed.
 * @retval XST_FAILURE A driver call failed or the DMA reset timed out.
 */
int adc_hw_init(XScuGic *IntcInstance);

/**
 * @brief Wait for the next completed DMA batch and copy it out.
 *
 * Blocks for up to 2.5 s. On timeout the DMA registers are logged and the
 * stream is restarted.
 *
 * @param adc_data Destination, at least @p max_batch_length * ADC_ENTRY_SIZE bytes.
 * @param max_batch_length Capacity of @p adc_data, in entries.
 *
 * @return Number of entries copied; 0 on timeout, abort or invalid request.
 */
uint32_t adc_hw_read(uint8_t *adc_data, uint32_t max_batch_length);

/**
 * @brief Set the number of entries per DMA batch.
 *
 * @param entries Batch size, 1 to ADC_MAX_WORDS_PER_BATCH.
 *
 * @retval XST_SUCCESS Batch size written to the PL.
 * @retval XST_FAILURE @p entries is out of range.
 */
int adc_hw_set_batch_size(uint16_t entries);

/**
 * @brief Reset the S2MM channel, arm the first buffer and enable the PL stream.
 *
 * Pending batches are discarded.
 *
 * @retval XST_SUCCESS Stream running.
 * @retval XST_FAILURE DMA reset timed out or the transfer could not be queued.
 */
int adc_hw_start_stream(void);

/**
 * @brief Start the PL sample stream.
 *
 * @retval XST_SUCCESS Always.
 */
int adc_hw_enable_stream(void);

/**
 * @brief Stop the PL sample stream.
 *
 * @retval XST_SUCCESS Always.
 */
int adc_hw_disable_stream(void);

/** @brief Wake a blocked adc_hw_read() so that it returns 0. */
void adc_hw_abort_read(void);

/** @} */

#endif /* ADC_HW_H */
