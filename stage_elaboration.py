# stage_elaboration.py
# High-level elaboration: build a high-level netlist (Module/Signal/Gate)
# from the PyVerilog AST. Minimal support for the counter example:
# - ports (input/output) + reg/wire widths
# - always @(posedge clk) with if(rst) / else if(enable)
# - nonblocking assignments <=
# - expressions: Identifier, IntConst, Plus

import os
import re
from netlist import Module, Signal, Gate

from pyverilog.vparser.ast import (
    Source, Description, ModuleDef,
    Ioport, Input, Output, Wire, Reg,
    Width, IntConst, Identifier,
    Always, SensList, Sens, Block,
    IfStatement, NonblockingSubstitution,
    Plus
)

def _parse_width(node):
    """Return bit-width as int. If no width, return 1."""
    if node is None:
        return 1
    # Width: msb, lsb are IntConst
    if isinstance(node, Width):
        msb = int(node.msb.value, 0)
        lsb = int(node.lsb.value, 0)
        return abs(msb - lsb) + 1
    return 1

def _get_or_create_signal(mod: Module, name: str, width=1, **attrs):
    """Fetch existing signal or create a new one."""
    s = mod.get_signal(name)
    if s is None:
        s = Signal(name=name, width=width, **attrs)
        mod.add_signal(s)
    else:
        # Update width/attrs if needed (keep it simple)
        if s.width == 1 and width != 1:
            s.width = width
        # OR the attributes
        s.is_input = s.is_input or attrs.get("is_input", False)
        s.is_output = s.is_output or attrs.get("is_output", False)
        s.is_reg = s.is_reg or attrs.get("is_reg", False)
    return s

def _intconst_decl_width(value: str) -> int | None:
    """
    Parse Verilog sized constant like: 4'b0, 8'hFF, 16'd3.
    Return declared width if present, else None.
    """
    m = re.match(r"^\s*(\d+)\s*'[bBdDhHoO].*$", value)
    return int(m.group(1)) if m else None


def _expr_to_signal_and_gates(mod: Module, expr, expected_width: int | None = None):
    extra_gates = []

    if isinstance(expr, Identifier):
        s = _get_or_create_signal(mod, expr.name)
        if expected_width is not None and s.width == 1 and expected_width != 1:
            s.width = expected_width
        return s, extra_gates

    if isinstance(expr, IntConst):
        declared = _intconst_decl_width(expr.value)
        w = declared or expected_width or 1
        const_name = f"CONST_{expr.value}"
        s = _get_or_create_signal(mod, const_name, width=w)
        return s, extra_gates

    if isinstance(expr, Plus):
        a_sig, a_g = _expr_to_signal_and_gates(mod, expr.left, expected_width=expected_width)
        b_sig, b_g = _expr_to_signal_and_gates(mod, expr.right, expected_width=expected_width)
        extra_gates.extend(a_g)
        extra_gates.extend(b_g)

        out_w = expected_width or max(a_sig.width, b_sig.width)
        tmp_name = f"tmp_add_{len(mod.gates)}"
        tmp = _get_or_create_signal(mod, tmp_name, width=out_w)

        extra_gates.append(Gate("ADD", [a_sig, b_sig], tmp))
        return tmp, extra_gates

    raise NotImplementedError(f"Expression not supported yet: {type(expr).__name__}")

