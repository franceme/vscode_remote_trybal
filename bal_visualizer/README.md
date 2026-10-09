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
| `<name>.html` | Each diagram's page, saved as one HTML file that works offline: it opens in any browser without VS Code or a network connection. |

The diagrams are the same ones the extension's "Visualize" code lenses open, for every function, method, service and resource function. Methods are named `Class.method`, and resources `service-<path>.<accessor>-<path>`.

Each file holds the whole diagram. The window starts at 1920×1200 and grows until everything the diagram draws fits, up to 16000 px each way. A larger diagram is cut off at that size, and `visualizations.json` and the summary say so.

- **PNG:** the diagram as the extension draws it.
- **SVG** (sequence diagrams): the output of the extension's own download button. It's large (5 to 20 MB), because the styles are inlined. The button saves the diagram's container, which is sized to the window, so a large diagram is complete only because the window has grown to fit it.
- **HTML:** a copy of the diagram's page (about 2.5 MB). It keeps the page's DOM and every CSS rule, with the fonts and images inlined as `data:` URLs, so it looks the same as the PNG. Scripts are left out, so it doesn't react to clicks or hovering. Sizes relative to the window are fixed at the captured size, so a smaller browser window scrolls instead of cutting the diagram off.

### The extension's own exports

The Ballerina extension (4.7.9) has no export commands in VS Code. Some diagram pages have a download button. Each one saves the page element that holds the diagram, at the size of its scroll area, with the [html-to-image](https://github.com/bubkoo/html-to-image) library:

| Diagram | Download button | Used here |
|---|---|---|
| Sequence diagram | SVG | yes, after the window has grown to fit |
| GraphQL, architecture, and persist (entity) diagrams | JPEG | no: the "Visualize" code lenses don't open them |
| Overview, data mapper, service | none | the PNG and HTML copy are the only exports |

`index.html` shows them all on one page. `visualizations.json` lists each diagram, its kind and line, and any that could not be captured.

## How it runs

After a successful build, `bal_builder.py` does the following:

1. On first use, it builds two images and reuses them afterwards:
   - `bal-builder-visualizer:<template image>-<extension version>-<hash>` is the template's dev-container image with [code-server](https://github.com/coder/code-server) (`CODE_SERVER_VERSION`), the Ballerina extension from [Open VSX](https://open-vsx.org/extension/wso2/ballerina), and `driver/` installed. It is based on the template's image, so the extension uses the template's own Ballerina version.
   - `bal-builder-browser:playwright-<version>` is Microsoft's Playwright image with Playwright for Python.
2. It starts code-server on the project, in a container from the first image.
3. It runs `capture.py` in a container from the second image, which shares the first container's network. Chromium opens the VS Code page. The driver then lists the "Visualize" code lenses of each file, and opens each diagram with `ballerina.show.diagram`. `capture.py` waits until the drawing stops changing and grows the window until the diagram fits. It skips what moves with the window's edges, such as the zoom buttons. Then it takes a screenshot of the webview, clicks the download button of sequence diagrams, and saves the page as HTML.
4. It copies the results to `DIR/visualizations/` and summarises them.

The extension version is the one the template's dev container installs. The builder finds `wso2.ballerina@X` in `.devcontainer/` (now `post_execution.py`), and falls back to `VISUALIZER_EXTENSION_VERSION`. Changing the version, `driver/`, or the template image builds a new visualizer image. `--rebuild-image` rebuilds both images.

A run with `--visualize` takes about a minute more: code-server and the language server start, then each diagram takes a few seconds. The first run also downloads code-server, the extension and the Playwright image (about 2 GB). If a diagram is not drawn within 90 seconds, it is reported as not captured, and the run fails like any other failed check.
