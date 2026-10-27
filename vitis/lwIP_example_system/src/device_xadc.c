/**
 * @file device_xadc.c
 * @brief Zynq die temperature from the PS XADC.
 */

#include <stdbool.h>
#include "xadcps.h"
#include "xparameters.h"
#include "device_xadc.h"

static XAdcPs g_xadc;             /**< XADC driver instance. */
static bool g_xadc_ready = false; /**< XADC initialized. */

/**
 * @brief Convert a raw XADC temperature code (UG480 transfer function).
 *
 * @param raw 16-bit XADC result, 12-bit code in bits 15:4.
 *
 * @return Temperature, in millidegrees Celsius.
 */
static int32_t raw_to_mc(uint16_t raw)
{
	return (int32_t)(((int64_t)(raw >> 4) * 503975) / 4096) - 273150;
}

/**
 * @brief Initialize the XADC, run its self-test and select safe sequencer mode.
 *
 * @retval 0 XADC ready.
 * @retval -1 No XADC configuration found.
 * @retval -2 Driver initialization failed.
 * @retval -3 Self-test failed.
 */
int device_xadc_init(void)
{
	XAdcPs_Config *cfg = XAdcPs_LookupConfig(XPAR_XADCPS_0_DEVICE_ID);
	if (cfg == NULL) {
		return -1;
	}
	if (XAdcPs_CfgInitialize(&g_xadc, cfg, cfg->BaseAddress) != XST_SUCCESS) {
		return -2;
	}
	if (XAdcPs_SelfTest(&g_xadc) != XST_SUCCESS) {
		return -3;
	}
	XAdcPs_SetSequencerMode(&g_xadc, XADCPS_SEQ_MODE_SAFE);
	g_xadc_ready = true;
	return 0;
}

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
int device_xadc_read_temp_mc(int32_t *now_mc, int32_t *min_mc, int32_t *max_mc)
{
	if (!g_xadc_ready) {
		return -1;
	}
	*now_mc = raw_to_mc(XAdcPs_GetAdcData(&g_xadc, XADCPS_CH_TEMP));
	*max_mc = raw_to_mc(XAdcPs_GetMinMaxMeasurement(&g_xadc, XADCPS_MAX_TEMP));
	*min_mc = raw_to_mc(XAdcPs_GetMinMaxMeasurement(&g_xadc, XADCPS_MIN_TEMP));
	return 0;
}
