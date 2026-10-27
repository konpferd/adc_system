/**
 * @file ads127l14_spi.c
 * @brief ADS127L14 ADC driver: register access and configuration over SPI.
 *
 * Every register access is verified by readback. The last read register
 * values are cached in ::adc.
 */

#include <stddef.h>
#include <string.h>
#include <stdio.h>

#include "xspips.h"
#include "xparameters.h"
#include "xil_printf.h"
#include "sleep.h"
#include "os_delay.h"
#include "device_gpio.h"
#include "ads127l14_spi.h"

#define ADS127L14_LOG_SETTING 2 /**< Log output: 0 off, 1 UART, 2 TCP console, 3 both. */

/** @brief Print to the output selected by ADS127L14_LOG_SETTING. */
#if ADS127L14_LOG_SETTING <= 0
#define ADS_LOG(...) ((void)0)
#elif ADS127L14_LOG_SETTING == 1
#define ADS_LOG(...) xil_printf(__VA_ARGS__)
#elif ADS127L14_LOG_SETTING == 2
#include "console.h"
#define ADS_LOG(...) console_printf(__VA_ARGS__)
#elif ADS127L14_LOG_SETTING >= 3
#include "console.h"
#define ADS_LOG(...)                                                                               \
	do {                                                                                       \
		char ads_log_buf_[128];                                                            \
		snprintf(ads_log_buf_, sizeof(ads_log_buf_), __VA_ARGS__);                         \
		xil_printf("%s", ads_log_buf_);                                                    \
		console_printf("%s", ads_log_buf_);                                                \
	} while (0)
#endif

#define ADS_SPI_DEVICE_ID XPAR_PS7_SPI_0_DEVICE_ID /**< SPI controller of the ADC. */
#define ADS_SPI_CS        0                        /**< Slave select of the ADC. */
#define ADS_SPI_CS_NONE   0xF                      /**< No slave selected. */

#define ADS_SPI_PRESCALER XSPIPS_CLK_PRESCALE_16 /**< SPI clock prescaler. */

#define ADS_RESET_PULSE_US    100  /**< RESET low time, in us. */
#define ADS_RESET_RECOVERY_US 1000 /**< Wait after RESET before SPI access, in us. */
#define ADS_SETTLE_US         1000 /**< Wait between a write and its readback, in us. */

#define ADS_NUM_CH    4    /**< Number of ADC channels. */
#define ADS_ADDR_MAX  0x50 /**< Highest register address. */
#define ADS_CMD_WRITE 0x80 /**< Write command bit. */
#define ADS_CMD_READ  0x00 /**< Read command bits. */

#define ADS_DEV_ID_ADS127L14 0x04 /**< DEV_ID of the ADS127L14. */

#define REG_DEV_ID     0x00             /**< DEV_ID register. */
#define REG_REV_ID     0x01             /**< REV_ID register. */
#define REG_STATUS     0x02             /**< STATUS register. */
#define REG_GEN_CFG1   0x08             /**< GEN_CFG1 register. */
#define REG_GEN_CFG2   0x09             /**< GEN_CFG2 register. */
#define REG_DP_CFG1    0x0B             /**< DP_CFG1 register. */
#define REG_DP_CFG2    0x0C             /**< DP_CFG2 register. */
#define REG_CLK_CFG    0x0D             /**< CLK_CFG register. */
#define REG_CH_CFG1(n) (0x11 + 8 * (n)) /**< CHn_CFG1 register of channel n. */
#define REG_CH_CFG2(n) (0x12 + 8 * (n)) /**< CHn_CFG2 register of channel n. */

#define GEN_CFG2_SPEED_SHIFT 1    /**< Speed mode field position. */
#define GEN_CFG2_SPEED_MASK  0x06 /**< Speed mode field. */
#define DP_CFG2_DCLK_SHIFT   5    /**< DCLK divider field position. */
#define DP_CFG2_DCLK_MASK    0x60 /**< DCLK divider field. */
#define CLK_CFG_DIV_MASK     0x07 /**< CLK divider field. */
#define CLK_CFG_SEL_MASK     0x08 /**< Clock source select: 1 external. */
#define CH_CFG2_FLTR_MASK    0x1F /**< Digital filter field. */
#define STATUS_W1C_MASK      0x7B /**< Write-1-to-clear flags of STATUS. */

#define HW_GEN_CFG1 0x05 /**< GEN_CFG1 value for this board. */
#define HW_DP_CFG1  0x30 /**< DP_CFG1 value for this board. */
#define HW_CLK_CFG  0x00 /**< CLK_CFG value for this board, without divider. */
#define HW_CH_CFG1  0x00 /**< CHn_CFG1 value for this board. */

