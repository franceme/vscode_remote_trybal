"""Capture the Ballerina VS Code extension's diagrams of a project, for bal_builder.py --visualize.

Runs with Playwright in a browser container that shares the network of the container running code-server
(a headless VS Code with the Ballerina extension and driver/ installed). For every .bal file it opens the
file's overview and each diagram the extension offers through its "Visualize" code lenses (functions,
methods, services and resource functions), and saves:
  - a PNG of the diagram as the extension shows it (a sequence diagram, a data mapper, a service, ...);
  - for sequence diagrams, also the SVG of the extension's own "download" button (the whole diagram).
It writes visualizations.json (what was captured, and what failed) and index.html (all of them on one page).

    python3 capture.py --out DIR [--workspace /workspace]

Exits 0 when the diagrams could be listed, even if some of them failed; visualizations.json says which.
"""

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Page, sync_playwright

VIEWPORT = {"width": 1920, "height": 1200}
SERVER_TIMEOUT = 120  # seconds for code-server to answer ...
DRIVER_TIMEOUT = 180  # ... for the driver extension to start in the browser's VS Code window ...
TARGETS_TIMEOUT = 600  # ... for the language server to list what can be visualized ...
RENDER_TIMEOUT = 90  # ... and for one diagram to render
STABLE_FOR = 1.5  # seconds a diagram must stay unchanged to count as rendered

# What the open diagram is. The webview's #diagram holds a loader until the extension has drawn something.
DIAGRAM_STATE = """() => {
    const d = document.querySelector('#diagram');
    if (!d) return null;
    const errors = document.querySelector('#errors');
    if (errors) return {error: errors.innerText.trim()};
    if (d.querySelector('.loader')) return {loading: true};
    const text = d.innerText;
    let kind = 'diagram';
    if (d.querySelector('.lowcode-diagram')) kind = 'sequence';
    else if (/Data Mapper:/.test(text)) kind = 'data-mapper';
    else if (d.querySelector('.service-member') || /^\\s*Service\\b/m.test(text)) kind = 'service';
    return {kind, size: d.innerHTML.length};
}"""
SVG_EXPORT = ".tools > .zoom-control-wrapper:last-child"  # the sequence diagram's download button (html-to-image SVG)


