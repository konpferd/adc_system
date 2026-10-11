`timescale 1ns / 1ps

module blinky_block(
    input clk,
    output wire [3:0] led
);
    reg [23:0] clk_counter;
    reg [3:0] led_counter;
    
    always @(posedge  clk) begin
        if (clk_counter == 0) begin
            led_counter <= led_counter + 1;
        end
        clk_counter <= clk_counter + 1;
    end
    
    assign led = led_counter;
endmodule
