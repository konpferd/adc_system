/**
 * @file device_xadc.h
 * @brief Zynq die temperature from the PS XADC.
 *
 * @defgroup device_xadc XADC temperature
 * @{
 */

#ifndef DEVICE_XADC_H
#define DEVICE_XADC_H
#include <stdint.h>

/**
 * @brief Initialize the XADC, run its self-test and select safe sequencer mode.
 *
 * @retval 0 XADC ready.
 * @retval -1 No XADC configuration found.
 * @retval -2 Driver initialization failed.
 * @retval -3 Self-test failed.
 */
int device_xadc_init(void);

/**
 * @brief Read the current, minimum and maximum die temperature.
 *
 * @param now_mc Current temperature, in millidegrees Celsius.
 * @param min_mc Minimum since power-up, in millidegrees Celsius.
 * @param max_mc Maximum since power-up, in millidegrees Celsius.
 *
 * @retval 0 Temperatures read.
 * @retval -1 XADC not initialized.
 */
int device_xadc_read_temp_mc(int32_t *now_mc, int32_t *min_mc, int32_t *max_mc);

/** @} */

#endif /* DEVICE_XADC_H */
