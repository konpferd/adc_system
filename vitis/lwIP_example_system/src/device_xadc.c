#include <stdbool.h>
#include "xadcps.h"
#include "xparameters.h"
#include "device_xadc.h"

static XAdcPs g_xadc;
static bool   g_xadc_ready = false;

/* XADC temperature transfer function (UG480): T = code * 503.975 / 4096 - 273.15.
 * Integer math in milli-degrees, no float formatting needed. */
static int32_t raw_to_mc(uint16_t raw)
{
    return (int32_t)(((int64_t)(raw >> 4) * 503975) / 4096) - 273150;
}

int device_xadc_init(void)
{
    XAdcPs_Config *cfg = XAdcPs_LookupConfig(XPAR_XADCPS_0_DEVICE_ID);
    if (cfg == NULL)
        return -1;
    if (XAdcPs_CfgInitialize(&g_xadc, cfg, cfg->BaseAddress) != XST_SUCCESS)
        return -2;
    if (XAdcPs_SelfTest(&g_xadc) != XST_SUCCESS)       /* also resets the XADC */
        return -3;
    XAdcPs_SetSequencerMode(&g_xadc, XADCPS_SEQ_MODE_SAFE);  /* default: on-chip sensors, calibrated */
    g_xadc_ready = true;
    return 0;
}

int device_xadc_read_temp_mc(int32_t *now_mc, int32_t *min_mc, int32_t *max_mc)
{
    if (!g_xadc_ready)
        return -1;
    *now_mc = raw_to_mc(XAdcPs_GetAdcData(&g_xadc, XADCPS_CH_TEMP));
    *max_mc = raw_to_mc(XAdcPs_GetMinMaxMeasurement(&g_xadc, XADCPS_MAX_TEMP));
    *min_mc = raw_to_mc(XAdcPs_GetMinMaxMeasurement(&g_xadc, XADCPS_MIN_TEMP));
    return 0;
}
