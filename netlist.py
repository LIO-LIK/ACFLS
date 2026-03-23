# netlist.py
import json

class Signal:
    """Represents a wire or register in the design."""
    def __init__(self, name, width=1, is_input=False, is_output=False, is_reg=False):
        self.name = name
        self.width = width
        self.is_input = is_input
        self.is_output = is_output
        self.is_reg = is_reg
        self.id = id(self)  # Unique ID for hashing/graphing

    def __repr__(self):
        # debugging representation
        direction = "IN" if self.is_input else ("OUT" if self.is_output else "WIRE")
        kind = "REG" if self.is_reg else "NET"
        return f"<{self.name} [{self.width}] {direction} {kind}>"

    def to_dict(self):
        """Serialization for JSON export"""
        return {
            "name": self.name,
            "width": self.width,
            "attributes": {
                "input": self.is_input,
                "output": self.is_output,
                "reg": self.is_reg
            }
        }

class Gate:
    """Represents a logic operation. Before Bit-Blasting, this can be high-level (e.g., OP="ADD"). 
    After Bit-Blasting, this is strictly low-level (e.g., OP="AND", OP="DFF")."""
    def __init__(self, op_type, inputs, output):
        self.op_type = op_type   # e.g., "AND", "OR", "NOT", "ADD", "MUX", "DFF"
        self.inputs = inputs     # List of Signal objects
        self.output = output     # Single Signal object driven by this gate

    def __repr__(self):
        input_names = [s.name for s in self.inputs]
        return f"[{self.op_type}] {input_names} -> {self.output.name}"

    def to_dict(self):
        """Serialization for JSON export"""
        return {
            "type": self.op_type,
            "inputs": [s.name for s in self.inputs],
            "output": self.output.name
        }

class Instance:
    """Represents an instantiated sub-module or black-box component (like a DSP block)."""
    def __init__(self, module_type, instance_name, port_connections, parameters=None):
        self.module_type = module_type          # like "ALU", "DSPsomething"
        self.instance_name = instance_name      # like "my_alu", "math_block_0"
        self.port_connections = port_connections # Dict mapping port_name (str) -> Signal object
        self.parameters = parameters or {}      # Dict mapping param_name (str) -> value

    def __repr__(self):
        ports = ", ".join([f".{port}({sig.name})" for port, sig in self.port_connections.items()])
        return f"[{self.module_type}] {self.instance_name} ({ports})"

    def to_dict(self):
        """Serialization for JSON export"""
        return {
            "module_type": self.module_type,
            "instance_name": self.instance_name,
            "parameters": self.parameters,
            "port_connections": {port: sig.name for port, sig in self.port_connections.items()}
        }

class Module:
    """The container for the entire design."""
    def __init__(self, name):
        self.name = name
        self.signals = {}  # Dict mapping name -> Signal object
        self.gates = []    # List of Gate objects
        self.instances = [] # List of Instance objects (NEW)

    def add_signal(self, signal):
        self.signals[signal.name] = signal

    def get_signal(self, name):
        return self.signals.get(name)

    def add_gate(self, gate):
        self.gates.append(gate)
        
    def add_instance(self, instance):
        self.instances.append(instance)

    def to_json(self):
        """Dumps the entire netlist to a JSON-compatible dictionary"""
        return {
            "module_name": self.name,
            "signals": [s.to_dict() for s in self.signals.values()],
            "gates": [g.to_dict() for g in self.gates],
            "instances": [i.to_dict() for i in self.instances] # Export instances
        }

    def save_json(self, filename):
        with open(filename, 'w') as f:
            json.dump(self.to_json(), f, indent=4)
        print(f"Saved intermediate netlist to {filename}")