/** Divider of each CLK_DIV code. */
static const uint8_t clk_div_value[8] = {1, 2, 3, 4, 8, 8, 8, 8};
/** Divider of each DCLK_DIV code. */
static const uint8_t dclk_div_value[4] = {1, 2, 4, 8};

/** Highest fCLK of each speed mode, in Hz. */
static const uint32_t speed_fclk_max_hz[4] = {3290000UL, 13150000UL, 26300000UL, 33660000UL};
/** Lowest fCLK in any speed mode, in Hz. */
#define FCLK_MIN_HZ 500000UL

/**
 * @brief Three times the oversampling ratio of each FLTR code.
 *
 * The factor 3 keeps the Sinc3 ratios integral.
 * - 0 to 7: wideband, OSR 32 to 4096
 * - 8 to 18: Sinc4, OSR 12 to 4096
 * - 19 to 27: Sinc4 (OSR 32) and Sinc1, OSR 2 to 1000
 * - 28 to 29: Sinc3, OSR 26667 and 32000
 * - 30 to 31: Sinc3 (OSR 32000) and Sinc1, OSR 3 and 5
 */
static const uint32_t fltr_osr_x3[32] = {96,   192,   384,   768,   1536,  3072,  6144,   12288,
					 36,   48,    72,    96,    192,   384,   768,    1536,
					 3072, 6144,  12288, 192,   384,   960,   1920,   3840,
					 9600, 19200, 38400, 96000, 80000, 96000, 288000, 480000};

static XSpiPs g_spi;          /**< PS SPI driver instance. */
static bool g_spi_up = false; /**< SPI controller initialized. */

/** @brief Driver state and cached registers. */
static struct {
	bool ready;                  /**< Device identified and configured. */
	uint8_t device_id;           /**< DEV_ID. */
	uint8_t rev_id;              /**< REV_ID. */
	uint8_t status;              /**< Last STATUS read. */
	uint8_t gen_cfg2;            /**< GEN_CFG2. */
	uint8_t dp_cfg2;             /**< DP_CFG2. */
	uint8_t clk_cfg;             /**< CLK_CFG. */
	uint8_t ch_cfg1[ADS_NUM_CH]; /**< CHn_CFG1 of each channel. */
	uint8_t ch_cfg2[ADS_NUM_CH]; /**< CHn_CFG2 of each channel. */
} adc;

/**
 * @brief Transfer one 2-byte SPI frame.
 *
 * @param b0 First byte sent.
 * @param b1 Second byte sent.
 * @param r0 First byte received, may be NULL.
 * @param r1 Second byte received, may be NULL.
 *
 * @retval ADS127L14_OK Frame transferred.
 * @retval ADS127L14_ERR_SPI SPI transfer failed.
 */
static int ads_frame(uint8_t b0, uint8_t b1, uint8_t *r0, uint8_t *r1)
{
	uint8_t tx[2] = {b0, b1};
	uint8_t rx[2] = {0, 0};

	XSpiPs_SetSlaveSelect(&g_spi, ADS_SPI_CS);
	int st = XSpiPs_PolledTransfer(&g_spi, tx, rx, sizeof(tx));
	XSpiPs_SetSlaveSelect(&g_spi, ADS_SPI_CS_NONE);

	if (st != XST_SUCCESS) {
		return ADS127L14_ERR_SPI;
	}

	if (r0) {
		*r0 = rx[0];
	}
	if (r1) {
		*r1 = rx[1];
	}
	return ADS127L14_OK;
}

/**
 * @brief Read one register.
 *
 * @param addr Register address, up to ADS_ADDR_MAX.
 * @param value Register value.
 *
 * @retval ADS127L14_OK Register read.
 * @retval ADS127L14_ERR_NOT_READY SPI controller not initialized.
 * @retval ADS127L14_ERR_PARAM @p value is NULL or @p addr out of range.
 * @retval ADS127L14_ERR_SPI SPI transfer failed.
 */
int ads127l14_reg_read(uint8_t addr, uint8_t *value)
{
	if (!g_spi_up) {
		return ADS127L14_ERR_NOT_READY;
	}

	if (value == NULL || addr > ADS_ADDR_MAX) {
		return ADS127L14_ERR_PARAM;
	}

	int rc = ads_frame((uint8_t)(ADS_CMD_READ | addr), 0x00, NULL, NULL);
	if (rc != ADS127L14_OK) {
		return rc;
	}

	return ads_frame(0x00, 0x00, value, NULL);
}

/**
 * @brief Write one register.
 *
 * @param addr Register address, up to ADS_ADDR_MAX.
 * @param value Value to write.
 *
 * @retval ADS127L14_OK Register written.
 * @retval ADS127L14_ERR_NOT_READY SPI controller not initialized.
 * @retval ADS127L14_ERR_PARAM @p addr out of range.
 * @retval ADS127L14_ERR_SPI SPI transfer failed.
 */
