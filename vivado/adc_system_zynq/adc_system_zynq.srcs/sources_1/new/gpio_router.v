`timescale 1ns / 1ps

module gpio_router(
    input  wire [63:0] pin_o,
    input  wire [63:0] pin_t,
    output wire [63:0] pin_i,

    output wire [3:0] eth_led,
    output wire       analog_pwr_en,
    output wire [3:0] ch_disable,
    output wire [1:0] pga_r,
    output wire adc_start,
    output wire adc_r,
    
    input  wire [3:0] mic_conected,
    input  wire adc_error,
    
    inout  wire [5:0] adc_io,
    
    output wire [3:0] user_led
);

    // ------------------------------------------------------------------------
    // GPIO output routing
    // ------------------------------------------------------------------------

    assign eth_led          = pin_o[3:0];
    assign ch_disable       = pin_o[7:4];
    assign pga_r            = pin_o[9:8];
    assign analog_pwr_en    = pin_o[16];
    assign adc_start        = pin_o[17];
    assign adc_r            = pin_o[18];
    assign user_led         = pin_o[27:24];
    
    assign pin_i[22:19]     = mic_conected;
    assign pin_i[23]        = adc_error;

    // ------------------------------------------------------------------------
    // GPIO input routing
    // All other unused GPIO inputs are tied high.
    // ------------------------------------------------------------------------

    assign pin_i[9:0]   = 10'h3FF;
    assign pin_i[18:16] = 3'h7;
    assign pin_i[63:24] = 40'hFFFFFFFFFF;

    // ------------------------------------------------------------------------
    // Bidirectional ADC GPIO
    // T = 0 : FPGA drives the pin
    // T = 1 : FPGA releases the pin (Hi-Z)
    // ------------------------------------------------------------------------

    genvar i;
    generate
        for (i = 0; i < 6; i = i + 1) begin : gen_adc_iobuf
            IOBUF adc_iobuf (
                .I  (pin_o[10 + i]),
                .O  (pin_i[10 + i]),
                .T  (pin_t[10 + i]),
                .IO (adc_io[i])
            );
        end
    endgenerate

endmodule