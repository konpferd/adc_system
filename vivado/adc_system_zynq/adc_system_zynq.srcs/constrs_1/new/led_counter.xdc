###############################################################
# adc_system.xdc
###############################################################

# MIC SENSE PINS - Bank 35, VCCO = 3.3V
set_property PACKAGE_PIN F21 [get_ports {mic_sense[0]}]
set_property PACKAGE_PIN F22 [get_ports {mic_sense[1]}]
set_property PACKAGE_PIN C17 [get_ports {mic_sense[2]}]
set_property PACKAGE_PIN C18 [get_ports {mic_sense[3]}]

set_property IOSTANDARD LVCMOS33 [get_ports {mic_sense[*]}]

# USER LEDs - Bank 35, VCCO = 3.3V
set_property PACKAGE_PIN A16 [get_ports {led[0]}]
set_property PACKAGE_PIN A17 [get_ports {led[1]}]
set_property PACKAGE_PIN A18 [get_ports {led[2]}]
set_property PACKAGE_PIN A19 [get_ports {led[3]}]

set_property IOSTANDARD LVCMOS33 [get_ports {led[*]}]
set_property SLEW       SLOW     [get_ports {led[*]}]
set_property DRIVE      8        [get_ports {led[*]}]
set_false_path -to [get_ports {led[*]}]

# ETH bi-color status LEDs - Bank 35, VCCO = 3.3V
set_property PACKAGE_PIN D21 [get_ports {eth_led[0]}]
set_property PACKAGE_PIN E21 [get_ports {eth_led[1]}]
set_property PACKAGE_PIN B20 [get_ports {eth_led[2]}]
set_property PACKAGE_PIN B19 [get_ports {eth_led[3]}]

set_property IOSTANDARD LVCMOS33 [get_ports {eth_led[*]}]
set_property SLEW       SLOW     [get_ports {eth_led[*]}]
set_property DRIVE      8        [get_ports {eth_led[*]}]
set_false_path -to [get_ports {eth_led[*]}]

# PGA interface - Bank 35, VCCO = 3.3V
set_property PACKAGE_PIN C15 [get_ports {pga_r[0]}]
set_property PACKAGE_PIN B15 [get_ports {pga_r[1]}]
set_property PACKAGE_PIN D17 [get_ports {pga_sync}]
set_property PACKAGE_PIN D16 [get_ports {pga_sdo}]
set_property PACKAGE_PIN E19 [get_ports {pga_sdi}]
set_property PACKAGE_PIN B17 [get_ports {pga_sclk}]

set_property IOSTANDARD LVCMOS33 [get_ports {pga_r[*]}]
set_property IOSTANDARD LVCMOS33 [get_ports {pga_sync}]
set_property IOSTANDARD LVCMOS33 [get_ports {pga_sdo}]
set_property IOSTANDARD LVCMOS33 [get_ports {pga_sdi}]
set_property IOSTANDARD LVCMOS33 [get_ports {pga_sclk}]

# CH disable - Bank 35, VCCO = 3.3V
set_property PACKAGE_PIN F18 [get_ports {ch_disable[0]}]
set_property PACKAGE_PIN E18 [get_ports {ch_disable[1]}]
set_property PACKAGE_PIN D15 [get_ports {ch_disable[2]}]
set_property PACKAGE_PIN E15 [get_ports {ch_disable[3]}]

set_property IOSTANDARD LVCMOS33 [get_ports {ch_disable[*]}]

# analog power enable - Bank 34, VCCO = 3.3V
set_property PACKAGE_PIN N17 [get_ports {analog_pwr_en}]

set_property IOSTANDARD LVCMOS33 [get_ports {analog_pwr_en}]

# ADC system - Bank 13, VCCO = 1.8V
set_property PACKAGE_PIN AB2 [get_ports {adc_dout[0]}]
set_property PACKAGE_PIN Y4 [get_ports {adc_dout[1]}]
set_property PACKAGE_PIN AB5 [get_ports {adc_dout[2]}]
set_property PACKAGE_PIN AB7 [get_ports {adc_dout[3]}]

set_property PACKAGE_PIN AB10 [get_ports {adc_fsync}]
set_property PACKAGE_PIN T4 [get_ports {adc_dclk}]
set_property PACKAGE_PIN AA9 [get_ports {adc_clkin}]

set_property PACKAGE_PIN V8 [get_ports {adc_start}]
set_property PACKAGE_PIN U10 [get_ports {adc_error}]
set_property PACKAGE_PIN W6 [get_ports {adc_r}]
set_property PACKAGE_PIN Y11 [get_ports {adc_sdo}]
set_property PACKAGE_PIN R6 [get_ports {adc_sdi}]
set_property PACKAGE_PIN V7 [get_ports {adc_cs}]
set_property PACKAGE_PIN U6 [get_ports {adc_sclk}]

# IO0
set_property PACKAGE_PIN W11 [get_ports {adc_io[0]}]
# IO1
set_property PACKAGE_PIN V10 [get_ports {adc_io[1]}]
# IO4
set_property PACKAGE_PIN V12 [get_ports {adc_io[2]}]
# IO5
set_property PACKAGE_PIN Y6 [get_ports {adc_io[3]}]
# IO6
set_property PACKAGE_PIN AA7 [get_ports {adc_io[4]}]
# IO7
set_property PACKAGE_PIN Y9 [get_ports {adc_io[5]}]

set_property IOSTANDARD LVCMOS18 [get_ports {adc_dout[*]}]
set_property IOSTANDARD LVCMOS18 [get_ports {adc_io[*]}]

set_property IOSTANDARD LVCMOS18 [get_ports {adc_fsync}]
set_property IOSTANDARD LVCMOS18 [get_ports {adc_dclk}]
set_property IOSTANDARD LVCMOS18 [get_ports {adc_clkin}]
set_property IOSTANDARD LVCMOS18 [get_ports {adc_start}]
set_property IOSTANDARD LVCMOS18 [get_ports {adc_error}]
set_property IOSTANDARD LVCMOS18 [get_ports {adc_r}]
set_property IOSTANDARD LVCMOS18 [get_ports {adc_sdo}]
set_property IOSTANDARD LVCMOS18 [get_ports {adc_sdi}]
set_property IOSTANDARD LVCMOS18 [get_ports {adc_cs}]
set_property IOSTANDARD LVCMOS18 [get_ports {adc_sclk}]

set_property CLOCK_DEDICATED_ROUTE FALSE [get_nets adc_dclk_IBUF]

#BITSTREAM SETTINGS
set_property CONFIG_VOLTAGE 3.3  [current_design]
set_property CFGBVS         VCCO [current_design]
set_property BITSTREAM.GENERAL.COMPRESS TRUE [current_design]