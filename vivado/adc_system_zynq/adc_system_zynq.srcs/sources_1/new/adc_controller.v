`timescale 1ns / 1ps

module adc_controller (
    input  wire         adc_dclk,
    input  wire [3:0]   adc_dout,
    input  wire         adc_fsync,
    input  wire         adc_error,
    input  wire         adc_start,
    output wire         adc_clkin,
    input  wire         adc_resetn,

    input  wire         m_axis_aclk,
    input  wire         m_axis_aresetn,
    output wire [127:0] m_axis_tdata,
    output wire [15:0]  m_axis_tkeep,
    output wire         m_axis_tvalid,
    input  wire         m_axis_tready,
    output wire         m_axis_tlast,

    output wire         adc_error_out,
    output reg  [31:0]  word_drop_count
);
    assign adc_clkin = 1'b0;

    // ---- input registers + fsync edge detect ----
    reg [3:0] dout_q;
    reg       fsync_q, fsync_qq;
    always @(posedge adc_dclk) begin
        dout_q   <= adc_dout;
        fsync_q  <= adc_fsync;
        fsync_qq <= fsync_q;
    end
    wire sync_rise = fsync_q & ~fsync_qq;

    reg [4:0]  bit_cnt;
    reg [31:0] frame_cnt;
    wire       frame_done = sync_rise & (bit_cnt > 5'd24);

    always @(posedge adc_dclk) begin
        if (!adc_resetn) begin
            bit_cnt   <= 5'd0;
            frame_cnt <= 32'd0;
        end else if (sync_rise) begin
            bit_cnt <= 5'd0;
            if (frame_done) frame_cnt <= frame_cnt + 32'd1;
        end else if (bit_cnt != 5'd31) begin
            bit_cnt <= bit_cnt + 5'd1;
        end
    end

    reg [23:0] ch0, ch1, ch2, ch3;
    always @(posedge adc_dclk) begin
        if (sync_rise || bit_cnt < 5'd23) begin
            ch0 <= {ch0[22:0], dout_q[0]};
            ch1 <= {ch1[22:0], dout_q[1]};
            ch2 <= {ch2[22:0], dout_q[2]};
            ch3 <= {ch3[22:0], dout_q[3]};
        end
    end

    wire         fifo_full, fifo_empty, fifo_wr_rst_busy;
    wire [127:0] fifo_dout;
    wire         can_write = ~fifo_full & ~fifo_wr_rst_busy;

    always @(posedge adc_dclk) begin
        if (!adc_resetn)                      word_drop_count <= 32'd0;
        else if (frame_done & ~can_write)     word_drop_count <= word_drop_count + 32'd1;
    end

    xpm_fifo_async #(
        .FIFO_MEMORY_TYPE ("auto"),
        .FIFO_WRITE_DEPTH (16),
        .WRITE_DATA_WIDTH (128),
        .READ_MODE        ("fwft"),
        .READ_DATA_WIDTH  (128),
        .RELATED_CLOCKS   (0),
        .CDC_SYNC_STAGES  (2),
        .DOUT_RESET_VALUE ("0"),
        .ECC_MODE         ("no_ecc"),
        .FULL_RESET_VALUE (0),
        .USE_ADV_FEATURES ("0000")
    ) u_cdc_fifo (
        .wr_clk        (adc_dclk),
        .wr_en         (frame_done & can_write),
        .din           ({frame_cnt, ch0, ch1, ch2, ch3}),
        .full          (fifo_full),
        .overflow      (),
        .wr_rst_busy   (fifo_wr_rst_busy),
        .rd_clk        (m_axis_aclk),
        .rd_en         (m_axis_tvalid & m_axis_tready),
        .dout          (fifo_dout),
        .empty         (fifo_empty),
        .underflow     (),
        .rd_rst_busy   (),
        .sleep         (1'b0),
        .rst           (~m_axis_aresetn),
        .injectsbiterr (1'b0),
        .injectdbiterr (1'b0),
        .sbiterr       (),
        .dbiterr       ()
    );

    assign m_axis_tvalid = ~fifo_empty;
    assign m_axis_tdata  = fifo_dout;
    assign m_axis_tkeep  = 16'hFFFF;
    assign m_axis_tlast  = 1'b1;

    xpm_cdc_single #(.DEST_SYNC_FF(3), .INIT_SYNC_FF(0), .SIM_ASSERT_CHK(0), .SRC_INPUT_REG(0))
    u_err_sync (.src_clk(1'b0), .src_in(adc_error), .dest_clk(m_axis_aclk), .dest_out(adc_error_out));

endmodule
