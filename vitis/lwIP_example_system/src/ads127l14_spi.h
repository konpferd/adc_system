/**
 * @file ads127l14_spi.h
 * @brief ADS127L14 ADC driver: register access and configuration over SPI.
 *
 * @defgroup ads127l14 ADS127L14 ADC driver
 * @{
 */

#ifndef ADS127L14_SPI_H
#define ADS127L14_SPI_H

#include <stdint.h>
#include <stdbool.h>

/** @brief Driver return codes. */
typedef enum {
	ADS127L14_OK = 0,             /**< Success. */
	ADS127L14_ERR_NOT_READY = -1, /**< Initialisation or configuration not done or failed. */
	ADS127L14_ERR_PARAM = -2,     /**< Argument out of range. */
	ADS127L14_ERR_SPI = -3,       /**< SPI transfer failed. */
	ADS127L14_ERR_VERIFY = -4,    /**< Readback differs from the written value. */
	ADS127L14_ERR_CLK_LIMIT = -5, /**< fCLK outside the range of the speed mode. */
	ADS127L14_ERR_MISMATCH = -6,  /**< Channels are not configured identically. */
	ADS127L14_ERR_DEV_ID = -7     /**< DEV_ID is not 0x04 (ADS127L14). */
} ads127l14_status_t;

/** @brief Speed modes and their fCLK limits. */
typedef enum {
	ADS127L14_SPEED_LOW = 0,  /**< fCLK up to 3.29 MHz (nominal 3.2 MHz). */
	ADS127L14_SPEED_MID = 1,  /**< fCLK up to 13.15 MHz (nominal 12.8 MHz). */
	ADS127L14_SPEED_HIGH = 2, /**< fCLK up to 26.3 MHz (nominal 25.6 MHz). */
	ADS127L14_SPEED_MAX = 3   /**< fCLK up to 33.66 MHz (nominal 32.768 MHz). */
} ads127l14_speed_t;

#ifndef ADS127L14_FCLK_INT_HZ
#define ADS127L14_FCLK_INT_HZ 25600000UL /**< Internal oscillator frequency, in Hz. */
#endif
#ifndef ADS127L14_FCLK_EXT_HZ
#define ADS127L14_FCLK_EXT_HZ 0UL /**< External clock frequency in Hz, 0 if not fitted. */
#endif

#define ADS127L14_SPI_DEFAULT_SPEED_MODE ADS127L14_SPEED_HIGH /**< Default speed mode. */
#define ADS127L14_SPI_DEFAULT_DCLK_DIV   0 /**< DCLK divider code: 0:/1 1:/2 2:/4 3:/8. */
#define ADS127L14_SPI_DEFAULT_CLK_DIV    0 /**< CLK divider code: 0:/1 1:/2 2:/3 3:/4 4:/8. */
#define ADS127L14_SPI_DEFAULT_FLTR       9 /**< Filter code: Sinc4, OSR 16. */

#define ADS127L14_FLTR_WIDEBAND_OSR32 0  /**< Filter code: wideband, OSR 32. */
#define ADS127L14_FLTR_WIDEBAND_OSR64 1  /**< Filter code: wideband, OSR 64. */
#define ADS127L14_FLTR_SINC4_OSR16    9  /**< Filter code: Sinc4, OSR 16. */
#define ADS127L14_FLTR_SINC4_OSR32    11 /**< Filter code: Sinc4, OSR 32. */
#define ADS127L14_FLTR_SINC4_OSR64    12 /**< Filter code: Sinc4, OSR 64. */

/**
 * @brief Initialize the SPI controller, reset the ADC and read its configuration.
 *
 * @retval ADS127L14_OK Device identified and configuration read.
 * @retval ADS127L14_ERR_MISMATCH Channel configurations differ; the driver stays usable.
 * @retval ADS127L14_ERR_DEV_ID The device is not an ADS127L14.
 * @retval ADS127L14_ERR_SPI SPI initialization or transfer failed.
 */
