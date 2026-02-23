# stage_elaboration.py
# High-level elaboration: build a high-level netlist (Module/Signal/Gate)
# from the PyVerilog AST.

import os
import re
from netlist import Module, Signal, Gate

from pyverilog.vparser.ast import (
    Source, Description, ModuleDef,
    Ioport, Input, Output, Wire, Reg,
    Width, IntConst, Identifier,
    Always, SensList, Sens, Block,
    IfStatement, NonblockingSubstitution, BlockingSubstitution,
    CaseStatement, Case,
    Plus, Minus, Eq, Land, Lor,
    Unot, NotEq, Repeat, Assign, Decl, Parameter, Pointer
)

# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------

def _parse_const_value(val_str):
    if "'" in val_str:
        width_part, val_part = val_str.split("'")
        base = val_part[0].lower()
        number = val_part[1:]
        number = number.replace('x', '0').replace('z', '0')
        
        try:
            if base == 'b': return int(number, 2)
            elif base == 'h': return int(number, 16)
            elif base == 'd': return int(number)
        except ValueError:
            return 0
    return int(val_str)

def _resolve_param(node, param_env):
    """Evaluates AST nodes to integers using the parameter environment."""
    if isinstance(node, IntConst):
        return _parse_const_value(node.value)
    if isinstance(node, Identifier) and node.name in param_env:
        return param_env[node.name]
    if isinstance(node, Minus):
        return _resolve_param(node.left, param_env) - _resolve_param(node.right, param_env)
    return 0

def _parse_width(node, param_env):
    if node is None: return 1
    if isinstance(node, Width):
        msb = _resolve_param(node.msb, param_env)
        lsb = _resolve_param(node.lsb, param_env)
        return abs(msb - lsb) + 1
    return 1

def _get_or_create_signal(mod: Module, name: str, width=1, **attrs):
    s = mod.get_signal(name)
    if s is None:
        s = Signal(name=name, width=width, **attrs)
        mod.add_signal(s)
    else:
        if s.width == 1 and width != 1: s.width = width
        s.is_input = s.is_input or attrs.get("is_input", False)
        s.is_output = s.is_output or attrs.get("is_output", False)
        s.is_reg = s.is_reg or attrs.get("is_reg", False)
    return s

def _intconst_decl_width(value: str):
    m = re.match(r"^\s*(\d+)\s*'[bBdDhHoO].*$", value)
    return int(m.group(1)) if m else None

# -------------------------------------------------------------------------
# Expression Parsing
# -------------------------------------------------------------------------

