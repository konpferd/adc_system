/**
 * @file net_util.c
 * @brief lwIP socket helpers.
 *
 * Errors are read with SO_ERROR because lwIP and newlib keep separate errno
 * variables.
 */

#include "lwip/sockets.h"
#include "net_util.h"

/**
 * @brief Get the pending error of a socket (SO_ERROR).
 *
 * @param sd Socket.
 *
 * @return Pending error, 0 if none, -1 if the query failed.
 */
int net_sock_error(int sd)
{
	int err = 0;
	socklen_t len = sizeof(err);
	if (lwip_getsockopt(sd, SOL_SOCKET, SO_ERROR, &err, &len) != 0) {
		return -1;
	}
	return err;
}

/**
 * @brief Send a whole buffer, waiting while the TCP send buffer is full.
 *
 * @param sd Connected socket.
 * @param data Data to send.
 * @param len Number of bytes.
 * @param stall_timeout_ms Longest wait without progress, in ms.
 *
 * @retval 0 All data sent.
 * @retval -1 Socket error or stall timeout.
 */
int net_send_all(int sd, const void *data, size_t len, uint32_t stall_timeout_ms)
{
	const uint8_t *p = (const uint8_t *)data;

	while (len > 0) {
		int n = lwip_send(sd, p, len, MSG_DONTWAIT);
		if (n > 0) {
			p += n;
			len -= (size_t)n;
			continue;
		}
		if (n < 0 && net_sock_error(sd) != 0) {
			return -1;
		}

		fd_set wfds;
		FD_ZERO(&wfds);
		FD_SET(sd, &wfds);
		struct timeval tv = {.tv_sec = stall_timeout_ms / 1000,
				     .tv_usec = (stall_timeout_ms % 1000) * 1000};
		if (lwip_select(sd + 1, NULL, &wfds, NULL, &tv) <= 0) {
			return -1;
		}
	}
	return 0;
}

/**
 * @brief Enable TCP keepalive: 5 s idle, 1 s interval, 3 probes.
 *
 * @param sd Connected socket.
 */
void net_enable_keepalive(int sd)
{
	int on = 1;
	lwip_setsockopt(sd, SOL_SOCKET, SO_KEEPALIVE, &on, sizeof(on));
#if LWIP_TCP_KEEPALIVE
	int idle = 5, intvl = 1, cnt = 3;
	lwip_setsockopt(sd, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof(idle));
	lwip_setsockopt(sd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof(intvl));
	lwip_setsockopt(sd, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof(cnt));
#endif
}
