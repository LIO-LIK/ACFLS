# stage_bitblast.py
# Bit-blasting: convert high-level gates (ADD, EQ, MUX, AND/OR) into 1-bit primitives.
# Supports:
# - Expand buses into bit signals: <name>_<i> (LSB=0)
# - ADD/SUB -> ripple-carry (2's complement)
# - EQ/NEQ/LT -> XNOR trees and Ripple Comparators
# - MUX (wide) -> Array of 1-bit MUXes
# - Shifts -> MUX-based Barrel Shifters
# - Concat/Slice -> Pure wire routing (Buffers)

import re
import math
from netlist import Signal, Gate

# helpers: naming
def bit_name(base: str, i: int) -> str:
    return f"{base}_{i}"

def tmp_name(prefix: str, idx: int) -> str:
    return f"tmp_{prefix}_{idx}"

# helpers: const parsing
_const_re = re.compile(r"^\s*(\d+)\s*'([bBdDhHoO])\s*([0-9a-fA-FxXzZ_]+)\s*$")

def parse_verilog_const(value: str, width_hint: int = 1) -> list[int]:
    value = value.strip()
    m = _const_re.match(value)
    if m:
        w = int(m.group(1))
        base = m.group(2).lower()
        digits = m.group(3).replace("_", "")
        digits_clean = re.sub(r"[xXzZ]", "0", digits)

        if base == "b":
            bits_str = digits_clean.zfill(w)
            bits = [1 if c == "1" else 0 for c in bits_str[::-1]]
            return bits[:w]
        elif base == "d":
            v = int(digits_clean, 10)
        elif base == "h":
            v = int(digits_clean, 16)
        elif base == "o":
            v = int(digits_clean, 8)
        else:
            v = 0
        bits = [(v >> i) & 1 for i in range(w)]
        return bits

    try:
        v = int(value, 0)
    except Exception:
        v = 0
    w = max(1, width_hint)
    return [(v >> i) & 1 for i in range(w)]


