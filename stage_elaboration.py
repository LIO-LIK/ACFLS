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
    Plus, Eq, Land, Lor
)

# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------

def _parse_const_value(val_str):
    if "'" in val_str:
        width_part, val_part = val_str.split("'")
        base = val_part[0].lower()
        number = val_part[1:]
        # Handle "don't cares" (x/z) by treating them as 0 for synthesis
        number = number.replace('x', '0').replace('z', '0')
        
        try:
            if base == 'b': return int(number, 2)
            elif base == 'h': return int(number, 16)
            elif base == 'd': return int(number)
        except ValueError:
            return 0
    return int(val_str)

def _parse_width(node):
    if node is None: return 1
    if isinstance(node, Width):
        msb = int(node.msb.value, 0)
        lsb = int(node.lsb.value, 0)
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

def _expr_to_signal_and_gates(mod: Module, expr, expected_width=None):
    extra_gates = []

    if isinstance(expr, Identifier):
        s = _get_or_create_signal(mod, expr.name)
        if expected_width is not None and s.width == 1 and expected_width != 1:
            s.width = expected_width
        return s, extra_gates

    if isinstance(expr, IntConst):
        val = _parse_const_value(expr.value)
        declared_w = _intconst_decl_width(expr.value)
        w = declared_w or expected_width or 32
        const_name = f"CONST_{val}_{w}b_{id(expr)}"
        s = _get_or_create_signal(mod, const_name, width=w)
        return s, extra_gates

    op_map = {Plus: "ADD", Eq: "EQ", Land: "AND", Lor: "OR"}
    expr_type = type(expr)
    
    if expr_type in op_map:
        op_name = op_map[expr_type]
        req_w = expected_width if op_name == "ADD" else None
        
        a_sig, a_g = _expr_to_signal_and_gates(mod, expr.left, expected_width=req_w)
        b_sig, b_g = _expr_to_signal_and_gates(mod, expr.right, expected_width=req_w)
        extra_gates.extend(a_g)
        extra_gates.extend(b_g)

        out_w = 1 if op_name in ["EQ", "AND", "OR"] else (expected_width or max(a_sig.width, b_sig.width))
        tmp = _get_or_create_signal(mod, f"tmp_{op_name}_{len(mod.gates)}", width=out_w)

        extra_gates.append(Gate(op_name, [a_sig, b_sig], tmp))
        return tmp, extra_gates

    raise NotImplementedError(f"Expression not supported yet: {type(expr).__name__}")

# -------------------------------------------------------------------------
# Advanced Combinational Logic Extraction (SSA Style)
# -------------------------------------------------------------------------