int ads127l14_reg_write(uint8_t addr, uint8_t value)
{
	if (!g_spi_up) {
		return ADS127L14_ERR_NOT_READY;
	}
	if (addr > ADS_ADDR_MAX) {
		return ADS127L14_ERR_PARAM;
	}

	return ads_frame((uint8_t)(ADS_CMD_WRITE | addr), value, NULL, NULL);
}

/**
 * @brief Write a register and verify it by readback.
 *
 * @param addr Register address.
 * @param value Value to write.
 * @param mask Bits compared on readback.
 *
 * @retval ADS127L14_OK Value written and verified.
 * @retval ADS127L14_ERR_VERIFY Readback differs.
 * @retval ADS127L14_ERR_SPI SPI transfer failed.
 */
static int ads_write_verify(uint8_t addr, uint8_t value, uint8_t mask)
{
	uint8_t rb;
	int rc = ads127l14_reg_write(addr, value);
	if (rc != ADS127L14_OK) {
		return rc;
	}

	os_delay_us(ADS_SETTLE_US);

	rc = ads127l14_reg_read(addr, &rb);
	if (rc != ADS127L14_OK) {
		return rc;
	}

	return ((rb & mask) == (value & mask)) ? ADS127L14_OK : ADS127L14_ERR_VERIFY;
}

/**
 * @brief Read-modify-write a register field and verify it.
 *
 * @param addr Register address.
 * @param mask Field to change.
 * @param bits New field value, already shifted.
 *
 * @retval ADS127L14_OK Field written and verified.
 * @retval ADS127L14_ERR_VERIFY Readback differs.
 * @retval ADS127L14_ERR_SPI SPI transfer failed.
 */
static int ads_update(uint8_t addr, uint8_t mask, uint8_t bits)
{
	uint8_t old;

	int rc = ads127l14_reg_read(addr, &old);
	if (rc != ADS127L14_OK) {
		return rc;
	}

	return ads_write_verify(addr, (uint8_t)((old & ~mask) | (bits & mask)), mask);
}

/**
 * @brief Get the clock source frequency selected by CLK_CFG.
 *
 * @param clk_cfg CLK_CFG value.
 *
 * @return Clock source frequency in Hz, 0 if unknown.
 */
static uint32_t src_hz_from_clk_cfg(uint8_t clk_cfg)
{
	return (clk_cfg & CLK_CFG_SEL_MASK) ? ADS127L14_FCLK_EXT_HZ : ADS127L14_FCLK_INT_HZ;
}

/**
 * @brief Check that fCLK is within the range of a speed mode.
 *
 * @param speed Speed mode.
 * @param clk_cfg CLK_CFG value.
 *
 * @return true if fCLK is within range or the clock source is unknown.
 */
static bool clocking_ok(uint8_t speed, uint8_t clk_cfg)
{
	uint32_t src = src_hz_from_clk_cfg(clk_cfg);
	if (src == 0) {
		return true;
	}

	uint32_t fclk = src / clk_div_value[clk_cfg & CLK_CFG_DIV_MASK];
	return fclk >= FCLK_MIN_HZ && fclk <= speed_fclk_max_hz[speed & 3];
}

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
					    uint8_t fltr)
{
	if (f_source_hz == 0 || clk_div_code > 7 || fltr > 31) {
		return 0;
	}

	uint64_t num = (uint64_t)f_source_hz * 3000ULL;
	uint64_t den = (uint64_t)clk_div_value[clk_div_code] * 2ULL * fltr_osr_x3[fltr];
	return (uint32_t)(num / den);
}

/**
 * @brief Get the per-channel sample rate in millihertz.
 *
 * @return Sample rate in mHz; 0 if the driver is not ready.
 */
uint32_t ads127l14_spi_get_sample_rate_mhz(void)
{
	if (!adc.ready) {
		return 0;
	}

	return ads127l14_spi_calc_sample_rate_mhz(src_hz_from_clk_cfg(adc.clk_cfg),
						  adc.clk_cfg & CLK_CFG_DIV_MASK,
						  adc.ch_cfg2[0] & CH_CFG2_FLTR_MASK);
}

/**
 * @brief Get the per-channel sample rate.
 *
 * @return Sample rate in Hz, rounded; 0 if the driver is not ready.
 */
uint32_t ads127l14_spi_get_sample_rate(void)
{
	return (ads127l14_spi_get_sample_rate_mhz() + 500) / 1000;
}

