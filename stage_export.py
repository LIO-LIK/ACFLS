# stage_export.py
# Export a gate-level netlist (after bit-blasting) into BLIF format.

from netlist import Module

def run(mod: Module, out_path: str):
    """
    Export the given Module (gate-level) to a BLIF file.
    Called by main.py as: stage_export.run(my_module, args.output)
    """

    # 1) Collect primary inputs / outputs
    inputs = []
    outputs = []

    for s in mod.signals.values():
        if s.name in ("CONST0", "CONST1"):
            continue
        # Only export 1-bit primary ports in BLIF (buses are already flattened by bitblast)
        if s.width != 1:
            continue
        if s.is_input:
            inputs.append(s.name)
        if s.is_output:
            outputs.append(s.name)

    inputs = sorted(set(inputs))
    outputs = sorted(set(outputs))

    # 2) Write BLIF
    with open(out_path, "w") as f:
        def w(line: str = ""):
            f.write(line + "\n")

        w(f".model {mod.name}")
        if inputs:  w(".inputs " + " ".join(inputs))
        if outputs: w(".outputs " + " ".join(outputs))
        w("")

        # 3) Emit constant drivers
        if mod.get_signal("CONST0"):
            w(".names CONST0")
            w("")  # Logic 0

        if mod.get_signal("CONST1"):
            w(".names CONST1")
            w("1") # Logic 1
        w("")

        # 4) Emit gates
        for g in mod.gates:
            op = g.op_type
            ins = [s.name for s in g.inputs]
            out = g.output.name

            if op == "NOT":
                w(f".names {ins[0]} {out}")
                w("0 1")

            elif op == "BUF":
                w(f".names {ins[0]} {out}")
                w("1 1")

            elif op == "AND":
                w(f".names {ins[0]} {ins[1]} {out}")
                w("11 1")

            elif op == "OR":
                w(f".names {ins[0]} {ins[1]} {out}")
                w("1- 1")
                w("-1 1")

            elif op == "XOR":
                w(f".names {ins[0]} {ins[1]} {out}")
                w("10 1")
                w("01 1")

            elif op == "MUX":
                # Convention: inputs = [sel, true_in, false_in]
                sel, t_in, f_in = ins
                w(f".names {sel} {t_in} {f_in} {out}")
                w("11- 1") # Sel=1, True=1 -> 1
                w("0-1 1") # Sel=0, False=1 -> 1

            elif op == "DFF":
                # Convention: inputs = [d, clk]
                d, clk = ins
                w(f".latch {d} {out} re {clk} 0")

            else:
                raise NotImplementedError(f"Export: unsupported gate type '{op}'")

        # 5) Emit Instances (Sub-modules / Black Boxes)
        if hasattr(mod, 'instances') and mod.instances:
            w("")
            w("# Sub-circuits (Instances)")
            for inst in mod.instances:
                # Format: .subckt <model_name> <port>=<wire> <port>=<wire> ...
                line = f".subckt {inst.module_type}"
                
                for port_name, sig_name in inst.port_connections.items():
                    if sig_name is None:
                        continue # Unconnected port
                        
                    sig = mod.get_signal(sig_name)
                    if sig is None:
                        continue
                        
                    # Unroll buses into 1-bit port mappings
                    if sig.width == 1:
                        line += f" {port_name}={sig.name}"
                    else:
                        for i in range(sig.width):
                            line += f" {port_name}_{i}={sig.name}_{i}"
                
                w(line)

        w(".end")
    
    print(f"Exported BLIF to {out_path}")