#include <stddef.h>
#include <string.h>
#include <stdio.h>

#include "xspips.h"
#include "xparameters.h"
#include "xil_printf.h"
#include "sleep.h"
#include "os_delay.h"
#include "device_gpio.h"
#include "pga_spi.h"

/* Logging settings:
 * <=0 	- no logs
 * 1 	- UART console only
 * 2 	- TCP console only
 * >=3 	- TCP + UART
 */
#define PGA_LOG_SETTING        3

#if PGA_LOG_SETTING <= 0
#define PGA_LOG(...)             ((void)0)
#elif PGA_LOG_SETTING == 1
#define PGA_LOG(...)             xil_printf(__VA_ARGS__)
#elif PGA_LOG_SETTING == 2
#include "console.h"
#define PGA_LOG(...)             console_printf(__VA_ARGS__)
#elif PGA_LOG_SETTING >= 3
#include "console.h"
#define PGA_LOG(...)                                    \
    do {                                                \
        char pga_log_buf_[128];                         \
        snprintf(pga_log_buf_, sizeof(pga_log_buf_),    \
                 __VA_ARGS__);                          \
        xil_printf("%s", pga_log_buf_);                 \
        console_printf("%s", pga_log_buf_);             \
    } while (0)
#endif

#define PGA_SPI_DEVICE_ID        XPAR_PS7_SPI_1_DEVICE_ID
#define PGA_SPI_CS               0
#define PGA_SPI_CS_NONE          0xF
#define PGA_SPI_PRESCALER        XSPIPS_CLK_PRESCALE_256

#define PGA_RESET_PULSE_US       100
#define PGA_RESET_RECOVERY_US    100
#define PGA_EEPROM_PROGRAM_US    50000
#define PGA_EEPROM_READBACK_US   30

#define PGA_FRAME(cmd, addr, data) \
    ((uint16_t)((((cmd) & 0xF) << 12) | (((addr) & 0xF) << 8) | ((data) & 0xFF)))

#define PGA_CODE_TO_DATA(code)  ((uint8_t)(((code) & PGA_SPI_MAX_CODE) << 1))
#define PGA_DATA_TO_CODE(word)  ((uint8_t)(((word) >> 1) & PGA_SPI_MAX_CODE))

#define PGA_CMD_NOP              0x0
#define PGA_CMD_WRITE_RDAC       0x1
#define PGA_CMD_READBACK         0x3
#define PGA_CMD_COPY_EEPROM      0x7

#define PGA_RB_EEPROM            0x1
#define PGA_RB_RDAC              0x3

#define PGA_NUM_CHIPS            2
#define PGA_POTS_PER_CHIP        2

#define PGA_CH(chip, pot)        ((chip) * PGA_POTS_PER_CHIP + (pot))


static XSpiPs g_spi;

static struct {
    bool    ready;
    uint8_t rdac[PGA_SPI_NUM_CHANNELS];
    uint8_t eeprom[PGA_SPI_NUM_CHANNELS];
} pga = {
    .ready  = false,
    .rdac   = { PGA_SPI_DEFAULT_VAL_CH0, PGA_SPI_DEFAULT_VAL_CH1,
                PGA_SPI_DEFAULT_VAL_CH2, PGA_SPI_DEFAULT_VAL_CH3 },
    .eeprom = { PGA_SPI_DEFAULT_VAL_CH0, PGA_SPI_DEFAULT_VAL_CH1,
                PGA_SPI_DEFAULT_VAL_CH2, PGA_SPI_DEFAULT_VAL_CH3 },
};

static int pga_frame(uint16_t w_chip0, uint16_t w_chip1,
                     uint16_t *r_chip0, uint16_t *r_chip1)
{
    uint8_t tx[4] = { (uint8_t)(w_chip0 >> 8), (uint8_t)w_chip0,
                      (uint8_t)(w_chip1 >> 8), (uint8_t)w_chip1 };
    uint8_t rx[4] = { 0 };

    XSpiPs_SetSlaveSelect(&g_spi, PGA_SPI_CS);
    int st = XSpiPs_PolledTransfer(&g_spi, tx, rx, sizeof(tx));
    XSpiPs_SetSlaveSelect(&g_spi, PGA_SPI_CS_NONE);

    if (st != XST_SUCCESS)
        return PGA_ERR_SPI;

    if (r_chip0) *r_chip0 = (uint16_t)((rx[0] << 8) | rx[1]);
    if (r_chip1) *r_chip1 = (uint16_t)((rx[2] << 8) | rx[3]);
    return PGA_OK;
}