/**
 * @brief Read the configuration of all channels and check that they match.
 *
 * @retval ADS127L14_OK All channels identical.
 * @retval ADS127L14_ERR_MISMATCH At least one channel differs from channel 0.
 * @retval ADS127L14_ERR_NOT_READY SPI controller not initialized.
 * @retval ADS127L14_ERR_SPI SPI transfer failed.
 */
int ads127l14_spi_verify_channels(void)
{
	if (!g_spi_up) {
		return ADS127L14_ERR_NOT_READY;
	}

	for (int n = 0; n < ADS_NUM_CH; n++) {
		int rc = ads127l14_reg_read(REG_CH_CFG1(n), &adc.ch_cfg1[n]);
		if (rc != ADS127L14_OK) {
			return rc;
		}

		rc = ads127l14_reg_read(REG_CH_CFG2(n), &adc.ch_cfg2[n]);
		if (rc != ADS127L14_OK) {
			return rc;
		}
	}
	for (int n = 1; n < ADS_NUM_CH; n++) {
		if (adc.ch_cfg1[n] != adc.ch_cfg1[0] || adc.ch_cfg2[n] != adc.ch_cfg2[0]) {
			ADS_LOG("[ADC] CHANNEL MISMATCH: CH0 %02X/%02X vs CH%d %02X/%02X\r\n",
				adc.ch_cfg1[0], adc.ch_cfg2[0], n, adc.ch_cfg1[n], adc.ch_cfg2[n]);
			return ADS127L14_ERR_MISMATCH;
		}
	}
	return ADS127L14_OK;
}

/**
 * @brief Refresh the cached clock, data port and channel registers.
 *
 * @retval ADS127L14_OK Registers read and channels identical.
 * @retval ADS127L14_ERR_MISMATCH Channels differ.
 * @retval ADS127L14_ERR_SPI SPI transfer failed.
 */
static int ads_sync(void)
{
	int rc = ads127l14_reg_read(REG_GEN_CFG2, &adc.gen_cfg2);
	if (rc != ADS127L14_OK) {
		return rc;
	}

	rc = ads127l14_reg_read(REG_DP_CFG2, &adc.dp_cfg2);
	if (rc != ADS127L14_OK) {
		return rc;
	}

	rc = ads127l14_reg_read(REG_CLK_CFG, &adc.clk_cfg);
	if (rc != ADS127L14_OK) {
		return rc;
	}

	return ads127l14_spi_verify_channels();
}

/** @brief Pulse the RESET pin and wait for the ADC to recover. */
static void ads_hw_reset(void)
{
	device_gpio_write_pin(ADC_RESET_PIN, 0);
	os_delay_us(ADS_RESET_PULSE_US);
	device_gpio_write_pin(ADC_RESET_PIN, 1);
	os_delay_us(ADS_RESET_RECOVERY_US);
}

/**
 * @brief Initialize the SPI controller, reset the ADC and read its configuration.
 *
 * @retval ADS127L14_OK Device identified and configuration read.
 * @retval ADS127L14_ERR_MISMATCH Channel configurations differ; the driver stays usable.
 * @retval ADS127L14_ERR_DEV_ID The device is not an ADS127L14.
 * @retval ADS127L14_ERR_SPI SPI initialization or transfer failed.
 */
int ads127l14_spi_init(void)
{
	adc.ready = false;
	g_spi_up = false;

	XSpiPs_Config *cfg = XSpiPs_LookupConfig(ADS_SPI_DEVICE_ID);
	if (cfg == NULL) {
		return ADS127L14_ERR_SPI;
	}

	if (XSpiPs_CfgInitialize(&g_spi, cfg, cfg->BaseAddress) != XST_SUCCESS) {
		return ADS127L14_ERR_SPI;
	}

	if (XSpiPs_SetOptions(&g_spi, XSPIPS_MASTER_OPTION | XSPIPS_MANUAL_START_OPTION |
					      XSPIPS_FORCE_SSELECT_OPTION |
					      XSPIPS_CLK_PHASE_1_OPTION) != XST_SUCCESS) {
		return ADS127L14_ERR_SPI;
	}

	if (XSpiPs_SetClkPrescaler(&g_spi, ADS_SPI_PRESCALER) != XST_SUCCESS) {
		return ADS127L14_ERR_SPI;
	}

	XSpiPs_SetSlaveSelect(&g_spi, ADS_SPI_CS_NONE);
	g_spi_up = true;

	ads_hw_reset();

	int rc = ads127l14_reg_read(REG_DEV_ID, &adc.device_id);
	if (rc != ADS127L14_OK) {
		return rc;
	}

	if (adc.device_id != ADS_DEV_ID_ADS127L14) {
		return ADS127L14_ERR_DEV_ID;
	}

	rc = ads127l14_reg_read(REG_REV_ID, &adc.rev_id);
	if (rc != ADS127L14_OK) {
		return rc;
	}

	adc.ready = true;
	rc = ads_sync();
	if (rc != ADS127L14_OK && rc != ADS127L14_ERR_MISMATCH) {
		adc.ready = false;
	}

	return rc;
}

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
int ads127l14_spi_read_device_data(uint8_t *device_id, uint8_t *revision)
{
	if (!adc.ready) {
		return ADS127L14_ERR_NOT_READY;
	}

	if (device_id == NULL || revision == NULL) {
		return ADS127L14_ERR_PARAM;
	}

	int rc = ads127l14_reg_read(REG_DEV_ID, device_id);
	if (rc != ADS127L14_OK) {
		return rc;
	}

	rc = ads127l14_reg_read(REG_REV_ID, revision);
	if (rc != ADS127L14_OK) {
		return rc;
	}

	adc.device_id = *device_id;
	adc.rev_id = *revision;
	return ADS127L14_OK;
}

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
int ads127l14_spi_read_status(uint8_t *status)
{
	if (!adc.ready) {
		return ADS127L14_ERR_NOT_READY;
	}

	if (status == NULL) {
		return ADS127L14_ERR_PARAM;
	}

	int rc = ads127l14_reg_read(REG_STATUS, status);
	if (rc == ADS127L14_OK) {
		adc.status = *status;
	}

	return rc;
}

