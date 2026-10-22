// Designed for 2 daisy chained AD5122BCPZ10-RL7 digital potentiometer
#ifndef PGA_SPI_H
#define PGA_SPI_H

#include <stdint.h>
#include <stdbool.h>

#define PGA_SPI_NUM_CHANNELS    4
#define PGA_SPI_MAX_CODE        0x7F

#define PGA_SPI_DEFAULT_VAL_CH0 0x40
#define PGA_SPI_DEFAULT_VAL_CH1 0x40
#define PGA_SPI_DEFAULT_VAL_CH2 0x40
#define PGA_SPI_DEFAULT_VAL_CH3 0x40

typedef enum {
    PGA_OK             =  0,
    PGA_ERR_NOT_READY  = -1,   /* pga_spi_init() not done / failed          */
    PGA_ERR_PARAM      = -2,   /* NULL pointer or code > PGA_SPI_MAX_CODE   */
    PGA_ERR_SPI        = -3,   /* Zynq SPI transfer failed                  */
    PGA_ERR_VERIFY     = -4    /* readback differs from what was written    */
} pga_status_t;

int  pga_spi_init(void);
int  pga_spi_system_reset(void);
int  pga_spi_system_config(void);
int  pga_spi_set_rdac(uint8_t channel0, uint8_t channel1,
                      uint8_t channel2, uint8_t channel3);
int  pga_spi_read_rdac(uint8_t *channel0, uint8_t *channel1,
                       uint8_t *channel2, uint8_t *channel3);
int  pga_spi_eeprom_save_rdac(void);

void pga_spi_display_settings(void);

#endif /* PGA_SPI_H */
