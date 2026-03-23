# stage_elaboration.py
# High-level elaboration: build a high-level netlist (Module/Signal/Gate/Instance)
# from the PyVerilog AST.

import os
import re
from netlist import Module, Signal, Gate, Instance

from pyverilog.vparser.ast import (
    Source, Description, ModuleDef,
    Ioport, Input, Output, Wire, Reg,
    Width, IntConst, Identifier,
    Always, SensList, Sens, Block,
    IfStatement, NonblockingSubstitution, BlockingSubstitution,
    CaseStatement, Case,
    Plus, Minus, Times, Divide,
    Eq, NotEq, LessThan, GreaterThan, LessEq, GreaterEq,
    Land, Lor, And, Or, Xor, Xnor, Unot, Ulnot,
    Sll, Srl, Sra,
    Repeat, Assign, Decl, Parameter, Pointer, Partselect, Concat,
    InstanceList, PortArg, ParamArg, Cond
)

# Helpers

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

# Expression Parsing

def _expr_to_signal_and_gates(mod: Module, expr, expected_width=None, param_env=None):
    if param_env is None: param_env = {}
    extra_gates = []

    # Identifiers & Parameters
    if isinstance(expr, Identifier):
        if expr.name in param_env:
            val = param_env[expr.name]
            w = expected_width or 32
            s = _get_or_create_signal(mod, f"CONST_{val}_{w}b", width=w)
            return s, extra_gates
        else:
            s = _get_or_create_signal(mod, expr.name)
            if expected_width is not None and s.width == 1 and expected_width != 1:
                s.width = expected_width
            return s, extra_gates

    # Hardcoded Constants
    if isinstance(expr, IntConst):
        val = _parse_const_value(expr.value)
        declared_w = _intconst_decl_width(expr.value)
        w = declared_w or expected_width or 32
        const_name = f"CONST_{val}_{w}b_{id(expr)}"
        s = _get_or_create_signal(mod, const_name, width=w)
        return s, extra_gates

    # Unary Operators
    if isinstance(expr, (Unot, Ulnot)):
        val_sig, g = _expr_to_signal_and_gates(mod, expr.right, expected_width, param_env)
        val_sig, g = _expr_to_signal_and_gates(mod, expr.right, expected_width, param_env)
        out_sig = _get_or_create_signal(mod, f"tmp_not_{len(mod.gates)}", width=val_sig.width)
        extra_gates.extend(g)
        extra_gates.append(Gate("NOT", [val_sig], out_sig))
        return out_sig, extra_gates

    # Replication Operator
    if isinstance(expr, Repeat):
        times = _resolve_param(expr.times, param_env)
        s = _get_or_create_signal(mod, f"CONST_0_{times}b_rep", width=times)
        return s, extra_gates

    # Expanded Binary Operators (Math, Logic, Comp, Shifts)
    op_map = {
        Plus: "ADD", Minus: "SUB", Times: "MUL", Divide: "DIV",
        Eq: "EQ", NotEq: "NEQ", LessThan: "LT", GreaterThan: "GT",
        LessEq: "LE", GreaterEq: "GE",
        Land: "AND", Lor: "OR",                           # Logical
        And: "AND", Or: "OR", Xor: "XOR", Xnor: "XNOR",   # Bitwise
        Sll: "SLL", Srl: "SRL", Sra: "SRA"                # Shifts
    }
    expr_type = type(expr)
    
    if expr_type in op_map:
        op_name = op_map[expr_type]
        req_w = expected_width if op_name in ["ADD", "SUB", "MUL", "DIV"] else None
        
        a_sig, a_g = _expr_to_signal_and_gates(mod, expr.left, req_w, param_env)
        b_sig, b_g = _expr_to_signal_and_gates(mod, expr.right, req_w, param_env)
        extra_gates.extend(a_g + b_g)

        # Comparisons output 1 bit. Math/Shifts output standard width.
        is_comp = op_name in ["EQ", "NEQ", "LT", "GT", "LE", "GE", "AND", "OR"]
        out_w = 1 if is_comp else (expected_width or max(a_sig.width, b_sig.width))
        tmp = _get_or_create_signal(mod, f"tmp_{op_name}_{len(mod.gates)}", width=out_w)

        extra_gates.append(Gate(op_name, [a_sig, b_sig], tmp))
        return tmp, extra_gates

    # Part-Select (Bit Slicing) e.g., Instr[31:20]
    if isinstance(expr, Partselect):
        bus_sig, g_bus = _expr_to_signal_and_gates(mod, expr.var, None, param_env)
        msb = _resolve_param(expr.msb, param_env)
        lsb = _resolve_param(expr.lsb, param_env)
        w = abs(msb - lsb) + 1
        
        out_sig = _get_or_create_signal(mod, f"tmp_slice_{msb}_{lsb}_{len(mod.gates)}", width=w)
        extra_gates.extend(g_bus)
        extra_gates.append(Gate(f"SLICE_{msb}_{lsb}", [bus_sig], out_sig))
        return out_sig, extra_gates

    # Concatenation e.g., {A, B, C}
    if isinstance(expr, Concat):
        input_sigs = []
        total_width = 0
        for item in expr.list:
            sig, g = _expr_to_signal_and_gates(mod, item, None, param_env)
            input_sigs.append(sig)
            extra_gates.extend(g)
            total_width += sig.width
            
        out_sig = _get_or_create_signal(mod, f"tmp_concat_{len(mod.gates)}", width=total_width)
        extra_gates.append(Gate("CONCAT", input_sigs, out_sig))
        return out_sig, extra_gates

    # Array Read (Pointer) -> 32-to-1 MUX Tree
    if isinstance(expr, Pointer):
        array_name = expr.var.name
        idx_sig, g_idx = _expr_to_signal_and_gates(mod, expr.ptr, None, param_env)
        extra_gates.extend(g_idx)
        
        depth = param_env.get("MDEPTH", 32)
        width = param_env.get("DWIDTH", 32)
        current_mux_out = _get_or_create_signal(mod, f"{array_name}_0", width=width)
        
        for i in range(1, depth):
            reg_sig = _get_or_create_signal(mod, f"{array_name}_{i}", width=width)
            i_const_sig = _get_or_create_signal(mod, f"CONST_{i}_{idx_sig.width}b", width=idx_sig.width)
            eq_sig = _get_or_create_signal(mod, f"tmp_ptr_eq_{array_name}_{i}_{len(mod.gates)}", width=1)
            extra_gates.append(Gate("EQ", [idx_sig, i_const_sig], eq_sig))
            next_mux_out = _get_or_create_signal(mod, f"tmp_ptr_mux_{array_name}_{i}_{len(mod.gates)}", width=width)
            extra_gates.append(Gate("MUX", [eq_sig, reg_sig, current_mux_out], next_mux_out))
            current_mux_out = next_mux_out

        return current_mux_out, extra_gates

    # Ternary Operator (Condition ? True : False) -> MUX
    if isinstance(expr, Cond):
        cond_sig, g_cond = _expr_to_signal_and_gates(mod, expr.cond, 1, param_env)
        true_sig, g_t = _expr_to_signal_and_gates(mod, expr.true_value, expected_width, param_env)
        false_sig, g_f = _expr_to_signal_and_gates(mod, expr.false_value, expected_width, param_env)
        
        w = expected_width or max(true_sig.width, false_sig.width)
        out_sig = _get_or_create_signal(mod, f"tmp_cond_{len(mod.gates)}", width=w)
        
        extra_gates.extend(g_cond + g_t + g_f)
        extra_gates.append(Gate("MUX", [cond_sig, true_sig, false_sig], out_sig))
        
        return out_sig, extra_gates

    raise ValueError(f"Expression not supported yet: {type(expr).__name__}")

