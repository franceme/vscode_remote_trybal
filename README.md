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
```

Each run copies this folder (or `--template DIR` / `$BAL_BUILDER_TEMPLATE`) to a temporary folder, replaces `main.bal` with the given file, starts a container from the `.devcontainer` image, runs the Makefile target in it and streams the output. After a successful build the temporary folder and the container are removed; `--keep` leaves both and prints where they are. A failed build keeps the temporary folder (`project/` and `build.log`) and prints its path.

`-o DIR` copies a build's outputs out of the container, so they can be used after the temporary copy is gone: `target/bin` (the executable `.jar`, run with `java -jar`, plus the native executable for `build-graalvm`), the image `.tar` of `build-docker` (load it with `docker load -i`; without `-o` it is saved as `./<file name>.tar`) and the reports of these checks:

| Option | Runs | Report in `DIR/` |
|---|---|---|
| `--test` | `make test`: `bal test` with a test report and code coverage. The tests come from this folder's `tests/` and from `--test-file FILE` (repeatable). | `test-report/`: `index.html`, `test_results.json`, JaCoCo `coverage-report.xml` |
| `--scan` | `bal scan` static analysis, run in `ballerina/ballerina:2201.13.6` because it does not support Ballerina 2201.7.1 | `scan-report/`: `index.html`, `scan_results.json`, `scan_results.sarif` (for code scanning in CI) |
| | Trivy: known vulnerabilities in the `.jar` (in the image, for `build-docker`) and a CycloneDX SBOM | `trivy/`: `vulnerabilities.json`, `vulnerabilities.txt`, `sbom.cdx.json` |

The results are also summarised at the end of each run. Failing tests, or a check that cannot run, make the run fail; scan findings do not. The first `--scan` downloads the scan image and Trivy's databases, which are kept in the `bal-builder-trivy-cache` Docker volume.

The command line needs Python 3.9+ and Docker. The MCP server (stdio) also needs Python 3.10+ and the `mcp` package:

```sh
claude mcp add ballerina-builder -- uv run /path/to/bal_builder.py mcp        # uv installs mcp itself
claude mcp add ballerina-builder -- python3.13 /path/to/bal_builder.py mcp    # after: python3.13 -m pip install mcp
```

Its tools `compile_ballerina`, `build_graalvm` and `build_docker` take `bal_file` (a path) or `source` (the code), plus `output_dir`, `run_tests`, `test_files`, `test_source`, `security_scan` and `keep_temp`. Build output streams as MCP progress notifications.
