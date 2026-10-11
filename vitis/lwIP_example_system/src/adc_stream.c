#include <string.h>
#include <errno.h>
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
#include "semphr.h"

#define FRAME_MAGIC        0x41444331u  /* 'ADC1' */
#define WORDS_PER_BATCH    ADC_MAX_WORDS_PER_BATCH

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

    while (1) {
        if (client_sd < 0) {
            remote_len = sizeof(remote);
            client_sd = lwip_accept(listen_sd, (struct sockaddr *)&remote, &remote_len);
            if (client_sd >= 0) {
                int one = 1;
                lwip_setsockopt(client_sd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
                xil_printf("ADC stream: client connected\r\n");
            } else {
                vTaskDelay(pdMS_TO_TICKS(20));
                continue; /* no client yet; don't burn samples with nobody listening */
            }
        }

        uint32_t n = adc_hw_read(dma_rx_buffer, (uint32_t) WORDS_PER_BATCH);
        if (n == 0)
            continue;

        adc_frame_header_t hdr = {
            .magic = FRAME_MAGIC,
            .seq = frame_seq++,
            .word_count = n,
            .sample_rate_hz = ADC_MAX_SAMPLE_RATE_HZ,
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

        int wr = lwip_write(client_sd, send_buf, send_len);
        eth_led_status_note_activity();
        if (wr < 0) {
            xil_printf("ADC stream: write failed, errno=%d, free_heap=%u, dropping client\r\n", errno, (unsigned)xPortGetFreeHeapSize());
            close(client_sd);
            client_sd = -1;
        } else if ((size_t)wr != send_len) {
            /* Partial write -- with a blocking socket this is unusual but not
             * impossible under memory pressure. Log it; decide whether to loop
             * lwip_write() for the remainder or just drop the client and resync. */
            xil_printf("ADC stream: SHORT write %d/%u, errno=%d\r\n", wr, (unsigned)send_len, errno);
        }
    }
}
