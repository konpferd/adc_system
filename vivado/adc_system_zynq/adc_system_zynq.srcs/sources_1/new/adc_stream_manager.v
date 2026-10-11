`timescale 1ns / 1ps

module adc_stream_manager (
    input  wire         axis_aclk,
    input  wire         axis_aresetn,

    // ctrl_in[15] = stream enable
    // ctrl_in[14:0] = packet length in beats
    input  wire [15:0]  ctrl_in,

    // Input stream
    input  wire [127:0] s_axis_tdata,
    input  wire [15:0]  s_axis_tkeep,
    input  wire         s_axis_tvalid,
    output wire         s_axis_tready,
    input  wire         s_axis_tlast,

    // output stream
    output reg [127:0]  m_axis_tdata,
    output reg [15:0]   m_axis_tkeep,
    output reg          m_axis_tvalid,
    input  wire         m_axis_tready,
    output reg          m_axis_tlast
);

    wire        enable = ctrl_in[15];
    wire [14:0] packet_width = ctrl_in[14:0];

    reg [14:0] packet_counter;

    assign s_axis_tready = 1'b1;

    always @(posedge axis_aclk) begin

        if (!axis_aresetn) begin
            m_axis_tdata  <= 128'd0;
            m_axis_tkeep  <= 16'd0;
            m_axis_tvalid <= 1'b0;
            m_axis_tlast  <= 1'b0;

            packet_counter <= 15'd0;
        end else begin
            if (!enable) begin
                m_axis_tdata  <= 128'd0;
                m_axis_tkeep  <= 16'd0;
                m_axis_tvalid <= 1'b0;
                m_axis_tlast  <= 1'b0;

                packet_counter <= 15'd0;
            end else begin
                if (!m_axis_tvalid || m_axis_tready) begin
                    if (s_axis_tvalid) begin
                        m_axis_tdata  <= s_axis_tdata;
                        m_axis_tkeep  <= s_axis_tkeep;
                        m_axis_tvalid <= 1'b1;

                        if (packet_counter >= packet_width - 1) begin
                            m_axis_tlast <= 1'b1;
                            packet_counter <= 15'd0;
                        end else begin
                            m_axis_tlast <= 1'b0;
                            packet_counter <= packet_counter + 1'b1;
                        end
                    end else begin
                        m_axis_tvalid <= 1'b0;
                        m_axis_tlast  <= 1'b0;
                    end
                end
            end
        end
    end

endmodule