def _expr_to_signal_and_gates(mod: Module, expr, expected_width=None, param_env=None):
    if param_env is None: param_env = {}
    extra_gates = []

    # 1. Identifiers & Parameters
    if isinstance(expr, Identifier):
        if expr.name in param_env:
            # It's a parameter constant (e.g. DWIDTH)
            val = param_env[expr.name]
            w = expected_width or 32
            s = _get_or_create_signal(mod, f"CONST_{val}_{w}b", width=w)
            return s, extra_gates
        else:
            # It's a normal wire/reg
            s = _get_or_create_signal(mod, expr.name)
            if expected_width is not None and s.width == 1 and expected_width != 1:
                s.width = expected_width
            return s, extra_gates

    # 2. Hardcoded Constants
    if isinstance(expr, IntConst):
        val = _parse_const_value(expr.value)
        declared_w = _intconst_decl_width(expr.value)
        w = declared_w or expected_width or 32
        const_name = f"CONST_{val}_{w}b_{id(expr)}"
        s = _get_or_create_signal(mod, const_name, width=w)
        return s, extra_gates

    # 3. Unary Operators
    if isinstance(expr, Unot):
        val_sig, g = _expr_to_signal_and_gates(mod, expr.right, expected_width, param_env)
        out_sig = _get_or_create_signal(mod, f"tmp_not_{len(mod.gates)}", width=val_sig.width)
        extra_gates.extend(g)
        extra_gates.append(Gate("NOT", [val_sig], out_sig))
        return out_sig, extra_gates

    # 4. Replication Operator
    if isinstance(expr, Repeat):
        times = _resolve_param(expr.times, param_env)
        # For this MVP, we assume replication is just generating 0s
        s = _get_or_create_signal(mod, f"CONST_0_{times}b_rep", width=times)
        return s, extra_gates

    # 5. Not Equal Operator
    if isinstance(expr, NotEq):
        a_sig, g_a = _expr_to_signal_and_gates(mod, expr.left, None, param_env)
        b_sig, g_b = _expr_to_signal_and_gates(mod, expr.right, None, param_env)
        eq_out = _get_or_create_signal(mod, f"tmp_eq_{len(mod.gates)}", width=1)
        neq_out = _get_or_create_signal(mod, f"tmp_neq_{len(mod.gates)}", width=1)
        
        extra_gates.extend(g_a + g_b)
        extra_gates.append(Gate("EQ", [a_sig, b_sig], eq_out))
        extra_gates.append(Gate("NOT", [eq_out], neq_out))
        return neq_out, extra_gates

    # 6. Binary Operators
    op_map = {Plus: "ADD", Eq: "EQ", Land: "AND", Lor: "OR"}
    expr_type = type(expr)
    
    if expr_type in op_map:
        op_name = op_map[expr_type]
        req_w = expected_width if op_name == "ADD" else None
        
        a_sig, a_g = _expr_to_signal_and_gates(mod, expr.left, req_w, param_env)
        b_sig, b_g = _expr_to_signal_and_gates(mod, expr.right, req_w, param_env)
        extra_gates.extend(a_g)
        extra_gates.extend(b_g)

        out_w = 1 if op_name in ["EQ", "AND", "OR"] else (expected_width or max(a_sig.width, b_sig.width))
        tmp = _get_or_create_signal(mod, f"tmp_{op_name}_{len(mod.gates)}", width=out_w)

        extra_gates.append(Gate(op_name, [a_sig, b_sig], tmp))
        return tmp, extra_gates

    # STEP 2: Array Read (Pointer) -> Build a 32-to-1 MUX Tree
    if isinstance(expr, Pointer):
        array_name = expr.var.name
        # 1. Evaluate the index expression (e.g., RA1)
        idx_sig, g_idx = _expr_to_signal_and_gates(mod, expr.ptr, None, param_env)
        extra_gates.extend(g_idx)
        
        # We assume a standard 32-depth RISC-V register file for this MVP
        depth = param_env.get("MDEPTH", 32)
        width = param_env.get("DWIDTH", 32)
        
        # 2. Start with Register 0 as the default
        current_mux_out = _get_or_create_signal(mod, f"{array_name}_0", width=width)
        
        # 3. Build a chain of MUXes for registers 1 through 31
        for i in range(1, depth):
            reg_sig = _get_or_create_signal(mod, f"{array_name}_{i}", width=width)
            
            # Create a constant for the current index 'i'
            i_const_sig = _get_or_create_signal(mod, f"CONST_{i}_{idx_sig.width}b", width=idx_sig.width)
            
            # Check if index == i
            eq_sig = _get_or_create_signal(mod, f"tmp_ptr_eq_{array_name}_{i}_{len(mod.gates)}", width=1)
            extra_gates.append(Gate("EQ", [idx_sig, i_const_sig], eq_sig))
            
            # MUX: If index == i, select reg_sig, else select previous MUX output
            next_mux_out = _get_or_create_signal(mod, f"tmp_ptr_mux_{array_name}_{i}_{len(mod.gates)}", width=width)
            extra_gates.append(Gate("MUX", [eq_sig, reg_sig, current_mux_out], next_mux_out))
            
            current_mux_out = next_mux_out

        return current_mux_out, extra_gates

    raise ValueError(f"Expression not supported yet: {type(expr).__name__}")

# -------------------------------------------------------------------------
# Advanced Combinational Logic Extraction (SSA Style)
# -------------------------------------------------------------------------

def _extract_comb_logic(mod, stmt, target_name, current_sig, param_env):
    gates = []

    if isinstance(stmt, Block):
        for s in stmt.statements:
            current_sig, g = _extract_comb_logic(mod, s, target_name, current_sig, param_env)
            gates.extend(g)
        return current_sig, gates

    elif isinstance(stmt, (BlockingSubstitution, NonblockingSubstitution)):
        # SCAFFOLDING FOR STEP 2: Array Writes
        if isinstance(stmt.left.var, Pointer):
            if stmt.left.var.var.name == target_name:
                print(f"  [Scaffolding] Found Pointer Write to: {target_name}. Decoder generation needed.")
            return current_sig, gates

        if stmt.left.var.name == target_name:
            target_sig = mod.get_signal(target_name)
            rhs_sig, g = _expr_to_signal_and_gates(mod, stmt.right.var, expected_width=target_sig.width, param_env=param_env)
            gates.extend(g)
            return rhs_sig, gates
        return current_sig, gates

    elif isinstance(stmt, IfStatement):
        cond_sig, cond_g = _expr_to_signal_and_gates(mod, stmt.cond, param_env=param_env)
        gates.extend(cond_g)

        true_sig, true_g = _extract_comb_logic(mod, stmt.true_statement, target_name, current_sig, param_env)
        gates.extend(true_g)

        if stmt.false_statement:
            false_sig, false_g = _extract_comb_logic(mod, stmt.false_statement, target_name, current_sig, param_env)
            gates.extend(false_g)
        else:
            false_sig = current_sig 

        if true_sig == false_sig:
            return true_sig, gates 

        mux_out = _get_or_create_signal(mod, f"mux_{target_name}_{len(mod.gates)}", width=true_sig.width)
        gates.append(Gate("MUX", [cond_sig, true_sig, false_sig], mux_out))
        return mux_out, gates

    return current_sig, gates

