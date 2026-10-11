#include "xspips.h"
#include "xparameters.h"
#include "xil_printf.h"
#include "ads127l14_spi.h"
#include "device_gpio.h"
#include "xil_io.h"
#include "sleep.h"

#define PPCAT(a, b)  PPCAT_I(a, b)
#define PPCAT_I(a, b) a ## b

#define ADS127L14_SPI_DEVICE_ID XPAR_PS7_SPI_0_DEVICE_ID
#define ADS127L14_CLK_PRESCALER_VAL 16
#define ADS127L14_CLK_PRESCALER PPCAT(XSPIPS_CLK_PRESCALE_, ADS127L14_CLK_PRESCALER_VAL)

static XSpiPs g_spi;

int ads127l14_spi_init(void)
{
    XSpiPs_Config *cfg = XSpiPs_LookupConfig(ADS127L14_SPI_DEVICE_ID);
    if (XSpiPs_CfgInitialize(&g_spi, cfg, cfg->BaseAddress) != XST_SUCCESS)
        return -1;

    XSpiPs_SetOptions(&g_spi,
    		XSPIPS_MASTER_OPTION |
			XSPIPS_MANUAL_START_OPTION |
			XSPIPS_FORCE_SSELECT_OPTION |
			XSPIPS_CLK_PHASE_1_OPTION);

    XSpiPs_SetClkPrescaler(&g_spi, ADS127L14_CLK_PRESCALER);

    XSpiPs_SetSlaveSelect(&g_spi, 0);

    device_gpio_write_pin(ADC_RESET_PIN, 0);
	usleep(100);
	device_gpio_write_pin(ADC_RESET_PIN, 1);

    return 0;
}

uint8_t ads127l14_reg_read(uint8_t addr, uint8_t *value)
{
    int status;

	uint8_t tx_req[2] = { (uint8_t)(addr & 0x3F), 0 };
    uint8_t rx_req[2] = { 0 };

    // Request Data
    XSpiPs_SetSlaveSelect(&g_spi, 0);
    status = XSpiPs_PolledTransfer(&g_spi, tx_req, rx_req, 2);
    XSpiPs_SetSlaveSelect(&g_spi, 0xF);

    if (status != XST_SUCCESS)
        return -status;

    uint8_t tx_pull[2] = { 0 };
	uint8_t rx_pull[2] = { 0 };

	// Pull Data
	XSpiPs_SetSlaveSelect(&g_spi, 0);
	status = XSpiPs_PolledTransfer(&g_spi, tx_pull, rx_pull, 2);
	XSpiPs_SetSlaveSelect(&g_spi, 0xF);

	if (status != XST_SUCCESS)
		return -status;

    *value = rx_pull[0];
    return 0;
}

uint8_t ads127l14_reg_write(uint8_t addr, uint8_t value)
{
    uint8_t tx[2] = { (uint8_t)(0x80 | (addr & 0x3F)), value };
    uint8_t rx[2];

    XSpiPs_SetSlaveSelect(&g_spi, 0);
    int status = XSpiPs_PolledTransfer(&g_spi, tx, rx, 2);
    XSpiPs_SetSlaveSelect(&g_spi, 0xF);

    return (status == XST_SUCCESS && rx[1] == 0x00) ? 0 : -status;
}