/**
 * @brief Clear the write-1-to-clear flags of STATUS and read it back.
 *
 * @retval ADS127L14_OK Flags cleared.
 * @retval ADS127L14_ERR_NOT_READY Driver not initialized.
 * @retval ADS127L14_ERR_SPI SPI transfer failed.
 */
int ads127l14_spi_clear_status(void)
{
	if (!adc.ready) {
		return ADS127L14_ERR_NOT_READY;
	}

	int rc = ads127l14_reg_write(REG_STATUS, STATUS_W1C_MASK);
	if (rc != ADS127L14_OK) {
		return rc;
	}

	os_delay_us(ADS_SETTLE_US);
	return ads127l14_reg_read(REG_STATUS, &adc.status);
}

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
int ads127l14_spi_set_speed_mode(uint8_t speed_mode)
{
	if (!adc.ready) {
		return ADS127L14_ERR_NOT_READY;
	}

	if (speed_mode > ADS127L14_SPEED_MAX) {
		return ADS127L14_ERR_PARAM;
	}

	uint8_t clk_cfg;

	int rc = ads127l14_reg_read(REG_CLK_CFG, &clk_cfg);
	if (rc != ADS127L14_OK) {
		return rc;
	}

	if (!clocking_ok(speed_mode, clk_cfg)) {
		return ADS127L14_ERR_CLK_LIMIT;
	}

	rc = ads_update(REG_GEN_CFG2, GEN_CFG2_SPEED_MASK,
			(uint8_t)(speed_mode << GEN_CFG2_SPEED_SHIFT));

	(void)ads127l14_reg_read(REG_GEN_CFG2, &adc.gen_cfg2);
	return rc;
}

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
int ads127l14_spi_set_dclk_div(uint8_t dclk_div)
{
	if (!adc.ready) {
		return ADS127L14_ERR_NOT_READY;
	}

	if (dclk_div > 3) {
		return ADS127L14_ERR_PARAM;
	}

	int rc = ads_update(REG_DP_CFG2, DP_CFG2_DCLK_MASK,
			    (uint8_t)(dclk_div << DP_CFG2_DCLK_SHIFT));

	(void)ads127l14_reg_read(REG_DP_CFG2, &adc.dp_cfg2);
	return rc;
}

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
int ads127l14_spi_set_clk_div(uint8_t clk_div)
{
	if (!adc.ready) {
		return ADS127L14_ERR_NOT_READY;
	}

	if (clk_div > 4) {
		return ADS127L14_ERR_PARAM;
	}

	uint8_t gen2, clk_cfg;
	int rc = ads127l14_reg_read(REG_GEN_CFG2, &gen2);
	if (rc != ADS127L14_OK) {
		return rc;
	}

	rc = ads127l14_reg_read(REG_CLK_CFG, &clk_cfg);
	if (rc != ADS127L14_OK) {
		return rc;
	}

	uint8_t speed = (uint8_t)((gen2 & GEN_CFG2_SPEED_MASK) >> GEN_CFG2_SPEED_SHIFT);
	if (!clocking_ok(speed, (uint8_t)((clk_cfg & ~CLK_CFG_DIV_MASK) | clk_div))) {
		return ADS127L14_ERR_CLK_LIMIT;
	}

	rc = ads_update(REG_CLK_CFG, CLK_CFG_DIV_MASK, clk_div);
	(void)ads127l14_reg_read(REG_CLK_CFG, &adc.clk_cfg);
	return rc;
}