# -------------------------------------------------------------------------
# Main Run Loop
# -------------------------------------------------------------------------

def run(ast):
    if not isinstance(ast, Source): raise TypeError("Expected PyVerilog Source node.")
    desc = ast.description
    top = next((d for d in desc.definitions if isinstance(d, ModuleDef)), None)
    if not top: raise ValueError("No ModuleDef found.")

    mod = Module(top.name)

    # 1. Parse Parameters
    param_env = {}
    if top.paramlist:
        for p in top.paramlist.params:
            if isinstance(p, Decl) and isinstance(p.list[0], Parameter):
                param = p.list[0]
                param_env[param.name] = _resolve_param(param.value.var, param_env)
    
    print(f"  > Loaded Parameters: {param_env}")

    # 2. Parse Ports
    for p in top.portlist.ports:
        if isinstance(p, Ioport):
            first, second = p.first, p.second
            width = _parse_width(first.width, param_env)
            if isinstance(first, Input):
                _get_or_create_signal(mod, first.name, width=width, is_input=True)
            elif isinstance(first, Output):
                is_reg = isinstance(second, Reg)
                _get_or_create_signal(mod, first.name, width=width, is_output=True, is_reg=is_reg)

    # 3. Parse Items (Arrays, Assigns, Always)
    for item in top.items:
        # A. Array Declarations (Scaffolding)
        if isinstance(item, Decl):
            for decl in item.list:
                if isinstance(decl, Reg) and getattr(decl, 'dimensions', None):
                    w = _parse_width(decl.width, param_env)
                    print(f"  [Scaffolding] Found Array '{decl.name}' with width {w}. Array unrolling needed.")

        # B. Continuous Assignments (assign RD1 = RF[RA1])
        elif isinstance(item, Assign):
            target_name = item.left.var.name
            target_sig = mod.get_signal(target_name)
            
            # Generate the logic (which triggers our MUX builder above)
            rhs_sig, g = _expr_to_signal_and_gates(mod, item.right.var, target_sig.width, param_env)
            for gate in g: mod.add_gate(gate)
            
            # Wire the MUX output to the target port (RD1/RD2)
            mod.add_gate(Gate("BUF", [rhs_sig], target_sig))

        # C. Always Blocks
        elif isinstance(item, Always):
            is_clocked = False
            if isinstance(item.sens_list, SensList):
                for s in item.sens_list.list:
                    if s.type == "posedge": is_clocked = True
            
            if is_clocked:
                # SEQUENTIAL
                clk_name = item.sens_list.list[0].sig.name
                clk_sig = _get_or_create_signal(mod, clk_name, is_input=True)
                body = item.statement
                if isinstance(body, Block): body = body.statements[0]
                
                if isinstance(body, IfStatement):
                    # We pass param_env down
                    rst_sig, g = _expr_to_signal_and_gates(mod, body.cond, param_env=param_env)
                    for gate in g: mod.add_gate(gate)
                    
                    # (For the MVP array scaffold, we will just print warnings if it hits an array)
                    print("  [Scaffolding] Processing sequential block...")

            else:
                # COMBINATIONAL
                target_candidates = [s.name for s in mod.signals.values() if s.is_output or s.is_reg]
                for target_name in target_candidates:
                    initial_sig = mod.get_signal(target_name)
                    final_sig, gates = _extract_comb_logic(mod, item.statement, target_name, initial_sig, param_env)
                    if final_sig != initial_sig:
                        for g in gates: mod.add_gate(g)
                        mod.add_gate(Gate("BUF", [final_sig], mod.get_signal(target_name)))

    mod.save_json("debug_02_elab.json")
    return mod