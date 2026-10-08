# bal_visualizer

The diagrams of the Ballerina VS Code extension, saved as files by `bal_builder.py --visualize -o DIR`. The extension draws them itself, so they look exactly as they do in VS Code. No command-line tool can draw them: the extension renders them in a VS Code webview, using data from the Ballerina language server. So the builder runs a real VS Code without a screen and takes pictures of the webviews with a browser.

| File | Purpose |
|---|---|
| [driver/](driver/) | A small VS Code extension. It lists what the Ballerina extension can visualize and opens one diagram at a time when `capture.py` asks for it over HTTP. |
| [capture.py](capture.py) | Runs with Playwright. It opens each diagram, waits until it is drawn, saves it, and writes `visualizations.json` and `index.html`. |

## What you get

In `DIR/visualizations/`, one folder per `.bal` file of the package (`tests/` is left out):

| File | Diagram |
|---|---|
| `overview.png` | The file's overview: its functions, services, records and classes. |
| `<function>.sequence.png` and `.svg` | The sequence diagram of a function, method or resource function: its statements, workers, and the clients and endpoints it calls. |
| `<function>.data-mapper.png` | The data mapper of a data-mapping function, such as `function f(A a) returns B => {...}`. |
| `service-<path>.service.png` | A service with its resources. |

The diagrams are the same ones the extension's "Visualize" code lenses open, for every function, method, service and resource function. Methods are named `Class.method`, and resources `service-<path>.<accessor>-<path>`.

The PNGs show the diagram as the extension draws it in a 1920×1200 window. Anything that doesn't fit is cut off. For sequence diagrams the `.svg` is the output of the extension's own download button, and it holds the whole diagram. It's large (a few MB), because the styles are inlined. It opens in any browser.

`index.html` shows them all on one page. `visualizations.json` lists each diagram, its kind and line, and any that could not be captured.

## How it runs

After a successful build, `bal_builder.py` does the following:

1. On first use, it builds two images and reuses them afterwards:
   - `bal-builder-visualizer:<template image>-<extension version>-<hash>` is the template's dev-container image with [code-server](https://github.com/coder/code-server) (`CODE_SERVER_VERSION`), the Ballerina extension from [Open VSX](https://open-vsx.org/extension/wso2/ballerina), and `driver/` installed. It is based on the template's image, so the extension uses the template's own Ballerina version.
   - `bal-builder-browser:playwright-<version>` is Microsoft's Playwright image with Playwright for Python.
2. It starts code-server on the project, in a container from the first image.
3. It runs `capture.py` in a container from the second image, which shares the first container's network. Chromium opens the VS Code page. The driver then lists the "Visualize" code lenses of each file, and opens each diagram with `ballerina.show.diagram`. `capture.py` waits until the drawing stops changing, takes a screenshot of the webview, and, for sequence diagrams, clicks the download button.
4. It copies the results to `DIR/visualizations/` and summarises them.

The extension version is the one the template's dev container installs. The builder finds `wso2.ballerina@X` in `.devcontainer/` (now `post_execution.py`), and falls back to `VISUALIZER_EXTENSION_VERSION`. Changing the version, `driver/`, or the template image builds a new visualizer image. `--rebuild-image` rebuilds both images.

A run with `--visualize` takes about a minute more: code-server and the language server start, then each diagram takes a few seconds. The first run also downloads code-server, the extension and the Playwright image (about 2 GB). If a diagram is not drawn within 90 seconds, it is reported as not captured, and the run fails like any other failed check.
