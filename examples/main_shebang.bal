#!/usr/bin/env -S bal_builder.py run -- World
// Hello world as a script. Run it directly, with any names to greet:
//   chmod +x examples/main_shebang.bal
//   ./examples/main_shebang.bal Ada "Grace Hopper"
// The #! line has bal_builder.py build this file and run it. "World" on that line is the first argument, and
// the arguments given on the command line follow it. bal_builder.py must be on PATH, for example:
//   ln -s "$PWD/bal_builder.py" ~/.local/bin/bal_builder.py
import ballerina/io;

public function main(string... names) {
    foreach string name in names {
        io:println("Hello, ", name, "!");
    }
}
