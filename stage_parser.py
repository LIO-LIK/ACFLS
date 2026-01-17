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
    # parse returns a tuple: (ast, directives)
    # We only care about the AST.
    try:
        ast, directives = parse([filename])
    except Exception as e:
        print(f"  ! Error parsing Verilog: {e}")
        sys.exit(1)

    # 2. Save Debug Output
    # PyVerilog nodes have a show() method that prints to stdout.
    # We will capture that output to save it to a file.
    debug_filename = "debug_01_ast.txt"
    print(f"  > Saving AST debug info to {debug_filename}...")
    
    with open(debug_filename, 'w') as f:
        # Redirect stdout to the file temporarily to capture ast.show()
        original_stdout = sys.stdout
        sys.stdout = f
        try:
            ast.show()
        finally:
            sys.stdout = original_stdout

    return ast

if __name__ == "__main__":
    # Allow running this stage in isolation for testing
    if len(sys.argv) < 2:
        print("Usage: python stage_parser.py <file.v>")
    else:
        run(sys.argv[1])