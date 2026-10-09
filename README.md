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
python3 bal_builder.py compile       hello.bal -o out                # make build, then copy the .jar to out/main.jar
python3 bal_builder.py build-graalvm hello.bal -o out                # make build_graalvm, then copy the native executable and .jar to out/main and out/main.jar
python3 bal_builder.py build-docker  hello.bal                       # make build_docker, then docker save to ./main.tar
python3 bal_builder.py compile       hello.bal -o out --name hello   # the same, with the outputs named hello: out/hello.jar
python3 bal_builder.py compile       hello.bal --test --scan -o out  # also run the tests and security scans, with their reports in out/
python3 bal_builder.py compile       hello.bal --max-complexity 10   # also measure complexity; fail if a function is over 10
python3 bal_builder.py compile       hello.bal --visualize -o out    # also save the VS Code extension's diagrams in out/visualizations/
python3 bal_builder.py run           hello.bal a b                   # make build, then run the program with the arguments a b
```

Each run copies this folder (or `--template DIR` / `$BAL_BUILDER_TEMPLATE`) to a temporary folder, replaces `main.bal` with the given file, starts a container from the `.devcontainer` image, runs the Makefile target in it and streams the output. After a successful build the temporary folder and the container are removed; `--keep` leaves both and prints where they are. A failed build keeps the temporary folder (`project/` and `build.log`) and prints its path.

`-o DIR` copies a build's outputs out of the container, so they can be used after the temporary copy is gone: `target/bin` (the executable `.jar`, run with `java -jar`, plus the native executable for `build-graalvm`), the image `.tar` of `build-docker` (load it with `docker load -i`; without `-o` it is saved in the current folder) and the reports of these checks:

| Option | Runs | Report in `DIR/` |
|---|---|---|
| `--test` | `make test`: `bal test` with a test report and code coverage. The tests come from this folder's `tests/` and from `--test-file FILE` (repeatable). | `test-report/`: `index.html`, `test_results.json`, JaCoCo `coverage-report.xml` |
| `--scan` | `bal scan` static analysis, run in `ballerina/ballerina:2201.13.6` because it does not support Ballerina 2201.7.1 | `scan-report/`: `index.html`, `scan_results.json`, `scan_results.sarif` (for code scanning in CI) |
| | Trivy: known vulnerabilities in the `.jar` (in the image, for `build-docker`) and a CycloneDX SBOM | `trivy/`: `vulnerabilities.json`, `vulnerabilities.txt`, `sbom.cdx.json` |
| `--complexity` | [bal_complexity](bal_complexity/README.md): cyclomatic and cognitive complexity, deepest nesting and lines of code of each function, measured with the template's own Ballerina parser. `--max-complexity N` (implies `--complexity`) fails the run when a function's cyclomatic complexity is over N. | `complexity/`: `complexity.json`, `complexity.txt` |
| `--visualize` | [bal_visualizer](bal_visualizer/README.md): the diagrams of the Ballerina VS Code extension (the version `.devcontainer` installs), drawn by the extension itself in a headless VS Code: each file's overview, and the sequence diagram, data mapper or service view of each function, method, service and resource. Needs `-o`. | `visualizations/`: `index.html`; for each diagram, whole, a PNG, an HTML copy that works offline, and for sequence diagrams the extension's SVG export; `visualizations.json` |

The results are also summarised at the end of each run. Failing tests, a function over `--max-complexity`, a diagram that could not be captured, or a check that cannot run make the run fail; scan findings do not. The first `--scan` downloads the scan image and Trivy's databases, which are kept in the `bal-builder-trivy-cache` Docker volume. The first `--visualize` builds two images (code-server with the extension, and a Playwright browser; about 2 GB to download).

The outputs are named `main`, not after this project's package (`vscode_remote_trybal`): `main.jar`, the native executable `main`, and the image `main:latest`, saved as `main.tar`. `--name NAME` (MCP: `output_name`) names them `NAME` instead, with the image name in lowercase. Without `-o` or `--name`, each `build-docker` run saves `./main.tar` over the previous one.

### Pinned dependencies

`// dependency: org/name:version` comments pin the versions of the packages a file uses. Without them, Ballerina picks the version: the one bundled with the distribution, or the newest compatible one on Ballerina Central.

```ballerina
// dependency: ballerina/toml:0.3.0
// dependency: ballerina/uuid:1.5.0
import ballerina/toml;
import ballerina/uuid;
```

