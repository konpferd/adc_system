ADC acquisition firmware (TE0720 / Zynq-7020, FreeRTOS + lwIP)
==============================================================

Firmware for the ADS127L14 4-channel 24-bit ADC board. It configures the
ADC and the PGA over SPI, streams the samples received from the PL (AXI DMA)
to a PC over TCP, and provides a TCP command console.

Started from the Xilinx "FreeRTOS lwIP Echo Server" template; the echo
server has been removed.


Network
-------

  IP address   192.168.1.10
  Netmask      255.255.255.0
  Gateway      192.168.1.1
  MAC address  02:00:00:AD:C0:01

  TCP 5000     command console       (CONSOLE_PORT,    console.h)
  TCP 5001     sample stream         (ADC_STREAM_PORT, adc_stream.h)

The addresses are set in network_thread() in main.c. Every board running
this firmware uses the same MAC and IP address.


Start-up
--------

main() starts lwIP; network_thread() then brings up the Ethernet interface
and initializes the hardware in this order. User LEDs 0..3 light up as the
first four steps complete:

  1. GPIO (EMIO pins)          device_gpio_init()             user LED 0
  2. AXI DMA + interrupt       adc_hw_init()                  user LED 1
  3. ADS127L14 SPI             ads127l14_spi_init()           user LED 2
  4. PGA (2 x AD5122) SPI      pga_spi_init()                 user LED 3
  5. XADC (die temperature)    device_xadc_init()
  6. ADC default config        ads127l14_spi_system_config()

The results are kept in main_init_report and sent to every console client
when it connects. After that the LED task mirrors the microphone-sense
inputs on the user LEDs.


Tasks
-----

  Task                    Priority  Stack (words)  Source
  tcpip_thread (lwIP)     5         1024           BSP
  xemacif_input_thread    4         1024           main.c
  adc_stream              3         4096           adc_stream.c
  console (listener)      2         2048           console.c
  console_c (per client)  2         4096           console.c
  eth_led_stat            1         1024           led_status.c


Sample stream (port 5001)
-------------------------

One client at a time; a new connection replaces the old one. The DMA runs
only while sampling is requested (ADC START) and a client is connected.
A client that blocks sending for 2 s is dropped.

Each DMA batch is sent as one frame, little-endian:

  Header (20 bytes)
    uint32  magic            0x41444331 ("ADC1")
    uint32  seq              frame number, reset on connect
    uint32  word_count       entries in the payload
    uint32  sample_rate_hz   per-channel sample rate
    uint8   channel_mask     0x0F
    uint8   bits_per_sample  24
    uint8   reserved[2]

  Payload: word_count entries of 16 bytes
    bytes  0..2   ch3   24-bit
    bytes  3..5   ch2   24-bit
    bytes  6..8   ch1   24-bit
    bytes  9..11  ch0   24-bit
    bytes 12..15  hw_frame_ctr (uint32, counts every PL sample frame)

Batch size: ADC_WORDS_PER_BATCH = 2048 entries by default, at most
ADC_MAX_WORDS_PER_BATCH = 4095 (16-bit AXI DMA buffer-length register).
Every START or RESET restarts the DMA, so hw_frame_ctr jumps once.


Command console (port 5000)
---------------------------

Line based, case-insensitive, up to 4 clients. Every reply starts with
"OK" or "ERR" and ends with the prompt "> ". Log messages of the drivers
are sent to all clients. Type HELP for the list.

  HELP
  ANALOG_PWR ON | OFF
  ADC READ_ID | RESET | START | STOP | STATUS | CLEAR_STATUS
  ADC DISPLAY_SETTINGS
  ADC SET SPEED_MODE <0-3>
  ADC SET DCLK_DIV <1|2|4|8>
  ADC SET CLK_DIV <1|2|3|4|8>
  ADC SET FLTR <0-31>
  PGA SET_GAIN <ch0> <ch1> <ch2> <ch3>     codes 0x00..0x7F
  PGA READ_GAIN | DISPLAY_SETTINGS | RESET | SAVE_EEPROM
  CH_STATUS
  DEVICE_TEMP     OK DEVICE_TEMP=<degC> MIN=<degC> MAX=<degC> DEVICE_WRN_LVL=75

The PGA needs analog power (ANALOG_PWR ON). PGA DISPLAY_SETTINGS lists the
codes by ADC channel (ch0..ch3), the same numbering as SET_GAIN / READ_GAIN.


Source files
------------

  main.c              start-up, network settings, task creation
  adc_hw.c/.h         AXI DMA (S2MM) ping-pong receive, batch control GPIO
  adc_stream.c/.h     stream server (port 5001)
  console.c/.h        command console (port 5000), console_printf()
  ads127l14_spi.c/.h  ADS127L14 driver: registers, clocking, filter, sample rate
  pga_spi.c/.h        AD5122 PGA driver (2 chips, daisy chain)
  device_gpio.c/.h    EMIO pin map and GPIO access
  device_xadc.c/.h    die temperature
  led_status.c/.h     Ethernet link/activity LEDs, user LEDs
  net_util.c/.h       socket helpers (send with stall timeout, keepalive)
  os_delay.h          microsecond delay (vTaskDelay or busy-wait)
  platform_config.h   lwIP platform selection
  iic_phyreset.c      Xilinx template, ZCU102 only (not built for TE0720)

Logging: each driver has a *_LOG_SETTING macro at the top of its .c file
(0 off, 1 UART, 2 TCP console, 3 both). Keep UART logging out of the
stream path: polled UART output stalls the stream task.


Build notes
-----------

- Hardware: the platform uses hw_export/adc_system_bd_wrapper.xsa.
- BSP domain freertos10_xilinx_ps7_cortexa9_0 must be built with
  -fno-strict-aliasing (ps7_cortexa9_0 -> extra_compiler_flags). Without it
  GCC miscompiles the FreeRTOS 10.4.6 list handling in xTaskResumeAll() and
  the board freezes under streaming load.
- lwIP option "lwip_stats" is enabled; the LED task uses lwip_stats.link.
- Programming without the Vitis GUI: scripts/program.bat (jtag | boot | qspi)
  in the repository root.


Code style and documentation
----------------------------

- Zephyr coding style: ../.clang-format  (clang-format -i src/*.c src/*.h)
- Doxygen on all functions, types and macros: ../Doxyfile  (run doxygen in
  the project folder; HTML output in docs/html)
