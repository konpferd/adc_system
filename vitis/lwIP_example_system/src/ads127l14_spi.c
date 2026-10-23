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

/* Logging settings:
 * <=0 	- no logs
 * 1 	- UART console only
 * 2 	- TCP console only
 * >=3 	- TCP + UART
 */
#define ADS127L14_LOG_SETTING        2

#if ADS127L14_LOG_SETTING <= 0
#define ADS_LOG(...)             ((void)0)
#elif ADS127L14_LOG_SETTING == 1
#define ADS_LOG(...)             xil_printf(__VA_ARGS__)
#elif ADS127L14_LOG_SETTING == 2
#include "console.h"
#define ADS_LOG(...)             console_printf(__VA_ARGS__)
#elif ADS127L14_LOG_SETTING >= 3
#include "console.h"
#define ADS_LOG(...)                                    \
    do {                                                \
        char ads_log_buf_[128];                         \
        snprintf(ads_log_buf_, sizeof(ads_log_buf_),    \
                 __VA_ARGS__);                          \
        xil_printf("%s", ads_log_buf_);                 \
        console_printf("%s", ads_log_buf_);             \
    } while (0)
#endif

#define ADS_SPI_DEVICE_ID        XPAR_PS7_SPI_0_DEVICE_ID
#define ADS_SPI_CS               0
#define ADS_SPI_CS_NONE          0xF

#define ADS_SPI_PRESCALER        XSPIPS_CLK_PRESCALE_16

#define ADS_RESET_PULSE_US       100
#define ADS_RESET_RECOVERY_US    1000
#define ADS_SETTLE_US            1000

#define ADS_NUM_CH               4
#define ADS_ADDR_MAX             0x50
#define ADS_CMD_WRITE            0x80
#define ADS_CMD_READ           	 0x00

#define ADS_DEV_ID_ADS127L14     0x04

#define REG_DEV_ID               0x00
#define REG_REV_ID               0x01
#define REG_STATUS               0x02
#define REG_GEN_CFG1             0x08
#define REG_GEN_CFG2             0x09
#define REG_DP_CFG1              0x0B
#define REG_DP_CFG2              0x0C
#define REG_CLK_CFG              0x0D
#define REG_CH_CFG1(n)           (0x11 + 8 * (n))
#define REG_CH_CFG2(n)           (0x12 + 8 * (n))

#define GEN_CFG2_SPEED_SHIFT     1
#define GEN_CFG2_SPEED_MASK      0x06
#define DP_CFG2_DCLK_SHIFT       5
#define DP_CFG2_DCLK_MASK        0x60
#define CLK_CFG_DIV_MASK         0x07
#define CLK_CFG_SEL_MASK         0x08
#define CH_CFG2_FLTR_MASK        0x1F
#define STATUS_W1C_MASK          0x7B

#define HW_GEN_CFG1   0x05
#define HW_DP_CFG1    0x30
#define HW_CLK_CFG    0x00
#define HW_CH_CFG1    0x00

static const uint8_t  clk_div_value[8]  = { 1, 2, 3, 4, 8, 8, 8, 8 };
static const uint8_t  dclk_div_value[4] = { 1, 2, 4, 8 };
static const uint32_t speed_fclk_max_hz[4] = { 3290000UL, 13150000UL, 26300000UL, 33660000UL };
#define FCLK_MIN_HZ              500000UL

/*
 *  0..7   wideband    32 64 128 256 512 1024 2048 4096
 *  8..18  sinc4       12 16 24 32 64 128 256 512 1024 2048 4096
 *  19..27 sinc4+sinc1 (sinc4 OSR 32) x sinc1 {2 4 10 20 40 100 200 400 1000}
 *  28..29 sinc3       26667 32000
 *  30..31 sinc3+sinc1 32000 x {3 5}
 */
static const uint32_t fltr_osr_x3[32] = {
       96,    192,    384,    768,   1536,   3072,   6144,  12288,
       36,     48,     72,     96,    192,    384,    768,   1536,   3072,  6144, 12288,
      192,    384,    960,   1920,   3840,   9600,  19200,  38400,  96000,
    80000,  96000, 288000, 480000
};

static XSpiPs g_spi;
static bool   g_spi_up = false;

