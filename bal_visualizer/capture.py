"""Capture the Ballerina VS Code extension's diagrams of a project, for bal_builder.py --visualize.

Runs with Playwright in a browser container that shares the network of the container running code-server
(a headless VS Code with the Ballerina extension and driver/ installed). For every .bal file it opens the
file's overview and each diagram the extension offers through its "Visualize" code lenses (functions,
methods, services and resource functions), grows the window until the whole diagram fits, and saves:
  - a PNG of the diagram as the extension shows it (a sequence diagram, a data mapper, a service, ...);
  - for sequence diagrams, also the SVG of the extension's own "download" button;
  - an HTML copy of the diagram's page that works offline: styles, fonts and images included, scripts left out.
It writes visualizations.json (what was captured, and what failed) and index.html (all of them on one page).

    python3 capture.py --out DIR [--workspace /workspace]

Exits 0 when the diagrams could be listed, even if some of them failed; visualizations.json says which.
"""

import argparse
import html
import json
import math
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

# The window grows until the whole diagram is inside the webview: what doesn't fit is cut off in the PNG, and in the
# SVG export, which covers the diagram's container rather than what is drawn in it.
FIT_PROBE = 200  # px the window grows by, to tell what is drawn from what sticks to the window's edges
FIT_MARGIN = 32  # px left free beyond the diagram
MAX_VIEWPORT = 16000  # px; Chromium can't take larger screenshots

# The boxes of what the diagram draws (text, SVG shapes, images), in the order of window.__balDrawn; with `remember`,
# that list is made first. Their containers are left out: many are sized to the window, so they always fill it.
DRAWN = r"""(remember) => {
    const root = document.querySelector('#diagram');
    const visible = (e) => {
        for (; e && e !== root; e = e.parentElement) {
            const s = getComputedStyle(e);
            if (s.visibility === 'hidden' || s.display === 'none' || s.opacity === '0') return false;
        }
        return true;
    };
    if (remember) {
        const drawn = [];
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        for (let t = walker.nextNode(); t; t = walker.nextNode()) if (t.textContent.trim()) drawn.push(t);
        drawn.push(...root.querySelectorAll('path, rect, line, circle, ellipse, polygon, polyline, image, use, img, canvas'));
        window.__balDrawn = drawn;
    }
    const range = document.createRange();
    return window.__balDrawn.map((node) => {
        const text = node.nodeType === Node.TEXT_NODE;
        if (!node.isConnected || !visible(text ? node.parentElement : node)) return null;
        if (text) range.selectNodeContents(node);
        const b = (text ? range : node).getBoundingClientRect();
        return b.width || b.height ? [b.right, b.bottom] : null;
    });
}"""

# A self-contained copy of the webview page, for viewing offline: its DOM without scripts or <base>, every CSS rule
# (also those added with insertRule, which aren't in any <style>'s text), and fonts and images as data: URLs. Sizes
# relative to the window (vh, vw) are frozen at their pixel values, so the copy shows the whole diagram in any window.
SNAPSHOT = r"""async (title) => {
    const vh = innerHeight / 100, vw = innerWidth / 100;
    const units = {vh, vw, vmin: Math.min(vh, vw), vmax: Math.max(vh, vw)};
    const freeze = (text) => text.replace(/(-?\d*\.?\d+)(vh|vw|vmin|vmax)\b/g, (m, n, unit) => `${+(n * units[unit]).toFixed(2)}px`);
    const loaded = new Map();
    const dataUrl = (url) => {
        if (!loaded.has(url)) {
            loaded.set(url, fetch(url)
                .then((r) => r.ok ? r.blob() : Promise.reject(r.status))
                .then((blob) => new Promise((ok, fail) => {
                    const reader = new FileReader();
                    reader.onload = () => ok(reader.result);
                    reader.onerror = fail;
                    reader.readAsDataURL(blob);
                }))
                .catch(() => 'data:,'));  // what the page itself can't load (a broken font URL) stays empty, as it is there
        }
        return loaded.get(url);
    };
    const URLS = /url\(\s*(['"]?)(.*?)\1\s*\)/g;
    const inline = async (css, base) => {
        const urls = await Promise.all([...css.matchAll(URLS)].map((m) =>
            m[2].startsWith('data:') || m[2].startsWith('#') ? m[2] : dataUrl(new URL(m[2], base).href)));
        let i = 0;
        return css.replace(URLS, () => `url("${urls[i++]}")`);
    };
    const css = [];
    for (const sheet of [...document.styleSheets, ...(document.adoptedStyleSheets || [])]) {
        let text = '';
        try {
            text = [...sheet.cssRules].map((rule) => rule.cssText).join('\n');
        } catch (e) {  // another origin's stylesheet
            if (sheet.href) text = await fetch(sheet.href).then((r) => r.text()).catch(() => '');
        }
        css.push(await inline(text, sheet.href || document.baseURI));
    }
    const page = document.documentElement.cloneNode(true);
    const canvases = document.querySelectorAll('canvas');  // a clone of a canvas is blank: keep its pixels as an image
    page.querySelectorAll('canvas').forEach((copy, i) => {
        const img = document.createElement('img');
        try { img.src = canvases[i].toDataURL(); } catch (e) {}
        img.style.cssText = copy.style.cssText;
        img.width = canvases[i].width;
        img.height = canvases[i].height;
        copy.replaceWith(img);
    });
    page.querySelectorAll('base, script, style, link[rel~="stylesheet"], link[rel="preload"], link[rel="modulepreload"], meta')
        .forEach((e) => e.remove());
    for (const img of page.querySelectorAll('img[src]')) {
        const src = img.getAttribute('src');
        if (!src.startsWith('data:')) img.setAttribute('src', await dataUrl(new URL(src, document.baseURI).href));
    }
    for (const image of page.querySelectorAll('image')) {
        for (const name of ['href', 'xlink:href']) {
            const href = image.getAttribute(name);
            if (href && !href.startsWith('#') && !href.startsWith('data:')) image.setAttribute(name, await dataUrl(new URL(href, document.baseURI).href));
        }
    }
    for (const e of page.querySelectorAll('[style]')) e.setAttribute('style', freeze(await inline(e.getAttribute('style'), document.baseURI)));
    for (const e of page.querySelectorAll('[width], [height]')) {
        for (const name of ['width', 'height']) if (e.hasAttribute(name)) e.setAttribute(name, freeze(e.getAttribute(name)));
    }
    const head = page.querySelector('head') || page.insertBefore(document.createElement('head'), page.firstChild);
    const charset = document.createElement('meta');
    charset.setAttribute('charset', 'utf-8');
    const name = document.createElement('title');
    name.textContent = title;
    const style = document.createElement('style');
    style.textContent = freeze(css.join('\n')) + `\nhtml { overflow: auto !important; }` +
        `\nhtml, body { min-width: ${innerWidth}px !important; min-height: ${innerHeight}px !important; }`;
    head.prepend(charset, name);
    head.append(style);
    return '<!DOCTYPE html>\n' + page.outerHTML + '\n';
}"""


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


