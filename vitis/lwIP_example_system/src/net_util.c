#include "lwip/sockets.h"
#include "net_util.h"

int net_sock_error(int sd)
{
    int err = 0;
    socklen_t len = sizeof(err);
    if (lwip_getsockopt(sd, SOL_SOCKET, SO_ERROR, &err, &len) != 0)
        return -1;
    return err;
}

int net_send_all(int sd, const void *data, size_t len, uint32_t stall_timeout_ms)
{
    const uint8_t *p = (const uint8_t *)data;

    while (len > 0) {
        int n = lwip_send(sd, p, len, MSG_DONTWAIT);
        if (n > 0) {
            p   += n;
            len -= (size_t)n;
            continue;
        }
        if (n < 0 && net_sock_error(sd) != 0)
            return -1;                       /* RST / closed / aborted */

        /* buffer full: wait until writable, or give up after the stall timeout */
        fd_set wfds;
        FD_ZERO(&wfds);
        FD_SET(sd, &wfds);
        struct timeval tv = { .tv_sec  = stall_timeout_ms / 1000,
                              .tv_usec = (stall_timeout_ms % 1000) * 1000 };
        if (lwip_select(sd + 1, NULL, &wfds, NULL, &tv) <= 0)
            return -1;
    }
    return 0;
}
void net_enable_keepalive(int sd)
{
    int on = 1;
    lwip_setsockopt(sd, SOL_SOCKET, SO_KEEPALIVE, &on, sizeof(on));
#if LWIP_TCP_KEEPALIVE
    int idle = 5, intvl = 1, cnt = 3;
    lwip_setsockopt(sd, IPPROTO_TCP, TCP_KEEPIDLE,  &idle,  sizeof(idle));
    lwip_setsockopt(sd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof(intvl));
    lwip_setsockopt(sd, IPPROTO_TCP, TCP_KEEPCNT,   &cnt,   sizeof(cnt));
#endif
}