class Driver:
    """The driver extension's HTTP API (driver/extension.js)."""

    def __init__(self, url: str) -> None:
        self.url = url

    def call(self, route: str, body: Optional[dict] = None, timeout: float = 300) -> dict:
        request = urllib.request.Request(self.url + route, data=json.dumps(body or {}).encode(), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{route}: {json.load(exc).get('error', exc)}") from None


def wait_for(what: str, seconds: float, attempt):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return attempt()
        except (OSError, RuntimeError, PlaywrightError) as exc:
            if time.monotonic() > deadline:
                raise RuntimeError(f"{what} did not answer within {seconds:.0f}s: {exc}") from None
            time.sleep(1)


def diagram_frame(page: Page, old: Optional[Frame]) -> Optional[Frame]:
    """The webview frame holding the extension's #diagram (not the one of the diagram shown before)."""
    for frame in page.frames:
        if frame is old or frame.is_detached():
            continue
        try:
            if frame.query_selector("#diagram"):
                return frame
        except PlaywrightError:  # navigated or detached meanwhile
            continue
    return None


def rendered(page: Page, old: Optional[Frame]) -> tuple:
    """Wait until a new diagram has been drawn and stopped changing; returns (frame, kind)."""
    deadline = time.monotonic() + RENDER_TIMEOUT
    frame, last, since = None, None, 0.0
    while time.monotonic() < deadline:
        frame = diagram_frame(page, old) or frame
        state = None
        if frame is not None:
            try:
                state = frame.evaluate(DIAGRAM_STATE)
            except PlaywrightError:
                frame = None
        if state and state.get("error"):
            raise RuntimeError(f"the extension could not draw it: {state['error']}")
        if state and state.get("kind"):
            if state != last:
                last, since = state, time.monotonic()
            elif time.monotonic() - since >= STABLE_FOR:
                return frame, state["kind"]
        time.sleep(0.5)
    raise RuntimeError(f"it was not drawn within {RENDER_TIMEOUT}s")


def label(item: dict, items: list) -> str:
    """A readable name for a visualize target from its first line: main, Counter.increment, service /api.get greeting."""
    header = item["header"]
    service = re.match(r"service\b\s*(.*?)\s+on\b", header)
    resource = re.search(r"\bresource\s+function\s+(\S+)\s+(.+?)\s*\(", header)  # the path may hold [string id]
    function = re.search(r"\bfunction\s+([^\s(]+)", header)
    if service:
        return f"service {service.group(1)}".strip()
    name = f"{resource.group(1)} {resource.group(2)}" if resource else function.group(1) if function else f"line {item['position']['startLine'] + 1}"
    start = item["position"]["startLine"]
    # Inside a service (which has its own target) use the service's name; inside a class, the class's.
    enclosing = [
        other for other in items
        if other is not item and other["position"]["startLine"] < start <= other["position"]["endLine"]
    ]
    if enclosing:
        return f"{label(enclosing[-1], items)}.{name}"
    return ".".join([*item.get("parents", []), name])


def slug(text: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9._-]+", "-", text)).strip("-.") or "diagram"


def capture(page: Page, driver: Driver, out: Path, workspace: str) -> dict:
    page.goto(f"http://127.0.0.1:8080/?folder={workspace}")
    page.wait_for_selector(".monaco-workbench", timeout=SERVER_TIMEOUT * 1000)
    versions = wait_for("The visualizer driver extension", DRIVER_TIMEOUT, lambda: driver.call("/health", timeout=30))
    print(f"VS Code {versions['vscode']}, Ballerina extension {versions['ballerina']}", flush=True)
    print("Listing what the Ballerina extension can visualize (starts its language server)", flush=True)
    files = driver.call("/targets", timeout=TARGETS_TIMEOUT)
    manifest = {"vscode": versions["vscode"], "extension": versions["ballerina"], "files": []}
    old = None
    for entry in files:
        folder = out / entry["file"]
        folder.mkdir(parents=True, exist_ok=True)
        jobs = [("overview", None, None)] + [(label(item, entry["items"]), item["position"], item) for item in entry["items"]]
        diagrams, used = [], set()
        for name, position, item in jobs:
            line = position["startLine"] + 1 if position else None
            record = {"name": name, "line": line}
            print(f"  {entry['file']}: {name}", flush=True)
            try:
                driver.call("/open", {"fsPath": entry["fsPath"], "position": position})
                frame, kind = rendered(page, old)
                old = frame
                kind = "overview" if position is None else kind
                stem = "overview" if position is None else f"{slug(name)}.{kind}"
                if stem in used:
                    stem = f"{stem}.L{line}"
                used.add(stem)
                record["kind"] = kind
                frame.locator("#diagram").screenshot(path=str(folder / f"{stem}.png"))
                record["png"] = f"{entry['file']}/{stem}.png"
                if kind == "sequence":
                    try:
                        with page.expect_download(timeout=60000) as download:
                            frame.click(SVG_EXPORT)
                        download.value.save_as(str(folder / f"{stem}.svg"))
                        record["svg"] = f"{entry['file']}/{stem}.svg"
                    except PlaywrightError as exc:
                        record["svg_error"] = f"the extension's SVG export failed: {exc}".splitlines()[0]
            except (RuntimeError, PlaywrightError, OSError) as exc:
                record["error"] = str(exc).splitlines()[0]
                print(f"    failed: {record['error']}", flush=True)
            diagrams.append(record)
        manifest["files"].append({"file": entry["file"], "diagrams": diagrams})
    return manifest


def index_html(manifest: dict) -> str:
    esc = html.escape
    sections = []
    for entry in manifest["files"]:
        figures = []
        for d in entry["diagrams"]:
            where = f" (line {d['line']})" if d.get("line") else ""
            caption = f"{esc(d['name'])}{esc(where)} <span>{esc(d.get('kind', ''))}</span>"
            if d.get("svg"):
                caption += f' · <a href="{esc(d["svg"])}">SVG</a>'
            body = f'<a href="{esc(d["png"])}"><img src="{esc(d["png"])}" alt="{esc(d["name"])}" loading="lazy"></a>' if d.get("png") else ""
            if d.get("error"):
                body += f'<p class="error">{esc(d["error"])}</p>'
            figures.append(f"<figure>{body}<figcaption>{caption}</figcaption></figure>")
        sections.append(f"<section><h2>{esc(entry['file'])}</h2>{''.join(figures)}</section>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ballerina diagrams</title>
<style>
  body {{ font: 15px/1.4 system-ui, sans-serif; margin: 0 auto; padding: 16px; max-width: 1400px; background: #fff; color: #1d1d1f; }}
  figure {{ margin: 0 0 32px; }}
  img {{ max-width: 100%; border: 1px solid #ddd; }}
  figcaption span {{ color: #666; }}
  .error {{ color: #b00020; }}
</style></head><body>
<h1>Ballerina diagrams</h1>
<p>Drawn by the Ballerina VS Code extension {esc(manifest['extension'])} in VS Code {esc(manifest['vscode'])}.</p>
{''.join(sections)}
</body></html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workspace", default="/workspace")
    parser.add_argument("--driver", default="http://127.0.0.1:8766")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    driver = Driver(args.driver)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_context(viewport=VIEWPORT, accept_downloads=True).new_page()
            wait_for("code-server", SERVER_TIMEOUT, lambda: urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=5).read())
            manifest = capture(page, driver, args.out, args.workspace)
        except (RuntimeError, PlaywrightError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            browser.close()
    (args.out / "visualizations.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (args.out / "index.html").write_text(index_html(manifest), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
