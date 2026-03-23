# ACFLS
Logic Synthesizer Project for ACF.

Final segmentation of the porject:

1. Main file, reads input file and orchestrates calls for other modules. (main.py)

2. Parsing, Inputs .v source file, Wraps PyVerilog to generate AST (stage_parser.py)

3. High level elaboration, input AST, flattens hierarchy (resolves includes and module initiations), Sequential inference(identifies "always @(posedge) blocks and marks signals as D-flip-flops), resolves parameters (like WIDTH=32), outputs high-level netlist (stage_elaboration.py)

4. Bit blasting, input high-level netlist, expands signals into individual wire objects, replaces operators with gates (+,-,== to adders, XORs etc.), replaces if/else with MUX structures, outputs list of primitives (stage_bitblast.py)

5. Export: input list of primitives / gate level netlist, translates into target output format(BLIF).(stage_export.py)

6. Define Python classes necessary for the netlist(netlist.py)

RISC-V CORE CODE FILES HANDLED:
ALUControl.v :  O
Control.v:      O
Mem_Model.v:    XXX (Scope)
REG_FILE.v:     O
RISCV_CLKRST.v: XXX (testbench)
RISCV_TOP.v:    XXX (Scope)

ATTENTION: MAKE SURE TO INSTALL PYVERILOG WITH "pip install pyverilog" BEFORE RUNNING
If you encounter WinError2: install iverilog and during installation check "Add to PATH" box.

LIMITATIONS: Our project does NOT handle multiple file inputs, each verilog file MUST be synthesized individually to ensure correctness.
We do not check for dependencies or logic errors, we assume the verilog code to be correct. The goal is not to create a functioning Linter.(The parser may catch syntax errors.)

Memory instantiations (such as in Mem_Model.v) are unsupported and would require an optimized approach to not melt my Computer, therefore it is not implemented.

There is no conscious attempt made at optimization.

Credit: RISC-V CPU code: https://github.com/hushon/Tiny-RISCV-CPU/tree/master