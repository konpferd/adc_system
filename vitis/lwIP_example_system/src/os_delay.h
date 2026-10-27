/**
 * @file os_delay.h
 * @brief Microsecond delay that yields to other tasks when possible.
 *
 * @defgroup os_delay Delays
 * @{
 */

#ifndef OS_DELAY_H
#define OS_DELAY_H

#include <stdint.h>
#include "FreeRTOS.h"
#include "task.h"
#include "sleep.h"

/**
 * @brief Delay for at least @p us microseconds.
 *
 * Delays of 1 ms or more block the calling task with vTaskDelay(); one extra
 * tick covers the partial first tick. Shorter delays, and all delays before
 * the scheduler runs, busy-wait with usleep(). Not for use in an ISR.
 *
 * @param us Delay, in microseconds.
 */
static inline void os_delay_us(uint32_t us)
{
	if (us >= 1000u && xTaskGetSchedulerState() == taskSCHEDULER_RUNNING) {
		TickType_t ticks = pdMS_TO_TICKS((us + 999u) / 1000u) + 1;
		vTaskDelay(ticks);
	} else if (us > 0u) {
		usleep(us);
	}
}

/** @} */

#endif /* OS_DELAY_H */
