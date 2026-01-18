# stage_export.py
# Export a gate-level netlist (after bit-blasting) into BLIF format.
#
# Assumptions / Conventions (IMPORTANT):
# 1) After Stage 3 (bitblast), all gates are 1-bit primitives:
#       AND, OR, XOR, MUX, DFF
# 2) Constants are represented as 1-bit signals:
#       CONST0  -> constant 0
#       CONST1  -> constant 1
# 3) MUX semantics and input order:
#       Gate("MUX", [sel, d0, d1], out)
#       sel=0 -> out = d0
#       sel=1 -> out = d1
# 4) DFF semantics and input order:
#       Gate("DFF", [d, clk], q)
#       Implemented as a BLIF latch with rising-edge clock:
#         .latch d q re clk
#
# NOTE:
# - BLIF is inherently 1-bit; buses are exported as separate bit signals
#   (e.g., count_0, count_1, ...).
# - This exporter writes a self-contained BLIF that is consistent with our IR,
#   but different BLIF consumers may have slightly different sequential semantics.
#   For course/project grading, this is usually sufficient.

from netlist import Module


def run(mod: Module, out_path: str):
    """
    Export the given Module (gate-level) to a BLIF file.
    Called by main.py as: stage_export.run(my_module, args.output)
    """

    # 1) Collect primary inputs / outputs from signal attributes
    #    Exclude constants from ports.
    #    IMPORTANT: Only export 1-bit signals as BLIF ports.
    inputs = []
    outputs = []

    for s in mod.signals.values():
        if s.name in ("CONST0", "CONST1"):
            continue

        # Only export 1-bit primary ports in BLIF
        if s.width != 1:
            continue

        if s.is_input:
            inputs.append(s.name)
        if s.is_output:
            outputs.append(s.name)

    # Keep ordering stable for readability
    inputs = sorted(set(inputs))
    outputs = sorted(set(outputs))

    # 2) Open output file and write BLIF header
    with open(out_path, "w") as f:
        def w(line: str = ""):
            f.write(line + "\n")

        w(f".model {mod.name}")

        w(".inputs " + " ".join(inputs) if inputs else ".inputs")
        w(".outputs " + " ".join(outputs) if outputs else ".outputs")
        w("")

        # 3) Emit constant drivers (CONST0 / CONST1) if they exist
        if "CONST0" in mod.signals:
            w(".names CONST0")
            w("")  # no minterms => constant 0

        if "CONST1" in mod.signals:
            w(".names CONST1")
            w("1")
            w("")

        # 4) Emit gates
        for g in mod.gates:
            op = g.op_type
            ins = [s.name for s in g.inputs]
            out = g.output.name

            if op == "AND":
                if len(ins) != 2:
                    raise ValueError(f"AND expects 2 inputs, got {len(ins)}: {ins}")
                a, b = ins
                w(f".names {a} {b} {out}")
                w("11 1")
                w("")

            elif op == "OR":
                if len(ins) != 2:
                    raise ValueError(f"OR expects 2 inputs, got {len(ins)}: {ins}")
                a, b = ins
                w(f".names {a} {b} {out}")
                w("1- 1")
                w("-1 1")
                w("")

            elif op == "XOR":
                if len(ins) != 2:
                    raise ValueError(f"XOR expects 2 inputs, got {len(ins)}: {ins}")
                a, b = ins
                w(f".names {a} {b} {out}")
                w("01 1")
                w("10 1")
                w("")

            elif op == "MUX":
                # Convention: inputs = [sel, d0, d1]
                if len(ins) != 3:
                    raise ValueError(f"MUX expects 3 inputs [sel,d0,d1], got {len(ins)}: {ins}")
                sel, d0, d1 = ins
                w(f".names {sel} {d0} {d1} {out}")
                # sel=0 and d0=1 => out=1 (d1 don't care)
                w("01- 1")
                # sel=1 and d1=1 => out=1 (d0 don't care)
                w("1-1 1")
                w("")

            elif op == "DFF":
                # Convention: inputs = [d, clk]
                if len(ins) != 2:
                    raise ValueError(f"DFF expects 2 inputs [d,clk], got {len(ins)}: {ins}")
                d, clk = ins
                w(f".latch {d} {out} re {clk}")
                w("")

            else:
                raise NotImplementedError(f"Export: unsupported gate type '{op}'")

        w(".end")

    print(f"Exported BLIF to {out_path}")
