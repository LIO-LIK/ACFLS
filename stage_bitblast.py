# stage_bitblast.py
# Bit-blasting: convert high-level gates (ADD, DFF_EN_RST) into 1-bit primitives.
# For now supports the counter example:
# - Expand buses into bit signals: <name>_<i> (LSB=0)
# - ADD -> ripple-carry XOR/AND/OR (modulo width)
# - DFF_EN_RST -> MUX(enable, hold, d_en) -> MUX(rst, mux_en, d_rst) -> DFF
# - Constants CONST_* -> per-bit CONST0/CONST1

import re
from netlist import Signal, Gate

# ---------- helpers: naming ----------
def bit_name(base: str, i: int) -> str:
    return f"{base}_{i}"

def tmp_name(prefix: str, idx: int) -> str:
    return f"tmp_{prefix}_{idx}"

# ---------- helpers: const parsing ----------
_const_re = re.compile(r"^\s*(\d+)\s*'([bBdDhHoO])\s*([0-9a-fA-FxXzZ_]+)\s*$")

def parse_verilog_const(value: str, width_hint: int = 1) -> list[int]:
    """
    Return little-endian bit list (LSB first) with length = width.
    Only supports 0/1 digits well. Treat x/z as 0 for now.
    Examples: "4'b0" -> [0,0,0,0], "1" -> [1] (width_hint used)
    """
    value = value.strip()
    m = _const_re.match(value)
    if m:
        w = int(m.group(1))
        base = m.group(2).lower()
        digits = m.group(3).replace("_", "")
        # Convert to integer, treating x/z as 0
        digits_clean = re.sub(r"[xXzZ]", "0", digits)

        if base == "b":
            bits_str = digits_clean
            # if shorter than width, pad on left
            bits_str = bits_str.zfill(w)
            bits = [1 if c == "1" else 0 for c in bits_str[::-1]]  # LSB first
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

    # Unsized constant (e.g. "1")
    try:
        v = int(value, 0)
    except Exception:
        v = 0
    w = max(1, width_hint)
    return [(v >> i) & 1 for i in range(w)]


