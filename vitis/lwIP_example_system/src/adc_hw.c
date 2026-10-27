/**
 * @file adc_hw.c
 * @brief AXI DMA receive path for the ADS127L14 sample stream.
 *
 * The PL packs ADC samples into fixed-size batches. The AXI DMA writes them
 * into two ping-pong buffers; the S2MM interrupt re-arms the idle buffer and
 * signals adc_hw_read().
 */

#include <string.h>
#include <stdbool.h>
#include <stdio.h>
#include <inttypes.h>

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

#define ADC_HW_LOG_SETTING 2 /**< Log output: 0 off, 1 UART, 2 TCP console, 3 both. */

/** @brief Print to the output selected by ADC_HW_LOG_SETTING. */
#if ADC_HW_LOG_SETTING <= 0
#define ADC_HW_LOG(...) ((void)0)
#elif ADC_HW_LOG_SETTING == 1
#define ADC_HW_LOG(...) xil_printf(__VA_ARGS__)
#elif ADC_HW_LOG_SETTING == 2
#include "console.h"
#define ADC_HW_LOG(...) console_printf(__VA_ARGS__)
#elif ADC_HW_LOG_SETTING >= 3
#include "console.h"
#define ADC_HW_LOG(...)                                                                            \
	do {                                                                                       \
		char adc_log_buf_[128];                                                            \
		snprintf(adc_log_buf_, sizeof(adc_log_buf_), __VA_ARGS__);                         \
		xil_printf("%s", adc_log_buf_);                                                    \
		console_printf("%s", adc_log_buf_);                                                \
	} while (0)
#endif

/** AXI GPIO carrying the batch control word. */
#define BATCH_GPIO_DEVICE_ID XPAR_AXI_GPIO_0_DEVICE_ID
/** GPIO channel of the batch control word. */
#define BATCH_GPIO_CHANNEL   1

/** AXI DMA device ID. */
#define DMA_DEV_ID XPAR_AXIDMA_0_DEVICE_ID
/** GIC interrupt ID of the S2MM interrupt. */
#define RX_INTR_ID XPAR_FABRIC_AXI_DMA_0_S2MM_INTROUT_INTR

/** Size of one DMA buffer, in bytes. */
#define ADC_BATCH_BYTES (ADC_MAX_WORDS_PER_BATCH * ADC_ENTRY_SIZE)
/** Number of ping-pong DMA buffers. */
#define ADC_NUM_BUFS    2

static XGpio g_batch_gpio; /**< Batch control GPIO instance. */
static XAxiDma AxiDma;     /**< AXI DMA instance. */

static uint16_t g_batch_words = ADC_WORDS_PER_BATCH; /**< DMA batch size, in entries. */
static bool hw_stream_enabled = false;               /**< PL stream enable bit as last written. */

static volatile int g_active_buf;     /**< Buffer the DMA is filling. */
static volatile int g_ready_buf = -1; /**< Last completed buffer, -1 if none. */
static volatile int g_dma_error;      /**< Last DMA error seen by the ISR. */

static SemaphoreHandle_t dma_rx_sem; /**< Given by the ISR for every batch. */

/** Ping-pong DMA buffers. */
static uint8_t g_adc_buf[ADC_NUM_BUFS][ADC_BATCH_BYTES] __attribute__((aligned(64)));

static volatile uint32_t isr_hit_count = 0; /**< DMA interrupts since boot. */

static volatile bool g_abort_read = false; /**< Ends a blocked adc_hw_read(). */

/** @brief Wake a blocked adc_hw_read() so that it returns 0. */
void adc_hw_abort_read(void)
{
	g_abort_read = true;
	xSemaphoreGive(dma_rx_sem);
}

/**
 * @brief Write the batch control word to the PL.
 *
 * Bits 14:0 hold the batch size in entries, bit 15 enables the stream.
 */
static void adc_hw_set_stream_configure()
{
	uint16_t ctrl = (g_batch_words & 0x7FFFu) | ((hw_stream_enabled ? 1u : 0u) << 15);

	XGpio_DiscreteWrite(&g_batch_gpio, BATCH_GPIO_CHANNEL, ctrl);
}

/**
 * @brief Set the number of entries per DMA batch.
 *
 * @param entries Batch size, 1 to ADC_MAX_WORDS_PER_BATCH.
 *
 * @retval XST_SUCCESS Batch size written to the PL.
 * @retval XST_FAILURE @p entries is out of range.
 */
int adc_hw_set_batch_size(uint16_t entries)
{
	if (entries == 0 || entries > ADC_MAX_WORDS_PER_BATCH) {
		return XST_FAILURE;
	}

	g_batch_words = entries;
	adc_hw_set_stream_configure();
	return XST_SUCCESS;
}

/**
 * @brief Start the PL sample stream.
 *
 * @retval XST_SUCCESS Always.
 */
int adc_hw_enable_stream(void)
{
	hw_stream_enabled = true;
	adc_hw_set_stream_configure();
	return XST_SUCCESS;
}