static struct {
    bool    ready;
    uint8_t device_id;
    uint8_t rev_id;
    uint8_t status;
    uint8_t gen_cfg2;
    uint8_t dp_cfg2;
    uint8_t clk_cfg;
    uint8_t ch_cfg1[ADS_NUM_CH];
    uint8_t ch_cfg2[ADS_NUM_CH];
} adc;

static int ads_frame(uint8_t b0, uint8_t b1, uint8_t *r0, uint8_t *r1)
{
    uint8_t tx[2] = { b0, b1 };
    uint8_t rx[2] = { 0, 0 };

    XSpiPs_SetSlaveSelect(&g_spi, ADS_SPI_CS);
    int st = XSpiPs_PolledTransfer(&g_spi, tx, rx, sizeof(tx));
    XSpiPs_SetSlaveSelect(&g_spi, ADS_SPI_CS_NONE);

    if (st != XST_SUCCESS)
        return ADS127L14_ERR_SPI;

    if (r0) *r0 = rx[0];
    if (r1) *r1 = rx[1];
    return ADS127L14_OK;
}

int ads127l14_reg_read(uint8_t addr, uint8_t *value)
{
    if (!g_spi_up)
        return ADS127L14_ERR_NOT_READY;

    if (value == NULL || addr > ADS_ADDR_MAX)
        return ADS127L14_ERR_PARAM;

    int rc = ads_frame((uint8_t)(ADS_CMD_READ | addr), 0x00, NULL, NULL);
    if (rc != ADS127L14_OK)
        return rc;

    return ads_frame(0x00, 0x00, value, NULL);
}

int ads127l14_reg_write(uint8_t addr, uint8_t value)
{
    if (!g_spi_up)
        return ADS127L14_ERR_NOT_READY;
    if (addr > ADS_ADDR_MAX)
        return ADS127L14_ERR_PARAM;

    return ads_frame((uint8_t)(ADS_CMD_WRITE | addr), value, NULL, NULL);
}

static int ads_write_verify(uint8_t addr, uint8_t value, uint8_t mask)
{
    uint8_t rb;
    int rc = ads127l14_reg_write(addr, value);
    if (rc != ADS127L14_OK)
        return rc;

    os_delay_us(ADS_SETTLE_US);

    rc = ads127l14_reg_read(addr, &rb);
    if (rc != ADS127L14_OK)
        return rc;

    return ((rb & mask) == (value & mask)) ? ADS127L14_OK : ADS127L14_ERR_VERIFY;
}

static int ads_update(uint8_t addr, uint8_t mask, uint8_t bits)
{
    uint8_t old;

    int rc = ads127l14_reg_read(addr, &old);
    if (rc != ADS127L14_OK)
        return rc;

    return ads_write_verify(addr, (uint8_t)((old & ~mask) | (bits & mask)), mask);
}

static uint32_t src_hz_from_clk_cfg(uint8_t clk_cfg)
{
    return (clk_cfg & CLK_CFG_SEL_MASK) ? ADS127L14_FCLK_EXT_HZ : ADS127L14_FCLK_INT_HZ;
}

static bool clocking_ok(uint8_t speed, uint8_t clk_cfg)
{
    uint32_t src = src_hz_from_clk_cfg(clk_cfg);
    if (src == 0)
        return true;

    uint32_t fclk = src / clk_div_value[clk_cfg & CLK_CFG_DIV_MASK];
    return fclk >= FCLK_MIN_HZ && fclk <= speed_fclk_max_hz[speed & 3];
}

uint32_t ads127l14_spi_calc_sample_rate_mhz(uint32_t f_source_hz,
                                            uint8_t clk_div_code,
                                            uint8_t fltr)
{
    if (f_source_hz == 0 || clk_div_code > 7 || fltr > 31)
        return 0;

    uint64_t num = (uint64_t)f_source_hz * 3000ULL;
    uint64_t den = (uint64_t)clk_div_value[clk_div_code] * 2ULL * fltr_osr_x3[fltr];
    return (uint32_t)(num / den);
}

uint32_t ads127l14_spi_get_sample_rate_mhz(void)
{
    if (!adc.ready)
        return 0;

    return ads127l14_spi_calc_sample_rate_mhz(src_hz_from_clk_cfg(adc.clk_cfg),
                                              adc.clk_cfg & CLK_CFG_DIV_MASK,
                                              adc.ch_cfg2[0] & CH_CFG2_FLTR_MASK);
}

uint32_t ads127l14_spi_get_sample_rate(void)
{
    return (ads127l14_spi_get_sample_rate_mhz() + 500) / 1000;
}