# Advanced Combinational Logic Extraction

def _extract_comb_logic(mod, stmt, target_name, current_sig, param_env):
    gates = []

    if isinstance(stmt, Block):
        for s in stmt.statements:
            current_sig, g = _extract_comb_logic(mod, s, target_name, current_sig, param_env)
            gates.extend(g)
        return current_sig, gates

    elif isinstance(stmt, (BlockingSubstitution, NonblockingSubstitution)):
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
    
    elif isinstance(stmt, CaseStatement):
        comp_sig, comp_g = _expr_to_signal_and_gates(mod, stmt.comp, param_env=param_env)
        gates.extend(comp_g)

        default_stmt = None
        normal_cases = []
        for c in stmt.caselist:
            if c.cond is None: default_stmt = c.statement
            else: normal_cases.append(c)

        if default_stmt:
            result_sig, g = _extract_comb_logic(mod, default_stmt, target_name, current_sig, param_env)
            gates.extend(g)
        else:
            result_sig = current_sig

        for c in reversed(normal_cases):
            cond_sigs = []
            for cond_expr in c.cond:
                val_sig, val_g = _expr_to_signal_and_gates(mod, cond_expr, expected_width=comp_sig.width, param_env=param_env)
                gates.extend(val_g)
                
                eq_out = _get_or_create_signal(mod, f"eq_{len(mod.gates)}", width=1)
                gates.append(Gate("EQ", [comp_sig, val_sig], eq_out))
                cond_sigs.append(eq_out)

            final_cond = cond_sigs[0]
            for i in range(1, len(cond_sigs)):
                or_out = _get_or_create_signal(mod, f"or_{len(mod.gates)}", width=1)
                gates.append(Gate("OR", [final_cond, cond_sigs[i]], or_out))
                final_cond = or_out

            true_sig, true_g = _extract_comb_logic(mod, c.statement, target_name, current_sig, param_env)
            gates.extend(true_g)

            if true_sig != result_sig:
                mux_out = _get_or_create_signal(mod, f"mux_{target_name}_{len(mod.gates)}", width=true_sig.width)
                gates.append(Gate("MUX", [final_cond, true_sig, result_sig], mux_out))
                result_sig = mux_out

        return result_sig, gates

    return current_sig, gates