/**
 * @brief Stop the PL sample stream.
 *
 * @retval XST_SUCCESS Always.
 */
int adc_hw_disable_stream(void)
{
	hw_stream_enabled = false;
	adc_hw_set_stream_configure();
	return XST_SUCCESS;
}

/**
 * @brief S2MM interrupt handler.
 *
 * Acknowledges the interrupt, queues the next buffer and gives dma_rx_sem.
 *
 * @param CallbackRef AXI DMA instance.
 */
static void dma_rx_isr(void *CallbackRef)
{
	XAxiDma *AxiDmaInst = (XAxiDma *)CallbackRef;

	uint32_t IrqStatus;
	BaseType_t xHigherPriorityTaskWoken = pdFALSE;

	isr_hit_count++;

	IrqStatus = XAxiDma_IntrGetIrq(AxiDmaInst, XAXIDMA_DEVICE_TO_DMA);

	XAxiDma_IntrAckIrq(AxiDmaInst, IrqStatus, XAXIDMA_DEVICE_TO_DMA);

	if (IrqStatus & XAXIDMA_IRQ_ERROR_MASK) {
		g_dma_error = IrqStatus;

		portYIELD_FROM_ISR(xHigherPriorityTaskWoken);
		return;
	}

	if (IrqStatus & XAXIDMA_IRQ_IOC_MASK) {
		int completed = g_active_buf;
		int next = (g_active_buf + 1) % ADC_NUM_BUFS;

		g_ready_buf = completed;

		if (XAxiDma_SimpleTransfer(AxiDmaInst, (UINTPTR)g_adc_buf[next],
					   g_batch_words * ADC_ENTRY_SIZE,
					   XAXIDMA_DEVICE_TO_DMA) != XST_SUCCESS) {
			g_dma_error = XST_FAILURE;
			return;
		}

		g_active_buf = next;

		xSemaphoreGiveFromISR(dma_rx_sem, &xHigherPriorityTaskWoken);
	}

	portYIELD_FROM_ISR(xHigherPriorityTaskWoken);
}

/**
 * @brief Reset the S2MM channel, arm the first buffer and enable the PL stream.
 *
 * @retval XST_SUCCESS Stream running.
 * @retval XST_FAILURE DMA reset timed out or the transfer could not be queued.
 */
int adc_hw_start_stream(void)
{
	uint32_t len = g_batch_words * ADC_ENTRY_SIZE;

	adc_hw_disable_stream();

	XAxiDma_Reset(&AxiDma);

	if (!XAxiDma_ResetIsDone(&AxiDma)) {
		uint32_t guard = 1000000;
		while (!XAxiDma_ResetIsDone(&AxiDma) && --guard) {
		}
		if (!guard) {
			ADC_HW_LOG("DMA: reset timeout\r\n");
			return XST_FAILURE;
		}
	}

	XAxiDma_IntrDisable(&AxiDma, XAXIDMA_IRQ_ALL_MASK, XAXIDMA_DEVICE_TO_DMA);

	XAxiDma_IntrEnable(&AxiDma, (XAXIDMA_IRQ_IOC_MASK | XAXIDMA_IRQ_ERROR_MASK),
			   XAXIDMA_DEVICE_TO_DMA);

	g_abort_read = false;

	while (xSemaphoreTake(dma_rx_sem, 0) == pdTRUE) {
	}

	g_active_buf = 0;
	g_ready_buf = -1;
	g_dma_error = 0;

	int rc = XAxiDma_SimpleTransfer(&AxiDma, (UINTPTR)g_adc_buf[g_active_buf], len,
					XAXIDMA_DEVICE_TO_DMA);

	if (rc != XST_SUCCESS) {
		ADC_HW_LOG("DMA: restart failed\r\n");
		return XST_FAILURE;
	}

	adc_hw_enable_stream();
	return XST_SUCCESS;
}

/**
 * @brief Initialize the batch control GPIO, the AXI DMA and its interrupt.
 *
 * @param IntcInstance Interrupt controller the S2MM interrupt is connected to.
 *
 * @retval XST_SUCCESS Initialization completed.
 * @retval XST_FAILURE A driver call failed or the DMA reset timed out.
 */