int ads127l14_spi_verify_channels(void)
{
    if (!g_spi_up)
        return ADS127L14_ERR_NOT_READY;

    for (int n = 0; n < ADS_NUM_CH; n++) {
        int rc = ads127l14_reg_read(REG_CH_CFG1(n), &adc.ch_cfg1[n]);
        if (rc != ADS127L14_OK)
            return rc;

        rc = ads127l14_reg_read(REG_CH_CFG2(n), &adc.ch_cfg2[n]);
        if (rc != ADS127L14_OK)
            return rc;
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

static int ads_sync(void)
{
    int rc = ads127l14_reg_read(REG_GEN_CFG2, &adc.gen_cfg2);
    if (rc != ADS127L14_OK) return rc;

    rc = ads127l14_reg_read(REG_DP_CFG2, &adc.dp_cfg2);
    if (rc != ADS127L14_OK) return rc;

    rc = ads127l14_reg_read(REG_CLK_CFG, &adc.clk_cfg);
    if (rc != ADS127L14_OK) return rc;

    return ads127l14_spi_verify_channels();
}

static void ads_hw_reset(void)
{
    device_gpio_write_pin(ADC_RESET_PIN, 0);
    os_delay_us(ADS_RESET_PULSE_US);
    device_gpio_write_pin(ADC_RESET_PIN, 1);
    os_delay_us(ADS_RESET_RECOVERY_US);
}


int ads127l14_spi_init(void)
{
    adc.ready = false;
    g_spi_up  = false;

    XSpiPs_Config *cfg = XSpiPs_LookupConfig(ADS_SPI_DEVICE_ID);
    if (cfg == NULL)
        return ADS127L14_ERR_SPI;

    if (XSpiPs_CfgInitialize(&g_spi, cfg, cfg->BaseAddress) != XST_SUCCESS)
        return ADS127L14_ERR_SPI;

    if (XSpiPs_SetOptions(&g_spi,
                          XSPIPS_MASTER_OPTION |
                          XSPIPS_MANUAL_START_OPTION |
                          XSPIPS_FORCE_SSELECT_OPTION |
                          XSPIPS_CLK_PHASE_1_OPTION) != XST_SUCCESS)
        return ADS127L14_ERR_SPI;

    if (XSpiPs_SetClkPrescaler(&g_spi, ADS_SPI_PRESCALER) != XST_SUCCESS)
        return ADS127L14_ERR_SPI;

    XSpiPs_SetSlaveSelect(&g_spi, ADS_SPI_CS_NONE);
    g_spi_up = true;

    ads_hw_reset();

    int rc = ads127l14_reg_read(REG_DEV_ID, &adc.device_id);
    if (rc != ADS127L14_OK)
        return rc;

    if (adc.device_id != ADS_DEV_ID_ADS127L14)
        return ADS127L14_ERR_DEV_ID;

    rc = ads127l14_reg_read(REG_REV_ID, &adc.rev_id);
    if (rc != ADS127L14_OK)
        return rc;

    adc.ready = true;
    rc = ads_sync();
    if (rc != ADS127L14_OK && rc != ADS127L14_ERR_MISMATCH)
        adc.ready = false;

    return rc;
}

int ads127l14_spi_read_device_data(uint8_t *device_id, uint8_t *revision)
{
    if (!adc.ready)
        return ADS127L14_ERR_NOT_READY;

    if (device_id == NULL || revision == NULL)
        return ADS127L14_ERR_PARAM;

    int rc = ads127l14_reg_read(REG_DEV_ID, device_id);
    if (rc != ADS127L14_OK)
        return rc;

    rc = ads127l14_reg_read(REG_REV_ID, revision);
    if (rc != ADS127L14_OK)
        return rc;

    adc.device_id = *device_id;
    adc.rev_id    = *revision;
    return ADS127L14_OK;
}

int ads127l14_spi_read_status(uint8_t *status)
{
    if (!adc.ready)
        return ADS127L14_ERR_NOT_READY;

    if (status == NULL)
        return ADS127L14_ERR_PARAM;

    int rc = ads127l14_reg_read(REG_STATUS, status);
    if (rc == ADS127L14_OK)
        adc.status = *status;

    return rc;
}

int ads127l14_spi_clear_status(void)
{
    if (!adc.ready)
        return ADS127L14_ERR_NOT_READY;

    int rc = ads127l14_reg_write(REG_STATUS, STATUS_W1C_MASK);
    if (rc != ADS127L14_OK)
        return rc;

    os_delay_us(ADS_SETTLE_US);
    return ads127l14_reg_read(REG_STATUS, &adc.status);
}

int ads127l14_spi_set_speed_mode(uint8_t speed_mode)
{
    if (!adc.ready)
        return ADS127L14_ERR_NOT_READY;

    if (speed_mode > ADS127L14_SPEED_MAX)
        return ADS127L14_ERR_PARAM;

    uint8_t clk_cfg;

    int rc = ads127l14_reg_read(REG_CLK_CFG, &clk_cfg);
    if (rc != ADS127L14_OK)
        return rc;

    if (!clocking_ok(speed_mode, clk_cfg))
        return ADS127L14_ERR_CLK_LIMIT;

    rc = ads_update(REG_GEN_CFG2, GEN_CFG2_SPEED_MASK,
                    (uint8_t)(speed_mode << GEN_CFG2_SPEED_SHIFT));

    (void)ads127l14_reg_read(REG_GEN_CFG2, &adc.gen_cfg2);
    return rc;
}

int ads127l14_spi_set_dclk_div(uint8_t dclk_div)
{
    if (!adc.ready)
        return ADS127L14_ERR_NOT_READY;

    if (dclk_div > 3)
        return ADS127L14_ERR_PARAM;

    int rc = ads_update(REG_DP_CFG2, DP_CFG2_DCLK_MASK,
                        (uint8_t)(dclk_div << DP_CFG2_DCLK_SHIFT));

    (void)ads127l14_reg_read(REG_DP_CFG2, &adc.dp_cfg2);
    return rc;
}

int ads127l14_spi_set_clk_div(uint8_t clk_div)
{
    if (!adc.ready)
        return ADS127L14_ERR_NOT_READY;

    if (clk_div > 4)
        return ADS127L14_ERR_PARAM;

    uint8_t gen2, clk_cfg;
    int rc = ads127l14_reg_read(REG_GEN_CFG2, &gen2);
    if (rc != ADS127L14_OK)
        return rc;

    rc = ads127l14_reg_read(REG_CLK_CFG, &clk_cfg);
    if (rc != ADS127L14_OK)
        return rc;

    uint8_t speed = (uint8_t)((gen2 & GEN_CFG2_SPEED_MASK) >> GEN_CFG2_SPEED_SHIFT);
    if (!clocking_ok(speed, (uint8_t)((clk_cfg & ~CLK_CFG_DIV_MASK) | clk_div)))
        return ADS127L14_ERR_CLK_LIMIT;

    rc = ads_update(REG_CLK_CFG, CLK_CFG_DIV_MASK, clk_div);
    (void)ads127l14_reg_read(REG_CLK_CFG, &adc.clk_cfg);
    return rc;
}

int ads127l14_spi_set_clocking(uint8_t speed_mode, uint8_t clk_div)
{
    if (!adc.ready)
        return ADS127L14_ERR_NOT_READY;

    if (speed_mode > ADS127L14_SPEED_MAX || clk_div > 4)
        return ADS127L14_ERR_PARAM;

    uint8_t gen2, clk_cfg;
    int rc = ads127l14_reg_read(REG_GEN_CFG2, &gen2);
    if (rc != ADS127L14_OK)
        return rc;

    rc = ads127l14_reg_read(REG_CLK_CFG, &clk_cfg);
    if (rc != ADS127L14_OK)
        return rc;

    uint8_t old_speed = (uint8_t)((gen2 & GEN_CFG2_SPEED_MASK) >> GEN_CFG2_SPEED_SHIFT);
    uint8_t new_clk   = (uint8_t)((clk_cfg & ~CLK_CFG_DIV_MASK) | clk_div);

    if (!clocking_ok(speed_mode, new_clk))
        return ADS127L14_ERR_CLK_LIMIT;

    bool div_first = clocking_ok(old_speed, new_clk);
    if (div_first) {
        rc = ads_update(REG_CLK_CFG, CLK_CFG_DIV_MASK, clk_div);
        if (rc == ADS127L14_OK)
            rc = ads_update(REG_GEN_CFG2, GEN_CFG2_SPEED_MASK,
                            (uint8_t)(speed_mode << GEN_CFG2_SPEED_SHIFT));
    } else {
        rc = ads_update(REG_GEN_CFG2, GEN_CFG2_SPEED_MASK,
                        (uint8_t)(speed_mode << GEN_CFG2_SPEED_SHIFT));
        if (rc == ADS127L14_OK)
            rc = ads_update(REG_CLK_CFG, CLK_CFG_DIV_MASK, clk_div);
    }

    (void)ads127l14_reg_read(REG_GEN_CFG2, &adc.gen_cfg2);
    (void)ads127l14_reg_read(REG_CLK_CFG, &adc.clk_cfg);
    return rc;
}

static int ads_write_cfg2_all(uint8_t value)
{
    int result = ADS127L14_OK;

    for (int n = 0; n < ADS_NUM_CH; n++) {
        int rc = ads127l14_reg_write(REG_CH_CFG2(n), value);
        if (rc != ADS127L14_OK)
            return rc;
    }

    os_delay_us(ADS_SETTLE_US);

    for (int n = 0; n < ADS_NUM_CH; n++) {
        uint8_t rb;
        int rc = ads127l14_reg_read(REG_CH_CFG2(n), &rb);
        if (rc != ADS127L14_OK)
            return rc;

        if (rb != value)
            result = ADS127L14_ERR_VERIFY;
    }
    return result;
}

int ads127l14_spi_set_fltr(uint8_t fltr)
{
    if (!adc.ready)
        return ADS127L14_ERR_NOT_READY;

    if (fltr > 31)
        return ADS127L14_ERR_PARAM;

    uint8_t cur;
    int rc = ads127l14_reg_read(REG_CH_CFG2(0), &cur);
    if (rc != ADS127L14_OK)
        return rc;

    rc = ads_write_cfg2_all((uint8_t)((cur & ~CH_CFG2_FLTR_MASK) | fltr));
    if (rc != ADS127L14_OK) {
        (void)ads_write_cfg2_all(cur);
        (void)ads127l14_spi_verify_channels();
        return rc;
    }

    return ads127l14_spi_verify_channels();
}

static int ads_cfg_step(const char *name, uint8_t addr, uint8_t value)
{
    ADS_LOG("[ADC CONFIG] Configuring %s ...", name);
    int rc = ads_write_verify(addr, value, 0xFF);
    if (rc == ADS127L14_OK)
        ADS_LOG(" PASS [CONFIG: %02X]\r\n", value);
    else
        ADS_LOG(" FAIL [ERR: %d]\r\n", rc);

    return rc;
}

int ads127l14_spi_system_config(void)
{
    if (!g_spi_up)
        return ADS127L14_ERR_NOT_READY;

    const uint8_t gen_cfg2 = (uint8_t)(ADS127L14_SPI_DEFAULT_SPEED_MODE << GEN_CFG2_SPEED_SHIFT);
    const uint8_t dp_cfg2  = (uint8_t)(ADS127L14_SPI_DEFAULT_DCLK_DIV << DP_CFG2_DCLK_SHIFT);
    const uint8_t clk_cfg  = (uint8_t)(HW_CLK_CFG | ADS127L14_SPI_DEFAULT_CLK_DIV);
    const uint8_t ch_cfg2  = (uint8_t)(ADS127L14_SPI_DEFAULT_FLTR & CH_CFG2_FLTR_MASK);

    if (ADS127L14_SPI_DEFAULT_SPEED_MODE > ADS127L14_SPEED_MAX ||
        ADS127L14_SPI_DEFAULT_DCLK_DIV   > 3 ||
        ADS127L14_SPI_DEFAULT_CLK_DIV    > 4 ||
        ADS127L14_SPI_DEFAULT_FLTR       > 31)
        return ADS127L14_ERR_PARAM;
    if (!clocking_ok(ADS127L14_SPI_DEFAULT_SPEED_MODE, clk_cfg)) {
        ADS_LOG("[ADC CONFIG] default speed mode / clock divider combination is out of spec\r\n");
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
    if (rc != ADS127L14_OK)
        return rc;
    ADS_LOG("[ADC CONFIG] Revision: %02X\r\n", adc.rev_id);

    if ((rc = ads_cfg_step("GEN_CFG1", REG_GEN_CFG1, HW_GEN_CFG1)) != ADS127L14_OK) return rc;
    if ((rc = ads_cfg_step("GEN_CFG2", REG_GEN_CFG2, gen_cfg2))    != ADS127L14_OK) return rc;
    if ((rc = ads_cfg_step("DP_CFG1",  REG_DP_CFG1,  HW_DP_CFG1))  != ADS127L14_OK) return rc;
    if ((rc = ads_cfg_step("DP_CFG2",  REG_DP_CFG2,  dp_cfg2))     != ADS127L14_OK) return rc;
    if ((rc = ads_cfg_step("CLK_CFG",  REG_CLK_CFG,  clk_cfg))     != ADS127L14_OK) return rc;

    for (int n = 0; n < ADS_NUM_CH; n++) {
        char name[10];
        name[0] = 'C'; name[1] = 'H'; name[2] = (char)('0' + n);
        name[3] = '_'; name[4] = 'C'; name[5] = 'F'; name[6] = 'G';
        name[7] = '1'; name[8] = '\0';
        if ((rc = ads_cfg_step(name, REG_CH_CFG1(n), HW_CH_CFG1)) != ADS127L14_OK) return rc;
        name[7] = '2';
        if ((rc = ads_cfg_step(name, REG_CH_CFG2(n), ch_cfg2))    != ADS127L14_OK) return rc;
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

static void print_rate(uint32_t mhz)
{
	ADS_LOG("%lu.%03lu Hz", (unsigned long)(mhz / 1000), (unsigned long)(mhz % 1000));
}

static void print_filter(uint8_t f)
{
    uint32_t osr = (fltr_osr_x3[f] + 1) / 3;
    const char *type = (f < 8) ? "Wideband" : (f < 19) ? "Sinc4" :
                       (f < 28) ? "Sinc4+Sinc1 (total OSR)" :
                       (f < 30) ? "Sinc3" : "Sinc3+Sinc1 (total OSR)";
    ADS_LOG("%s, OSR = %lu", type, (unsigned long)osr);
}

void ads127l14_spi_display_settings(void)
{
    static const char *const speed_name[4] = { "Low", "Mid", "High", "Max" };

    ADS_LOG("====== ADC SETTINGS ======\r\n");
    ADS_LOG("ADC %s\r\n", adc.ready ? "enabled" : "disabled");
    ADS_LOG("Device ID / rev : 0x%02X / 0x%02X\r\n", adc.device_id, adc.rev_id);
    ADS_LOG("Status          : 0x%02X\r\n", adc.status);
    if (!adc.ready) {
    	ADS_LOG("==========================\r\n");
        return;
    }

    uint8_t  speed  = (uint8_t)((adc.gen_cfg2 & GEN_CFG2_SPEED_MASK) >> GEN_CFG2_SPEED_SHIFT);
    uint8_t  dcode  = (uint8_t)((adc.dp_cfg2 & DP_CFG2_DCLK_MASK) >> DP_CFG2_DCLK_SHIFT);
    uint8_t  ccode  = adc.clk_cfg & CLK_CFG_DIV_MASK;
    uint32_t src    = src_hz_from_clk_cfg(adc.clk_cfg);
    uint32_t fclk   = src / clk_div_value[ccode];
    uint32_t rate   = ads127l14_spi_get_sample_rate_mhz();
    uint32_t fdclk  = src / dclk_div_value[dcode];

    ADS_LOG("Speed mode      : %s (%u)\r\n", speed_name[speed], speed);
    ADS_LOG("Clock source    : %s, /%u -> fCLK = %lu Hz\r\n",
               (adc.clk_cfg & CLK_CFG_SEL_MASK) ? "external" : "internal oscillator",
               clk_div_value[ccode], (unsigned long)fclk);
    if (!clocking_ok(speed, adc.clk_cfg))
    	ADS_LOG("  WARNING: fCLK is outside the range allowed for this speed mode\r\n");
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
        if (cycles < 24)   /* 4-lane TDM, 24-bit data, no status/CRC byte */
        	ADS_LOG("  WARNING: < 24 bits needed per frame");
    }
    ADS_LOG("\r\n");

    for (int n = 0; n < ADS_NUM_CH; n++)
    	ADS_LOG("CH%d CFG1/CFG2   : 0x%02X / 0x%02X\r\n", n, adc.ch_cfg1[n], adc.ch_cfg2[n]);
    ADS_LOG("Channels        : %s\r\n",
               (ads127l14_spi_verify_channels() == ADS127L14_OK) ? "identical" : "MISMATCH");
    ADS_LOG("==========================\r\n");
}