int ads127l14_spi_init(void);

/**
 * @brief Reset the ADC and write the default configuration with readback.
 *
 * @retval ADS127L14_OK Configuration applied and all channels identical.
 * @retval ADS127L14_ERR_NOT_READY SPI controller not initialized.
 * @retval ADS127L14_ERR_PARAM Default settings out of range.
 * @retval ADS127L14_ERR_CLK_LIMIT Default speed mode and divider are out of spec.
 * @retval ADS127L14_ERR_DEV_ID The device is not an ADS127L14.
 * @retval ADS127L14_ERR_VERIFY A register readback failed.
 * @retval ADS127L14_ERR_SPI SPI transfer failed.
 */
int ads127l14_spi_system_config(void);

/**
 * @brief Read one register.
 *
 * @param addr Register address, up to 0x50.
 * @param value Register value.
 *
 * @retval ADS127L14_OK Register read.
 * @retval ADS127L14_ERR_NOT_READY SPI controller not initialized.
 * @retval ADS127L14_ERR_PARAM @p value is NULL or @p addr out of range.
 * @retval ADS127L14_ERR_SPI SPI transfer failed.
 */
int ads127l14_reg_read(uint8_t addr, uint8_t *value);

/**
 * @brief Write one register.
 *
 * @param addr Register address, up to 0x50.
 * @param value Value to write.
 *
 * @retval ADS127L14_OK Register written.
 * @retval ADS127L14_ERR_NOT_READY SPI controller not initialized.
 * @retval ADS127L14_ERR_PARAM @p addr out of range.
 * @retval ADS127L14_ERR_SPI SPI transfer failed.
 */
int ads127l14_reg_write(uint8_t addr, uint8_t value);

/**
 * @brief Read the DEV_ID and REV_ID registers.
 *
 * @param device_id Device ID.
 * @param revision Silicon revision.
 *
 * @retval ADS127L14_OK Registers read.
 * @retval ADS127L14_ERR_NOT_READY Driver not initialized.
 * @retval ADS127L14_ERR_PARAM A pointer is NULL.
 * @retval ADS127L14_ERR_SPI SPI transfer failed.
 */
int ads127l14_spi_read_device_data(uint8_t *device_id, uint8_t *revision);

/**
 * @brief Read the STATUS register.
 *
 * @param status STATUS register value.
 *
 * @retval ADS127L14_OK Register read.
 * @retval ADS127L14_ERR_NOT_READY Driver not initialized.
 * @retval ADS127L14_ERR_PARAM @p status is NULL.
 * @retval ADS127L14_ERR_SPI SPI transfer failed.
 */
int ads127l14_spi_read_status(uint8_t *status);

/**
 * @brief Clear the write-1-to-clear flags of STATUS and read it back.
 *
 * @retval ADS127L14_OK Flags cleared.
 * @retval ADS127L14_ERR_NOT_READY Driver not initialized.
 * @retval ADS127L14_ERR_SPI SPI transfer failed.
 */
int ads127l14_spi_clear_status(void);

/**
 * @brief Set the speed mode.
 *
 * @param speed_mode Speed mode, see ads127l14_speed_t.
 *
 * @retval ADS127L14_OK Speed mode set.
 * @retval ADS127L14_ERR_NOT_READY Driver not initialized.
 * @retval ADS127L14_ERR_PARAM @p speed_mode out of range.
 * @retval ADS127L14_ERR_CLK_LIMIT Current fCLK is outside the range of the mode.
 * @retval ADS127L14_ERR_VERIFY Readback failed.
 */
int ads127l14_spi_set_speed_mode(uint8_t speed_mode);

