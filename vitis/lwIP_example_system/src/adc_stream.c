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

/* Logging settings:
 * <=0 	- no logs
 * 1 	- UART console only
 * 2 	- TCP console only
 * >=3 	- TCP + UART
 */
#define STREAM_LOG_SETTING        3

#if STREAM_LOG_SETTING <= 0
#define STREAM_LOG(...)             ((void)0)
#elif STREAM_LOG_SETTING == 1
#define STREAM_LOG(...)             xil_printf(__VA_ARGS__)
#elif STREAM_LOG_SETTING == 2
#include "console.h"
#define STREAM_LOG(...)             console_printf(__VA_ARGS__)
#elif STREAM_LOG_SETTING >= 3
#include "console.h"
#define STREAM_LOG(...)                                 \
    do {                                                \
        char stream_log_buf_[128];                         \
        snprintf(stream_log_buf_, sizeof(stream_log_buf_),    \
                 __VA_ARGS__);                          \
        xil_printf("%s", stream_log_buf_);                 \
        console_printf("%s", stream_log_buf_);             \
    } while (0)
#endif

#define FRAME_MAGIC        0x41444331u  /* 'ADC1' */
#define WORDS_PER_BATCH    ADC_MAX_WORDS_PER_BATCH

#define STREAM_SEND_TIMEOUT_MS 2000

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint32_t seq;
    uint32_t word_count;
    uint32_t sample_rate_hz;
    uint8_t  channel_mask;
    uint8_t  bits_per_sample;
    uint8_t  reserved[2];
} adc_frame_header_t;

// 1. Add this globally, outside of any function.
// __attribute__((aligned(32))) forces it to sit perfectly on Zynq cache line boundaries.
static uint8_t dma_rx_buffer[WORDS_PER_BATCH * ADC_ENTRY_SIZE] __attribute__((aligned(32)));

static volatile bool     s_run_requested = false;
static volatile uint32_t s_run_epoch     = 0;      /* bumped by every request -> forces a clean restart */
static TaskHandle_t      s_stream_task   = NULL;

void adc_stream_set_run(bool run)
{
    s_run_requested = run;
    s_run_epoch++;                    /* single writer: console commands run under g_cfg_mutex */
    if (!run)
        adc_hw_abort_read();          /* wake a blocked adc_hw_read() immediately */
    if (s_stream_task)
        xTaskNotifyGive(s_stream_task);
}

bool adc_stream_get_run(void)
{
    return s_run_requested;
}

/* Close the current client and stop the hardware stream if it was running. */
static void stream_drop_client(int *client_sd, bool *active, bool abort_conn)
{
	if (abort_conn) {
	   struct linger lg = { .l_onoff = 1, .l_linger = 0 };
	   lwip_setsockopt(*client_sd, SOL_SOCKET, SO_LINGER, &lg, sizeof lg);
   }

    close(*client_sd);
    *client_sd = -1;
    if (*active) {
        adc_hw_disable_stream();
        *active = false;
    }
}

/* True when the PC has closed or reset the stream connection.
 * The PC never sends data on this port, so "readable" can only mean FIN or RST. */
