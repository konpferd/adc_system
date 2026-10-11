#include "adc_hw.h"
#include "FreeRTOS.h"
#include "task.h"
#include "semphr.h"
#include "xaxidma.h"
#include "xaxidma_hw.h"
#include "xparameters.h"
#include "xil_cache.h"
#include "xil_printf.h"
#include "xgpio.h"
#include <string.h>
#include <stdbool.h>

#define BATCH_GPIO_DEVICE_ID  XPAR_AXI_GPIO_0_DEVICE_ID
#define BATCH_GPIO_CHANNEL    1

#define DMA_DEV_ID XPAR_AXIDMA_0_DEVICE_ID
#define RX_INTR_ID XPAR_FABRIC_AXI_DMA_0_S2MM_INTROUT_INTR

#define ADC_BATCH_BYTES  (ADC_MAX_WORDS_PER_BATCH * ADC_ENTRY_SIZE)
#define ADC_NUM_BUFS     2

static XGpio g_batch_gpio;
static XAxiDma AxiDma;

static uint16_t g_batch_words = ADC_WORDS_PER_BATCH;
static bool hw_stream_enabled = false;

static volatile int g_active_buf;
static volatile int g_ready_buf = -1;
static volatile int g_dma_error;

static SemaphoreHandle_t dma_rx_sem;

static uint8_t g_adc_buf[ADC_NUM_BUFS][ADC_BATCH_BYTES] __attribute__((aligned(64)));

static volatile uint32_t isr_hit_count = 0;

static void adc_hw_set_stream_configure(){
	uint16_t ctrl = (g_batch_words & 0x7FFFu) | ((hw_stream_enabled ? 1u : 0u) << 15);

	XGpio_DiscreteWrite(
		&g_batch_gpio,
		BATCH_GPIO_CHANNEL,
		ctrl
	);
}

int adc_hw_set_batch_size(uint16_t entries)
{
    if (entries == 0 || entries > ADC_MAX_WORDS_PER_BATCH)
        return XST_FAILURE;

    g_batch_words = entries;
    adc_hw_set_stream_configure();
    return XST_SUCCESS;
}

int adc_hw_enable_stream(void){
	hw_stream_enabled = true;
	adc_hw_set_stream_configure();
	return XST_SUCCESS;
}

int adc_hw_disable_stream(void){
	hw_stream_enabled = false;
	adc_hw_set_stream_configure();
	return XST_SUCCESS;
}

static void dma_rx_isr(void *CallbackRef) {
    XAxiDma *AxiDmaInst = (XAxiDma *)CallbackRef;

    uint32_t IrqStatus;
    BaseType_t xHigherPriorityTaskWoken = pdFALSE;

    isr_hit_count++;

    IrqStatus = XAxiDma_IntrGetIrq(
		AxiDmaInst,
		XAXIDMA_DEVICE_TO_DMA);

	XAxiDma_IntrAckIrq(
		AxiDmaInst,
		IrqStatus,
		XAXIDMA_DEVICE_TO_DMA);

	if (IrqStatus & XAXIDMA_IRQ_ERROR_MASK) {
		g_dma_error = IrqStatus;

		portYIELD_FROM_ISR(xHigherPriorityTaskWoken);
		return;
	}

	if (IrqStatus & XAXIDMA_IRQ_IOC_MASK) {
		int completed = g_active_buf;
		int next = (g_active_buf + 1) % ADC_NUM_BUFS;

		g_ready_buf = completed;

		if (XAxiDma_SimpleTransfer(
				AxiDmaInst,
				(UINTPTR)g_adc_buf[next],
				g_batch_words * ADC_ENTRY_SIZE,
				XAXIDMA_DEVICE_TO_DMA) != XST_SUCCESS) {

			g_dma_error = XST_FAILURE;
			return;
		}

		g_active_buf = next;

		xSemaphoreGiveFromISR(
			dma_rx_sem,
			&xHigherPriorityTaskWoken);
	}

	portYIELD_FROM_ISR(xHigherPriorityTaskWoken);
}