def settle(frame: Frame) -> None:
    """Wait until the diagram in `frame` stops changing again, after the window was resized."""
    deadline = time.monotonic() + RENDER_TIMEOUT
    last, since = None, time.monotonic()
    while time.monotonic() < deadline:
        state = frame.evaluate(DIAGRAM_STATE)
        if state != last:
            last, since = state, time.monotonic()
        elif time.monotonic() - since >= STABLE_FOR:
            return
        time.sleep(0.5)
    raise RuntimeError(f"it did not settle within {RENDER_TIMEOUT}s of resizing the window")


def fit(page: Page, frame: Frame) -> bool:
    """Grow the window until the whole diagram is inside the webview; False if it is larger than MAX_VIEWPORT allows."""
    size = page.viewport_size
    before = frame.evaluate(DRAWN, True)
    page.set_viewport_size({"width": size["width"] + FIT_PROBE, "height": size["height"] + FIT_PROBE})
    settle(frame)
    after = frame.evaluate(DRAWN, False)
    # What moved or grew with the window's right or bottom edge sticks to it (zoom buttons, ...): it always fits.
    kept = [i for i, (a, b) in enumerate(zip(before, after)) if a and b and b[0] - a[0] < FIT_PROBE * 0.75 and b[1] - a[1] < FIT_PROBE * 0.75]
    for attempt in range(5):  # centred diagrams move as the window grows, so measure again until it stays the same
        boxes = frame.evaluate(DRAWN, False)
        right = max((boxes[i][0] for i in kept if boxes[i]), default=0)
        bottom = max((boxes[i][1] for i in kept if boxes[i]), default=0)
        width, height = frame.evaluate("() => [innerWidth, innerHeight]")  # the webview's, inside VS Code's window
        size = page.viewport_size
        needed = {
            "width": max(VIEWPORT["width"], size["width"] + math.ceil(right + FIT_MARGIN - width)),
            "height": max(VIEWPORT["height"], size["height"] + math.ceil(bottom + FIT_MARGIN - height)),
        }
        wanted = {key: min(MAX_VIEWPORT, value) for key, value in needed.items()}
        if wanted == size or attempt == 4:
            return wanted == needed and wanted == size
        page.set_viewport_size(wanted)
        settle(frame)
    return False


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
                page.set_viewport_size(VIEWPORT)
                driver.call("/open", {"fsPath": entry["fsPath"], "position": position})
                frame, kind = rendered(page, old)
                old = frame
                kind = "overview" if position is None else kind
                stem = "overview" if position is None else f"{slug(name)}.{kind}"
                if stem in used:
                    stem = f"{stem}.L{line}"
                used.add(stem)
                record["kind"] = kind
                if not fit(page, frame):
                    record["cut_off"] = f"larger than {MAX_VIEWPORT}px, so cut off at that size"
                    print(f"    {record['cut_off']}", flush=True)
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
                try:
                    title = f"{entry['file']}: {name} ({kind})"
                    (folder / f"{stem}.html").write_text(frame.evaluate(SNAPSHOT, title), encoding="utf-8")
                    record["html"] = f"{entry['file']}/{stem}.html"
                except PlaywrightError as exc:
                    record["html_error"] = f"the HTML copy failed: {exc}".splitlines()[0]
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
            if d.get("html"):
                caption += f' · <a href="{esc(d["html"])}">HTML</a>'
            body = f'<a href="{esc(d["png"])}"><img src="{esc(d["png"])}" alt="{esc(d["name"])}" loading="lazy"></a>' if d.get("png") else ""
            for problem in ("error", "cut_off", "html_error"):
                if d.get(problem):
                    body += f'<p class="error">{esc(d[problem])}</p>'
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