def run(mod):
    # 0) Ensure global CONST0/CONST1 exist
    const0 = mod.get_signal("CONST0")
    if const0 is None:
        const0 = Signal("CONST0", width=1)
        mod.add_signal(const0)

    const1 = mod.get_signal("CONST1")
    if const1 is None:
        const1 = Signal("CONST1", width=1)
        mod.add_signal(const1)

    # 1) Build bit-signal mapping
    bits_map = {} 

    def get_bits(sig: Signal) -> list[Signal]:
        if sig.width == 1:
            bits_map[sig.name] = [sig]
            return [sig]
        if sig.name in bits_map:
            return bits_map[sig.name]
        blist = []
        for i in range(sig.width):
            bn = bit_name(sig.name, i)
            b = mod.get_signal(bn)
            if b is None:
                b = Signal(bn, width=1, is_input=sig.is_input, is_output=sig.is_output, is_reg=sig.is_reg)
                mod.add_signal(b)
            blist.append(b)
        bits_map[sig.name] = blist
        return blist

    # Pre-create bit signals
    for s in list(mod.signals.values()):
        get_bits(s)

    def const_bits_from_signal(sig: Signal, width_hint: int) -> list[Signal]:
        if not sig.name.startswith("CONST_"):
            return get_bits(sig)
        raw = sig.name.split('_', 1)[1] # remove "CONST_"
        bits = parse_verilog_const(raw.split('_')[0], width_hint=width_hint)
        out = []
        for b in bits:
            out.append(const1 if b == 1 else const0)
        return out

    def get_operand_bits(sig, width):
        if sig.name.startswith("CONST_"):
            bits = const_bits_from_signal(sig, width_hint=width)
        else:
            bits = get_bits(sig)
        if len(bits) < width:
            bits = bits + [const0] * (width - len(bits))
        return bits[:width]

    # 2) Rewrite/bitblast gates
    new_gates = []
    tmp_idx = 0

    def new_tmp(prefix="t"):
        nonlocal tmp_idx
        name = tmp_name(prefix, tmp_idx)
        tmp_idx += 1
        s = mod.get_signal(name)
        if s is None:
            s = Signal(name=name, width=1)
            mod.add_signal(s)
        return s

    # 1-bit Primitive Constructors
    def XOR2(a, b, out): new_gates.append(Gate("XOR", [a, b], out))
    def AND2(a, b, out): new_gates.append(Gate("AND", [a, b], out))
    def OR2(a, b, out):  new_gates.append(Gate("OR",  [a, b], out))
    def NOT1(a, out):    new_gates.append(Gate("NOT", [a], out))
    def MUX2(sel, d1, d0, out): new_gates.append(Gate("MUX", [sel, d1, d0], out))
    def DFF(d, clk, q):  new_gates.append(Gate("DFF", [d, clk], q))
    def BUF(a, out):     AND2(a, const1, out) # Buffer is just AND with 1

    print(f"  > Bit-blasting {len(mod.gates)} high-level gates...")
    if mod.instances:
        print(f"  > Passing through {len(mod.instances)} instantiated components (Black Boxes).")

    for g in mod.gates:
        op = g.op_type
        
        # WIRING (Concat, Slice, Not, Buf)
        if op in ["NOT", "BUF"]:
            inp = g.inputs[0]
            out = g.output
            for i in range(out.width):
                if op == "NOT": NOT1(get_operand_bits(inp, out.width)[i], get_bits(out)[i])
                if op == "BUF": BUF(get_operand_bits(inp, out.width)[i], get_bits(out)[i])
            continue

        if op.startswith("SLICE_"):
            msb = int(op.split("_")[1])
            lsb = int(op.split("_")[2])
            inp = g.inputs[0]
            out = g.output
            inp_bits = get_operand_bits(inp, inp.width)
            out_bits = get_bits(out)
            
            for i in range(abs(msb - lsb) + 1):
                BUF(inp_bits[lsb + i], out_bits[i])
            continue

        if op == "CONCAT":
            out_bits = get_bits(g.output)
            idx = 0
            # Verilog {A, B} makes A the MSB. So we reverse the list to wire LSB first.
            for inp in reversed(g.inputs):
                inp_bits = get_bits(inp)
                for b in inp_bits:
                    BUF(b, out_bits[idx])
                    idx += 1
            continue

        # BITWISE LOGIC (AND, OR, XOR)
        if op in ["AND", "OR", "XOR"]:
            a, b = g.inputs
            out = g.output
            w = out.width
            a_bits = get_operand_bits(a, w)
            b_bits = get_operand_bits(b, w)
            out_bits = get_bits(out)
            
            constructor = XOR2 if op == "XOR" else (AND2 if op == "AND" else OR2)
            for i in range(w):
                constructor(a_bits[i], b_bits[i], out_bits[i])
            continue

        # ARITHMETIC (ADD, SUB)
        if op in ["ADD", "SUB"]:
            a, b = g.inputs
            out = g.output
            w = out.width
            a_bits = get_operand_bits(a, w)
            b_bits = get_operand_bits(b, w)
            out_bits = get_bits(out)

            # For Subtraction, we do A + (~B) + 1
            is_sub = (op == "SUB")
            carry = const1 if is_sub else const0

            for i in range(w):
                b_val = b_bits[i]
                if is_sub:
                    b_val = new_tmp("notb")
                    NOT1(b_bits[i], b_val)

                # sum = a ^ b ^ carry
                t1 = new_tmp("xor")
                XOR2(a_bits[i], b_val, t1)
                sbit = out_bits[i]
                XOR2(t1, carry, sbit)

                # carry_out = (a&b) | (a&c) | (b&c)
                t_ab = new_tmp("and"); AND2(a_bits[i], b_val, t_ab)
                t_ac = new_tmp("and"); AND2(a_bits[i], carry, t_ac)
                t_bc = new_tmp("and"); AND2(b_val, carry, t_bc)

                t_or1 = new_tmp("or"); OR2(t_ab, t_ac, t_or1)
                cnext = new_tmp("or"); OR2(t_or1, t_bc, cnext)
                carry = cnext
            continue

        # COMPARISONS (EQ, NEQ, LT)
        if op in ["EQ", "NEQ"]:
            a, b = g.inputs
            out = g.output
            w = max(a.width, b.width)
            a_bits = get_operand_bits(a, w)
            b_bits = get_operand_bits(b, w)
            
            eq_bits = []
            for i in range(w):
                t_xor = new_tmp("xor_eq")
                t_xnor = new_tmp("xnor_eq")
                XOR2(a_bits[i], b_bits[i], t_xor)
                NOT1(t_xor, t_xnor)
                eq_bits.append(t_xnor)
            
            # Reduce AND
            curr = eq_bits[0]
            for i in range(1, len(eq_bits)):
                next_tmp = new_tmp("and_red")
                AND2(curr, eq_bits[i], next_tmp)
                curr = next_tmp
            
            if op == "EQ":
                BUF(curr, get_bits(out)[0])
            else:
                NOT1(curr, get_bits(out)[0])
            continue

        if op == "LT":
            # Simple magnitude comparator (Ripple Borrow)
            a, b = g.inputs
            out = g.output
            w = max(a.width, b.width)
            a_bits = get_operand_bits(a, w)
            b_bits = get_operand_bits(b, w)
            
            borrow = const0
            for i in range(w):
                # borrow_out = (~A & B) | (~(A ^ B) & borrow_in)
                not_a = new_tmp("nota"); NOT1(a_bits[i], not_a)
                a_lt_b = new_tmp("altb"); AND2(not_a, b_bits[i], a_lt_b)
                
                a_xnor_b = new_tmp("axnorb")
                t_xor = new_tmp("axorb"); XOR2(a_bits[i], b_bits[i], t_xor)
                NOT1(t_xor, a_xnor_b)
                
                eq_and_borrow = new_tmp("eq_b"); AND2(a_xnor_b, borrow, eq_and_borrow)
                
                next_borrow = new_tmp("b_out")
                OR2(a_lt_b, eq_and_borrow, next_borrow)
                borrow = next_borrow
            
            BUF(borrow, get_bits(out)[0])
            continue

        # BARREL SHIFTERS (SLL, SRL, SRA)
        if op in ["SLL", "SRL", "SRA"]:
            val, amt = g.inputs
            out = g.output
            w = out.width
            val_bits = get_operand_bits(val, w)
            amt_bits = get_operand_bits(amt, amt.width)
            
            stages = math.ceil(math.log2(w)) if w > 1 else 1
            curr_bits = val_bits
            
            for s in range(stages):
                shift_val = 1 << s
                next_bits = []
                sel = amt_bits[s] if s < len(amt_bits) else const0
                
                for i in range(w):
                    if op == "SLL":
                        src_idx = i - shift_val
                        pad_bit = const0
                    else: # SRL or SRA
                        src_idx = i + shift_val
                        pad_bit = curr_bits[w-1] if op == "SRA" else const0
                        
                    if 0 <= src_idx < w:
                        true_in = curr_bits[src_idx]
                    else:
                        true_in = pad_bit
                        
                    mux_out = new_tmp(f"shift_{s}_{i}")
                    MUX2(sel, true_in, curr_bits[i], mux_out)
                    next_bits.append(mux_out)
                curr_bits = next_bits
                
            for i in range(w):
                BUF(curr_bits[i], get_bits(out)[i])
            continue

        # MUX & DFF
        if op == "MUX":
            sel, t_in, f_in = g.inputs
            out = g.output
            w = out.width
            sel_bit = get_bits(sel)[0]
            t_bits = get_operand_bits(t_in, w)
            f_bits = get_operand_bits(f_in, w)
            out_bits = get_bits(out)
            
            for i in range(w):
                MUX2(sel_bit, t_bits[i], f_bits[i], out_bits[i])
            continue

        if op == "DFF_EN_RST":
            d_en, q_old, en, d_rst, rst, clk = g.inputs
            q = g.output
            w = q.width
            d_en_bits = get_operand_bits(d_en, w)
            d_rst_bits = get_operand_bits(d_rst, w)
            q_old_bits = get_operand_bits(q_old, w)
            q_bits = get_bits(q)
            en_b = get_bits(en)[0]; rst_b = get_bits(rst)[0]; clk_b = get_bits(clk)[0]

            for i in range(w):
                mux_en = new_tmp("mux"); MUX2(en_b, d_en_bits[i], q_old_bits[i], mux_en)
                mux_rst = new_tmp("mux"); MUX2(rst_b, d_rst_bits[i], mux_en, mux_rst)
                DFF(mux_rst, clk_b, q_bits[i])
            continue

        print(f"  [Warning] Bitblast: ignoring unsupported macro {op}")

    mod.gates = new_gates
    mod.save_json("debug_03_bitblast.json")