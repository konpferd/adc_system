#ifndef PGA_SPI_H
#define PGA_SPI_H
#include <stdint.h>

#define PGA_SPI_DEFAULT_VAL_CH0 0x40
#define PGA_SPI_DEFAULT_VAL_CH1 0x40
#define PGA_SPI_DEFAULT_VAL_CH2 0x40
#define PGA_SPI_DEFAULT_VAL_CH3 0x40

int pga_spi_init(void);
void pga_spi_system_config(void);
void pga_spi_system_reset(void);
void pga_spi_set_rdac(uint8_t channel0, uint8_t channel1, uint8_t channel2, uint8_t channel3);
void pga_spi_eeprom_save_rdac(void);

#endif	/* PGA_SPI_H */
