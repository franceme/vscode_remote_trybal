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
python3 bal_builder.py compile       hello.bal          # make build
python3 bal_builder.py build-graalvm hello.bal -d       # make build_graalvm, then copy target/bin to ./dist
python3 bal_builder.py build-docker  hello.bal -o out/  # make build_docker, then docker save to out/hello.tar
```

Each run copies this folder (or `--template DIR` / `$BAL_BUILDER_TEMPLATE`) to a temporary folder, replaces `main.bal` with the given file, starts a container from the `.devcontainer` image, runs the Makefile target in it and streams the output. After a successful build the temporary folder and the container are removed; `--keep` leaves both and prints where they are. A failed build keeps the temporary folder (`project/` and `build.log`) and prints its path.

The command line needs Python 3.9+ and Docker. The MCP server (stdio) also needs Python 3.10+ and the `mcp` package:

```sh
claude mcp add ballerina-builder -- uv run /path/to/bal_builder.py mcp        # uv installs mcp itself
claude mcp add ballerina-builder -- python3.13 /path/to/bal_builder.py mcp    # after: python3.13 -m pip install mcp
```

Its tools `compile_ballerina`, `build_graalvm` and `build_docker` take `bal_file` (a path) or `source` (the code), plus `keep_temp`. Build output streams as MCP progress notifications.
