/**
 * @file adc_stream.h
 * @brief TCP server streaming ADC batches to one client.
 *
 * @defgroup adc_stream ADC stream server
 * @{
 */

#ifndef ADC_STREAM_H
#define ADC_STREAM_H

#include <stdbool.h>

#define ADC_STREAM_PORT 5001 /**< TCP port of the stream server. */

/**
 * @brief Stream server task.
 *
 * Accepts one client on ADC_STREAM_PORT; a new client replaces the old one.
 * The DMA runs only while streaming is requested and a client is connected.
 *
 * @param p Unused.
 */
void adc_stream_thread(void *p);

/**
 * @brief Request streaming on or off.
 *
 * Every call restarts the DMA on the next pass of the stream task.
 *
 * @param run true to stream, false to stop.
 */
void adc_stream_set_run(bool run);

/**
 * @brief Get the requested streaming state.
 *
 * @return true if streaming is requested.
 */
bool adc_stream_get_run(void);

/** @} */

#endif /* ADC_STREAM_H */
