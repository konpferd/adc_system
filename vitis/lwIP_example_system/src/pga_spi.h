/**
 * @file pga_spi.h
 * @brief PGA gain control: two daisy-chained AD5122 digital potentiometers.
 *
 * Potentiometers are numbered in daisy-chain order; potentiometer n sets the
 * gain of ADC channel 3 - n.
 *
 * @defgroup pga_spi PGA driver
 * @{
 */

#ifndef PGA_SPI_H
#define PGA_SPI_H

#include <stdint.h>
#include <stdbool.h>

#define PGA_SPI_NUM_CHANNELS 4    /**< Number of potentiometers (ADC channels). */
#define PGA_SPI_MAX_CODE     0x7F /**< Highest wiper code (7-bit). */

#define PGA_SPI_DEFAULT_VAL_CH0 0x40 /**< Default wiper code of potentiometer 0. */
#define PGA_SPI_DEFAULT_VAL_CH1 0x40 /**< Default wiper code of potentiometer 1. */
#define PGA_SPI_DEFAULT_VAL_CH2 0x40 /**< Default wiper code of potentiometer 2. */
#define PGA_SPI_DEFAULT_VAL_CH3 0x40 /**< Default wiper code of potentiometer 3. */

/** @brief Driver return codes. */
typedef enum {
	PGA_OK = 0,             /**< Success. */
	PGA_ERR_NOT_READY = -1, /**< pga_spi_init() not done or failed. */
	PGA_ERR_PARAM = -2,     /**< NULL pointer or code above PGA_SPI_MAX_CODE. */
	PGA_ERR_SPI = -3,       /**< SPI transfer failed. */
	PGA_ERR_VERIFY = -4     /**< Readback differs from the written value. */
} pga_status_t;

/**
 * @brief Initialize the SPI controller, reset the PGAs and read their state.
 *
 * @retval PGA_OK Wiper and EEPROM values read.
 * @retval PGA_ERR_SPI SPI initialization or transfer failed.
 */
int pga_spi_init(void);

/**
 * @brief Reset both PGAs; the wipers reload from EEPROM.
 *
 * The cached wiper and EEPROM values are re-read if the driver is ready.
 *
 * @retval PGA_OK Reset done.
 * @retval PGA_ERR_SPI SPI transfer failed.
 */
int pga_spi_system_reset(void);

/**
 * @brief Set all channels to their default wiper codes.
 *
 * @retval PGA_OK Codes set and verified.
 * @retval PGA_ERR_NOT_READY Driver not initialized.
 * @retval PGA_ERR_SPI SPI transfer failed.
 * @retval PGA_ERR_VERIFY Readback failed.
 */
int pga_spi_system_config(void);

/**
 * @brief Set the wiper codes of all channels and verify them.
 *
 * @param channel0 Wiper code of potentiometer 0 (ADC channel 3), up to PGA_SPI_MAX_CODE.
 * @param channel1 Wiper code of potentiometer 1 (ADC channel 2), up to PGA_SPI_MAX_CODE.
 * @param channel2 Wiper code of potentiometer 2 (ADC channel 1), up to PGA_SPI_MAX_CODE.
 * @param channel3 Wiper code of potentiometer 3 (ADC channel 0), up to PGA_SPI_MAX_CODE.
 *
 * @retval PGA_OK Codes set and verified.
 * @retval PGA_ERR_NOT_READY Driver not initialized.
 * @retval PGA_ERR_PARAM A code is out of range.
 * @retval PGA_ERR_SPI SPI transfer failed.
 * @retval PGA_ERR_VERIFY Readback failed.
 */
int pga_spi_set_rdac(uint8_t channel0, uint8_t channel1, uint8_t channel2, uint8_t channel3);

/**
 * @brief Read the wiper codes of all channels.
 *
 * @param channel0 Wiper code of potentiometer 0 (ADC channel 3).
 * @param channel1 Wiper code of potentiometer 1 (ADC channel 2).
 * @param channel2 Wiper code of potentiometer 2 (ADC channel 1).
 * @param channel3 Wiper code of potentiometer 3 (ADC channel 0).
 *
 * @retval PGA_OK Codes read.
 * @retval PGA_ERR_NOT_READY Driver not initialized.
 * @retval PGA_ERR_PARAM A pointer is NULL.
 * @retval PGA_ERR_SPI SPI transfer failed.
 */
int pga_spi_read_rdac(uint8_t *channel0, uint8_t *channel1, uint8_t *channel2, uint8_t *channel3);

/**
 * @brief Store the current wiper codes in the EEPROM of both PGAs.
 *
 * @retval PGA_OK Codes stored and verified.
 * @retval PGA_ERR_NOT_READY Driver not initialized.
 * @retval PGA_ERR_SPI SPI transfer failed.
 * @retval PGA_ERR_VERIFY EEPROM differs from the wiper codes.
 */
int pga_spi_eeprom_save_rdac(void);

/** @brief Print the wiper and EEPROM codes to the PGA log, by ADC channel. */
void pga_spi_display_settings(void);

/** @} */

#endif /* PGA_SPI_H */