int adc_hw_init(XScuGic *IntcInstance)
{
	XAxiDma_Config *Config;

	if (XGpio_Initialize(&g_batch_gpio, BATCH_GPIO_DEVICE_ID) != XST_SUCCESS) {
		return XST_FAILURE;
	}
	XGpio_SetDataDirection(&g_batch_gpio, BATCH_GPIO_CHANNEL, 0x0);

	adc_hw_disable_stream();

	Config = XAxiDma_LookupConfig(DMA_DEV_ID);
	if (!Config) {
		return XST_FAILURE;
	}
	if (XAxiDma_CfgInitialize(&AxiDma, Config) != XST_SUCCESS) {
		return XST_FAILURE;
	}
	if (XAxiDma_HasSg(&AxiDma)) {
		return XST_FAILURE;
	}

	XAxiDma_Reset(&AxiDma);
	uint32_t guard = 1000000;
	while (!XAxiDma_ResetIsDone(&AxiDma) && --guard) {
	}
	if (!guard) {
		ADC_HW_LOG("DMA: reset timeout\r\n");
		return XST_FAILURE;
	}

	dma_rx_sem = xSemaphoreCreateBinary();

	if (dma_rx_sem == NULL) {
		return XST_FAILURE;
	}

	XScuGic_SetPriorityTriggerType(IntcInstance, RX_INTR_ID, 0xA0, 0x3);
	if (XScuGic_Connect(IntcInstance, RX_INTR_ID, (Xil_InterruptHandler)dma_rx_isr, &AxiDma) !=
	    XST_SUCCESS) {
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

	if (XAxiDma_SimpleTransfer(&AxiDma, (UINTPTR)g_adc_buf[g_active_buf],
				   g_batch_words * ADC_ENTRY_SIZE,
				   XAXIDMA_DEVICE_TO_DMA) != XST_SUCCESS) {
		return XST_FAILURE;
	}

	return XST_SUCCESS;
}

/**
 * @brief Wait for the next completed DMA batch and copy it out.
 *
 * @param adc_data Destination, at least @p max_batch_length * ADC_ENTRY_SIZE bytes.
 * @param max_batch_length Capacity of @p adc_data, in entries.
 *
 * @return Number of entries copied; 0 on timeout, abort or invalid request.
 */
uint32_t adc_hw_read(uint8_t *adc_data, uint32_t max_batch_length)
{
	if (max_batch_length > ADC_MAX_WORDS_PER_BATCH) {
		ADC_HW_LOG("adc_hw_read: caller asked for %" PRIu32 " entries, "
			   "hardware batch is limited at %d\r\n",
			   max_batch_length, ADC_MAX_WORDS_PER_BATCH);

		return 0;
	}

	if (xSemaphoreTake(dma_rx_sem, pdMS_TO_TICKS(2500)) != pdTRUE) {
		uint32_t dmasr =
			XAxiDma_ReadReg(AxiDma.RegBase, XAXIDMA_RX_OFFSET + XAXIDMA_SR_OFFSET);

		uint32_t dmacr =
			XAxiDma_ReadReg(AxiDma.RegBase, XAXIDMA_RX_OFFSET + XAXIDMA_CR_OFFSET);

		uint32_t da_low = XAxiDma_ReadReg(AxiDma.RegBase,
						  XAXIDMA_RX_OFFSET + XAXIDMA_DESTADDR_OFFSET);

		uint32_t len =
			XAxiDma_ReadReg(AxiDma.RegBase, XAXIDMA_RX_OFFSET + XAXIDMA_BUFFLEN_OFFSET);

		ADC_HW_LOG("DMA Timeout!\r\n");
		ADC_HW_LOG("DMA buffer address: 0x%08lx\r\n",
			   (unsigned long)(UINTPTR)&g_adc_buf[0][0]);
		ADC_HW_LOG("\tS2MM_DMACR:\t0x%08" PRIX32 "\r\n", dmacr);
		ADC_HW_LOG("\tS2MM_DMASR:\t0x%08" PRIX32 "\r\n", dmasr);
		ADC_HW_LOG("\tS2MM_DA:\t0x%08" PRIX32 "\r\n", da_low);
		ADC_HW_LOG("\tS2MM_LEN:\t0x%08" PRIX32 "\r\n", len);
		ADC_HW_LOG("\tactive_buf:\t%d\r\n", g_active_buf);
		ADC_HW_LOG("\tready_buf:\t%d\r\n", g_ready_buf);
		ADC_HW_LOG("\tdma_error:\t0x%08X\r\n", (unsigned)g_dma_error);
		ADC_HW_LOG("\thit_count:\t%" PRIu32 "\r\n", isr_hit_count);

		ADC_HW_LOG("DMA: resetting S2MM...\r\n");
		if (adc_hw_start_stream() != XST_SUCCESS) {
			ADC_HW_LOG("DMA: Recovery failed\r\n");
		}
		ADC_HW_LOG("DMA: resetting S2MM completed\r\n");

		return 0;
	}

	if (g_abort_read) {
		g_abort_read = false;
		return 0;
	}

	int idx = g_ready_buf;

	if (idx < 0 || idx >= ADC_NUM_BUFS) {
		ADC_HW_LOG("DMA: invalid ready buffer index %d\r\n", idx);

		return 0;
	}

	uint32_t dma_bytes = g_batch_words * ADC_ENTRY_SIZE;

	Xil_DCacheInvalidateRange((UINTPTR)g_adc_buf[idx], dma_bytes);

	uint32_t copy_size = (max_batch_length < g_batch_words) ? max_batch_length : g_batch_words;

	memcpy(adc_data, g_adc_buf[idx], copy_size * ADC_ENTRY_SIZE);

	return copy_size;
}