/**
 * @brief Set the speed mode and the CLK divider together.
 *
 * The register written first is the one that keeps fCLK within the limits
 * of the active speed mode.
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
int ads127l14_spi_set_clocking(uint8_t speed_mode, uint8_t clk_div)
{
	if (!adc.ready) {
		return ADS127L14_ERR_NOT_READY;
	}

	if (speed_mode > ADS127L14_SPEED_MAX || clk_div > 4) {
		return ADS127L14_ERR_PARAM;
	}

	uint8_t gen2, clk_cfg;
	int rc = ads127l14_reg_read(REG_GEN_CFG2, &gen2);
	if (rc != ADS127L14_OK) {
		return rc;
	}

	rc = ads127l14_reg_read(REG_CLK_CFG, &clk_cfg);
	if (rc != ADS127L14_OK) {
		return rc;
	}

	uint8_t old_speed = (uint8_t)((gen2 & GEN_CFG2_SPEED_MASK) >> GEN_CFG2_SPEED_SHIFT);
	uint8_t new_clk = (uint8_t)((clk_cfg & ~CLK_CFG_DIV_MASK) | clk_div);

	if (!clocking_ok(speed_mode, new_clk)) {
		return ADS127L14_ERR_CLK_LIMIT;
	}

	bool div_first = clocking_ok(old_speed, new_clk);
	if (div_first) {
		rc = ads_update(REG_CLK_CFG, CLK_CFG_DIV_MASK, clk_div);
		if (rc == ADS127L14_OK) {
			rc = ads_update(REG_GEN_CFG2, GEN_CFG2_SPEED_MASK,
					(uint8_t)(speed_mode << GEN_CFG2_SPEED_SHIFT));
		}
	} else {
		rc = ads_update(REG_GEN_CFG2, GEN_CFG2_SPEED_MASK,
				(uint8_t)(speed_mode << GEN_CFG2_SPEED_SHIFT));
		if (rc == ADS127L14_OK) {
			rc = ads_update(REG_CLK_CFG, CLK_CFG_DIV_MASK, clk_div);
		}
	}

	(void)ads127l14_reg_read(REG_GEN_CFG2, &adc.gen_cfg2);
	(void)ads127l14_reg_read(REG_CLK_CFG, &adc.clk_cfg);
	return rc;
}

/**
 * @brief Write CHn_CFG2 of all channels and verify them.
 *
 * @param value CHn_CFG2 value.
 *
 * @retval ADS127L14_OK All channels written and verified.
 * @retval ADS127L14_ERR_VERIFY A readback differs.
 * @retval ADS127L14_ERR_SPI SPI transfer failed.
 */
static int ads_write_cfg2_all(uint8_t value)
{
	int result = ADS127L14_OK;

	for (int n = 0; n < ADS_NUM_CH; n++) {
		int rc = ads127l14_reg_write(REG_CH_CFG2(n), value);
		if (rc != ADS127L14_OK) {
			return rc;
		}
	}

	os_delay_us(ADS_SETTLE_US);

	for (int n = 0; n < ADS_NUM_CH; n++) {
		uint8_t rb;
		int rc = ads127l14_reg_read(REG_CH_CFG2(n), &rb);
		if (rc != ADS127L14_OK) {
			return rc;
		}

		if (rb != value) {
			result = ADS127L14_ERR_VERIFY;
		}
	}
	return result;
}

/**
 * @brief Set the digital filter on all channels.
 *
 * @param fltr Filter code, 0 to 31.
 *
 * @retval ADS127L14_OK Filter set and channels identical.
 * @retval ADS127L14_ERR_NOT_READY Driver not initialized.
 * @retval ADS127L14_ERR_PARAM @p fltr out of range.
 * @retval ADS127L14_ERR_VERIFY Readback failed; previous filter restored.
 * @retval ADS127L14_ERR_MISMATCH Channels differ after the update.
 */
int ads127l14_spi_set_fltr(uint8_t fltr)
{
	if (!adc.ready) {
		return ADS127L14_ERR_NOT_READY;
	}

	if (fltr > 31) {
		return ADS127L14_ERR_PARAM;
	}

	uint8_t cur;
	int rc = ads127l14_reg_read(REG_CH_CFG2(0), &cur);
	if (rc != ADS127L14_OK) {
		return rc;
	}

	rc = ads_write_cfg2_all((uint8_t)((cur & ~CH_CFG2_FLTR_MASK) | fltr));
	if (rc != ADS127L14_OK) {
		(void)ads_write_cfg2_all(cur);
		(void)ads127l14_spi_verify_channels();
		return rc;
	}

	return ads127l14_spi_verify_channels();
}