- The pins are written to the `Dependencies.toml` of the temporary copy. If the template's `Dependencies.toml` belongs to this package, it is kept and the pinned packages replace their entries in it. Otherwise Ballerina would ignore that file, so a new one is written.
- Before the Makefile target, `bal build --sticky` pulls the pinned versions from Ballerina Central. Ballerina then keeps that resolution, so `make` (and `make test` for `--test`) builds with the same versions. A plain `bal build` would move to the newest compatible versions instead.
- After the build, the summary lists the version of each pinned package that the build used. The run fails if one differs from its pin, or if the build doesn't use a pinned package at all (nothing imports it, directly or through another package). With `run`, the program then doesn't start.
- A pin can also name a package that the file uses only through another package, such as `ballerina/crypto` through `ballerina/uuid`.
- If Ballerina Central doesn't have a pinned version, `bal build --sticky` fails, and Ballerina reports it as `cannot resolve module`. A comment that isn't of the form `org/name:version`, or two different pins for one package, stops the run before it starts.
- Pins compile the file twice (`bal build --sticky`, then the Makefile target) and pull the pinned packages on every run, because each run uses a fresh container.
- The comments work the same in `#!` scripts and in MCP `source`.

[examples/main_dependencies.bal](examples/main_dependencies.bal) pins `ballerina/toml` 0.3.0 and `ballerina/uuid` 1.5.0. Ballerina 2201.7.1 bundles toml 0.4.0 and uuid 1.6.0, and Central also has uuid 1.5.1. `python3 bal_builder.py run examples/main_dependencies.bal` prints a UUID for each service in a TOML document. `compile` ends with `Dependencies (// dependency: comments): ballerina/toml 0.3.0, ballerina/uuid 1.5.0 as declared.`

### Ballerina scripts

With a `#!` first line, a `.bal` file runs like a script: it is built in a copy of this project, and then run with the arguments it was given.

```ballerina
#!/usr/bin/env -S bal_builder.py run -- --greeting "Good morning"
import ballerina/io;

public type Options record {|
    string greeting = "Hello";
|};

public function main(*Options options, string... names) {
    foreach string name in names {
        io:println(options.greeting, ", ", name, "!");
    }
}
```

```sh
chmod +x greet.bal
./greet.bal Ada Grace      # prints Good morning, Ada! and Good morning, Grace!
```

- [examples/main_shebang.bal](examples/main_shebang.bal) is a hello world written this way: `./examples/main_shebang.bal Ada` prints `Hello, World!` and `Hello, Ada!`.
- `bal_builder.py` must be on `PATH` (for example a symlink in `~/.local/bin`), or the line can name it by its full path. `env -S` is needed for a line with more than one word on Linux; macOS accepts it too.
- After `run`, the line can hold the builder's options (`--scan`, `--test`, `--complexity`, `-o DIR`, ...), then `--`, then the program's arguments. The line's arguments come first, then those of the command line, which all go to the program. Quotes on the line work the same on Linux and macOS.
- The program reads its arguments as it would under `java -jar`. `--name value` or `--name=value` sets an option: a field of `main`'s included record parameter, such as `greeting` above. The other arguments fill `main`'s parameters. An option `main` doesn't have is an error (`undefined option`). Arguments after a further `--` are never options, for example a file name that starts with `-`. [examples/main_cli_args.bal](examples/main_cli_args.bal) shows this with `--format` and `--out` options and a list of files.
- The program runs in the build container, in the copy of this project, so it sees this project's files (`README.md`, `Makefile`, ...), not the ones in your current folder. Files it writes are removed along with the container. To work with your own files, build the jar with `compile -o DIR` and run it with `java -jar`.
- Before the build, the `#!` line is turned into a `//` comment, because Ballerina doesn't accept `#!`. Line numbers in errors stay the same. `compile` and the other commands accept such files too. VS Code still marks the line as an error, because the extension sees the file as it is.
- stdout is the program's own, and stdin reaches it (piped, or typed at a terminal, where Ctrl-C stops it). The script's exit code is the program's. Build output is hidden; `-v` shows it on stderr. If the build or one of the requested checks fails, the program doesn't run: the reason and the end of the build output go to stderr, and the exit code is 1. When the line asks for checks, their summary follows the program's output on stderr.
- Each run builds the file in a fresh container, which takes about 8 seconds once the template image exists.
- `bal_builder.py run [options] hello.bal [args...]` does the same without a `#!` line. Options go before the file; everything after it goes to the program.

The command line needs Python 3.9+ and Docker. The MCP server (stdio) also needs Python 3.10+ and the `mcp` package:

```sh
claude mcp add ballerina-builder -- uv run /path/to/bal_builder.py mcp        # uv installs mcp itself
claude mcp add ballerina-builder -- python3.13 /path/to/bal_builder.py mcp    # after: python3.13 -m pip install mcp
```

Its tools `compile_ballerina`, `build_graalvm` and `build_docker` take `bal_file` (a path) or `source` (the code, which can pin dependencies with `// dependency:` comments), plus `output_dir`, `output_name`, `run_tests`, `test_files`, `test_source`, `security_scan`, `complexity`, `max_complexity`, `visualize` and `keep_temp`. Build output streams as MCP progress notifications.