static int dma_restart(void)
{
    uint32_t len = g_batch_words * ADC_ENTRY_SIZE;

    xil_printf("DMA: resetting S2MM...\r\n");
    adc_hw_disable_stream();

    XAxiDma_Reset(&AxiDma);

    if (!XAxiDma_ResetIsDone(&AxiDma)) {
        while (!XAxiDma_ResetIsDone(&AxiDma)) {
        }
    }

    XAxiDma_IntrDisable(
        &AxiDma,
        XAXIDMA_IRQ_ALL_MASK,
        XAXIDMA_DEVICE_TO_DMA);

    XAxiDma_IntrEnable(
        &AxiDma,
        (XAXIDMA_IRQ_IOC_MASK | XAXIDMA_IRQ_ERROR_MASK),
        XAXIDMA_DEVICE_TO_DMA);

    while (xSemaphoreTake(dma_rx_sem, 0) == pdTRUE) {}

    g_active_buf = 0;
	g_ready_buf = -1;
	g_dma_error = 0;

	int rc = XAxiDma_SimpleTransfer(
	        &AxiDma,
	        (UINTPTR)g_adc_buf[g_active_buf],
	        len,
	        XAXIDMA_DEVICE_TO_DMA);

	xil_printf("DMA SimpleTransfer rc=%d\r\n", rc);

	xil_printf("DMA regs immediately after arm:\r\n");
	xil_printf("  CR   = 0x%08lx\r\n",
			   (unsigned long)XAxiDma_ReadReg(AxiDma.RegBase, 0x30));
	xil_printf("  SR   = 0x%08lx\r\n",
			   (unsigned long)XAxiDma_ReadReg(AxiDma.RegBase, 0x34));
	xil_printf("  DA   = 0x%08lx\r\n",
			   (unsigned long)XAxiDma_ReadReg(AxiDma.RegBase, 0x48));
	xil_printf("  DA_M = 0x%08lx\r\n",
			   (unsigned long)XAxiDma_ReadReg(AxiDma.RegBase, 0x4C));
	xil_printf("  LEN  = 0x%08lx\r\n",
			   (unsigned long)XAxiDma_ReadReg(AxiDma.RegBase, 0x58));

	if (rc != XST_SUCCESS) {
		xil_printf("DMA: restart failed\r\n");
		return XST_FAILURE;
	}

	adc_hw_enable_stream();
    return XST_SUCCESS;
}

int adc_hw_init(XScuGic *IntcInstance) {
    XAxiDma_Config *Config;

    if (XGpio_Initialize(&g_batch_gpio, BATCH_GPIO_DEVICE_ID) != XST_SUCCESS)
        return XST_FAILURE;
    XGpio_SetDataDirection(&g_batch_gpio, BATCH_GPIO_CHANNEL, 0x0);

    adc_hw_disable_stream();

    Config = XAxiDma_LookupConfig(DMA_DEV_ID);
    if (!Config) return XST_FAILURE;
    if (XAxiDma_CfgInitialize(&AxiDma, Config) != XST_SUCCESS) return XST_FAILURE;
    if (XAxiDma_HasSg(&AxiDma)) return XST_FAILURE;

    XAxiDma_Reset(&AxiDma);
	while (!XAxiDma_ResetIsDone(&AxiDma)) {}

	dma_rx_sem = xSemaphoreCreateBinary();

	if (dma_rx_sem == NULL)
		return XST_FAILURE;

    XScuGic_SetPriorityTriggerType(IntcInstance, RX_INTR_ID, 0xA0, 0x3);
    if (XScuGic_Connect(IntcInstance, RX_INTR_ID,
                       (Xil_InterruptHandler)dma_rx_isr, &AxiDma) != XST_SUCCESS) {
        return XST_FAILURE;
    }
    XScuGic_Enable(IntcInstance, RX_INTR_ID);

    XAxiDma_IntrDisable(&AxiDma, XAXIDMA_IRQ_ALL_MASK, XAXIDMA_DEVICE_TO_DMA);
    XAxiDma_IntrEnable(&AxiDma, (XAXIDMA_IRQ_IOC_MASK | XAXIDMA_IRQ_ERROR_MASK),
                       XAXIDMA_DEVICE_TO_DMA);

    adc_hw_set_batch_size(g_batch_words);

    g_active_buf = 0;
    g_ready_buf = -1;
    g_dma_error = 0;

    if (XAxiDma_SimpleTransfer(
            &AxiDma,
            (UINTPTR)g_adc_buf[g_active_buf],
            g_batch_words * ADC_ENTRY_SIZE,
            XAXIDMA_DEVICE_TO_DMA) != XST_SUCCESS) {

        return XST_FAILURE;
    }

    uint32_t s = XAxiDma_ReadReg(AxiDma.RegBase, 0x34);
    uint32_t c = XAxiDma_ReadReg(AxiDma.RegBase, 0x30);

    xil_printf("\r\nIMMEDIATE: DMACR=%08lx DMASR=%08lx\r\n", c, s);
    xil_printf("DMA RegBase = 0x%08lx\r\n",
               (unsigned long)AxiDma.RegBase);

    return XST_SUCCESS;
}

