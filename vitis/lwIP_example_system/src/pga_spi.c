#include "xspips.h"
#include "xparameters.h"
#include "xil_printf.h"
#include "pga_spi.h"
#include "device_gpio.h"
#include "xil_io.h"
#include "sleep.h"

#define PPCAT(a, b)  PPCAT_I(a, b)
#define PPCAT_I(a, b) a ## b

#define PGA_SPI_DEVICE_ID XPAR_PS7_SPI_1_DEVICE_ID
#define PGA_CLK_PRESCALER_VAL 256
#define PGA_CLK_PRESCALER PPCAT(XSPIPS_CLK_PRESCALE_, PGA_CLK_PRESCALER_VAL)

static XSpiPs g_spi;

static uint8_t pga_spi_transfer(uint16_t payload_u0, uint16_t payload_u1, uint16_t *buffer_u0, uint16_t *buffer_u1){
	int status;

	uint8_t tx_req[4] = {
			(uint8_t)((payload_u0 & 0xFF00) >> 8), (uint8_t)(payload_u0 & 0x00FF),
			(uint8_t)((payload_u1 & 0xFF00) >> 8), (uint8_t)(payload_u1 & 0x00FF)
	};
	uint8_t rx_req[4] = {0};

	// Request Data
	XSpiPs_SetSlaveSelect(&g_spi, 0);
	status = XSpiPs_PolledTransfer(&g_spi, tx_req, rx_req, 4);
	XSpiPs_SetSlaveSelect(&g_spi, 0xF);

	if (status != XST_SUCCESS)
		return -status;

	uint8_t tx_pull[4] = { 0 };
	uint8_t rx_pull[4] = { 0 };

	if (buffer_u0 != NULL || buffer_u1 != NULL){
	// Pull Data
	XSpiPs_SetSlaveSelect(&g_spi, 0);
	status = XSpiPs_PolledTransfer(&g_spi, tx_pull, rx_pull, 4);
	XSpiPs_SetSlaveSelect(&g_spi, 0xF);

	if (status != XST_SUCCESS)
		return -status;

	if (buffer_u0 != NULL)
		*buffer_u0 = ((uint16_t)rx_pull[0] << 8) | ((uint16_t)rx_pull[1]);
	if (buffer_u1 != NULL)
		*buffer_u1 = ((uint16_t)rx_pull[2] << 8) | ((uint16_t)rx_pull[3]);
	}

	return 0;
}

int pga_spi_init(void)
{
    XSpiPs_Config *cfg = XSpiPs_LookupConfig(PGA_SPI_DEVICE_ID);
    if (XSpiPs_CfgInitialize(&g_spi, cfg, cfg->BaseAddress) != XST_SUCCESS)
        return -1;

    XSpiPs_SetOptions(&g_spi,
    		XSPIPS_MASTER_OPTION |
			XSPIPS_MANUAL_START_OPTION |
			XSPIPS_FORCE_SSELECT_OPTION |
			XSPIPS_CLK_PHASE_1_OPTION);

    XSpiPs_SetClkPrescaler(&g_spi, PGA_CLK_PRESCALER);

    XSpiPs_SetSlaveSelect(&g_spi, 0);

    pga_spi_system_reset();

    return 0;
}

void pga_spi_system_reset(void){
    device_gpio_write_pin(RST_PGA_0_PIN, 0);
    device_gpio_write_pin(RST_PGA_1_PIN, 0);
	usleep(100);
	device_gpio_write_pin(RST_PGA_0_PIN, 1);
	device_gpio_write_pin(RST_PGA_1_PIN, 1);
}
