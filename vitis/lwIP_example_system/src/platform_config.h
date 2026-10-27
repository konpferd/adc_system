/**
 * @file platform_config.h
 * @brief Platform selection for the lwIP adapter.
 */

#ifndef __PLATFORM_CONFIG_H_
#define __PLATFORM_CONFIG_H_

/** Base address of the Ethernet MAC used by lwIP. */
#define PLATFORM_EMAC_BASEADDR XPAR_XEMACPS_0_BASEADDR

#define PLATFORM_ZYNQ /**< Target is a Zynq-7000. */

#endif /* __PLATFORM_CONFIG_H_ */
