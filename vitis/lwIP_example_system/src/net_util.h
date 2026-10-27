/**
 * @file net_util.h
 * @brief lwIP socket helpers.
 *
 * @defgroup net_util Socket helpers
 * @{
 */

#ifndef NET_UTIL_H
#define NET_UTIL_H
#include <stddef.h>
#include <stdint.h>

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
int net_send_all(int sd, const void *data, size_t len, uint32_t stall_timeout_ms);

/**
 * @brief Enable TCP keepalive: 5 s idle, 1 s interval, 3 probes.
 *
 * @param sd Connected socket.
 */
void net_enable_keepalive(int sd);

/**
 * @brief Get the pending error of a socket (SO_ERROR).
 *
 * @param sd Socket.
 *
 * @return Pending error, 0 if none, -1 if the query failed.
 */
int net_sock_error(int sd);

/** @} */

#endif /* NET_UTIL_H */
