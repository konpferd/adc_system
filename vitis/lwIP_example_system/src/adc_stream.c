/**
 * @file adc_stream.c
 * @brief TCP server streaming ADC batches to one client.
 *
 * Each batch is sent as one frame: an adc_frame_header_t followed by
 * word_count entries of ADC_ENTRY_SIZE bytes.
 */

#include <string.h>
#include <stdbool.h>

#include "lwip/sockets.h"
#include "xil_printf.h"
#include "FreeRTOS.h"
#include "task.h"
#include "adc_hw.h"
#include "adc_stream.h"
#include "led_status.h"
#include "xaxidma.h"
#include "xparameters.h"
#include "xscugic.h"
#include "xil_exception.h"
#include "ads127l14_spi.h"
#include "semphr.h"
#include "net_util.h"

#define STREAM_LOG_SETTING 2 /**< Log output: 0 off, 1 UART, 2 TCP console, 3 both. */

/** @brief Print to the output selected by STREAM_LOG_SETTING. */
#if STREAM_LOG_SETTING <= 0
#define STREAM_LOG(...) ((void)0)
#elif STREAM_LOG_SETTING == 1
#define STREAM_LOG(...) xil_printf(__VA_ARGS__)
#elif STREAM_LOG_SETTING == 2
#include "console.h"
#define STREAM_LOG(...) console_printf(__VA_ARGS__)
#elif STREAM_LOG_SETTING >= 3
#include "console.h"
#define STREAM_LOG(...)                                                                            \
	do {                                                                                       \
		char stream_log_buf_[128];                                                         \
		snprintf(stream_log_buf_, sizeof(stream_log_buf_), __VA_ARGS__);                   \
		xil_printf("%s", stream_log_buf_);                                                 \
		console_printf("%s", stream_log_buf_);                                             \
	} while (0)
#endif

#define FRAME_MAGIC     0x41444331u             /**< Frame header magic, ASCII "ADC1". */
#define WORDS_PER_BATCH ADC_MAX_WORDS_PER_BATCH /**< Capacity of the frame buffer, in entries. */

#define STREAM_SEND_TIMEOUT_MS 2000 /**< Longest send stall before the client is dropped. */

/** @brief Header in front of every stream frame. */
typedef struct __attribute__((packed)) {
	uint32_t magic;          /**< FRAME_MAGIC. */
	uint32_t seq;            /**< Frame number, reset on connect. */
	uint32_t word_count;     /**< Number of entries in the payload. */
	uint32_t sample_rate_hz; /**< Per-channel sample rate, in Hz. */
	uint8_t channel_mask;    /**< Channels present in the payload. */
	uint8_t bits_per_sample; /**< Bits per sample. */
	uint8_t reserved[2];     /**< Reserved, zero. */
} adc_frame_header_t;

/** Batch copied out of the DMA buffers. */
static uint8_t dma_rx_buffer[WORDS_PER_BATCH * ADC_ENTRY_SIZE] __attribute__((aligned(32)));

static volatile bool s_run_requested = false; /**< Streaming requested. */
static volatile uint32_t s_run_epoch = 0;     /**< Bumped by every request; forces a restart. */
static TaskHandle_t s_stream_task = NULL;     /**< Stream task, notified on requests. */

/**
 * @brief Request streaming on or off.
 *
 * @param run true to stream, false to stop.
 */
void adc_stream_set_run(bool run)
{
	s_run_requested = run;
	s_run_epoch++;
	if (!run) {
		adc_hw_abort_read();
	}
	if (s_stream_task) {
		xTaskNotifyGive(s_stream_task);
	}
}

/**
 * @brief Get the requested streaming state.
 *
 * @return true if streaming is requested.
 */
bool adc_stream_get_run(void)
{
	return s_run_requested;
}

/**
 * @brief Close the client socket and stop the PL stream if it is running.
 *
 * @param client_sd Client socket; set to -1.
 * @param active Stream state; cleared.
 */
static void stream_drop_client(int *client_sd, bool *active)
{
	close(*client_sd);
	*client_sd = -1;
	if (*active) {
		adc_hw_disable_stream();
		*active = false;
	}
}

/**
 * @brief Check, without blocking, whether the client closed the connection.
 *
 * @param sd Client socket.
 *
 * @return true on FIN or socket error.
 */