static bool stream_peer_closed(int sd)
{
    fd_set rfds;
    FD_ZERO(&rfds);
    FD_SET(sd, &rfds);
    struct timeval tv = { 0, 0 };                       /* poll, never wait */

    if (lwip_select(sd + 1, &rfds, NULL, NULL, &tv) <= 0)
        return false;                                   /* nothing pending: still open */

    uint8_t dummy;
    int n = lwip_recv(sd, &dummy, 1, MSG_DONTWAIT);
    if (n == 0)
        return true;                                    /* FIN: orderly close */
    if (n < 0)
    	 if (n < 0)
    	      return net_sock_error(sd) != 0;
    return false;                                       /* stray byte from PC: ignore */
}

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
    lwip_listen(listen_sd, 1);   /* one streaming client at a time */

    /* Make accept() non-blocking so we can also service already-connected
     * clients / detect disconnects in the same loop without a second thread. */
    int flags = lwip_fcntl(listen_sd, F_GETFL, 0);
    lwip_fcntl(listen_sd, F_SETFL, flags | O_NONBLOCK);

    xil_printf("ADC stream server listening on port %d\r\n", ADC_STREAM_PORT);

    s_stream_task = xTaskGetCurrentTaskHandle();
	bool     active        = false;           /* hardware stream currently enabled by us */
	uint32_t applied_epoch = s_run_epoch;

	while (1) {
		/* ---- accept: a new client always replaces the old one */
		remote_len = sizeof(remote);
		int new_sd = lwip_accept(listen_sd, (struct sockaddr *)&remote, &remote_len);
		if (new_sd >= 0) {
			if (client_sd >= 0) {
				STREAM_LOG("ADC stream: new client replaces the old one\r\n");
				stream_drop_client(&client_sd, &active, true);
			}
			client_sd = new_sd;
			int one = 1;
			lwip_setsockopt(client_sd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
			net_enable_keepalive(client_sd);
			frame_seq = 0;
			STREAM_LOG("ADC stream: client connected\r\n");
		}

		/* ---- apply the run request: stream only while requested AND someone listens */
		bool want = s_run_requested && (client_sd >= 0);
		if (want != active || (want && applied_epoch != s_run_epoch)) {
			if (want) {
				if (adc_hw_start_stream() != XST_SUCCESS) {   /* DMA reset + re-arm + enable */
					STREAM_LOG("ADC stream: DMA start failed\r\n");
					vTaskDelay(pdMS_TO_TICKS(100));
					continue;
				}
			} else {
				adc_hw_disable_stream();
			}
			active        = want;
			applied_epoch = s_run_epoch;
		}

		/* ---- idle: no client or sampling stopped -> sleep, but still notice a hang-up */
		if (!active) {
			if (client_sd >= 0 && stream_peer_closed(client_sd)) {
				STREAM_LOG("ADC stream: client closed the connection\r\n");
				stream_drop_client(&client_sd, &active, false);
			}
			ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(20));   /* returns at once on START/STOP */
			continue;
		}

		/* ---- streaming */
		if ((frame_seq & 127u) == 0 && stream_peer_closed(client_sd)) {
			STREAM_LOG("ADC stream: client closed the connection\r\n");
			stream_drop_client(&client_sd, &active, false);
			continue;
		}

		uint32_t n = adc_hw_read(dma_rx_buffer, (uint32_t)WORDS_PER_BATCH);
		if (n == 0)
			continue;

        adc_frame_header_t hdr = {
            .magic = FRAME_MAGIC,
            .seq = frame_seq++,
            .word_count = n,
            .sample_rate_hz = ads127l14_spi_get_sample_rate(),
            .channel_mask = 0x0F,
            .bits_per_sample = 24,
        };

        /* Combine header+payload into ONE buffer and ONE lwip_write() call instead
         * of two separate writes. This halves the per-iteration socket-call rate
         * and, more importantly, removes the failure mode where the header write
         * succeeds but the payload write fails (or vice versa), which would
         * desync the framing the Python side relies on. */
        static uint8_t send_buf[sizeof(adc_frame_header_t) + WORDS_PER_BATCH * ADC_ENTRY_SIZE * sizeof(uint8_t)];
        memcpy(send_buf, &hdr, sizeof(hdr));
        memcpy(send_buf + sizeof(hdr), dma_rx_buffer, n * ADC_ENTRY_SIZE * sizeof(uint8_t));
        size_t send_len = sizeof(hdr) + n * ADC_ENTRY_SIZE * sizeof(uint8_t);

        if (net_send_all(client_sd, send_buf, send_len, STREAM_SEND_TIMEOUT_MS) != 0) {
			STREAM_LOG("ADC stream: client stalled or gone, dropping it (free_heap=%u)\r\n",
					   (unsigned)xPortGetFreeHeapSize());
			stream_drop_client(&client_sd, &active, true);
			continue;
		}
    }
}