# Main Run Loop

def run(ast):
    if not isinstance(ast, Source): raise TypeError("Expected PyVerilog Source node.")
    desc = ast.description
    top = next((d for d in desc.definitions if isinstance(d, ModuleDef)), None)
    if not top: raise ValueError("No ModuleDef found.")

    mod = Module(top.name)

    # Parse Parameters
    param_env = {}
    if top.paramlist:
        for p in top.paramlist.params:
            if isinstance(p, Decl) and isinstance(p.list[0], Parameter):
                param = p.list[0]
                param_env[param.name] = _resolve_param(param.value.var, param_env)
    
    print(f"  > Loaded Parameters: {param_env}")

    # Parse Ports
    for p in top.portlist.ports:
        if isinstance(p, Ioport):
            first, second = p.first, p.second
            width = _parse_width(first.width, param_env)
            if isinstance(first, Input):
                _get_or_create_signal(mod, first.name, width=width, is_input=True)
            elif isinstance(first, Output):
                is_reg = isinstance(second, Reg)
                _get_or_create_signal(mod, first.name, width=width, is_output=True, is_reg=is_reg)

    # Parse Items (Arrays, Assigns, Always, Instances)
    for item in top.items:
        # Array Declarations
        # Declarations (Wires, Regs, Arrays)
        if isinstance(item, Decl):
            for decl in item.list:
                if isinstance(decl, (Wire, Reg)):
                    w = _parse_width(decl.width, param_env)
                    if getattr(decl, 'dimensions', None):
                        print(f"  [Scaffolding] Found Array '{decl.name}' with width {w}.")
                    else:
                        # Register the internal wire/reg
                        _get_or_create_signal(mod, decl.name, width=w, is_reg=isinstance(decl, Reg))

        # Continuous Assignments
        elif isinstance(item, Assign):
            target_name = item.left.var.name
            target_sig = mod.get_signal(target_name)
            
            # Fallback for implicitly declared 1-bit wires
            if target_sig is None:
                target_sig = _get_or_create_signal(mod, target_name, width=1)
            
            rhs_sig, g = _expr_to_signal_and_gates(mod, item.right.var, target_sig.width, param_env)
            for gate in g: mod.add_gate(gate)
            mod.add_gate(Gate("BUF", [rhs_sig], target_sig))

        # Module Instantiation (DSPs, Sub-Modules)
        elif isinstance(item, InstanceList):
            module_type = item.module
            for inst in item.instances:
                inst_name = inst.name
                port_connections = {}
                parameters = {}
                
                # Parse Parameters overrides (e.g. #(SIZE=32))
                for param in inst.parameterlist:
                    parameters[param.paramname] = _resolve_param(param.value, param_env)
                
                # Parse Port Connections (e.g. .A(wire1))
                for port in inst.portlist:
                    if port.argname:
                        port_sig, g = _expr_to_signal_and_gates(mod, port.argname, None, param_env)
                        for gate in g: mod.add_gate(gate)
                        port_connections[port.portname] = port_sig
                    else:
                        port_connections[port.portname] = None
                        
                # Create and Add Instance to the module
                new_inst = Instance(module_type, inst_name, port_connections, parameters)
                mod.add_instance(new_inst)
                print(f"  > Instantiated: {new_inst}")

        # Always Blocks
        elif isinstance(item, Always):
            is_clocked = False
            if isinstance(item.sens_list, SensList):
                for s in item.sens_list.list:
                    if s.type == "posedge": is_clocked = True
            
            if is_clocked:
                clk_name = item.sens_list.list[0].sig.name
                clk_sig = _get_or_create_signal(mod, clk_name, is_input=True)
                body = item.statement
                if isinstance(body, Block): body = body.statements[0]
                
                if isinstance(body, IfStatement):
                    # Fallback for generic sequential logic (MVP)
                    rst_sig, g = _expr_to_signal_and_gates(mod, body.cond, param_env=param_env)
                    for gate in g: mod.add_gate(gate)
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