uint32_t adc_hw_read(uint8_t *adc_data, uint32_t max_batch_length)
{
    if (max_batch_length > ADC_MAX_WORDS_PER_BATCH) {
        xil_printf(
            "adc_hw_read: caller asked for %u entries, "
            "hardware batch is limited at %d\r\n",
            max_batch_length,
            ADC_MAX_WORDS_PER_BATCH);

        return 0;
    }

    if (xSemaphoreTake(
    		dma_rx_sem,
			pdMS_TO_TICKS(2500)) != pdTRUE) {

		uint32_t dmasr =
			XAxiDma_ReadReg(
				AxiDma.RegBase,
				XAXIDMA_RX_OFFSET + XAXIDMA_SR_OFFSET);

		uint32_t dmacr =
			XAxiDma_ReadReg(
				AxiDma.RegBase,
				XAXIDMA_RX_OFFSET + XAXIDMA_CR_OFFSET);

		uint32_t da_low = XAxiDma_ReadReg(
		    AxiDma.RegBase,
		    XAXIDMA_RX_OFFSET + XAXIDMA_DESTADDR_OFFSET);

		uint32_t len = XAxiDma_ReadReg(
		    AxiDma.RegBase,
			XAXIDMA_RX_OFFSET + XAXIDMA_BUFFLEN_OFFSET);

		xil_printf(
			"DMA Timeout!\r\n"
			"	S2MM_DMACR:	0x%08X\r\n"
			"	S2MM_DMASR:	0x%08X\r\n"
			"	S2MM_DA:	0x%08X\r\n"
			"	S2MM_LEN:	0x%08X\r\n"
			"	active_buf:	%d\r\n"
			"	ready_buf:	%d\r\n"
			"	dma_error:	0x%08X\r\n"
			"	hit_count:	%d\r\n",
			dmacr,
			dmasr,
			da_low,
			len,
			g_active_buf,
			g_ready_buf,
			(unsigned)g_dma_error,
			isr_hit_count);

		xil_printf("DMA buffer address: 0x%08lx\r\n",
		           (unsigned long)(UINTPTR)&g_adc_buf[0][0]);

		if (dma_restart() != XST_SUCCESS) {
			xil_printf("DMA recovery failed\r\n");
		}

		return 0;
	}

	int idx = g_ready_buf;

	if (idx < 0 || idx >= ADC_NUM_BUFS) {
		xil_printf(
			"DMA: invalid ready buffer index %d\r\n",
			idx);

		return 0;
	}

	uint32_t dma_bytes =
		g_batch_words * ADC_ENTRY_SIZE;

	Xil_DCacheInvalidateRange(
		(UINTPTR)g_adc_buf[idx],
		dma_bytes);

	uint32_t copy_size =
		(max_batch_length < g_batch_words)
			? max_batch_length
			: g_batch_words;

	memcpy(
		adc_data,
		g_adc_buf[idx],
		copy_size * ADC_ENTRY_SIZE);

	return copy_size;
}