void ads127114_spi_system_config(void)
{
	uint8_t spi_err;
	uint8_t resp;
	uint8_t payload;

    device_gpio_write_pin(ADC_RESET_PIN, 0);
	usleep(10000);
	device_gpio_write_pin(ADC_RESET_PIN, 1);
	usleep(250);

	// Verify Device ID
	xil_printf("[ADC CONFIG] Verifying Device ID ...");
	spi_err = ads127l14_reg_read(0x00, &resp);
	if (spi_err) {
		xil_printf(" READ FAIL [ERR: %02X]\r\n", spi_err);
	} else if (resp == 0x04) {
		xil_printf(" PASS [ID: %02X]\r\n", resp);
	} else {
		xil_printf(" FAIL [ID: %02X]\r\n", resp);
	}

	// Read Revision ID
	xil_printf("[ADC CONFIG] Reading Revision ID ...");
	spi_err = ads127l14_reg_read(0x01, &resp);
	if (spi_err) {
		xil_printf(" READ FAIL [ERR: %02X]\r\n", spi_err);
	} else {
		xil_printf(" Revision: %02X\r\n", resp);
	}

	// Configure GEN_CFG1
	xil_printf("[ADC CONFIG] Configuring GEN_CFG1 ...");
	payload = 0b00000101;
	spi_err = ads127l14_reg_write(0x08, payload);
	if (spi_err) {
		xil_printf(" WRITE FAIL [ERR: %02X]\r\n", spi_err);
	} else {
		usleep(1000);
		spi_err = ads127l14_reg_read(0x08, &resp);
		if (spi_err) {
			xil_printf(" READ FAIL [ERR: %02X]\r\n", spi_err);
		} else if (resp == payload) {
			xil_printf(" PASS [CONFIG: %02X]\r\n", resp);
		} else {
			xil_printf(" FAIL [CONFIG: %02X]\r\n", resp);
		}
	}

	// Configure GEN_CFG2
	xil_printf("[ADC CONFIG] Configuring GEN_CFG2 ...");
	payload = 0b00000110;
	spi_err = ads127l14_reg_write(0x09, payload);
	if (spi_err) {
		xil_printf(" WRITE FAIL [ERR: %02X]\r\n", spi_err);
	} else {
		usleep(1000);
		spi_err = ads127l14_reg_read(0x09, &resp);
		if (spi_err) {
			xil_printf(" READ FAIL [ERR: %02X]\r\n", spi_err);
		} else if (resp == payload) {
			xil_printf(" PASS [CONFIG: %02X]\r\n", resp);
		} else {
			xil_printf(" FAIL [CONFIG: %02X]\r\n", resp);
		}
	}

	// Configure DP_CFG1
	xil_printf("[ADC CONFIG] Configuring DP_CFG1 ...");
	payload = 0b00110000;
	spi_err = ads127l14_reg_write(0x0B, payload);
	if (spi_err) {
		xil_printf(" WRITE FAIL [ERR: %02X]\r\n", spi_err);
	} else {
		usleep(1000);
		spi_err = ads127l14_reg_read(0x0B, &resp);
		if (spi_err) {
			xil_printf(" READ FAIL [ERR: %02X]\r\n", spi_err);
		} else if (resp == payload) {
			xil_printf(" PASS [CONFIG: %02X]\r\n", resp);
		} else {
			xil_printf(" FAIL [CONFIG: %02X]\r\n", resp);
		}
	}

	// Configure DP_CFG2
	xil_printf("[ADC CONFIG] Configuring DP_CFG2 ...");
	payload = 0b00000000;
	spi_err = ads127l14_reg_write(0x0C, payload);
	if (spi_err) {
		xil_printf(" WRITE FAIL [ERR: %02X]\r\n", spi_err);
	} else {
		usleep(1000);
		spi_err = ads127l14_reg_read(0x0C, &resp);
		if (spi_err) {
			xil_printf(" READ FAIL [ERR: %02X]\r\n", spi_err);
		} else if (resp == payload) {
			xil_printf(" PASS [CONFIG: %02X]\r\n", resp);
		} else {
			xil_printf(" FAIL [CONFIG: %02X]\r\n", resp);
		}
	}

	// Configure CH0_CFG1
	xil_printf("[ADC CONFIG] Configuring CH0_CFG1 ...");
	payload = 0b00000000;
	spi_err = ads127l14_reg_write(0x11, payload);
	if (spi_err) {
		xil_printf(" WRITE FAIL [ERR: %02X]\r\n", spi_err);
	} else {
		usleep(1000);
		spi_err = ads127l14_reg_read(0x11, &resp);
		if (spi_err) {
			xil_printf(" READ FAIL [ERR: %02X]\r\n", spi_err);
		} else if (resp == payload) {
			xil_printf(" PASS [CONFIG: %02X]\r\n", resp);
		} else {
			xil_printf(" FAIL [CONFIG: %02X]\r\n", resp);
		}
	}

	// Configure CH0_CFG2
	xil_printf("[ADC CONFIG] Configuring CH0_CFG2 ...");
	payload = 0b00001001;
	spi_err = ads127l14_reg_write(0x12, payload);
	if (spi_err) {
		xil_printf(" WRITE FAIL [ERR: %02X]\r\n", spi_err);
	} else {
		usleep(1000);
		spi_err = ads127l14_reg_read(0x12, &resp);
		if (spi_err) {
			xil_printf(" READ FAIL [ERR: %02X]\r\n", spi_err);
		} else if (resp == payload) {
			xil_printf(" PASS [CONFIG: %02X]\r\n", resp);
		} else {
			xil_printf(" FAIL [CONFIG: %02X]\r\n", resp);
		}
	}

	// Configure CH1_CFG1
	xil_printf("[ADC CONFIG] Configuring CH1_CFG1 ...");
	payload = 0b00000000;
	spi_err = ads127l14_reg_write(0x19, payload);
	if (spi_err) {
		xil_printf(" WRITE FAIL [ERR: %02X]\r\n", spi_err);
	} else {
		usleep(1000);
		spi_err = ads127l14_reg_read(0x19, &resp);
		if (spi_err) {
			xil_printf(" READ FAIL [ERR: %02X]\r\n", spi_err);
		} else if (resp == payload) {
			xil_printf(" PASS [CONFIG: %02X]\r\n", resp);
		} else {
			xil_printf(" FAIL [CONFIG: %02X]\r\n", resp);
		}
	}

	// Configure CH1_CFG2
	xil_printf("[ADC CONFIG] Configuring CH1_CFG2 ...");
	payload = 0b00001001;
	spi_err = ads127l14_reg_write(0x1A, payload);
	if (spi_err) {
		xil_printf(" WRITE FAIL [ERR: %02X]\r\n", spi_err);
	} else {
		usleep(1000);
		spi_err = ads127l14_reg_read(0x1A, &resp);
		if (spi_err) {
			xil_printf(" READ FAIL [ERR: %02X]\r\n", spi_err);
		} else if (resp == payload) {
			xil_printf(" PASS [CONFIG: %02X]\r\n", resp);
		} else {
			xil_printf(" FAIL [CONFIG: %02X]\r\n", resp);
		}
	}

	// Configure CH2_CFG1
	xil_printf("[ADC CONFIG] Configuring CH2_CFG1 ...");
	payload = 0b00000000;
	spi_err = ads127l14_reg_write(0x21, payload);
	if (spi_err) {
		xil_printf(" WRITE FAIL [ERR: %02X]\r\n", spi_err);
	} else {
		usleep(1000);
		spi_err = ads127l14_reg_read(0x21, &resp);
		if (spi_err) {
			xil_printf(" READ FAIL [ERR: %02X]\r\n", spi_err);
		} else if (resp == payload) {
			xil_printf(" PASS [CONFIG: %02X]\r\n", resp);
		} else {
			xil_printf(" FAIL [CONFIG: %02X]\r\n", resp);
		}
	}

	// Configure CH2_CFG2
	xil_printf("[ADC CONFIG] Configuring CH2_CFG2 ...");
	payload = 0b00001001;
	spi_err = ads127l14_reg_write(0x22, payload);
	if (spi_err) {
		xil_printf(" WRITE FAIL [ERR: %02X]\r\n", spi_err);
	} else {
		usleep(1000);
		spi_err = ads127l14_reg_read(0x22, &resp);
		if (spi_err) {
			xil_printf(" READ FAIL [ERR: %02X]\r\n", spi_err);
		} else if (resp == payload) {
			xil_printf(" PASS [CONFIG: %02X]\r\n", resp);
		} else {
			xil_printf(" FAIL [CONFIG: %02X]\r\n", resp);
		}
	}

	// Configure CH3_CFG1
	xil_printf("[ADC CONFIG] Configuring CH3_CFG1 ...");
	payload = 0b00000000;
	spi_err = ads127l14_reg_write(0x29, payload);
	if (spi_err) {
		xil_printf(" WRITE FAIL [ERR: %02X]\r\n", spi_err);
	} else {
		usleep(1000);
		spi_err = ads127l14_reg_read(0x29, &resp);
		if (spi_err) {
			xil_printf(" READ FAIL [ERR: %02X]\r\n", spi_err);
		} else if (resp == payload) {
			xil_printf(" PASS [CONFIG: %02X]\r\n", resp);
		} else {
			xil_printf(" FAIL [CONFIG: %02X]\r\n", resp);
		}
	}

	// Configure CH3_CFG2
	xil_printf("[ADC CONFIG] Configuring CH3_CFG2 ...");
	payload = 0b00001001;
	spi_err = ads127l14_reg_write(0x2A, payload);
	if (spi_err) {
		xil_printf(" WRITE FAIL [ERR: %02X]\r\n", spi_err);
	} else {
		usleep(1000);
		spi_err = ads127l14_reg_read(0x2A, &resp);
		if (spi_err) {
			xil_printf(" READ FAIL [ERR: %02X]\r\n", spi_err);
		} else if (resp == payload) {
			xil_printf(" PASS [CONFIG: %02X]\r\n", resp);
		} else {
			xil_printf(" FAIL [CONFIG: %02X]\r\n", resp);
		}
	}
}
