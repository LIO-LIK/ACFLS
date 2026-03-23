# ACFLS
Logic Synthesizer Project for ACF.

Final segmentation of the project:

1. main.py: Reads the input file and manages calls to the other pipeline stages.

2. Parsing `stage_parser.py`: Inputs the `.v` source file and wraps PyVerilog to generate an Abstract Syntax Tree (AST).

3. High-Level Elaboration `stage_elaboration.py`: Inputs the AST and infers hardware macros. 
   - Combinational logic (replaces `if/else`, `case`, and `? :` with MUX structures).
   - Sequential logic (identifies `always @(posedge)` blocks for D-Flip-Flops).
   - Unroll arrays into registers and decoders.
   - Resolve parameters (like `WIDTH=32`).
   - Preserve hierarchy by identifying Sub-Module Instantiations as black boxes.
   - Output a high-level netlist.

4. Bit-Blasting `stage_bitblast.py`: Inputs the high-level netlist and flattens it into structural hardware.
   - Expand multi-bit buses into 1-bit wire objects.
   - Replace high-level operators with 1-bit gate equivalents (like `+` to Ripple-Carry Adders, `==` to XNOR-trees, `<<` to Barrel Shifters etc).
   - Outputs a flattened list of 1-bit primitives.

5. Export `stage_export.py`: Inputs the gate-level netlist and translates it into the target output format (BLIF), (including `.subckt` mappings for instantiated modules.)

6. IR Database `netlist.py`: Defines the Python classes necessary for the internal representation (`Module`, `Signal`, `Gate`, `Instance`).

# RISC-V CORE CODE FILES HANDLED:
- `ALU.v` :        O (math, 2's complement sub, barrel shifters)
- `ALUControl.v` : O
- `Control.v`:     O
- `Mem_Model.v`:   XXX (Outside scope)
- `REG_FILE.v`:    O (array unrolling, decoders)
- `RISCV_CLKRST.v`:XXX (testbench file)
- `RISCV_TOP.v`:   O (hierarchical module instantiation)
(Additionally Support bit-slicing, concatenation)

# ATTENTION: 
Make sure to install PyVerilog with `pip install pyverilog` before running.
If you encounter `WinError2`: install `iverilog` and during installation check the "Add to PATH" box.

# LIMITATIONS: 
- Our project does NOT handle multiple file inputs at once. Each Verilog file MUST be synthesized individually to ensure correctness.
- We do not check for logic errors or missing dependencies; we assume the Verilog code is functionally correct. The goal is logic synthesis, not creating a functioning Linter (though the parser may catch syntax errors).
- Massive memory instantiations (such as in `Mem_Model.v`) are unsupported. Unrolling large arrays into gates would require a much more optimized (maybe in C or C++ or something) approach to not melt my Laptop or take until the heat death of the universe to compute. Actual tools like yosys map these to BRAM.
- There is no conscious attempt made at optimization (minimizing gate counts).

# TO RUN: 
`python main.py MyVerilogFile.v`

# Credit: RISC-V CPU code adapted from: https://github.com/hushon/Tiny-RISCV-CPU/tree/master