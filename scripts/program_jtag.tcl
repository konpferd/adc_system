# Load bitstream + application over JTAG (RAM only, lost at power-off).
# Usage:  xsct scripts/program_jtag.tcl        (run from C:\adc_system)
set root [file normalize [file join [file dirname [info script]] ..]]
set hw   $root/vitis/system_wrapper_lwIP/export/system_wrapper_lwIP/hw
set bit  [lindex [glob $hw/*.bit] 0]
set elf  $root/vitis/lwIP_example_system/Debug/lwIP_example_system.elf

connect
targets -set -nocase -filter {name =~ "APU*"}
rst -system
after 1000
puts "Bitstream: $bit"
fpga -file $bit
targets -set -nocase -filter {name =~ "APU*"}
source $hw/ps7_init.tcl
ps7_init
ps7_post_config
targets -set -nocase -filter {name =~ "*Cortex-A9*#0"}
rst -processor
puts "Application: $elf"
dow $elf
con
puts "Running."
disconnect