static int pga_query(uint16_t req0, uint16_t req1, unsigned delay_us,
                     uint16_t *r0, uint16_t *r1)
{
    int rc = pga_frame(req0, req1, NULL, NULL);
    if (rc != PGA_OK)
        return rc;
    if (delay_us)
        os_delay_us(delay_us);
    return pga_frame(PGA_CMD_NOP, PGA_CMD_NOP, r0, r1);
}

static int pga_read_all(uint8_t src, uint8_t out[PGA_SPI_NUM_CHANNELS])
{
    for (int pot = 0; pot < PGA_POTS_PER_CHIP; pot++) {
        uint8_t  addr = (src == PGA_RB_EEPROM) ? (uint8_t)(pot << 1) : (uint8_t)pot;
        uint16_t req  = PGA_FRAME(PGA_CMD_READBACK, addr, src);
        uint16_t r0 = 0, r1 = 0;

        int rc = pga_query(req, req, (src == PGA_RB_EEPROM) ? PGA_EEPROM_READBACK_US : 0, &r0, &r1);
        if (rc != PGA_OK)
            return rc;

        out[PGA_CH(0, pot)] = PGA_DATA_TO_CODE(r0);
		out[PGA_CH(1, pot)] = PGA_DATA_TO_CODE(r1);
    }
    return PGA_OK;
}

static void pga_hw_reset(void)
{
    device_gpio_write_pin(RST_PGA_0_PIN, 0);
    device_gpio_write_pin(RST_PGA_1_PIN, 0);
    os_delay_us(PGA_RESET_PULSE_US);
    device_gpio_write_pin(RST_PGA_0_PIN, 1);
    device_gpio_write_pin(RST_PGA_1_PIN, 1);
    os_delay_us(PGA_RESET_RECOVERY_US);
}

static int pga_sync_shadow(void)
{
    uint8_t rdac[PGA_SPI_NUM_CHANNELS], eeprom[PGA_SPI_NUM_CHANNELS];

    int rc = pga_read_all(PGA_RB_RDAC, rdac);
    if (rc != PGA_OK)
        return rc;
    rc = pga_read_all(PGA_RB_EEPROM, eeprom);
    if (rc != PGA_OK)
        return rc;

    memcpy(pga.rdac, rdac, sizeof(pga.rdac));
    memcpy(pga.eeprom, eeprom, sizeof(pga.eeprom));
    return PGA_OK;
}

int pga_spi_init(void)
{
    pga.ready = false;

    XSpiPs_Config *cfg = XSpiPs_LookupConfig(PGA_SPI_DEVICE_ID);
    if (cfg == NULL)
        return PGA_ERR_SPI;

    if (XSpiPs_CfgInitialize(&g_spi, cfg, cfg->BaseAddress) != XST_SUCCESS)
        return PGA_ERR_SPI;

    if (XSpiPs_SetOptions(&g_spi,
                          XSPIPS_MASTER_OPTION |
                          XSPIPS_MANUAL_START_OPTION |
                          XSPIPS_FORCE_SSELECT_OPTION |
                          XSPIPS_CLK_PHASE_1_OPTION) != XST_SUCCESS)
        return PGA_ERR_SPI;

    if (XSpiPs_SetClkPrescaler(&g_spi, PGA_SPI_PRESCALER) != XST_SUCCESS)
        return PGA_ERR_SPI;

    XSpiPs_SetSlaveSelect(&g_spi, PGA_SPI_CS_NONE);

    pga_hw_reset();

    pga.ready = true;
    int rc = pga_sync_shadow();
    if (rc != PGA_OK)
        pga.ready = false;
    return rc;
}

int pga_spi_system_reset(void)
{
    pga_hw_reset();
    return pga.ready ? pga_sync_shadow() : PGA_OK;
}

