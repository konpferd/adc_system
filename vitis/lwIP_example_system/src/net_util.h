#ifndef NET_UTIL_H
#define NET_UTIL_H
#include <stddef.h>
#include <stdint.h>

int  net_send_all(int sd, const void *data, size_t len, uint32_t stall_timeout_ms);
void net_enable_keepalive(int sd);
int net_sock_error(int sd);

#endif