static bool stream_peer_closed(int sd)
{
	fd_set rfds;
	FD_ZERO(&rfds);
	FD_SET(sd, &rfds);
	struct timeval tv = {0, 0};

	if (lwip_select(sd + 1, &rfds, NULL, NULL, &tv) <= 0) {
		return false;
	}

	uint8_t dummy;
	int n = lwip_recv(sd, &dummy, 1, MSG_DONTWAIT);
	if (n == 0) {
		return true;
	}
	if (n < 0) {
		if (n < 0) {
			return net_sock_error(sd) != 0;
		}
	}
	return false;
}

/**
 * @brief Stream server task.
 *
 * @param p Unused.
 */
void adc_stream_thread(void *p)
{
	int listen_sd, client_sd = -1;
	struct sockaddr_in addr, remote;
	socklen_t remote_len;
	uint32_t frame_seq = 0;

	memset(&addr, 0, sizeof(addr));
	addr.sin_family = AF_INET;
	addr.sin_port = htons(ADC_STREAM_PORT);
	addr.sin_addr.s_addr = INADDR_ANY;

	if ((listen_sd = lwip_socket(AF_INET, SOCK_STREAM, 0)) < 0) {
		xil_printf("adc_stream: socket() failed\r\n");
		vTaskDelete(NULL);
	}
	if (lwip_bind(listen_sd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		xil_printf("adc_stream: bind() failed\r\n");
		vTaskDelete(NULL);
	}
	lwip_listen(listen_sd, 1);

	int flags = lwip_fcntl(listen_sd, F_GETFL, 0);
	lwip_fcntl(listen_sd, F_SETFL, flags | O_NONBLOCK);

	xil_printf("ADC stream server listening on port %d\r\n", ADC_STREAM_PORT);

	s_stream_task = xTaskGetCurrentTaskHandle();
	bool active = false;
	uint32_t applied_epoch = s_run_epoch;

	while (1) {
		remote_len = sizeof(remote);
		int new_sd = lwip_accept(listen_sd, (struct sockaddr *)&remote, &remote_len);
		if (new_sd >= 0) {
			if (client_sd >= 0) {
				STREAM_LOG("ADC stream: new client replaces the old one\r\n");
				stream_drop_client(&client_sd, &active);
			}
			client_sd = new_sd;
			int one = 1;
			lwip_setsockopt(client_sd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
			net_enable_keepalive(client_sd);
			frame_seq = 0;
			STREAM_LOG("ADC stream: client connected\r\n");
		}

		bool want = s_run_requested && (client_sd >= 0);
		if (want != active || (want && applied_epoch != s_run_epoch)) {
			if (want) {
				if (adc_hw_start_stream() != XST_SUCCESS) {
					STREAM_LOG("ADC stream: DMA start failed\r\n");
					vTaskDelay(pdMS_TO_TICKS(100));
					continue;
				}
			} else {
				adc_hw_disable_stream();
			}
			active = want;
			applied_epoch = s_run_epoch;
		}

		if (!active) {
			if (client_sd >= 0 && stream_peer_closed(client_sd)) {
				STREAM_LOG("ADC stream: client closed the connection\r\n");
				stream_drop_client(&client_sd, &active);
			}
			ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(20));
			continue;
		}

		if ((frame_seq & 127u) == 0 && stream_peer_closed(client_sd)) {
			STREAM_LOG("ADC stream: client closed the connection\r\n");
			stream_drop_client(&client_sd, &active);
			continue;
		}

		uint32_t n = adc_hw_read(dma_rx_buffer, (uint32_t)WORDS_PER_BATCH);
		if (n == 0) {
			continue;
		}

		adc_frame_header_t hdr = {
			.magic = FRAME_MAGIC,
			.seq = frame_seq++,
			.word_count = n,
			.sample_rate_hz = ads127l14_spi_get_sample_rate(),
			.channel_mask = 0x0F,
			.bits_per_sample = 24,
		};

		static uint8_t send_buf[sizeof(adc_frame_header_t) +
					WORDS_PER_BATCH * ADC_ENTRY_SIZE * sizeof(uint8_t)];
		memcpy(send_buf, &hdr, sizeof(hdr));
		memcpy(send_buf + sizeof(hdr), dma_rx_buffer, n * ADC_ENTRY_SIZE * sizeof(uint8_t));
		size_t send_len = sizeof(hdr) + n * ADC_ENTRY_SIZE * sizeof(uint8_t);

		if (net_send_all(client_sd, send_buf, send_len, STREAM_SEND_TIMEOUT_MS) != 0) {
			STREAM_LOG("ADC stream: client stalled or gone, dropping it "
				   "(free_heap=%u)\r\n",
				   (unsigned)xPortGetFreeHeapSize());
			stream_drop_client(&client_sd, &active);
			continue;
		}
	}
}
