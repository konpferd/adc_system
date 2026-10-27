/*
 * Copyright (C) 2016 - 2019 Xilinx, Inc.
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without modification,
 * are permitted provided that the following conditions are met:
 *
 * 1. Redistributions of source code must retain the above copyright notice,
 *    this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright notice,
 *    this list of conditions and the following disclaimer in the documentation
 *    and/or other materials provided with the distribution.
 * 3. The name of the author may not be used to endorse or promote products
 *    derived from this software without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR IMPLIED
 * WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
 * MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT
 * SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
 * EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT
 * OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
 * INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
 * CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
 * IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY
 * OF SUCH DAMAGE.
 *
 */

/**
 * @file iic_phyreset.c
 * @brief PHY reset through the I2C I/O expander of the ZCU102 (Xilinx template).
 *
 * Built only for the ZCU102; unused on the TE0720.
 */

#include <stdio.h>

#include "xparameters.h"
#if defined(__arm__) || defined(__aarch64__)
#include "xil_printf.h"
#endif

#ifdef XPS_BOARD_ZCU102
#ifdef XPAR_XIICPS_0_DEVICE_ID
#include "xiicps.h"

#define BUF_LEN 10U /**< I2C write buffer size. */

#define IOEXPANDER1_ADDR 0x20U /**< I2C address of the I/O expander. */

#define IIC_SCLK_RATE_IOEXP 400000 /**< I2C clock rate, in Hz. */

#define CMD_CFG_0_REG    0x06U /**< Expander port 0 configuration register. */
#define CMD_OUTPUT_0_REG 0x02U /**< Expander port 0 output register. */
#define DATA_OUTPUT      0x0U  /**< All port 0 pins as outputs. */

#define DATA_COMMON_CFG  0xE0U /**< Port 0 output value. */
#define DATA_GT_0000_CFG 0x00U /**< Port 0 output value, GT bits. */

XIicPs I2c0InstancePtr; /**< I2C driver instance. */

/**
 * @brief Release the PHY reset through the I/O expander.
 *
 * @retval XST_SUCCESS PHY reset released.
 * @retval XST_FAILURE I2C access failed.
 */
int IicPhyReset(void)
{
	u8 WriteBuffer[BUF_LEN] = {0};
	XIicPs_Config *I2c0CfgPtr;
	int Status = XST_SUCCESS;

	I2c0CfgPtr = XIicPs_LookupConfig(XPAR_XIICPS_0_DEVICE_ID);
	if (I2c0CfgPtr == NULL) {
		Status = XST_FAILURE;
		return Status;
	}

	Status = XIicPs_CfgInitialize(&I2c0InstancePtr, I2c0CfgPtr, I2c0CfgPtr->BaseAddress);
	if (Status != XST_SUCCESS) {
		Status = XST_FAILURE;
		return Status;
	}

	XIicPs_SetSClk(&I2c0InstancePtr, IIC_SCLK_RATE_IOEXP);

	WriteBuffer[0] = CMD_CFG_0_REG;
	WriteBuffer[1] = DATA_OUTPUT;
	Status = XIicPs_MasterSendPolled(&I2c0InstancePtr, WriteBuffer, 2, IOEXPANDER1_ADDR);
	if (Status != XST_SUCCESS) {
		Status = XST_FAILURE;
		return Status;
	}

	while (XIicPs_BusIsBusy(&I2c0InstancePtr)) {
	}

	WriteBuffer[0] = CMD_OUTPUT_0_REG;

	WriteBuffer[1] = DATA_COMMON_CFG | DATA_GT_0000_CFG;

	Status = XIicPs_MasterSendPolled(&I2c0InstancePtr, WriteBuffer, 2, IOEXPANDER1_ADDR);
	if (Status != XST_SUCCESS) {
		Status = XST_FAILURE;
		return Status;
	}

	while (XIicPs_BusIsBusy(&I2c0InstancePtr)) {
	}

	xil_printf("IIC PHY reset on ZCU102 successful \n\r");
}
#endif
#endif