int pga_spi_set_rdac(uint8_t channel0, uint8_t channel1,
                     uint8_t channel2, uint8_t channel3)
{
    if (!pga.ready)
        return PGA_ERR_NOT_READY;

    const uint8_t req[PGA_SPI_NUM_CHANNELS] = { channel0, channel1, channel2, channel3 };

    for (int i = 0; i < PGA_SPI_NUM_CHANNELS; i++)
        if (req[i] > PGA_SPI_MAX_CODE)
            return PGA_ERR_PARAM;

    for (int pot = 0; pot < PGA_POTS_PER_CHIP; pot++) {
    	int rc = pga_frame(PGA_FRAME(PGA_CMD_WRITE_RDAC, pot, PGA_CODE_TO_DATA(req[PGA_CH(0, pot)])),
    	                   PGA_FRAME(PGA_CMD_WRITE_RDAC, pot, PGA_CODE_TO_DATA(req[PGA_CH(1, pot)])),
                           NULL, NULL);
        if (rc != PGA_OK)
            return rc;
    }

    uint8_t got[PGA_SPI_NUM_CHANNELS];
    int rc = pga_read_all(PGA_RB_RDAC, got);
    if (rc != PGA_OK)
        return rc;

    memcpy(pga.rdac, got, sizeof(pga.rdac));
    return (memcmp(req, got, sizeof(got)) == 0) ? PGA_OK : PGA_ERR_VERIFY;
}

int pga_spi_read_rdac(uint8_t *channel0, uint8_t *channel1,
                      uint8_t *channel2, uint8_t *channel3)
{
    if (!pga.ready)
        return PGA_ERR_NOT_READY;
    if (!channel0 || !channel1 || !channel2 || !channel3)
        return PGA_ERR_PARAM;

    uint8_t got[PGA_SPI_NUM_CHANNELS];
    int rc = pga_read_all(PGA_RB_RDAC, got);
    if (rc != PGA_OK)
        return rc;

    memcpy(pga.rdac, got, sizeof(pga.rdac));
    *channel0 = got[0];
    *channel1 = got[1];
    *channel2 = got[2];
    *channel3 = got[3];
    return PGA_OK;
}

int pga_spi_eeprom_save_rdac(void)
{
    if (!pga.ready)
        return PGA_ERR_NOT_READY;

    for (int pot = 0; pot < PGA_POTS_PER_CHIP; pot++) {
        uint16_t w = PGA_FRAME(PGA_CMD_COPY_EEPROM, pot, 0x01);
        int rc = pga_frame(w, w, NULL, NULL);
        if (rc != PGA_OK)
            return rc;
        os_delay_us(PGA_EEPROM_PROGRAM_US);
    }

    uint8_t rdac[PGA_SPI_NUM_CHANNELS], eeprom[PGA_SPI_NUM_CHANNELS];
    int rc = pga_read_all(PGA_RB_RDAC, rdac);
    if (rc != PGA_OK)
        return rc;
    rc = pga_read_all(PGA_RB_EEPROM, eeprom);
    if (rc != PGA_OK)
        return rc;

    memcpy(pga.rdac, rdac, sizeof(pga.rdac));
    memcpy(pga.eeprom, eeprom, sizeof(pga.eeprom));
    return (memcmp(rdac, eeprom, sizeof(rdac)) == 0) ? PGA_OK : PGA_ERR_VERIFY;
}

int pga_spi_system_config(void)
{
    return pga_spi_set_rdac(PGA_SPI_DEFAULT_VAL_CH0, PGA_SPI_DEFAULT_VAL_CH1,
                            PGA_SPI_DEFAULT_VAL_CH2, PGA_SPI_DEFAULT_VAL_CH3);
}

void pga_spi_display_settings(void)
{
	PGA_LOG("====== PGA SETTINGS ======\r\n");
	PGA_LOG("PGA %s\r\n\r\n", pga.ready ? "enabled" : "disabled");
    for (int i = 0; i < PGA_SPI_NUM_CHANNELS; i++)
    	PGA_LOG("PGA channel %d  : 0x%02X\r\n", i, pga.rdac[i]);
    PGA_LOG("\r\n");
    for (int i = 0; i < PGA_SPI_NUM_CHANNELS; i++)
    	PGA_LOG("PGA eeprom ch%d : 0x%02X\r\n", i, pga.eeprom[i]);
    PGA_LOG("==========================\r\n");
}