def run(mod):
    """
    Takes the high-level Module, modifies it in-place to be gate-level.
    Writes debug_03_bitblast.json.
    """

    # 0) Ensure global CONST0/CONST1 exist
    const0 = mod.get_signal("CONST0")
    if const0 is None:
        const0 = Signal("CONST0", width=1)
        mod.add_signal(const0)

    const1 = mod.get_signal("CONST1")
    if const1 is None:
        const1 = Signal("CONST1", width=1)
        mod.add_signal(const1)

    # 1) Build bit-signal mapping for all signals in module
    #    Keep original wide signals, but gates will use bit signals only.
    bits_map = {}  # name -> list[Signal] (len=width)

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
                b = Signal(
                    name=bn,
                    width=1,
                    is_input=sig.is_input,
                    is_output=sig.is_output,
                    is_reg=sig.is_reg
                )
                mod.add_signal(b)
            blist.append(b)
        bits_map[sig.name] = blist
        return blist

    # Pre-create bit signals for all declared signals
    for s in list(mod.signals.values()):
        get_bits(s)

    # helper to get const bits as signals
    def const_bits_from_signal(sig: Signal, width_hint: int) -> list[Signal]:
        # sig.name looks like "CONST_4'b0" or "CONST_1"
        if not sig.name.startswith("CONST_"):
            # fallback: treat as normal signal bits
            return get_bits(sig)

        raw = sig.name[len("CONST_"):]
        bits = parse_verilog_const(raw, width_hint=width_hint)
        out = []
        for b in bits:
            out.append(const1 if b == 1 else const0)
        return out

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

    # primitive constructors
    def XOR2(a, b, out): new_gates.append(Gate("XOR", [a, b], out))
    def AND2(a, b, out): new_gates.append(Gate("AND", [a, b], out))
    def OR2(a, b, out):  new_gates.append(Gate("OR",  [a, b], out))
    def MUX(sel, d0, d1, out): new_gates.append(Gate("MUX", [sel, d0, d1], out))
    def DFF(d, clk, q): new_gates.append(Gate("DFF", [d, clk], q))

    for g in mod.gates:
        op = g.op_type

        # -------- ADD (vector) -> ripple-carry (modulo width) --------
        if op == "ADD":
            a, b = g.inputs
            out = g.output

            out_bits = get_bits(out)
            w = len(out_bits)

            # Inputs: if const, materialize bits; else normal
            a_bits = const_bits_from_signal(a, width_hint=w) if a.name.startswith("CONST_") else get_bits(a)
            b_bits = const_bits_from_signal(b, width_hint=w) if b.name.startswith("CONST_") else get_bits(b)

            # extend/pad to width w
            def pad(bits):
                if len(bits) >= w:
                    return bits[:w]
                return bits + [const0] * (w - len(bits))

            a_bits = pad(a_bits)
            b_bits = pad(b_bits)

            carry = const0
            for i in range(w):
                # sum = a ^ b ^ carry
                t1 = new_tmp("xor")
                XOR2(a_bits[i], b_bits[i], t1)
                sbit = out_bits[i]
                XOR2(t1, carry, sbit)

                # carry_out = (a&b) | (a&c) | (b&c)
                t_ab = new_tmp("and")
                t_ac = new_tmp("and")
                t_bc = new_tmp("and")
                AND2(a_bits[i], b_bits[i], t_ab)
                AND2(a_bits[i], carry, t_ac)
                AND2(b_bits[i], carry, t_bc)

                t_or1 = new_tmp("or")
                OR2(t_ab, t_ac, t_or1)
                cnext = new_tmp("or")
                OR2(t_or1, t_bc, cnext)

                carry = cnext

            # modulo behavior: drop final carry
            continue

        # -------- DFF_EN_RST (vector) -> 2x MUX + DFF per bit --------
        if op == "DFF_EN_RST":
            # Inputs convention from your stage_elaboration:
            # [D_when_enable, Q_old, enable, D_reset, reset, clk] -> Q
            d_en, q_old, en, d_rst, rst, clk = g.inputs
            q = g.output

            q_bits = get_bits(q)
            w = len(q_bits)

            # d_en/d_rst may be CONST_*
            d_en_bits = const_bits_from_signal(d_en, width_hint=w) if d_en.name.startswith("CONST_") else get_bits(d_en)
            d_rst_bits = const_bits_from_signal(d_rst, width_hint=w) if d_rst.name.startswith("CONST_") else get_bits(d_rst)
            q_old_bits = get_bits(q_old)

            # en/rst/clk must be 1-bit
            en_b = get_bits(en)[0]
            rst_b = get_bits(rst)[0]
            clk_b = get_bits(clk)[0]

            # pad vectors to width w
            def pad(bits):
                if len(bits) >= w:
                    return bits[:w]
                return bits + [const0] * (w - len(bits))

            d_en_bits = pad(d_en_bits)
            d_rst_bits = pad(d_rst_bits)
            q_old_bits = pad(q_old_bits)

            for i in range(w):
                # mux_en = en ? d_en : q_old (hold)
                mux_en = new_tmp("mux")
                MUX(en_b, q_old_bits[i], d_en_bits[i], mux_en)

                # mux_rst = rst ? d_rst : mux_en  (reset priority)
                mux_rst = new_tmp("mux")
                MUX(rst_b, mux_en, d_rst_bits[i], mux_rst)

                # dff
                DFF(mux_rst, clk_b, q_bits[i])

            continue

        # -------- Already primitive (AND/OR/XOR/NOT/MUX/DFF) --------
        # If they ever appear with width>1 (not expected yet), you can extend later.
        if op in ("AND", "OR", "XOR", "NOT", "MUX", "DFF"):
            new_gates.append(g)
            continue

        raise NotImplementedError(f"Bitblast: unsupported gate type {op}")

    # 3) Replace gate list & dump debug
    mod.gates = new_gates
    mod.save_json("debug_03_bitblast.json")
