import sys
import os
from pyverilog.vparser.parser import parse

def run(filename):
    """
    Parses the Verilog file and returns the AST.
    Also saves a debug dump of the AST structure.
    """
    print(f"  > Parsing file: {filename}")
    
    # 1. Call the PyVerilog Parser
    try:
        ast, directives = parse([filename])
    except Exception as e:
        print(f"  ! Error parsing Verilog: {e}")
        sys.exit(1)

    # 2. Save Debug Output
    debug_filename = "debug_01_ast.txt"
    print(f"  > Saving AST debug info to {debug_filename}...")
    
    with open(debug_filename, 'w') as f:
        # PyVerilog's show() method accepts a 'buf' argument.
        # Pass the file object directly to it.
        ast.show(buf=f)

    return ast

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python stage_parser.py <file.v>")
    else:
        run(sys.argv[1])