def _extract_comb_logic(mod, stmt, target_name, current_sig):
    """
    Evaluates what a statement does to a specific target_name.
    Passes 'current_sig' down the tree so If/Else branches know their starting state.
    """
    gates = []

    # 1. Block (begin...end)
    if isinstance(stmt, Block):
        for s in stmt.statements:
            current_sig, g = _extract_comb_logic(mod, s, target_name, current_sig)
            gates.extend(g)
        return current_sig, gates

    # 2. Assignment
    elif isinstance(stmt, (BlockingSubstitution, NonblockingSubstitution)):
        if stmt.left.var.name == target_name:
            target_sig = mod.get_signal(target_name)
            rhs_sig, g = _expr_to_signal_and_gates(mod, stmt.right.var, expected_width=target_sig.width)
            gates.extend(g)
            return rhs_sig, gates
        return current_sig, gates

    # 3. If Statement
    elif isinstance(stmt, IfStatement):
        cond_sig, cond_g = _expr_to_signal_and_gates(mod, stmt.cond)
        gates.extend(cond_g)

        true_sig, true_g = _extract_comb_logic(mod, stmt.true_statement, target_name, current_sig)
        gates.extend(true_g)

        if stmt.false_statement:
            false_sig, false_g = _extract_comb_logic(mod, stmt.false_statement, target_name, current_sig)
            gates.extend(false_g)
        else:
            false_sig = current_sig # Keep previous value if no else branch

        if true_sig == false_sig:
            return true_sig, gates # Optimize out MUX if branches do the same thing

        mux_out = _get_or_create_signal(mod, f"mux_{target_name}_{len(mod.gates)}", width=true_sig.width)
        gates.append(Gate("MUX", [cond_sig, true_sig, false_sig], mux_out))
        return mux_out, gates

    # 4. Case Statement
    elif isinstance(stmt, CaseStatement):
        comp_sig, comp_g = _expr_to_signal_and_gates(mod, stmt.comp)
        gates.extend(comp_g)

        default_stmt = None
        normal_cases = []
        for c in stmt.caselist:
            if c.cond is None: default_stmt = c.statement
            else: normal_cases.append(c)

        if default_stmt:
            result_sig, g = _extract_comb_logic(mod, default_stmt, target_name, current_sig)
            gates.extend(g)
        else:
            result_sig = current_sig

        # Build MUX chain backwards from the default case
        for c in reversed(normal_cases):
            cond_sigs = []
            for cond_expr in c.cond:
                val_sig, val_g = _expr_to_signal_and_gates(mod, cond_expr, expected_width=comp_sig.width)
                gates.extend(val_g)
                
                eq_out = _get_or_create_signal(mod, f"eq_{len(mod.gates)}", width=1)
                gates.append(Gate("EQ", [comp_sig, val_sig], eq_out))
                cond_sigs.append(eq_out)

            # OR together multiple conditions (e.g. case (x) 1, 2: ...)
            final_cond = cond_sigs[0]
            for i in range(1, len(cond_sigs)):
                or_out = _get_or_create_signal(mod, f"or_{len(mod.gates)}", width=1)
                gates.append(Gate("OR", [final_cond, cond_sigs[i]], or_out))
                final_cond = or_out

            true_sig, true_g = _extract_comb_logic(mod, c.statement, target_name, current_sig)
            gates.extend(true_g)

            if true_sig != result_sig:
                mux_out = _get_or_create_signal(mod, f"mux_{target_name}_{len(mod.gates)}", width=true_sig.width)
                gates.append(Gate("MUX", [final_cond, true_sig, result_sig], mux_out))
                result_sig = mux_out

        return result_sig, gates

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

    # 1. Parse Ports
    for p in top.portlist.ports:
        if isinstance(p, Ioport):
            first, second = p.first, p.second
            width = _parse_width(first.width)
            if isinstance(first, Input):
                _get_or_create_signal(mod, first.name, width=width, is_input=True)
            elif isinstance(first, Output):
                is_reg = isinstance(second, Reg)
                _get_or_create_signal(mod, first.name, width=width, is_output=True, is_reg=is_reg)

    # 2. Parse Items (Always Blocks)
    for item in top.items:
        if isinstance(item, Always):
            is_clocked = False
            if isinstance(item.sens_list, SensList):
                for s in item.sens_list.list:
                    if s.type == "posedge": is_clocked = True
            
            if is_clocked:
                # SEQUENTIAL (Unchanged for MVP)
                clk_name = item.sens_list.list[0].sig.name
                clk_sig = _get_or_create_signal(mod, clk_name, is_input=True)
                body = item.statement
                if isinstance(body, Block): body = body.statements[0]
                
                if isinstance(body, IfStatement):
                    rst_sig = _get_or_create_signal(mod, body.cond.name)
                    then_stmt = body.true_statement
                    if isinstance(then_stmt, Block): then_stmt = then_stmt.statements[0]
                    target_sig = _get_or_create_signal(mod, then_stmt.left.var.name)
                    
                    rst_val_sig, g_rst = _expr_to_signal_and_gates(mod, then_stmt.right.var, target_sig.width)
                    for g in g_rst: mod.add_gate(g)

                    else_stmt = body.false_statement
                    if isinstance(else_stmt, IfStatement):
                        en_sig = _get_or_create_signal(mod, else_stmt.cond.name)
                        en_then = else_stmt.true_statement
                        if isinstance(en_then, Block): en_then = en_then.statements[0]
                        
                        next_val_sig, g_next = _expr_to_signal_and_gates(mod, en_then.right.var, target_sig.width)
                        for g in g_next: mod.add_gate(g)

                        mod.add_gate(Gate("DFF_EN_RST", [next_val_sig, target_sig, en_sig, rst_val_sig, rst_sig, clk_sig], target_sig))

            else:
                # COMBINATIONAL (SSA MUX Extraction)
                target_candidates = [s.name for s in mod.signals.values() if s.is_output or s.is_reg]
                
                for target_name in target_candidates:
                    # Initial state is the signal itself (acts as latch if never assigned)
                    initial_sig = mod.get_signal(target_name)
                    
                    final_sig, gates = _extract_comb_logic(mod, item.statement, target_name, initial_sig)
                    
                    if final_sig != initial_sig:
                        for g in gates: mod.add_gate(g)
                        mod.add_gate(Gate("BUF", [final_sig], mod.get_signal(target_name)))

    mod.save_json("debug_02_elab.json")
    return mod