/**
 * @brief Write and verify one register of the start-up configuration.
 *
 * @param name Register name for the log.
 * @param addr Register address.
 * @param value Value to write.
 *
 * @retval ADS127L14_OK Register written and verified.
 * @retval ADS127L14_ERR_VERIFY Readback differs.
 * @retval ADS127L14_ERR_SPI SPI transfer failed.
 */
static int ads_cfg_step(const char *name, uint8_t addr, uint8_t value)
{
	ADS_LOG("[ADC CONFIG] Configuring %s ...", name);
	int rc = ads_write_verify(addr, value, 0xFF);
	if (rc == ADS127L14_OK) {
		ADS_LOG(" PASS [CONFIG: %02X]\r\n", value);
	} else {
		ADS_LOG(" FAIL [ERR: %d]\r\n", rc);
	}

	return rc;
}

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
int ads127l14_spi_system_config(void)
{
	if (!g_spi_up) {
		return ADS127L14_ERR_NOT_READY;
	}

	const uint8_t gen_cfg2 =
		(uint8_t)(ADS127L14_SPI_DEFAULT_SPEED_MODE << GEN_CFG2_SPEED_SHIFT);
	const uint8_t dp_cfg2 = (uint8_t)(ADS127L14_SPI_DEFAULT_DCLK_DIV << DP_CFG2_DCLK_SHIFT);
	const uint8_t clk_cfg = (uint8_t)(HW_CLK_CFG | ADS127L14_SPI_DEFAULT_CLK_DIV);
	const uint8_t ch_cfg2 = (uint8_t)(ADS127L14_SPI_DEFAULT_FLTR & CH_CFG2_FLTR_MASK);

	if (ADS127L14_SPI_DEFAULT_SPEED_MODE > ADS127L14_SPEED_MAX ||
	    ADS127L14_SPI_DEFAULT_DCLK_DIV > 3 || ADS127L14_SPI_DEFAULT_CLK_DIV > 4 ||
	    ADS127L14_SPI_DEFAULT_FLTR > 31) {
		return ADS127L14_ERR_PARAM;
	}
	if (!clocking_ok(ADS127L14_SPI_DEFAULT_SPEED_MODE, clk_cfg)) {
		ADS_LOG("[ADC CONFIG] default speed mode / clock divider combination is out of "
			"spec\r\n");
		return ADS127L14_ERR_CLK_LIMIT;
	}

	adc.ready = false;
	ads_hw_reset();

	uint8_t resp;
	ADS_LOG("[ADC CONFIG] Verifying Device ID ...");
	int rc = ads127l14_reg_read(REG_DEV_ID, &resp);
	if (rc != ADS127L14_OK) {
		ADS_LOG(" READ FAIL [ERR: %d]\r\n", rc);
		return rc;
	}
	if (resp != ADS_DEV_ID_ADS127L14) {
		ADS_LOG(" FAIL [ID: %02X]\r\n", resp);
		return ADS127L14_ERR_DEV_ID;
	}
	ADS_LOG(" PASS [ID: %02X]\r\n", resp);
	adc.device_id = resp;

	rc = ads127l14_reg_read(REG_REV_ID, &adc.rev_id);
	if (rc != ADS127L14_OK) {
		return rc;
	}
	ADS_LOG("[ADC CONFIG] Revision: %02X\r\n", adc.rev_id);

	if ((rc = ads_cfg_step("GEN_CFG1", REG_GEN_CFG1, HW_GEN_CFG1)) != ADS127L14_OK) {
		return rc;
	}
	if ((rc = ads_cfg_step("GEN_CFG2", REG_GEN_CFG2, gen_cfg2)) != ADS127L14_OK) {
		return rc;
	}
	if ((rc = ads_cfg_step("DP_CFG1", REG_DP_CFG1, HW_DP_CFG1)) != ADS127L14_OK) {
		return rc;
	}
	if ((rc = ads_cfg_step("DP_CFG2", REG_DP_CFG2, dp_cfg2)) != ADS127L14_OK) {
		return rc;
	}
	if ((rc = ads_cfg_step("CLK_CFG", REG_CLK_CFG, clk_cfg)) != ADS127L14_OK) {
		return rc;
	}

	for (int n = 0; n < ADS_NUM_CH; n++) {
		char name[10];
		name[0] = 'C';
		name[1] = 'H';
		name[2] = (char)('0' + n);
		name[3] = '_';
		name[4] = 'C';
		name[5] = 'F';
		name[6] = 'G';
		name[7] = '1';
		name[8] = '\0';
		if ((rc = ads_cfg_step(name, REG_CH_CFG1(n), HW_CH_CFG1)) != ADS127L14_OK) {
			return rc;
		}
		name[7] = '2';
		if ((rc = ads_cfg_step(name, REG_CH_CFG2(n), ch_cfg2)) != ADS127L14_OK) {
			return rc;
		}
	}

	adc.ready = true;
	rc = ads_sync();
	if (rc != ADS127L14_OK) {
		adc.ready = false;
		return rc;
	}

	ADS_LOG("[ADC CONFIG] All channels identical. Sample rate: %lu Hz\r\n",
		(unsigned long)ads127l14_spi_get_sample_rate());
	return ADS127L14_OK;
}

