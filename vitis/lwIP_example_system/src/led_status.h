/**
 * @file led_status.h
 * @brief Ethernet status LEDs and user LEDs.
 *
 * @defgroup led_status Status LEDs
 * @{
 */

#ifndef LED_STATUS_H
#define LED_STATUS_H

/**
 * @brief LED task.
 *
 * Shows link speed and traffic on the Ethernet LED pairs and mirrors the
 * microphone sense inputs on the user LEDs.
 *
 * @param p Unused.
 */
void eth_led_status_thread(void *p);

/** @} */

#endif /* LED_STATUS_H */
