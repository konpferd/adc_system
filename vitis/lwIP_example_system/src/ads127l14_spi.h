#ifndef ADS127L14_SPI_H
#define ADS127L14_SPI_H
#include <stdint.h>

int ads127l14_spi_init(void);
void ads127114_spi_system_config(void);
uint8_t ads127l14_reg_read(uint8_t addr, uint8_t *value);
uint8_t ads127l14_reg_write(uint8_t addr, uint8_t value);

#endif	/* ADS127L14_SPI_H */
