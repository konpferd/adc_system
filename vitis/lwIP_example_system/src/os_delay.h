#ifndef OS_DELAY_H
#define OS_DELAY_H

#include <stdint.h>
#include "FreeRTOS.h"
#include "task.h"
#include "sleep.h"

/* Delay for at least `us` microseconds.
 *
 * - Delays of 1 ms or longer block the calling task (vTaskDelay), so lower-priority
 *   tasks (ADC stream, LEDs) keep running. One tick is added because vTaskDelay(n)
 *   may return up to one tick early (it counts tick edges, not elapsed time).
 * - Shorter delays (signal pulses, SPI turnaround) use the busy-wait usleep(): they are
 *   below the tick resolution and too short to be worth a context switch.
 * - Before the scheduler runs, or with the scheduler suspended, it always busy-waits.
 *
 * Must not be called from an ISR. */
static inline void os_delay_us(uint32_t us)
{
    if (us >= 1000u && xTaskGetSchedulerState() == taskSCHEDULER_RUNNING) {
        TickType_t ticks = pdMS_TO_TICKS((us + 999u) / 1000u) + 1;
        vTaskDelay(ticks);
    } else if (us > 0u) {
        usleep(us);
    }
}

#endif /* OS_DELAY_H */