/**
 * @brief Log a rate in Hz with three decimals.
 *
 * @param mhz Rate, in mHz.
 */
static void print_rate(uint32_t mhz)
{
	ADS_LOG("%lu.%03lu Hz", (unsigned long)(mhz / 1000), (unsigned long)(mhz % 1000));
}

/**
 * @brief Log the filter type and OSR of a FLTR code.
 *
 * @param f Filter code, 0 to 31.
 */
static void print_filter(uint8_t f)
{
	uint32_t osr = (fltr_osr_x3[f] + 1) / 3;
	const char *type = (f < 8)    ? "Wideband"
			   : (f < 19) ? "Sinc4"
			   : (f < 28) ? "Sinc4+Sinc1 (total OSR)"
			   : (f < 30) ? "Sinc3"
				      : "Sinc3+Sinc1 (total OSR)";
	ADS_LOG("%s, OSR = %lu", type, (unsigned long)osr);
}

/** @brief Print the current configuration to the ADC log. */
void ads127l14_spi_display_settings(void)
{
	static const char *const speed_name[4] = {"Low", "Mid", "High", "Max"};

	ADS_LOG("====== ADC SETTINGS ======\r\n");
	ADS_LOG("ADC %s\r\n", adc.ready ? "enabled" : "disabled");
	ADS_LOG("Device ID / rev : 0x%02X / 0x%02X\r\n", adc.device_id, adc.rev_id);
	ADS_LOG("Status          : 0x%02X\r\n", adc.status);
	if (!adc.ready) {
		ADS_LOG("==========================\r\n");
		return;
	}

	uint8_t speed = (uint8_t)((adc.gen_cfg2 & GEN_CFG2_SPEED_MASK) >> GEN_CFG2_SPEED_SHIFT);
	uint8_t dcode = (uint8_t)((adc.dp_cfg2 & DP_CFG2_DCLK_MASK) >> DP_CFG2_DCLK_SHIFT);
	uint8_t ccode = adc.clk_cfg & CLK_CFG_DIV_MASK;
	uint32_t src = src_hz_from_clk_cfg(adc.clk_cfg);
	uint32_t fclk = src / clk_div_value[ccode];
	uint32_t rate = ads127l14_spi_get_sample_rate_mhz();
	uint32_t fdclk = src / dclk_div_value[dcode];

	ADS_LOG("Speed mode      : %s (%u)\r\n", speed_name[speed], speed);
	ADS_LOG("Clock source    : %s, /%u -> fCLK = %lu Hz\r\n",
		(adc.clk_cfg & CLK_CFG_SEL_MASK) ? "external" : "internal oscillator",
		clk_div_value[ccode], (unsigned long)fclk);
	if (!clocking_ok(speed, adc.clk_cfg)) {
		ADS_LOG("  WARNING: fCLK is outside the range allowed for this speed mode\r\n");
	}
	ADS_LOG("Filter          : ");
	print_filter(adc.ch_cfg2[0] & CH_CFG2_FLTR_MASK);
	ADS_LOG(" (FLTR=%u)\r\n", adc.ch_cfg2[0] & CH_CFG2_FLTR_MASK);
	ADS_LOG("Sample rate     : ");
	print_rate(rate);
	ADS_LOG(" per channel\r\n");
	ADS_LOG("DCLK            : /%u -> %lu Hz", dclk_div_value[dcode], (unsigned long)fdclk);
	if (rate) {
		uint32_t cycles = (uint32_t)(((uint64_t)fdclk * 1000ULL) / rate);
		ADS_LOG(", %lu DCLK per FSYNC frame", (unsigned long)cycles);
		if (cycles < 24) {
			ADS_LOG("  WARNING: < 24 bits needed per frame");
		}
	}
	ADS_LOG("\r\n");

	for (int n = 0; n < ADS_NUM_CH; n++) {
		ADS_LOG("CH%d CFG1/CFG2   : 0x%02X / 0x%02X\r\n", n, adc.ch_cfg1[n],
			adc.ch_cfg2[n]);
	}
	ADS_LOG("Channels        : %s\r\n",
		(ads127l14_spi_verify_channels() == ADS127L14_OK) ? "identical" : "MISMATCH");
	ADS_LOG("==========================\r\n");
}
