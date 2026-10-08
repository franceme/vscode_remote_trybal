# vscode_remote_trybal

* https://ballerina.io/learn/
* https://ballerina.io/learn/by-example/
* https://ballerina.io/learn/work-with-llms-using-natural-expressions/
* https://github.com/ballerina-platform/module-ballerina-ai.np/tree/main/examples/popular-sportsperson
* https://blog.ballerina.io/posts/2025-04-26-introducing-natural-programming/
* https://github.com/ballerina-platform/module-ballerina-ai.np
* https://ballerina.io/use-cases/ai/
* https://ballerina.io/learn/by-example/query-expressions/
* https://learn-ballerina.github.io/best_practices/structure_your_code.html

## bal_builder.py

Builds one Ballerina file in a throwaway copy of this project, inside the `.devcontainer` image, without opening the dev container. It works as a command-line tool and as an MCP server.

```sh
python3 bal_builder.py compile       hello.bal -o out                # make build, then copy the .jar to out/
python3 bal_builder.py build-graalvm hello.bal -o out                # make build_graalvm, then copy the native executable and .jar to out/
python3 bal_builder.py build-docker  hello.bal                       # make build_docker, then docker save to ./hello.tar
python3 bal_builder.py compile       hello.bal --test --scan -o out  # also run the tests and security scans, with their reports in out/
python3 bal_builder.py compile       hello.bal --max-complexity 10   # also measure complexity; fail if a function is over 10
python3 bal_builder.py compile       hello.bal --visualize -o out    # also save the VS Code extension's diagrams in out/visualizations/
python3 bal_builder.py run           hello.bal a b                   # make build, then run the program with the arguments a b
```

Each run copies this folder (or `--template DIR` / `$BAL_BUILDER_TEMPLATE`) to a temporary folder, replaces `main.bal` with the given file, starts a container from the `.devcontainer` image, runs the Makefile target in it and streams the output. After a successful build the temporary folder and the container are removed; `--keep` leaves both and prints where they are. A failed build keeps the temporary folder (`project/` and `build.log`) and prints its path.

`-o DIR` copies a build's outputs out of the container, so they can be used after the temporary copy is gone: `target/bin` (the executable `.jar`, run with `java -jar`, plus the native executable for `build-graalvm`), the image `.tar` of `build-docker` (load it with `docker load -i`; without `-o` it is saved as `./<file name>.tar`) and the reports of these checks:

| Option | Runs | Report in `DIR/` |
|---|---|---|
| `--test` | `make test`: `bal test` with a test report and code coverage. The tests come from this folder's `tests/` and from `--test-file FILE` (repeatable). | `test-report/`: `index.html`, `test_results.json`, JaCoCo `coverage-report.xml` |
| `--scan` | `bal scan` static analysis, run in `ballerina/ballerina:2201.13.6` because it does not support Ballerina 2201.7.1 | `scan-report/`: `index.html`, `scan_results.json`, `scan_results.sarif` (for code scanning in CI) |
| | Trivy: known vulnerabilities in the `.jar` (in the image, for `build-docker`) and a CycloneDX SBOM | `trivy/`: `vulnerabilities.json`, `vulnerabilities.txt`, `sbom.cdx.json` |
| `--complexity` | [bal_complexity](bal_complexity/README.md): cyclomatic and cognitive complexity, deepest nesting and lines of code of each function, measured with the template's own Ballerina parser. `--max-complexity N` (implies `--complexity`) fails the run when a function's cyclomatic complexity is over N. | `complexity/`: `complexity.json`, `complexity.txt` |
| `--visualize` | [bal_visualizer](bal_visualizer/README.md): the diagrams of the Ballerina VS Code extension (the version `.devcontainer` installs), drawn by the extension itself in a headless VS Code: each file's overview, and the sequence diagram, data mapper or service view of each function, method, service and resource. Needs `-o`. | `visualizations/`: `index.html`, a PNG of each diagram (plus the extension's SVG export of sequence diagrams), `visualizations.json` |

The results are also summarised at the end of each run. Failing tests, a function over `--max-complexity`, a diagram that could not be captured, or a check that cannot run make the run fail; scan findings do not. The first `--scan` downloads the scan image and Trivy's databases, which are kept in the `bal-builder-trivy-cache` Docker volume. The first `--visualize` builds two images (code-server with the extension, and a Playwright browser; about 2 GB to download).

### Ballerina scripts

With a `#!` first line, a `.bal` file runs like a script: it is built in a copy of this project, and then run with the arguments it was given.

```ballerina
#!/usr/bin/env -S bal_builder.py run -- --greeting "Good morning"
import ballerina/io;

public function main(string... args) {
    io:println(args);
}
```

```sh
chmod +x greet.bal
./greet.bal Ada            # prints ["--greeting","Good morning","Ada"]
```

- [examples/main_shebang.bal](examples/main_shebang.bal) is a hello world written this way: `./examples/main_shebang.bal Ada` prints `Hello, World!` and `Hello, Ada!`.
- `bal_builder.py` must be on `PATH` (for example a symlink in `~/.local/bin`), or the line can name it by its full path. `env -S` is needed for a line with more than one word on Linux; macOS accepts it too.
- After `run`, the line can hold the builder's options (`--scan`, `--test`, `--complexity`, `-o DIR`, ...) and then the program's arguments. Put `--` before program arguments that start with `-`. The line's arguments come first, then those of the command line, which all go to the program. Quotes on the line work the same on Linux and macOS.
- Before the build, the `#!` line is turned into a `//` comment, because Ballerina doesn't accept `#!`. Line numbers in errors stay the same. `compile` and the other commands accept such files too. VS Code still marks the line as an error, because the extension sees the file as it is.
- stdout is the program's own, and stdin reaches it (piped, or typed at a terminal, where Ctrl-C stops it). The script's exit code is the program's. Build output is hidden; `-v` shows it on stderr. If the build or one of the requested checks fails, the program doesn't run: the reason and the end of the build output go to stderr, and the exit code is 1. When the line asks for checks, their summary follows the program's output on stderr.
- Each run builds the file in a fresh container, which takes about 8 seconds once the template image exists.
- `bal_builder.py run [options] hello.bal [args...]` does the same without a `#!` line. Options go before the file; everything after it goes to the program.

The command line needs Python 3.9+ and Docker. The MCP server (stdio) also needs Python 3.10+ and the `mcp` package:

```sh
claude mcp add ballerina-builder -- uv run /path/to/bal_builder.py mcp        # uv installs mcp itself
claude mcp add ballerina-builder -- python3.13 /path/to/bal_builder.py mcp    # after: python3.13 -m pip install mcp
```

Its tools `compile_ballerina`, `build_graalvm` and `build_docker` take `bal_file` (a path) or `source` (the code), plus `output_dir`, `run_tests`, `test_files`, `test_source`, `security_scan`, `complexity`, `max_complexity`, `visualize` and `keep_temp`. Build output streams as MCP progress notifications.