def run(ast):
    """
    Entry point called by main.py.
    Takes PyVerilog AST and returns a Module netlist object.
    Also writes debug_02_elab.json.
    """
    # Navigate: Source -> Description -> ModuleDef
    if not isinstance(ast, Source):
        raise TypeError("Expected PyVerilog Source node as AST root.")

    desc = ast.description
    if desc is None or not isinstance(desc, Description):
        raise TypeError("AST missing Description node.")

    # For now: pick the first module as top
    top = None
    for d in desc.definitions:
        if isinstance(d, ModuleDef):
            top = d
            break
    if top is None:
        raise ValueError("No ModuleDef found in AST.")

    mod = Module(top.name)

    # ---- 1) Ports / signals ----
    # top.portlist.ports is a list of Ioport
    for p in top.portlist.ports:
        if not isinstance(p, Ioport):
            continue

        first = p.first  # Input/Output/Inout
        second = p.second  # Wire/Reg

        # Input
        if isinstance(first, Input):
            name = first.name
            width = _parse_width(first.width)
            _get_or_create_signal(mod, name, width=width, is_input=True)

        # Output (may carry width)
        elif isinstance(first, Output):
            name = first.name
            width = _parse_width(first.width)
            # Determine if reg
            is_reg = isinstance(second, Reg)
            _get_or_create_signal(mod, name, width=width, is_output=True, is_reg=is_reg)

        # Inout not handled in minimal version
        else:
            raise NotImplementedError(f"Port direction not supported: {type(first).__name__}")

    # ---- 2) Always blocks: infer DFF + reset/enable behavior ----
    for item in top.items:
        if not isinstance(item, Always):
            continue

        # Detect posedge clk
        senslist = item.sens_list
        clk_name = None
        if isinstance(senslist, SensList) and senslist.list:
            s0 = senslist.list[0]
            if isinstance(s0, Sens) and s0.type == "posedge" and isinstance(s0.sig, Identifier):
                clk_name = s0.sig.name

        if clk_name is None:
            raise NotImplementedError("Only always @(posedge <clk>) supported in minimal version.")

        clk_sig = _get_or_create_signal(mod, clk_name, is_input=True)

        # Body is a Block, with an IfStatement inside for this counter example
        body = item.statement
        if isinstance(body, Block) and body.statements:
            stmt0 = body.statements[0]
        else:
            stmt0 = body

        if not isinstance(stmt0, IfStatement):
            raise NotImplementedError("Only if(...) inside always supported in minimal version.")

        # Pattern we handle:
        # if (rst) count <= 0;
        # else if (enable) count <= count + 1;
        rst_cond = stmt0.cond
        if not isinstance(rst_cond, Identifier):
            raise NotImplementedError("Reset condition must be Identifier in minimal version.")
        rst_sig = _get_or_create_signal(mod, rst_cond.name, is_input=True)

        # Then branch: NonblockingSubstitution to count
        then_stmt = stmt0.true_statement
        # It may be Block or direct NonblockingSubstitution
        if isinstance(then_stmt, Block):
            then_stmt = then_stmt.statements[0]

        if not isinstance(then_stmt, NonblockingSubstitution):
            raise NotImplementedError("Reset branch must be nonblocking assignment.")

        q_name = then_stmt.left.var.name  # Identifier under Lvalue
        q_sig = _get_or_create_signal(mod, q_name, is_reg=True)

        # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
        # Key change: propagate expected width using the destination reg width
        q_w = q_sig.width
        # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

        d_reset_expr = then_stmt.right.var  # under Rvalue
        d_reset_sig, g_reset = _expr_to_signal_and_gates(mod, d_reset_expr, expected_width=q_w)

        # Else branch: should be IfStatement(enable) ...
        else_stmt = stmt0.false_statement
        if not isinstance(else_stmt, IfStatement):
            raise NotImplementedError("Expected else-if(enable) pattern in minimal version.")

        en_cond = else_stmt.cond
        if not isinstance(en_cond, Identifier):
            raise NotImplementedError("Enable condition must be Identifier in minimal version.")
        en_sig = _get_or_create_signal(mod, en_cond.name, is_input=True)

        en_then = else_stmt.true_statement
        if isinstance(en_then, Block):
            en_then = en_then.statements[0]
        if not isinstance(en_then, NonblockingSubstitution):
            raise NotImplementedError("Enable branch must be nonblocking assignment.")

        d_en_expr = en_then.right.var
        d_en_sig, g_en = _expr_to_signal_and_gates(mod, d_en_expr, expected_width=q_w)

        # Add any gates needed for expressions (e.g., ADD)
        for g in (g_reset + g_en):
            mod.add_gate(g)

        # Now create a high-level sequential primitive.
        # We encode the behavior as a single DFF gate with extra control signals.
        # Since Gate only supports inputs + output, we pack inputs as:
        # [D_when_enable, Q_old, enable, D_reset, reset, clk]
        #
        # Later stages (bitblast/export) can interpret this convention.
        mod.add_gate(Gate("DFF_EN_RST", [d_en_sig, q_sig, en_sig, d_reset_sig, rst_sig, clk_sig], q_sig))

    # ---- Save intermediate ----
    mod.save_json("debug_02_elab.json")
    return mod
