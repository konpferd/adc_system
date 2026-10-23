@echo off
rem ADC board programming without the Vitis GUI.
rem   program.bat jtag   - load bitstream + app over JTAG (RAM, until power-off)
rem   program.bat boot   - build BOOT.BIN from the current FSBL, bitstream and app
rem   program.bat qspi   - build BOOT.BIN and write it to the on-module QSPI flash
setlocal
set VITIS=D:\programfiles\xilinx\vitis\2022.2
set HERE=%~dp0
set FSBL=%HERE%..\vitis\system_wrapper_lwIP\export\system_wrapper_lwIP\sw\system_wrapper_lwIP\boot\fsbl.elf
cd /d "%HERE%"

if /i "%1"=="jtag" (
  call "%VITIS%\bin\xsct.bat" "%HERE%program_jtag.tcl" || goto fail
  goto ok
)
if /i "%1"=="boot" goto boot
if /i "%1"=="qspi" goto boot
echo Usage: program.bat jtag ^| boot ^| qspi
exit /b 1

:boot
call "%VITIS%\bin\bootgen.bat" -image boot.bif -arch zynq -o "%HERE%BOOT.BIN" -w on || goto fail
echo BOOT.BIN written to %HERE%BOOT.BIN
if /i not "%1"=="qspi" goto ok
call "%VITIS%\bin\program_flash.bat" -f "%HERE%BOOT.BIN" -offset 0 -flash_type qspi-x4-single -fsbl "%FSBL%" -verify -cable type xilinx_tcf url TCP:127.0.0.1:3121 || goto fail
goto ok

:fail
echo FAILED
exit /b 1
:ok
echo Done.
exit /b 0
