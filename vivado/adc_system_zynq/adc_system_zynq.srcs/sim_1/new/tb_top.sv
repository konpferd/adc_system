`timescale 1ns / 1ps

`define TEST_COUNT 600
`define FSYNC_CLOCK_CYCLES 32
`define BATCH_SIZE 15'd8

module tb_top ();

    // System reset
    logic adc_resetn_sim;

    // ADC signals
    logic adc_dclk_sim;
    logic adc_fsync_sim;
    logic [3:0] adc_data_sim;
    logic adc_error_sim;
    logic adc_start_sim;
    logic adc_clkin_sim;

    // System signals
    logic m_axis_aclk_sim;
    logic m_axis_aresetn_sim;
    logic [127:0] m_axis_tdata_sim;
    logic [15:0] m_axis_tkeep_sim;
    logic m_axis_tvalid_sim;
    logic m_axis_tready_sim;
    logic m_axis_tlast_sim;

    // connection signals
    logic [127:0] axis_tdata_conn_sim;
    logic [15:0] axis_tkeep_conn_sim;
    logic axis_tvalid_conn_sim;
    logic axis_tready_conn_sim;
    logic axis_tlast_conn_sim;

    logic [14:0] adc_batch_size_sim;

    // Error out
    logic adc_error_out_sim;

    // Debug pin
    logic [31:0] drop_count_sim;

    // Sim signals
    logic [23:0] data0_sim;
    logic [23:0] data1_sim;
    logic [23:0] data2_sim;
    logic [23:0] data3_sim;

    logic [15:0] pack_counter_verif;

    logic control_flow_sim;

    // Configure constant signals
    assign adc_error_sim = 1'b0;    // error signal not simulated

    assign adc_batch_size_sim = `BATCH_SIZE;

    initial begin : starting_condition
        adc_dclk_sim = 0;
        adc_fsync_sim = 0;
        adc_data_sim = 0;
        m_axis_aclk_sim = 0;
        m_axis_tready_sim = 0;
        pack_counter_verif = 0;
        adc_start_sim = 1;
        control_flow_sim = 1;

        adc_resetn_sim = 0;
        m_axis_aresetn_sim = 0;
        #50;
        adc_resetn_sim = 1;
        m_axis_aresetn_sim = 1;

        m_axis_tready_sim = 1;
    end

    always begin : adc_clock_driver
        #35 adc_dclk_sim = ~adc_dclk_sim;
    end

    always begin : system_clock_driver
        #10 m_axis_aclk_sim = ~m_axis_aclk_sim;
    end

    initial begin : driver_and_monitor
        automatic int tests_count = 0;
        automatic int pass_count = 0;
        automatic int pass_batch_count = 0;
        automatic int err_batch_count = 0;
        automatic logic [31:0] packet_counter = 0;
        logic [127:0] exp_queue [$];

        $srandom(2137);
        repeat(10) @(posedge adc_dclk_sim);

        fork
            repeat(`TEST_COUNT) begin
                automatic int cycle_counter;
                tests_count ++;

                std::randomize(data0_sim);
                std::randomize(data1_sim);
                std::randomize(data2_sim);
                std::randomize(data3_sim);

                exp_queue.push_back({packet_counter, data0_sim, data1_sim, data2_sim, data3_sim});
                packet_counter = packet_counter + 31'd1;

                if (packet_counter == `TEST_COUNT) begin
                    fork
                        #23 adc_start_sim <= 0;
                    join_none;
                end

                cycle_counter = 0;
                repeat(`FSYNC_CLOCK_CYCLES) begin
                    @(negedge adc_dclk_sim);

                    if (cycle_counter < `FSYNC_CLOCK_CYCLES/2) begin
                        adc_fsync_sim <= 1;
                    end else begin
                        adc_fsync_sim <= 0;
                    end

                    if (cycle_counter < 24) begin
                        data0_sim <= data0_sim << 1;
                        data1_sim <= data1_sim << 1;
                        data2_sim <= data2_sim << 1;
                        data3_sim <= data3_sim << 1;

                        adc_data_sim[0] <= data0_sim[23];
                        adc_data_sim[1] <= data1_sim[23];
                        adc_data_sim[2] <= data2_sim[23];
                        adc_data_sim[3] <= data3_sim[23];
                    end else begin
                        adc_data_sim[0] <= 0;
                        adc_data_sim[1] <= 0;
                        adc_data_sim[2] <= 0;
                        adc_data_sim[3] <= 0;
                    end

                    cycle_counter++;
                end
            end
            while (1) begin
                logic [127:0] temp;

                do begin
                    @(posedge m_axis_aclk_sim);
                end while (!(m_axis_tvalid_sim && m_axis_tready_sim));

                temp = exp_queue.pop_front();

                if (temp == m_axis_tdata_sim) begin
                    $display("[%t] Correct output: 0x%032h", $time, m_axis_tdata_sim);
                    pass_count++;
                end else begin
                    $error("[%t] Incorrrect Z output: \nEXP: 0x%032h\nREC: 0x%032h\n", $time, temp, m_axis_tdata_sim);
                end

                if (m_axis_tlast_sim) begin
                    if (pack_counter_verif == `BATCH_SIZE - 1) begin
                        $display("[%t] Correct batch size: %d", $time, pack_counter_verif + 1);
                        pass_batch_count++;
                    end else begin
                        $error("[%t] Incorrect batch size: %d", $time, pack_counter_verif + 1);
                        err_batch_count++;
                    end

                    pack_counter_verif <= 0;
                end else begin
                    pack_counter_verif <= pack_counter_verif + 16'b1;
                end
            end
        join_any

        adc_fsync_sim <= 1;
        repeat(100) @(posedge adc_dclk_sim);
        $display("[%t] Simulation complete", $time);
        $display("====== Test Summery ======");
        $display("Tests:    %5d", tests_count);
        $display("PASSED:   %5d", pass_count);
        $display("FAILED:   %5d", tests_count - pass_count);
        $display("==== Batch Structure  ====");
        $display("PASSED:  %5d", pass_batch_count);
        $display("FAILED:  %5d", err_batch_count);
        $display("==========================");
        if (pass_count == tests_count && err_batch_count == 0) begin
            $display("------ TEST PASSED ------");
        end else begin
            $display("------ TEST FAILED ------");
        end
        $display("==========================");
        $finish;
    end

    adc_controller DUT_controller(
        // ---------------- ADC data port  --------
        .adc_dclk(adc_dclk_sim),
        .adc_dout(adc_data_sim),
        .adc_fsync(adc_fsync_sim),
        .adc_error(adc_error_sim),
        .adc_start(adc_start_sim),
        .adc_clkin(adc_clkin_sim),

        // ---------------- PL side ADC reset ------
        .adc_resetn(adc_resetn_sim),

        // ---------------- AXI4-Stream master -----
        .m_axis_aclk(m_axis_aclk_sim),
        .m_axis_aresetn(m_axis_aresetn_sim),

        .m_axis_tdata(axis_tdata_conn_sim),
        .m_axis_tkeep(axis_tkeep_conn_sim),
        .m_axis_tvalid(axis_tvalid_conn_sim),
        .m_axis_tready(axis_tready_conn_sim),
        .m_axis_tlast(axis_tlast_conn_sim),

        // ---------------- error status -> AXI GPIO -------
        .adc_error_out(adc_error_out_sim),

        // ---------------- debug -----------------------------
        .word_drop_count(drop_count_sim)
    );

    adc_stream_manager DUT_p_manager(
        .axis_aclk(m_axis_aclk_sim),
        .axis_aresetn(m_axis_aresetn_sim),

        .ctrl_in({control_flow_sim, adc_batch_size_sim}),

        .s_axis_tdata(axis_tdata_conn_sim),
        .s_axis_tkeep(axis_tkeep_conn_sim),
        .s_axis_tvalid(axis_tvalid_conn_sim),
        .s_axis_tready(axis_tready_conn_sim),
        .s_axis_tlast(axis_tlast_conn_sim),

        .m_axis_tdata(m_axis_tdata_sim),
        .m_axis_tkeep(m_axis_tkeep_sim),
        .m_axis_tvalid(m_axis_tvalid_sim),
        .m_axis_tready(m_axis_tready_sim),
        .m_axis_tlast(m_axis_tlast_sim)
    );

endmodule
