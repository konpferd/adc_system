#ifndef ADS127L14_SPI_H
#define ADS127L14_SPI_H

#include <stdint.h>
#include <stdbool.h>

typedef enum {
    ADS127L14_OK            =  0,
    ADS127L14_ERR_NOT_READY = -1,   /* init / config not done or failed       */
    ADS127L14_ERR_PARAM     = -2,   /* argument out of range                  */
    ADS127L14_ERR_SPI       = -3,   /* Zynq SPI transfer failed               */
    ADS127L14_ERR_VERIFY    = -4,   /* readback differs from what was written */
    ADS127L14_ERR_CLK_LIMIT = -5,   /* fCLK outside the speed mode's range    */
    ADS127L14_ERR_MISMATCH  = -6,   /* channels do not have identical config  */
    ADS127L14_ERR_DEV_ID    = -7    /* DEV_ID is not 0x04 (ADS127L14)         */
} ads127l14_status_t;

typedef enum {
    ADS127L14_SPEED_LOW  = 0,       /* fCLK <=  3.29 MHz (nominal  3.2  MHz) */
    ADS127L14_SPEED_MID  = 1,       /* fCLK <= 13.15 MHz (nominal 12.8  MHz) */
    ADS127L14_SPEED_HIGH = 2,       /* fCLK <= 26.3  MHz (nominal 25.6  MHz) */
    ADS127L14_SPEED_MAX  = 3        /* fCLK <= 33.66 MHz (nominal 32.768MHz) */
} ads127l14_speed_t;

#ifndef ADS127L14_FCLK_INT_HZ
#define ADS127L14_FCLK_INT_HZ   25600000UL
#endif
#ifndef ADS127L14_FCLK_EXT_HZ
#define ADS127L14_FCLK_EXT_HZ   0UL
#endif

#define ADS127L14_SPI_DEFAULT_SPEED_MODE  ADS127L14_SPEED_HIGH
#define ADS127L14_SPI_DEFAULT_DCLK_DIV    0     /* 0:/1  1:/2  2:/4  3:/8        */
#define ADS127L14_SPI_DEFAULT_CLK_DIV     0     /* 0:/1  1:/2  2:/3  3:/4  4:/8  */
#define ADS127L14_SPI_DEFAULT_FLTR        9     /* 01001b = Sinc4, OSR = 16      */

#define ADS127L14_FLTR_WIDEBAND_OSR32     0
#define ADS127L14_FLTR_WIDEBAND_OSR64     1
#define ADS127L14_FLTR_SINC4_OSR16        9
#define ADS127L14_FLTR_SINC4_OSR32        11
#define ADS127L14_FLTR_SINC4_OSR64        12

int  ads127l14_spi_init(void);

int  ads127l14_spi_system_config(void);

int  ads127l14_reg_read(uint8_t addr, uint8_t *value);
int  ads127l14_reg_write(uint8_t addr, uint8_t value);

int  ads127l14_spi_read_device_data(uint8_t *device_id, uint8_t *revision);
int  ads127l14_spi_read_status(uint8_t *status);
int  ads127l14_spi_clear_status(void);

int  ads127l14_spi_set_speed_mode(uint8_t speed_mode);
int  ads127l14_spi_set_dclk_div(uint8_t dclk_div);
int  ads127l14_spi_set_clk_div(uint8_t clk_div);
int  ads127l14_spi_set_clocking(uint8_t speed_mode, uint8_t clk_div);
int  ads127l14_spi_set_fltr(uint8_t fltr);
int  ads127l14_spi_verify_channels(void);

void ads127l14_spi_display_settings(void);

uint32_t ads127l14_spi_get_sample_rate(void);
uint32_t ads127l14_spi_get_sample_rate_mhz(void);

uint32_t ads127l14_spi_calc_sample_rate_mhz(uint32_t f_source_hz,
                                            uint8_t clk_div_code,
                                            uint8_t fltr);

#endif /* ADS127L14_SPI_H */