/**
 * @brief Set the DCLK divider.
 *
 * @param dclk_div Divider code: 0:/1 1:/2 2:/4 3:/8.
 *
 * @retval ADS127L14_OK Divider set.
 * @retval ADS127L14_ERR_NOT_READY Driver not initialized.
 * @retval ADS127L14_ERR_PARAM @p dclk_div out of range.
 * @retval ADS127L14_ERR_VERIFY Readback failed.
 */
int ads127l14_spi_set_dclk_div(uint8_t dclk_div);

/**
 * @brief Set the CLK divider.
 *
 * @param clk_div Divider code: 0:/1 1:/2 2:/3 3:/4 4:/8.
 *
 * @retval ADS127L14_OK Divider set.
 * @retval ADS127L14_ERR_NOT_READY Driver not initialized.
 * @retval ADS127L14_ERR_PARAM @p clk_div out of range.
 * @retval ADS127L14_ERR_CLK_LIMIT Resulting fCLK is outside the current speed mode.
 * @retval ADS127L14_ERR_VERIFY Readback failed.
 */
int ads127l14_spi_set_clk_div(uint8_t clk_div);

/**
 * @brief Set the speed mode and the CLK divider together.
 *
 * The two registers are written in the order that keeps fCLK within the
 * limits of the active speed mode.
 *
 * @param speed_mode Speed mode, see ads127l14_speed_t.
 * @param clk_div Divider code: 0:/1 1:/2 2:/3 3:/4 4:/8.
 *
 * @retval ADS127L14_OK Both settings applied.
 * @retval ADS127L14_ERR_NOT_READY Driver not initialized.
 * @retval ADS127L14_ERR_PARAM An argument is out of range.
 * @retval ADS127L14_ERR_CLK_LIMIT The combination is out of spec.
 * @retval ADS127L14_ERR_VERIFY Readback failed.
 */
int ads127l14_spi_set_clocking(uint8_t speed_mode, uint8_t clk_div);

/**
 * @brief Set the digital filter on all channels.
 *
 * The previous setting is restored if a channel fails to verify.
 *
 * @param fltr Filter code, 0 to 31.
 *
 * @retval ADS127L14_OK Filter set and channels identical.
 * @retval ADS127L14_ERR_NOT_READY Driver not initialized.
 * @retval ADS127L14_ERR_PARAM @p fltr out of range.
 * @retval ADS127L14_ERR_VERIFY Readback failed.
 * @retval ADS127L14_ERR_MISMATCH Channels differ after the update.
 */
int ads127l14_spi_set_fltr(uint8_t fltr);

/**
 * @brief Read the configuration of all channels and check that they match.
 *
 * @retval ADS127L14_OK All channels identical.
 * @retval ADS127L14_ERR_MISMATCH At least one channel differs from channel 0.
 * @retval ADS127L14_ERR_NOT_READY SPI controller not initialized.
 * @retval ADS127L14_ERR_SPI SPI transfer failed.
 */
int ads127l14_spi_verify_channels(void);

/** @brief Print the current configuration to the ADC log. */
void ads127l14_spi_display_settings(void);

/**
 * @brief Get the per-channel sample rate.
 *
 * @return Sample rate in Hz, rounded; 0 if the driver is not ready.
 */
uint32_t ads127l14_spi_get_sample_rate(void);

/**
 * @brief Get the per-channel sample rate in millihertz.
 *
 * @return Sample rate in mHz; 0 if the driver is not ready.
 */
uint32_t ads127l14_spi_get_sample_rate_mhz(void);

/**
 * @brief Compute the sample rate for a clock and filter setting.
 *
 * @param f_source_hz Clock source frequency, in Hz.
 * @param clk_div_code CLK divider code, 0 to 7.
 * @param fltr Filter code, 0 to 31.
 *
 * @return Sample rate in mHz; 0 if an argument is invalid.
 */
uint32_t ads127l14_spi_calc_sample_rate_mhz(uint32_t f_source_hz, uint8_t clk_div_code,
					    uint8_t fltr);

/** @} */

#endif /* ADS127L14_SPI_H */
