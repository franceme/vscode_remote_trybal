#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.10"]
# ///
"""Build a Ballerina file in a throwaway copy of a template project, inside the template's container.

One script, two front ends that share the same build code:

  CLI (Python 3.9+, needs nothing but Docker):
    python3 bal_builder.py compile       hello.bal -o out  # make build         (bal build), the .jar -> out/
    python3 bal_builder.py build-graalvm hello.bal -o out  # make build_graalvm (bal build --graalvm), native executable + .jar -> out/
    python3 bal_builder.py build-docker  hello.bal         # make build_docker  (bal build --cloud=docker), docker save -> ./hello.tar
    python3 bal_builder.py compile hello.bal --test --scan -o out  # also tests and security scans, reports -> out/
    python3 bal_builder.py compile hello.bal --complexity           # also complexity metrics per function
    python3 bal_builder.py run hello.bal a b                        # build, then run the program with a b

  Scripts: a .bal file whose first line is `#!/usr/bin/env -S bal_builder.py run [options] [-- args]`
  runs like a script (./hello.bal more args), with the line's args before those of the command line.

  MCP server over stdio, with the tools compile_ballerina, build_graalvm and build_docker
  (Python 3.10+ and the `mcp` package; `uv run` installs it from the metadata above):
    uv run bal_builder.py mcp
    claude mcp add ballerina-builder -- uv run /absolute/path/to/bal_builder.py mcp

Every build copies the template folder (default: the folder holding this script; or --template, or
$BAL_BUILDER_TEMPLATE) to a new temporary folder, replaces main.bal with the given file, starts a
container from the template's dev-container image (.devcontainer/), copies the project in, runs the
Makefile target and streams its output while it runs. The template is re-read on every build, so
changes to it apply straight away.

`// dependency: org/name:version` comments in the file pin the versions of the packages it uses: they are
locked in the copy's Dependencies.toml, `bal build --sticky` pulls them from Ballerina Central before the
Makefile target builds with them, and the build fails if it used another version (examples/main_dependencies.bal).

-o / --output DIR (MCP: output_dir) copies a build's outputs out of the container, so they outlive the
temporary copy: target/bin (the executable .jar, plus the native executable for build-graalvm),
build-docker's image .tar (saved as ./<file name>.tar without -o) and the reports of these checks:
  --test  `make test`: bal test with a test report and code coverage                  -> test-report/
  --scan  bal scan static analysis, run in a newer Ballerina image                     -> scan-report/
          and Trivy: vulnerabilities and a CycloneDX SBOM of the .jar or the image     -> trivy/
  --complexity  cyclomatic and cognitive complexity, nesting and size of each function,
          measured on the template's own Ballerina parser (bal_complexity/)          -> complexity/
  --visualize  the Ballerina VS Code extension's diagrams of each file, function and service
          (overview, sequence diagram, data mapper), drawn by the extension (bal_visualizer/) -> visualizations/
Their results are also summarised at the end of the run.

After a successful build the temporary folder and the container are removed. --keep (MCP: keep_temp)
leaves both in place and prints where they are. A failed or cancelled build always keeps the
temporary folder (project/, synced back from the container, and build.log) and prints its path; the
container is removed.
"""

import argparse
import codecs
import collections
import contextlib
import dataclasses
import fnmatch
import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import warnings
import zipfile
from pathlib import Path
from typing import Callable, Deque, Dict, Generator, List, Optional

SCRIPT_PATH = Path(__file__).resolve()
TEMPLATE_ENV = "BAL_BUILDER_TEMPLATE"
IMAGE_ENV = "BAL_BUILDER_IMAGE"

WORKDIR = "/workspace"  # where the project lives inside the build container
LABEL = "bal-builder"  # label on every container and template image this script creates

# capability -> Makefile target in the template
MAKE_TARGETS = {"compile": "build", "graalvm": "build_graalvm", "docker": "build_docker", "run": "build"}
RUN_PROGRAM = 'exec bal run target/bin/*.jar -- "$@"'  # run: the built jar, with the program's arguments

# --complexity: the analyser, next to this script (its README explains the rules and how to update them).
COMPLEXITY_DIR = SCRIPT_PATH.parent / "bal_complexity"
COMPLEXITY_SOURCE = COMPLEXITY_DIR / "BalComplexity.java"
# It runs with the template's parser jars on a full JDK, which Java's single-file launcher needs to compile
# it; the template's Ballerina ships only a JRE. The official Ballerina image (also used by --scan) has one.
COMPLEXITY_JDK_IMAGE = "ballerina/ballerina:2201.13.6"
COMPLEXITY_LISTED = 10  # most complex functions listed in the summary; complexity.txt has them all

# --visualize: the diagrams of the Ballerina VS Code extension, drawn by the extension itself in a headless VS Code
# (code-server) and captured with a browser (bal_visualizer/ explains how).
VISUALIZER_DIR = SCRIPT_PATH.parent / "bal_visualizer"
VISUALIZER_DRIVER = VISUALIZER_DIR / "driver"  # a small VS Code extension that opens each diagram
VISUALIZER_CAPTURE = VISUALIZER_DIR / "capture.py"  # runs with Playwright, saves the diagrams
# The extension version is the one the template's dev container installs (wso2.ballerina@X in .devcontainer/),
# so the diagrams look as they do there; this one when .devcontainer names none.
VISUALIZER_EXTENSION_VERSION = "4.7.9"
CODE_SERVER_VERSION = "4.139.1"
BROWSER_IMAGE = "mcr.microsoft.com/playwright/python:v1.55.0-noble"  # has the browsers, not the Python package ...
PLAYWRIGHT_VERSION = "1.55.0"  # ... which is installed at this, matching, version
VISUALIZER_DRIVER_PORT = 8766

# Never copied from the template: VCS data, build output and caches at any depth ...
EXCLUDE_EVERYWHERE = (".git", "target", "__pycache__", "*.pyc", ".DS_Store")
# ... and, at the top level, this script, its helpers and the places it writes its outputs by default.
EXCLUDE_AT_ROOT = (SCRIPT_PATH.name, COMPLEXITY_DIR.name, VISUALIZER_DIR.name, "dist", "*.tar")

# --scan: bal scan doesn't support the template's Ballerina 2201.7.1, so it runs in a newer official image.
SCAN_BALLERINA_IMAGE = "ballerina/ballerina:2201.13.6"
SCAN_TOOL = "scan:0.12.0"
TRIVY_IMAGE = "aquasec/trivy:0.74.0"
TRIVY_CACHE_VOLUME = f"{LABEL}-trivy-cache"  # Trivy's vulnerability databases, kept between runs

DOCKERD_TIMEOUT = 90  # seconds to wait for the Docker daemon inside the build container
TAIL_LINES = 150  # lines of build output kept for the result; MCP responses include them on failure ...
SUCCESS_TAIL_LINES = 20  # ... and only this many after a successful build
SCAN_ISSUES_LISTED = 50  # bal scan issues listed in the summary; the reports have them all
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


_RUNNING: Dict[str, "CancelToken"] = {}  # container -> cancel token, for builds still in progress
_RUNNING_LOCK = threading.Lock()


class BuildError(Exception):
    """A failure whose message is meant for the person or agent running the build."""


class Cancelled(BuildError):
    pass


class CancelToken:
    """Lets another thread stop a running build by killing the docker process it is waiting on."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._procs = set()

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            for proc in self._procs:
                if proc.poll() is None:
                    proc.kill()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled("The build was cancelled.")

    def attach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.add(proc)
            if self._event.is_set():
                proc.kill()

    def detach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)


@dataclasses.dataclass
class BuildRequest:
    mode: str  # "compile", "graalvm" or "docker"
    bal_file: Optional[Path] = None  # the .bal file that replaces main.bal ...
    source: Optional[str] = None  # ... or its source code (handy for MCP clients)
    template: Path = SCRIPT_PATH.parent
    image: Optional[str] = None  # use this image instead of the template's dev-container image
    rebuild_image: bool = False  # rebuild the template image (pulling its base) even if .devcontainer is unchanged
    keep: bool = False  # leave the temporary folder and the container in place
    output_dir: Optional[Path] = None  # copy target/bin, docker's image .tar and the check reports here
    tests: bool = False  # also run `make test` (bal test with a report and code coverage)
    test_files: List[Path] = dataclasses.field(default_factory=list)  # added to tests/ (implies tests)
    test_source: Optional[str] = None  # written to tests/main_test.bal (implies tests)
    scan: bool = False  # also run bal scan (static analysis) and Trivy (vulnerabilities, SBOM)
    complexity: bool = False  # also measure the complexity of each function
    max_complexity: Optional[int] = None  # fail when a function's cyclomatic complexity is higher (implies complexity)
    visualize: bool = False  # also save the VS Code extension's diagrams of each file and function (needs output_dir)
    program_args: List[str] = dataclasses.field(default_factory=list)  # run: the arguments of the program
    tar_path: Optional[Path] = None  # docker: set by _normalized to <output_dir or .>/<file stem>.tar
    dependencies: List["Dependency"] = dataclasses.field(default_factory=list)  # set by _normalized from `// dependency:` comments


@dataclasses.dataclass
class BuildResult:
    mode: str
    success: bool = False
    cancelled: bool = False
    message: str = ""
    problems: List[str] = dataclasses.field(default_factory=list)  # failed checks (the build itself worked)
    exit_code: Optional[int] = None
    failed_step: Optional[str] = None  # the command that failed, when it ran before the Makefile target
    seconds: float = 0.0
    artifacts: List[str] = dataclasses.field(default_factory=list)  # files written outside the temporary folder
    reports: List[str] = dataclasses.field(default_factory=list)  # report folders written to output_dir
    docker_image: Optional[str] = None
    image_tar: Optional[str] = None
    tests: Optional[dict] = None  # summary of bal test's test_results.json
    scan: Optional[dict] = None  # summary of bal scan's scan_results.json
    vulnerabilities: Optional[dict] = None  # summary of Trivy's report
    complexity: Optional[dict] = None  # summary of BalComplexity's report
    visualizations: Optional[dict] = None  # summary of the captured diagrams
    dependencies: Optional[List[list]] = None  # [package, declared version, version the build used] per `// dependency:`
    program_exit_code: Optional[int] = None  # run: the program's exit code, once it has run
    temp_dir: Optional[str] = None  # set when the temporary folder was left in place
    container: Optional[str] = None  # set when the container was left in place
    output_tail: str = ""


# --------------------------------------------------------------------------- output


class Reporter:
    """Receives a build's progress. The CLI prints raw output; the MCP server forwards lines."""

    def status(self, message: str) -> None:
        """A step taken by this script, e.g. 'Starting container ...'."""

    def output(self, text: str) -> None:
        """Build output exactly as it arrives (it may stop mid-line)."""

    def line(self, line: str) -> None:
        """The same build output, one complete line at a time, without ANSI escapes."""


class ConsoleReporter(Reporter):
    def __init__(self, stream=None) -> None:
        self._stream = stream or sys.stdout  # build output; `run` keeps stdout for the program's own output
        self._mid_line = False
        self._bold = sys.stderr.isatty() and "NO_COLOR" not in os.environ

    def status(self, message: str) -> None:
        if self._mid_line:
            self._stdout("\n")
            self._mid_line = False
        text = f"==> {message}"
        sys.stderr.write(f"\033[1m{text}\033[0m\n" if self._bold else f"{text}\n")
        sys.stderr.flush()

    def output(self, text: str) -> None:
        self._stdout(text)
        self._mid_line = not text.endswith(("\n", "\r"))

    def _stdout(self, text: str) -> None:
        try:
            self._stream.write(text)
            self._stream.flush()
        except BrokenPipeError:  # e.g. piped into `head`: drop further output, but finish and clean up
            os.dup2(os.open(os.devnull, os.O_WRONLY), self._stream.fileno())


class _Output:
    """Fans build output out to the reporter, build.log and the tail kept for the result."""

    def __init__(self, reporter: Reporter, log_path: Path) -> None:
        self.reporter = reporter
        self.log = open(log_path, "w", encoding="utf-8", errors="replace")
        self.tail: Deque[str] = collections.deque(maxlen=TAIL_LINES)
        self._partial = ""
        self._reporter_ok = True

    def _report(self, send, text: str) -> None:
        if self._reporter_ok:
            try:
                send(text)
            except OSError:  # e.g. the terminal went away: keep building, logging and cleaning up
                self._reporter_ok = False

    def status(self, message: str) -> None:
        self.end_stream()
        self.log.write(f"==> {message}\n")
        self.log.flush()
        self._report(self.reporter.status, message)

    def output(self, text: str) -> None:
        if not text:
            return
        self.log.write(text)
        self.log.flush()
        self._report(self.reporter.output, text)
        *complete, self._partial = (self._partial + text).split("\n")
        for line in complete:
            self._line(line)

    def end_stream(self) -> None:
        if self._partial:
            line, self._partial = self._partial, ""
            self._line(line)

    def _line(self, line: str) -> None:
        # For lines redrawn with '\r' (progress bars) keep what a terminal would end up showing.
        line = ANSI_ESCAPE.sub("", line.rstrip("\r").rsplit("\r", 1)[-1]).rstrip()
        self.tail.append(line)
        self._report(self.reporter.line, line)

    def close(self) -> None:
        self.end_stream()
        self.log.close()


# --------------------------------------------------------------------------- docker


class _Docker:
    """Thin wrapper around the docker CLI. Long-running commands stream into the build output."""

    def __init__(self, out: _Output, cancel: CancelToken) -> None:
        self.out = out
        self.cancel = cancel

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run a short docker command and capture its output."""
        proc = subprocess.run(
            ["docker", *args],
            stdin=subprocess.DEVNULL,  # in MCP mode stdin carries the protocol
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise BuildError(f"`docker {shlex.join(args)}` failed: {detail}")
        return proc

    def stream(self, *args: str, env: Optional[Dict[str, str]] = None, hide: Optional[Callable[[str], bool]] = None) -> int:
        """Run a docker command, passing its combined stdout/stderr on as it is produced.

        With `hide`, output is passed on a line at a time, leaving out the lines `hide` returns True for.
        """
        self.cancel.check()
        proc = subprocess.Popen(
            ["docker", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=dict(os.environ, **env) if env else None,
        )
        self.cancel.attach(proc)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        try:
            while True:
                chunk = os.read(proc.stdout.fileno(), 65536)  # returns as soon as anything arrives
                text = decoder.decode(chunk, final=not chunk)
                if hide is None:
                    self.out.output(text)
                else:
                    *lines, pending = (pending + text).split("\n")
                    if not chunk and pending:
                        lines.append(pending)
                    self.out.output("".join(f"{line}\n" for line in lines if not hide(line)))
                if not chunk:
                    break
            returncode = proc.wait()
        finally:
            self.cancel.detach(proc)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            self.out.end_stream()
        self.cancel.check()
        return returncode

    def run_program(self, container: str, args: List[str]) -> int:
        """Run the built program in the container, connected to this process's stdin, stdout and stderr."""
        self.cancel.check()
        tty = ["--tty"] if sys.stdin.isatty() and sys.stdout.isatty() else []  # then Ctrl-C reaches the program
        proc = subprocess.Popen(
            ["docker", "exec", "--interactive", *tty, "--workdir", WORKDIR, container, "sh", "-c", RUN_PROGRAM, "sh", *args],
            env=dict(os.environ, DOCKER_CLI_HINTS="false"),  # no "What's next" advert after the program's output
        )
        self.cancel.attach(proc)
        try:
            returncode = proc.wait()
        finally:
            self.cancel.detach(proc)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.cancel.check()
        return returncode

    def ensure_image(self, image: str) -> str:
        if self.run("image", "inspect", image, check=False).returncode != 0:
            self.out.status(f"Pulling {image}")
            if self.stream("pull", image) != 0:
                raise BuildError(f"Could not pull the image {image}.")
        return image

    def start(self, image: str, name: str, docker_in_docker: bool = False, volumes: tuple = (), network: Optional[str] = None) -> None:
        args = ["run", "--detach", "--init", "--name", name, "--label", f"{LABEL}=1"]
        for volume in volumes:
            args += ["--volume", volume]
        if network:
            args += ["--network", network]
        if docker_in_docker:
            # Keep the image's entrypoint: in the template image it starts dockerd, which needs privileges.
            args += ["--privileged", image, "sleep", "infinity"]
        else:
            args += ["--entrypoint", "sleep", image, "infinity"]
        self.run(*args)

    @contextlib.contextmanager
    def helper(self, image: str, kind: str, volumes: tuple = (), network: Optional[str] = None) -> Generator[str, None, None]:
        """A throwaway container for a check (bal scan, Trivy), removed afterwards whatever happens."""
        name = f"{LABEL}-{kind}-{uuid.uuid4().hex[:8]}"
        with _RUNNING_LOCK:
            _RUNNING[name] = self.cancel
        try:
            self.start(image, name, volumes=volumes, network=network)
            yield name
        finally:
            self.run("rm", "--force", "--volumes", name, check=False)
            with _RUNNING_LOCK:
                _RUNNING.pop(name, None)

    def copy_in(self, project: Path, container: str, image: str) -> None:
        self.run("cp", f"{project}{os.sep}.", f"{container}:{WORKDIR}")
        user = self.run("image", "inspect", "--format", "{{.Config.User}}", image).stdout.strip()
        if user and user.split(":")[0] not in ("root", "0"):
            # docker cp writes files as root; hand the workspace to the image's user.
            self.run("exec", "--user", "0", container, "chown", "-R", user, WORKDIR)

    def wait_for_daemon(self, container: str) -> None:
        self.out.status("Waiting for the Docker daemon inside the container")
        deadline = time.monotonic() + DOCKERD_TIMEOUT
        while True:
            self.cancel.check()
            proc = self.run("exec", container, "docker", "version", "--format", "{{.Server.Version}}", check=False)
            if proc.returncode == 0:
                self.out.status(f"Docker daemon {proc.stdout.strip()} is ready")
                return
            if time.monotonic() > deadline:
                raise BuildError(
                    f"The Docker daemon inside the build container did not start within {DOCKERD_TIMEOUT}s"
                    f" (the image's entrypoint is expected to start it): {proc.stderr.strip()}"
                )
            time.sleep(1)

    def copy_out(self, container: str, source_dir: str, dest: Path) -> List[str]:
        names = self.run("exec", container, "ls", "-1A", source_dir).stdout.splitlines()
        self.out.status(f"Downloading {source_dir} to {dest}")
        dest.mkdir(parents=True, exist_ok=True)
        self.run("cp", f"{container}:{source_dir}/.", str(dest))
        return [str(dest / name) for name in names]

    def built_image(self, container: str, output_lines: List[str]) -> str:
        """The image `bal build --cloud=docker` produced, as named in its 'docker run ...' hint."""
        for line in reversed(output_lines):
            match = re.match(r"\s*docker run\b.*\s(\S+)$", line)
            if match:
                return match.group(1)
        # Fallback: the inner daemon started empty, so the build's image is the one that is not a base image.
        listed = self.run("exec", container, "docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}").stdout.split()
        dockerfiles = self.run("exec", container, "sh", "-c", f"cat {WORKDIR}/target/docker/*/Dockerfile", check=False).stdout
        bases = set(re.findall(r"(?im)^\s*FROM\s+(?:--\S+\s+)*(\S+)", dockerfiles))
        candidates = [image for image in listed if image not in bases and "<none>" not in image]
        if len(candidates) == 1:
            return candidates[0]
        raise BuildError(f"Could not tell which image the build produced; images in the container: {', '.join(listed) or 'none'}.")

    def save(self, container: str, image: str, tar: Path) -> None:
        self.out.status(f"Saving image {image} to {tar}")
        tar.parent.mkdir(parents=True, exist_ok=True)
        partial = tar.with_name(tar.name + ".partial")
        try:
            with open(partial, "wb") as fh:
                proc = subprocess.Popen(
                    ["docker", "exec", container, "docker", "save", image],
                    stdin=subprocess.DEVNULL,
                    stdout=fh,
                    stderr=subprocess.PIPE,
                )
                self.cancel.attach(proc)
                try:
                    _, err = proc.communicate()
                finally:
                    self.cancel.detach(proc)
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
            if proc.returncode != 0:
                self.cancel.check()
                raise BuildError(f"`docker save {image}` failed: {err.decode(errors='replace').strip()}")
            os.replace(partial, tar)
        finally:
            if partial.exists():
                partial.unlink()
        self.out.status(f"Saved {tar} ({tar.stat().st_size / 1e6:.1f} MB)")


# --------------------------------------------------------------------------- template


def _excluded(name: str, patterns: tuple) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _ignore(root: Path, skip: frozenset, directory: str, names: List[str]) -> List[str]:
    patterns = EXCLUDE_EVERYWHERE + (EXCLUDE_AT_ROOT if Path(directory) == root else ())
    return [name for name in names if _excluded(name, patterns) or Path(directory, name) in skip]


def _copy_template(req: BuildRequest, project: Path, out: _Output) -> None:
    out.status(f"Copying template {req.template}")
    # Follow symlinks: the copy has to be self-contained inside the container, and writing to it must never
    # write through a link into the template. Skip the output folder too, in case it sits in the template.
    skip = frozenset([req.output_dir]) if req.output_dir is not None else frozenset()
    ignore = functools.partial(_ignore, req.template, skip)
    shutil.copytree(req.template, project, symlinks=False, ignore_dangling_symlinks=True, ignore=ignore)
    if req.source is not None:
        source = req.source
        out.status("Wrote the given source to main.bal")
    else:
        source = req.bal_file.read_text(encoding="utf-8")
        out.status(f"Replaced main.bal with {req.bal_file}")
    if source.startswith("#!"):
        # Ballerina reads `#` as documentation and rejects it before an import; a comment keeps the line numbers.
        source = "//" + source[2:]
        out.status("Turned the #! line into a // comment")
    (project / "main.bal").write_text(source, encoding="utf-8")
    tests = project / "tests"
    for test_file in req.test_files:
        tests.mkdir(exist_ok=True)
        shutil.copyfile(test_file, tests / test_file.name)
        out.status(f"Added {test_file} to tests/")
    if req.test_source is not None:
        tests.mkdir(exist_ok=True)
        (tests / "main_test.bal").write_text(req.test_source, encoding="utf-8")
        out.status("Wrote the given test source to tests/main_test.bal")


_JSONC_COMMENT = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.S)
_JSONC_TRAILING_COMMA = re.compile(r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])')


def _read_jsonc(path: Path) -> dict:
    """json.load for devcontainer.json, which allows comments and trailing commas."""

    def keep_strings(match: "re.Match") -> str:
        return match.group(0) if match.group(0).startswith('"') else ""

    text = _JSONC_COMMENT.sub(keep_strings, path.read_text(encoding="utf-8"))
    return json.loads(_JSONC_TRAILING_COMMA.sub(keep_strings, text))


def _template_image(req: BuildRequest, docker: _Docker, out: _Output) -> str:
    """The image to build in: --image, or the one the template's dev container uses."""
    if req.image:
        return docker.ensure_image(req.image)
    folder = req.template / ".devcontainer"
    config_path = folder / "devcontainer.json"
    config = _read_jsonc(config_path) if config_path.is_file() else {}
    if config.get("dockerComposeFile"):
        raise BuildError("Docker Compose dev containers are not supported; pass an image with --image.")
    if config.get("image"):
        return docker.ensure_image(config["image"])

    # Same defaults as dev containers: dockerfile and context are relative to devcontainer.json.
    build = config.get("build") or {}
    dockerfile = folder / (build.get("dockerfile") or config.get("dockerFile") or "Dockerfile")
    context = folder / (build.get("context") or config.get("context") or ".")
    if not dockerfile.is_file():
        raise BuildError(f"The template has no dev-container image ({dockerfile} is missing); pass one with --image.")
    name = re.sub(r"[^a-z0-9]+", "-", req.template.name.lower()).strip("-") or "template"
    tag = f"{LABEL}-{name}:{hashlib.sha1(str(req.template).encode()).hexdigest()[:10]}"

    # Rebuild only when the dev-container definition changed: every `docker build` makes a new image ID.
    digest = hashlib.sha256(json.dumps([build.get("args"), build.get("target")]).encode())
    context_files = (
        p for p in context.rglob("*") if p.is_file() and not any(_excluded(part, EXCLUDE_EVERYWHERE) for part in p.relative_to(context).parts)
    )
    for path in sorted({dockerfile, *context_files}):
        digest.update(os.path.relpath(path, context).encode() + b"\0" + path.read_bytes())
    inputs = digest.hexdigest()[:16]
    built = docker.run("image", "inspect", "--format", f'{{{{index .Config.Labels "{LABEL}.inputs"}}}}', tag, check=False)
    if not req.rebuild_image and built.returncode == 0 and built.stdout.strip() == inputs:
        out.status(f"Using the template image {tag} (.devcontainer unchanged since it was built)")
        return tag

    args = ["build", "--tag", tag, "--file", str(dockerfile), "--label", f"{LABEL}.template={req.template}"]
    args += ["--label", f"{LABEL}.inputs={inputs}"] + (["--pull"] if req.rebuild_image else [])
    for key, value in (build.get("args") or {}).items():
        args += ["--build-arg", f"{key}={value}"]
    if build.get("target"):
        args += ["--target", build["target"]]
    out.status(f"Building the template image {tag} from {dockerfile}")
    if docker.stream(*args, str(context), env={"BUILDKIT_PROGRESS": "plain"}) != 0:
        raise BuildError(f"Building the template image from {dockerfile} failed.")
    return tag


# --------------------------------------------------------------------------- dependencies (// dependency: comments)


@dataclasses.dataclass(frozen=True)
class Dependency:
    """A package version pinned with a `// dependency: org/name:version` comment in the built file."""

    org: str
    name: str
    version: str
    modules: tuple = ()  # the modules of the package that the file imports, e.g. ("toml",) or ("aws.s3",)


DEPENDENCY_COMMENT = re.compile(r"^[ \t]*//[ \t]*dependency:(.*)$", re.M)
DEPENDENCY_SPEC = re.compile(r"\s*([A-Za-z0-9_]+)/([A-Za-z0-9_.]+):(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)\s*")
IMPORT = re.compile(r"^[ \t]*import[ \t]+([A-Za-z0-9_]+)/([A-Za-z0-9_.]+)", re.M)


def _declared_dependencies(source: str, source_name: str) -> List[Dependency]:
    """The versions pinned by the file's `// dependency:` comments, each with the modules of it the file imports."""
    declared: Dict[tuple, Dependency] = {}
    for match in DEPENDENCY_COMMENT.finditer(source):
        where = f"{source_name}:{source.count(chr(10), 0, match.start()) + 1}"
        spec = DEPENDENCY_SPEC.fullmatch(match.group(1))
        if spec is None:
            raise BuildError(
                f"{where}: `{match.group(0).strip()}` should read `// dependency: org/name:version`,"
                " for example `// dependency: ballerina/toml:0.3.0`."
            )
        org, name, version = spec.groups()
        earlier = declared.get((org, name))
        if earlier is not None and earlier.version != version:
            raise BuildError(f"{where}: {org}/{name} is declared twice, as {earlier.version} and {version}.")
        declared[(org, name)] = Dependency(org, name, version)
    modules = collections.defaultdict(list)
    for org, module in sorted(set(IMPORT.findall(source))):
        # `import ballerinax/aws.s3` is package aws.s3 if that is declared, else module s3 of a declared aws.
        owners = [dep for dep in declared.values() if dep.org == org and (module == dep.name or module.startswith(dep.name + "."))]
        if owners:
            modules[max(owners, key=lambda dep: len(dep.name))].append(module)
    return [dataclasses.replace(dep, modules=tuple(modules[dep])) for dep in declared.values()]


_LOCK_TABLES = re.compile(r"(?m)^(?=\[\[package\]\])")


def _toml_value(text: str, key: str) -> Optional[str]:
    """The string `key = "..."` of a TOML table written the way Ballerina writes them, one key per line."""
    match = re.search(rf'(?m)^{re.escape(key)}\s*=\s*"([^"]*)"', text)
    return match.group(1) if match else None


def _toml_array(text: str, key: str) -> str:
    """The items of a `key = [ ... ]` array written over several lines, as Ballerina writes them."""
    match = re.search(rf"(?ms)^{re.escape(key)}\s*=\s*\[\n(.*?)^\]", text)
    return match.group(1) if match else ""


def _lock_entries(lock: str) -> Dict[tuple, str]:
    """The [[package]] tables of a Dependencies.toml, by (org, name)."""
    return {(_toml_value(table, "org") or "", _toml_value(table, "name") or ""): table.strip() for table in _LOCK_TABLES.split(lock)[1:]}


def _package_table(org: str, name: str, version: str, dependencies=(), modules=()) -> str:
    lines = ["[[package]]", f'org = "{org}"', f'name = "{name}"', f'version = "{version}"']
    if dependencies:
        lines += ["dependencies = [", ",\n".join(f'\t{{org = "{o}", name = "{n}"}}' for o, n in sorted(dependencies)), "]"]
    if modules:
        lines += ["modules = [", ",\n".join(f'\t{{org = "{org}", packageName = "{name}", moduleName = "{m}"}}' for m in modules), "]"]
    return "\n".join(lines)


def _dependencies_toml(lock: str, package: tuple, distribution: str, source_name: str, deps: List[Dependency]) -> str:
    """The template's Dependencies.toml with the declared versions locked in it.

    Ballerina only honours a locked version when the lock lists the package being built (with the dependency among
    its own when the code imports it), names the modules imported from the dependency, and was written for the
    running distribution; otherwise it resolves the newest compatible version.
    """
    org, name, version = package
    entries = _lock_entries(lock)
    root = entries.pop((org, name), "")  # the template's lock is its own only if it lists the package
    if not root:
        entries = {}  # another package's lock (or none at all), which Ballerina ignores
    root_dependencies = set(re.findall(r'\{org = "([^"]+)", name = "([^"]+)"\}', _toml_array(root, "dependencies")))
    root_modules = re.findall(r'moduleName = "([^"]+)"', _toml_array(root, "modules"))
    for dep in deps:
        entries[(dep.org, dep.name)] = _package_table(dep.org, dep.name, dep.version, modules=dep.modules)
        if dep.modules:
            root_dependencies.add((dep.org, dep.name))
    entries[(org, name)] = _package_table(org, name, version, root_dependencies, root_modules)
    header = (
        f"# Written by bal_builder.py, to lock the versions pinned by the `// dependency:` comments of {source_name}."
        f"\n\n[ballerina]\ndependencies-toml-version = \"2\"\ndistribution-version = \"{distribution}\""
    )
    return "\n\n".join([header, *(entries[key] for key in sorted(entries))]) + "\n"


def _lock_dependencies(req: BuildRequest, docker: _Docker, out: _Output, container: str, project: Path) -> None:
    """Lock the declared versions in the copy's Dependencies.toml, for the distribution in the build container."""
    manifest = re.search(r"(?ms)^\[package\][ \t]*\n(.*?)(?=^\[|\Z)", (project / "Ballerina.toml").read_text(encoding="utf-8"))
    package = tuple(_toml_value(manifest.group(1), key) if manifest else None for key in ("org", "name", "version"))
    if not all(package):
        raise BuildError("Pinning dependencies needs the org, name and version of [package] in the template's Ballerina.toml.")
    found = re.search(r"Ballerina (\d+\.\d+\.\d+)", docker.run("exec", container, "bal", "version").stdout)
    if found is None:
        raise BuildError("`bal version` in the build container did not name a Ballerina distribution.")
    path = project / "Dependencies.toml"
    lock = path.read_text(encoding="utf-8") if path.is_file() else ""
    if lock and (package[0], package[1]) not in _lock_entries(lock):
        out.status(f"The template's Dependencies.toml does not list {package[0]}/{package[1]}, so Ballerina ignores it; starting a new one")
    source_name = req.bal_file.name if req.bal_file is not None else "the given source"
    path.write_text(_dependencies_toml(lock, package, found.group(1), source_name, req.dependencies), encoding="utf-8")
    pinned = ", ".join(f"{dep.org}/{dep.name}:{dep.version}" for dep in req.dependencies)
    out.status(f"Locked the declared dependencies in Dependencies.toml (Ballerina {found.group(1)}): {pinned}")


def _check_dependencies(req: BuildRequest, docker: _Docker, container: str, result: BuildResult) -> Optional[str]:
    """Whether the build used each declared version, as its Dependencies.toml records."""
    lock = docker.run("exec", container, "cat", f"{WORKDIR}/Dependencies.toml").stdout
    used = {key: _toml_value(table, "version") for key, table in _lock_entries(lock).items()}
    result.dependencies = [[f"{dep.org}/{dep.name}", dep.version, used.get((dep.org, dep.name))] for dep in req.dependencies]
    wrong = [package for package, declared, version in result.dependencies if version != declared]
    if wrong:
        return f"the build did not use the declared version of {', '.join(wrong)}"
    return None


# --------------------------------------------------------------------------- build


def _normalized(req: BuildRequest) -> BuildRequest:
    """Absolute paths and filled-in defaults; raises BuildError for requests that cannot work."""
    if req.mode not in MAKE_TARGETS:
        raise BuildError(f"Unknown mode {req.mode!r}; expected one of {', '.join(MAKE_TARGETS)}.")
    if (req.bal_file is None) == (req.source is None):
        raise BuildError("Give either a .bal file or its source code (exactly one of them).")
    template = req.template.expanduser().resolve()
    if not (template / "Ballerina.toml").is_file():
        raise BuildError(f"The template {template} is not a Ballerina package (it has no Ballerina.toml).")
    bal_file = req.bal_file.expanduser().resolve() if req.bal_file is not None else None
    if bal_file is not None and not bal_file.is_file():
        raise BuildError(f"No such file: {bal_file}")
    stem = bal_file.stem if bal_file is not None else "main"
    if bal_file is not None:
        dependencies = _declared_dependencies(bal_file.read_text(encoding="utf-8"), str(req.bal_file))
    else:
        dependencies = _declared_dependencies(req.source, "source")
    test_files = [Path(path).expanduser().resolve() for path in req.test_files]
    for path in test_files:
        if not path.is_file():
            raise BuildError(f"No such test file: {path}")
    output_dir = req.output_dir.expanduser().resolve() if req.output_dir is not None else None
    if output_dir is not None and output_dir.exists() and not output_dir.is_dir():
        raise BuildError(f"The output path {output_dir} exists and is not a folder.")
    if req.max_complexity is not None and req.max_complexity < 1:
        raise BuildError("The complexity limit must be 1 or more.")
    if (req.complexity or req.max_complexity is not None) and not COMPLEXITY_SOURCE.is_file():
        raise BuildError(f"The complexity analyser {COMPLEXITY_SOURCE} is missing.")
    if req.visualize and output_dir is None:
        raise BuildError("The diagrams are saved in the output folder: pass one with -o DIR (MCP: output_dir).")
    if req.visualize and not (VISUALIZER_CAPTURE.is_file() and (VISUALIZER_DRIVER / "package.json").is_file()):
        raise BuildError(f"The visualizer in {VISUALIZER_DIR} is missing.")
    return dataclasses.replace(
        req,
        template=template,
        bal_file=bal_file,
        output_dir=output_dir,
        tests=req.tests or bool(test_files) or req.test_source is not None,
        test_files=test_files,
        complexity=req.complexity or req.max_complexity is not None,
        tar_path=(output_dir or Path.cwd()) / f"{stem}.tar" if req.mode == "docker" else None,
        dependencies=dependencies,
    )


def _check_docker() -> None:
    if shutil.which("docker") is None:
        raise BuildError("The docker CLI was not found on PATH.")
    proc = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise BuildError(f"Cannot reach the Docker daemon (is Docker running?): {proc.stderr.strip()}")


def _failure_hint(mode: str, tail: Deque[str]) -> str:
    out_of_memory = ("Error 137", "ran out of memory")  # killed by the kernel, or native-image's own message
    if mode == "graalvm" and any(any(text in line for text in out_of_memory) or line.strip() == "Killed" for line in tail):
        return "native-image ran out of memory: give Docker more memory (or stop other builds) and retry."
    return ""


# --------------------------------------------------------------------------- checks (--test, --scan, --complexity)

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")  # Trivy's, most severe first


def _checked(name: str, check: Callable[[], Optional[str]]) -> List[str]:
    """Run one check. A check that fails, or cannot run, becomes a problem instead of ending the build."""
    try:
        problem = check()
    except Cancelled:
        raise
    except (BuildError, OSError, ValueError) as exc:  # ValueError: results that are not valid JSON
        problem = f"{name} could not run: {exc}"
    return [problem] if problem else []


def _bin_dir(docker: _Docker, container: str, tmp: Path) -> Path:
    """target/bin, copied out of the build container once, into the temporary folder."""
    bin_dir = tmp / "bin"
    if not bin_dir.exists():
        docker.copy_out(container, f"{WORKDIR}/target/bin", bin_dir)
    return bin_dir


def _failure_summary(message: str) -> str:
    """One line from a bal test failure: its message, plus the expected and actual values of a failed assertion."""
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    if not lines:
        return ""
    first = re.sub(r'^error \{[^}]*\}\w*\s*\(?"?', "", lines[0])  # drop `error {ballerina/test:0}TestError ("`
    values = [re.sub(r"\s+:", ":", line) for line in lines if re.match(r"(expected|actual)\s*:", line)]
    return first + (f" ({', '.join(values)})" if values else "")


def _run_tests(docker: _Docker, out: _Output, container: str, reports: Path, result: BuildResult) -> Optional[str]:
    out.status("Running `make test` (bal test with a test report and code coverage)")
    returncode = docker.stream("exec", "--workdir", WORKDIR, container, "make", "test")
    raw = docker.run("exec", container, "cat", f"{WORKDIR}/target/report/test_results.json", check=False)
    if raw.returncode != 0:  # no results: there are no tests, or they did not compile
        if returncode == 0:
            result.tests = {"total": 0}
            return None
        return f"`make test` failed with exit code {returncode}"
    docker.run("cp", f"{container}:{WORKDIR}/target/report/.", str(reports / "test-report"))
    data = json.loads(raw.stdout)
    result.tests = {
        "total": data.get("totalTests", 0),
        "passed": data.get("passed", 0),
        "failed": data.get("failed", 0),
        "skipped": data.get("skipped", 0),
        "coverage": data.get("coveragePercentage"),
        "covered_lines": data.get("coveredLines", 0),
        "missed_lines": data.get("missedLines", 0),
        "failures": [
            [test.get("name"), _failure_summary(test.get("failureMessage") or "")]
            for module in data.get("moduleStatus", [])
            for test in module.get("tests", [])
            if test.get("status") == "FAILURE"
        ],
    }
    if result.tests["failed"]:
        return f"{result.tests['failed']} of {result.tests['total']} tests failed"
    return f"`make test` failed with exit code {returncode}" if returncode != 0 else None


def _hide_json_dump() -> Callable[[str], bool]:
    """A `hide` for bal scan, which prints its whole result: JSON from a '[' line to a ']' line, SARIF from '{' to '}'."""
    closing = None  # the unindented bracket that ends the dump being hidden

    def hide(line: str) -> bool:
        nonlocal closing
        if closing is None and line.rstrip() in ("[", "{"):
            closing = "]" if line.rstrip() == "[" else "}"
        if closing is not None:
            if line.rstrip() == closing:
                closing = None
            return True
        return False

    return hide


def _scan_summary(issues: list) -> dict:
    """bal scan's issues, most severe first, and the rules they break."""
    listed, rules = [], {}
    for issue in issues:
        location, rule = issue.get("location", {}), issue.get("rule", {})
        rules[rule.get("id")] = {"description": rule.get("description"), "help": rule.get("helpUri")}
        listed.append(
            {
                "file": location.get("filePath") or issue.get("fileName"),
                "line": location.get("startLine", -1) + 1,  # 0-based in the results
                "column": location.get("startColumn", -1) + 1,
                "severity": rule.get("severity"),
                "kind": rule.get("ruleKind"),
                "rule_id": rule.get("id"),
                "rule": rule.get("name"),
            }
        )
    listed.sort(key=lambda issue: (_severity_rank(issue["severity"]), issue["file"] or "", issue["line"], issue["column"]))
    return {"tool": SCAN_TOOL.replace(":", " "), "issues": listed, "rules": rules}


def _severity_rank(severity: Optional[str]) -> int:
    return SEVERITIES.index(severity) if severity in SEVERITIES else len(SEVERITIES)


def _scan_image(req: BuildRequest, docker: _Docker, out: _Output, tmp: Path) -> str:
    """SCAN_BALLERINA_IMAGE with SCAN_TOOL installed, built on first use (or with --rebuild-image)."""
    tag = f"{LABEL}-scan:{SCAN_BALLERINA_IMAGE.rsplit(':', 1)[-1]}-{SCAN_TOOL.replace(':', '-')}"
    if not req.rebuild_image and docker.run("image", "inspect", tag, check=False).returncode == 0:
        return tag
    context = tmp / "scan-image"
    context.mkdir(exist_ok=True)
    (context / "Dockerfile").write_text(f"FROM {SCAN_BALLERINA_IMAGE}\nRUN bal tool pull {SCAN_TOOL}\n", encoding="utf-8")
    out.status(f"Building the scan image {tag}: {SCAN_BALLERINA_IMAGE} with bal tool pull {SCAN_TOOL}")
    args = ["build", "--tag", tag, "--label", f"{LABEL}.scan=1"] + (["--pull"] if req.rebuild_image else [])
    if docker.stream(*args, str(context), env={"BUILDKIT_PROGRESS": "plain"}) != 0:
        raise BuildError(f"building the scan image {tag} failed")
    return tag


def _bal_scan(req: BuildRequest, docker: _Docker, out: _Output, project: Path, reports: Path, tmp: Path, result: BuildResult) -> Optional[str]:
    image = _scan_image(req, docker, out, tmp)
    with docker.helper(image, "scan") as container:
        docker.copy_in(project, container, image)
        out.status(f"Running bal scan (static analysis, {SCAN_TOOL}) in {SCAN_BALLERINA_IMAGE}")
        returncode = docker.stream("exec", "--workdir", WORKDIR, container, "bal", "scan", "--scan-report", hide=_hide_json_dump())
        raw = docker.run("exec", container, "cat", f"{WORKDIR}/target/report/scan_results.json", check=False)
        if raw.returncode != 0:
            return f"bal scan failed with exit code {returncode}"
        result.scan = _scan_summary(json.loads(raw.stdout))
        docker.run("cp", f"{container}:{WORKDIR}/target/report/.", str(reports / "scan-report"))
        # bal scan writes one format per run: JSON (and the HTML report) above, SARIF for code scanning in CI here.
        out.status("Running bal scan --format=sarif for a SARIF report")
        returncode = docker.stream(
            "exec", "--workdir", WORKDIR, container, "bal", "scan", "--scan-report", "--format=sarif", hide=_hide_json_dump()
        )
        sarif = f"{container}:{WORKDIR}/target/report/scan_results.sarif"
        if docker.run("cp", sarif, str(reports / "scan-report"), check=False).returncode != 0:
            return f"bal scan --format=sarif wrote no SARIF report (exit code {returncode})"
    return None


def _trivy(req: BuildRequest, docker: _Docker, out: _Output, container: str, reports: Path, tmp: Path, result: BuildResult) -> Optional[str]:
    if req.mode == "docker":  # the image: its OS packages and the jars in it
        source, label, target = req.tar_path, req.tar_path.name, ["image", "--input", f"/scan/{req.tar_path.name}"]
    else:  # target/bin: the libraries packed into the executable jar
        source, label, target = _bin_dir(docker, container, tmp), "target/bin", ["rootfs", "/scan"]
    # Ballerina images always contain jars; target/bin holds the executable .jar unless the Makefile changed.
    expects_jar = req.mode == "docker" or any(path.suffix == ".jar" for path in source.iterdir())
    quiet = ["--disable-telemetry", "--skip-version-check"]  # no usage data sent to Aqua, no update notices
    docker.ensure_image(TRIVY_IMAGE)
    with docker.helper(TRIVY_IMAGE, "trivy", volumes=(f"{TRIVY_CACHE_VOLUME}:/root/.cache",)) as trivy:
        docker.run("exec", trivy, "mkdir", "-p", "/scan", "/report")
        docker.run("cp", f"{source}{os.sep}." if source.is_dir() else str(source), f"{trivy}:/scan")
        out.status(f"Running Trivy on {label} (vulnerabilities and an SBOM; its first run downloads its databases)")
        for attempt in range(2):
            returncode = docker.stream(
                "exec", trivy, "trivy", *target, "--scanners", "vuln", "--timeout", "15m",
                "--format", "json", "--output", "/report/vulnerabilities.json", *quiet,
            )
            if returncode != 0:
                return f"Trivy failed with exit code {returncode}"
            data = json.loads(docker.run("exec", trivy, "cat", "/report/vulnerabilities.json").stdout)
            results = data.get("Results") or []
            packages = sum(len(item.get("Packages") or []) for item in results)
            if packages and (not expects_jar or any(item.get("Type") == "jar" for item in results)):
                break
            # Trivy skips a jar silently (logging only with --debug) when its cached Java database is damaged,
            # as seen with a download that left `database disk image is malformed` pages. Get a fresh one.
            if attempt == 0:
                out.status("Trivy analysed no .jar files, so its Java database is probably damaged; clearing it and scanning again (about a 1 GB download)")
                docker.run("exec", trivy, "trivy", "clean", "--java-db")
        else:
            return f"Trivy could not analyse the .jar files in {label}" if expects_jar else f"Trivy found no packages to check in {label}"
        for format_, name in (("table", "vulnerabilities.txt"), ("cyclonedx", "sbom.cdx.json")):  # from the JSON, no rescan
            docker.run("exec", trivy, "trivy", "convert", "--quiet", "--format", format_, "--output", f"/report/{name}", "/report/vulnerabilities.json")
        docker.run("cp", f"{trivy}:/report/.", str(reports / "trivy"))
    found = [vuln for item in data.get("Results") or [] for vuln in item.get("Vulnerabilities") or []]
    found.sort(key=lambda vuln: SEVERITIES.index(vuln.get("Severity")) if vuln.get("Severity") in SEVERITIES else len(SEVERITIES))
    counts = collections.Counter(vuln.get("Severity", "UNKNOWN") for vuln in found)
    result.vulnerabilities = {
        "target": label,
        "packages": packages,
        "counts": {severity: counts[severity] for severity in SEVERITIES if counts[severity]},
        "top": [
            [vuln.get("VulnerabilityID"), vuln.get("Severity"), vuln.get("PkgName"), vuln.get("InstalledVersion"), vuln.get("FixedVersion")]
            for vuln in found[:10]
        ],
    }
    return None


def _complexity(req: BuildRequest, docker: _Docker, out: _Output, container: str, project: Path, reports: Path, tmp: Path, result: BuildResult) -> Optional[str]:
    # The parser of the Ballerina version the build used, so the analyser reads the code as the compiler does.
    lib = 'lib="$(bal home)/bre/lib" && ls "$lib"/ballerina-parser-*.jar "$lib"/ballerina-tools-api-*.jar'
    found = docker.run("exec", container, "sh", "-c", lib, check=False)
    jars = found.stdout.split()
    version = re.search(r"ballerina-parser-(.+)\.jar$", jars[0]) if jars else None
    if found.returncode != 0 or len(jars) != 2 or version is None:
        return "the complexity analysis could not find Ballerina's parser (`bal home`/bre/lib/ballerina-parser-*.jar) in the build image"
    analyser = tmp / "complexity"
    analyser.mkdir()
    for jar in jars:
        docker.run("cp", f"{container}:{jar}", str(analyser))
    shutil.copy2(COMPLEXITY_SOURCE, analyser)
    docker.ensure_image(COMPLEXITY_JDK_IMAGE)
    with docker.helper(COMPLEXITY_JDK_IMAGE, "complexity") as helper:
        docker.copy_in(project, helper, COMPLEXITY_JDK_IMAGE)
        docker.run("cp", f"{analyser}{os.sep}.", f"{helper}:/tmp/complexity")
        out.status(f"Measuring complexity with the Ballerina {version.group(1)} parser ({COMPLEXITY_SOURCE.name})")
        returncode = docker.stream(
            "exec", "--workdir", WORKDIR, helper,
            "java", "-cp", "/tmp/complexity/*", f"/tmp/complexity/{COMPLEXITY_SOURCE.name}", WORKDIR, "/tmp/complexity.json",
        )
        raw = docker.run("exec", helper, "cat", "/tmp/complexity.json", check=False)
    if returncode != 0 or raw.returncode != 0:
        return f"the complexity analyser failed with exit code {returncode}"
    data = dict(ballerina=version.group(1), **json.loads(raw.stdout))

    functions = [
        dict(fn, file=item["file"], function=f"{fn['container']}.{fn['name']}" if fn["container"] else fn["name"])
        for item in data["files"]
        for fn in item["functions"]
    ]
    functions.sort(key=lambda fn: (-fn["cyclomatic"], -fn["cognitive"], fn["file"], fn["line"]))
    limit = req.max_complexity
    over = [fn for fn in functions if limit is not None and fn["cyclomatic"] > limit]
    mismatches = [  # from `// complexity-expect:` comments, as in bal_complexity/fixture.bal
        f"{fn['file']}:{fn['line']} {fn['function']}: {measure} is {fn.get(measure, 'not measured')}, expected {expected}"
        for fn in sorted(functions, key=lambda fn: (fn["file"], fn["line"]))
        for measure, expected in fn["expected"].items()
        if fn.get(measure) != expected
    ]
    result.complexity = {
        "ballerina": data["ballerina"],
        "files": len(data["files"]),
        "syntax_errors": sum(item["syntaxErrors"] for item in data["files"]),
        "limit": limit,
        "functions": functions,
        "over": len(over),
        "mismatches": mismatches,
    }
    folder = reports / "complexity"
    folder.mkdir()
    (folder / "complexity.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    (folder / "complexity.txt").write_text(_complexity_table(data, functions, limit), encoding="utf-8")

    problems = []
    if over:
        problems.append(f"{_plural(len(over), 'function is', 'functions are')} over the complexity limit of {limit}")
    if mismatches:
        problems.append(f"{_plural(len(mismatches), 'complexity expectation')} not met")
    return "; ".join(problems) or None


def _complexity_table(data: dict, functions: List[dict], limit: Optional[int]) -> str:
    """complexity.txt: every function, most complex first."""
    lines = [
        f"Complexity of {_plural(len(functions), 'function')} in {_plural(len(data['files']), 'file')}, measured with the"
        f" Ballerina {data['ballerina']} parser (BalComplexity rules version {data['rulesVersion']}).",
        "cyclomatic: McCabe's count of paths; cognitive: SonarSource's Cognitive Complexity; nesting: deepest block;"
        " lines: lines of code. See bal_complexity/README.md for how each is counted.",
        "",
        f"{'cyclomatic':>10}  {'cognitive':>9}  {'nesting':>7}  {'lines':>5}  {'params':>6}  function (location)",
    ]
    for fn in functions:
        flag = f"  over the limit of {limit}" if limit is not None and fn["cyclomatic"] > limit else ""
        lines.append(
            f"{fn['cyclomatic']:>10}  {fn['cognitive']:>9}  {fn['nesting']:>7}  {fn['lines']:>5}  {fn['parameters']:>6}"
            f"  {fn['function']} ({fn['file']}:{fn['line']}){flag}"
        )
    return "\n".join(lines) + "\n"


def _extension_version(req: BuildRequest) -> str:
    """The Ballerina extension version the template's dev container installs (wso2.ballerina@X in .devcontainer/)."""
    folder = req.template / ".devcontainer"
    for path in sorted(folder.rglob("*")) if folder.is_dir() else ():
        if path.is_file() and path.stat().st_size < 1_000_000:
            match = re.search(r"wso2\.ballerina@([0-9][\w.-]*)", path.read_text(encoding="utf-8", errors="replace"))
            if match:
                return match.group(1)
    return VISUALIZER_EXTENSION_VERSION


def _pack_vsix(folder: Path, dest: Path) -> None:
    """Package a VS Code extension folder as a .vsix (a zip with a manifest), so code-server can install it."""
    package = json.loads((folder / "package.json").read_text(encoding="utf-8"))
    manifest = f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="{package['name']}" Version="{package['version']}" Publisher="{package['publisher']}"/>
    <DisplayName>{package['displayName']}</DisplayName>
    <Properties><Property Id="Microsoft.VisualStudio.Code.Engine" Value="{package['engines']['vscode']}"/></Properties>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
  <Dependencies/>
  <Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/></Assets>
</PackageManifest>
"""
    content_types = (
        '<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension=".json" ContentType="application/json"/><Default Extension=".js" ContentType="application/javascript"/>'
        '<Default Extension=".vsixmanifest" ContentType="text/xml"/></Types>'
    )
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as vsix:
        vsix.writestr("extension.vsixmanifest", manifest)
        vsix.writestr("[Content_Types].xml", content_types)
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                vsix.write(path, "extension/" + path.relative_to(folder).as_posix())


# The headless VS Code's user settings: no telemetry, update checks, AI panels or welcome pages in the way.
VISUALIZER_SETTINGS = {
    "ballerina.enableTelemetry": False,
    "ballerina.codeLens.all.enabled": True,
    "telemetry.telemetryLevel": "off",
    "security.workspace.trust.enabled": False,
    "workbench.startupEditor": "none",
    "workbench.tips.enabled": False,
    "workbench.enableExperiments": False,
    "workbench.colorTheme": "Default Light Modern",
    "workbench.secondarySideBar.defaultVisibility": "hidden",
    "chat.disableAIFeatures": True,
    "remote.autoForwardPorts": False,  # else a toast announces the driver's port, over the diagram
    "extensions.autoCheckUpdates": False,
    "extensions.autoUpdate": False,
    "update.mode": "none",
}
VISUALIZER_HOME = "/opt/bal-visualizer"  # code-server's extensions and user data in the visualizer image


def _visualizer_image(req: BuildRequest, docker: _Docker, out: _Output, tmp: Path, base: str, version: str) -> str:
    """The template image plus code-server, the Ballerina extension and the driver, built on first use."""
    base_id = docker.run("image", "inspect", "--format", "{{.Id}}", base).stdout.strip().split(":")[-1]
    digest = hashlib.sha256(json.dumps([CODE_SERVER_VERSION, VISUALIZER_SETTINGS]).encode())
    for path in sorted(VISUALIZER_DRIVER.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(VISUALIZER_DRIVER).as_posix().encode() + b"\0" + path.read_bytes())
    tag = f"{LABEL}-visualizer:{base_id[:12]}-{version}-{digest.hexdigest()[:8]}"
    if not req.rebuild_image and docker.run("image", "inspect", tag, check=False).returncode == 0:
        return tag
    context = tmp / "visualizer-image"
    context.mkdir(exist_ok=True)
    _pack_vsix(VISUALIZER_DRIVER, context / "driver.vsix")
    (context / "settings.json").write_text(json.dumps(VISUALIZER_SETTINGS, indent=2) + "\n", encoding="utf-8")
    vsix = f"https://open-vsx.org/api/wso2/ballerina/{version}/file/wso2.ballerina-{version}.vsix"
    code_server = f"https://github.com/coder/code-server/releases/download/v{CODE_SERVER_VERSION}/code-server-{CODE_SERVER_VERSION}-linux"
    (context / "Dockerfile").write_text(
        f"""FROM {base}
USER root
RUN set -e; case "$(uname -m)" in x86_64) arch=amd64;; aarch64|arm64) arch=arm64;; *) echo "no code-server for $(uname -m)"; exit 1;; esac; \\
    curl -fsSL "{code_server}-$arch.tar.gz" | tar -xz -C /opt; \\
    ln -sf /opt/code-server-{CODE_SERVER_VERSION}-linux-$arch/bin/code-server /usr/local/bin/code-server; \\
    curl -fsSL -o /tmp/wso2.ballerina.vsix "{vsix}"
COPY driver.vsix settings.json /tmp/bal-visualizer/
RUN code-server --extensions-dir {VISUALIZER_HOME}/extensions \\
        --install-extension /tmp/wso2.ballerina.vsix --install-extension /tmp/bal-visualizer/driver.vsix \\
 && mkdir -p {VISUALIZER_HOME}/data/User && mv /tmp/bal-visualizer/settings.json {VISUALIZER_HOME}/data/User/ \\
 && rm -rf /tmp/wso2.ballerina.vsix /tmp/bal-visualizer && chmod -R a+rwX {VISUALIZER_HOME}
""",
        encoding="utf-8",
    )
    out.status(f"Building the visualizer image {tag}: code-server {CODE_SERVER_VERSION} and the Ballerina extension {version} on {base}")
    args = ["build", "--tag", tag, "--label", f"{LABEL}.visualizer=1"] + (["--pull"] if req.rebuild_image else [])
    if docker.stream(*args, str(context), env={"BUILDKIT_PROGRESS": "plain"}) != 0:
        raise BuildError(f"building the visualizer image {tag} failed")
    return tag


def _browser_image(req: BuildRequest, docker: _Docker, out: _Output, tmp: Path) -> str:
    """BROWSER_IMAGE with Playwright's Python package, built on first use."""
    tag = f"{LABEL}-browser:playwright-{PLAYWRIGHT_VERSION}"
    if not req.rebuild_image and docker.run("image", "inspect", tag, check=False).returncode == 0:
        return tag
    context = tmp / "browser-image"
    context.mkdir(exist_ok=True)
    (context / "Dockerfile").write_text(
        f"FROM {BROWSER_IMAGE}\nRUN pip install --no-cache-dir --break-system-packages playwright=={PLAYWRIGHT_VERSION}\n", encoding="utf-8"
    )
    out.status(f"Building the browser image {tag}: {BROWSER_IMAGE} with Playwright for Python")
    args = ["build", "--tag", tag, "--label", f"{LABEL}.browser=1"] + (["--pull"] if req.rebuild_image else [])
    if docker.stream(*args, str(context), env={"BUILDKIT_PROGRESS": "plain"}) != 0:
        raise BuildError(f"building the browser image {tag} failed")
    return tag


def _visualize(req: BuildRequest, docker: _Docker, out: _Output, project: Path, reports: Path, tmp: Path, base: str, result: BuildResult) -> Optional[str]:
    version = _extension_version(req)
    image = _visualizer_image(req, docker, out, tmp, base, version)
    browser_image = _browser_image(req, docker, out, tmp)
    folder = reports / "visualizations"
    code_server = [
        "code-server", "--auth", "none", "--bind-addr", "127.0.0.1:8080", "--disable-telemetry", "--disable-update-check",
        "--disable-workspace-trust", "--disable-getting-started-override",
        "--user-data-dir", f"{VISUALIZER_HOME}/data", "--extensions-dir", f"{VISUALIZER_HOME}/extensions", WORKDIR,
    ]
    with docker.helper(image, "visualizer") as editor:
        docker.copy_in(project, editor, image)
        out.status(f"Starting a headless VS Code (code-server {CODE_SERVER_VERSION}) with the Ballerina extension {version}")
        docker.run(
            "exec", "--detach", "--env", f"BAL_VISUALIZER_PORT={VISUALIZER_DRIVER_PORT}", editor,
            "sh", "-c", f"{shlex.join(code_server)} > /tmp/code-server.log 2>&1",
        )
        # The browser shares the editor's network, so code-server and the driver are on its 127.0.0.1.
        with docker.helper(browser_image, "browser", network=f"container:{editor}") as browser:
            docker.run("cp", str(VISUALIZER_CAPTURE), f"{browser}:/tmp/capture.py")
            out.status("Capturing the diagrams the extension draws for each file, function and service")
            returncode = docker.stream(
                "exec", browser, "python3", "/tmp/capture.py", "--out", "/tmp/visualizations", "--workspace", WORKDIR,
                "--driver", f"http://127.0.0.1:{VISUALIZER_DRIVER_PORT}",
            )
            if returncode == 0:
                docker.run("cp", f"{browser}:/tmp/visualizations/.", str(folder))
        if returncode != 0:
            log = docker.run("exec", editor, "tail", "-n", "40", "/tmp/code-server.log", check=False).stdout
            if log.strip():
                out.status("The end of code-server's log")
                out.output(log)
            return f"capturing the diagrams failed with exit code {returncode}"
    manifest = json.loads((folder / "visualizations.json").read_text(encoding="utf-8"))
    diagrams = [dict(diagram, file=entry["file"]) for entry in manifest["files"] for diagram in entry["diagrams"]]
    failed = [d for d in diagrams if d.get("error")]
    result.visualizations = {
        "extension": manifest["extension"],
        "vscode": manifest["vscode"],
        "files": len(manifest["files"]),
        "kinds": collections.Counter(d["kind"] for d in diagrams if not d.get("error")),
        "svgs": sum(1 for d in diagrams if d.get("svg")),
        "failed": [f"{d['file']}: {d['name']}: {d['error']}" for d in failed],
        "svg_failed": [f"{d['file']}: {d['name']}: {d['svg_error']}" for d in diagrams if d.get("svg_error")],
    }
    return f"{_plural(len(failed), 'diagram')} could not be captured" if failed else None


def _copy_outputs(req: BuildRequest, docker: _Docker, out: _Output, container: str, reports: Path, tmp: Path, result: BuildResult) -> None:
    out.status(f"Copying the outputs to {req.output_dir}")
    req.output_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(_bin_dir(docker, container, tmp).iterdir()):
        shutil.copy2(path, req.output_dir / path.name)
        result.artifacts.append(str(req.output_dir / path.name))
    for report in sorted(reports.iterdir()):
        shutil.copytree(report, req.output_dir / report.name, dirs_exist_ok=True)
        result.reports.append(str(req.output_dir / report.name))


def run_build(req: BuildRequest, reporter: Reporter, cancel: Optional[CancelToken] = None) -> BuildResult:
    """Run one build. Raises BuildError if it cannot start; any later failure is in the result."""
    req = _normalized(req)
    _check_docker()
    cancel = cancel or CancelToken()
    started = time.monotonic()
    result = BuildResult(mode=req.mode)
    tmp = Path(tempfile.mkdtemp(prefix=f"bal-builder-{req.mode}-"))
    project = tmp / "project"
    out = _Output(reporter, tmp / "build.log")
    docker = _Docker(out, cancel)
    container = None
    running = False  # run: the program has started
    target = MAKE_TARGETS[req.mode]
    try:
        out.status(f"Temporary folder: {tmp}")
        _copy_template(req, project, out)
        image = _template_image(req, docker, out)
        container = f"{LABEL}-{req.mode}-{uuid.uuid4().hex[:8]}"
        with _RUNNING_LOCK:
            _RUNNING[container] = cancel
        out.status(f"Starting container {container} from {image}")
        docker.start(image, container, docker_in_docker=req.mode == "docker")
        if req.dependencies:
            _lock_dependencies(req, docker, out, container, project)
        docker.copy_in(project, container, image)
        if req.mode == "docker":
            docker.wait_for_daemon(container)

        out.tail.clear()  # the result reports the build itself, not the image preparation
        if req.dependencies:
            # `bal build` alone would move to the newest compatible versions. A sticky build pulls the locked ones, and
            # Ballerina keeps a package's resolution for 24 hours after it built it, so `make` builds with them too.
            out.status("Resolving the declared dependencies with `bal build --sticky`, which keeps the locked versions")
            result.exit_code = docker.stream("exec", "--workdir", WORKDIR, container, "bal", "build", "--sticky")
            if result.exit_code != 0:
                result.failed_step = "bal build --sticky"
                result.message = (
                    f"It resolves the versions pinned with `// dependency:` comments, before `make {target}`. Ballerina"
                    " reports a version that Ballerina Central does not have as `cannot resolve module`."
                )
        if not result.exit_code:
            out.status(f"Running `make {target}` in {container}:{WORKDIR}")
            result.exit_code = docker.stream("exec", "--workdir", WORKDIR, container, "make", target)
            if result.exit_code != 0:
                result.message = _failure_hint(req.mode, out.tail)
        if result.exit_code == 0:
            if req.mode == "docker":
                result.docker_image = docker.built_image(container, list(out.tail))
                docker.save(container, result.docker_image, req.tar_path)
                result.image_tar = str(req.tar_path)
            reports = tmp / "reports"
            reports.mkdir()
            if req.dependencies:
                result.problems += _checked("The dependency check", lambda: _check_dependencies(req, docker, container, result))
            if req.scan:
                result.problems += _checked("bal scan", lambda: _bal_scan(req, docker, out, project, reports, tmp, result))
                result.problems += _checked("Trivy", lambda: _trivy(req, docker, out, container, reports, tmp, result))
            if req.complexity:
                result.problems += _checked(
                    "The complexity analysis", lambda: _complexity(req, docker, out, container, project, reports, tmp, result)
                )
            if req.visualize:
                result.problems += _checked(
                    "The visualization", lambda: _visualize(req, docker, out, project, reports, tmp, image, result)
                )
            if req.tests:  # last, so that the end of the output shows how the tests went
                result.problems += _checked("make test", lambda: _run_tests(docker, out, container, reports, result))
            if req.output_dir is not None:
                _copy_outputs(req, docker, out, container, reports, tmp, result)
            if result.image_tar:
                result.artifacts.append(result.image_tar)
            result.success = not result.problems
            if req.mode == "run" and result.success:  # the program runs only after a build whose checks passed
                out.status(f"Running the program with {_plural(len(req.program_args), 'argument')}: {shlex.join(req.program_args)}")
                running = True
                result.program_exit_code = docker.run_program(container, req.program_args)
    except Cancelled as exc:
        result.cancelled, result.message = True, str(exc)
    except KeyboardInterrupt:
        result.cancelled, result.message = True, "The program was interrupted." if running else "The build was interrupted."
    except (BuildError, OSError) as exc:
        result.message = str(exc)
    finally:
        keep_folder = req.keep or not result.success
        if container is not None:
            if keep_folder and not result.cancelled:  # let the kept folder show what the build left (target/, ...)
                docker.run("cp", f"{container}:{WORKDIR}/.", str(project), check=False)
            if req.keep:
                result.container = container
            else:
                out.status(f"Removing container {container}")
                docker.run("rm", "--force", "--volumes", container, check=False)
            with _RUNNING_LOCK:
                _RUNNING.pop(container, None)
        result.seconds = time.monotonic() - started
        result.output_tail = "\n".join(out.tail).strip("\n")
        if keep_folder:
            result.temp_dir = str(tmp)
            out.close()
        else:
            out.status(f"Removing temporary folder {tmp}")
            out.close()
            shutil.rmtree(tmp, ignore_errors=True)
    return result


def _duration(seconds: float) -> str:
    return f"{seconds:.1f}s" if seconds < 60 else f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


def _plural(count: int, singular: str, plural: Optional[str] = None) -> str:
    return f"{count} {singular if count == 1 else plural or singular + 's'}"


def _check_lines(result: BuildResult) -> List[str]:
    """Summaries of the dependency check and of the --test, --scan and --complexity results."""
    lines = []
    if result.dependencies is not None:
        pinned = [f"{package} {declared}" for package, declared, used in result.dependencies if used == declared]
        others = len(result.dependencies) - len(pinned)
        lines.append(
            f"Dependencies (// dependency: comments): {', '.join(pinned) or 'none'} as declared" + (f"; {others} not." if others else ".")
        )
        for package, declared, used in result.dependencies:
            if used is None:
                lines.append(f"  {package}: declared {declared}, but the build does not use it (nothing imports it, directly or through another package)")
            elif used != declared:
                lines.append(f"  {package}: declared {declared}, but the build used {used}")
    tests = result.tests
    if tests is not None and not tests["total"]:
        lines.append("Tests: none found (add them to the template's tests/ folder, or pass --test-file).")
    elif tests is not None:
        coverage = ""
        if tests["coverage"] is not None:
            total = tests["covered_lines"] + tests["missed_lines"]
            coverage = f"; line coverage {tests['coverage']}% ({tests['covered_lines']} of {total} lines)"
        lines.append(f"Tests: {tests['passed']} passed, {tests['failed']} failed, {tests['skipped']} skipped{coverage}.")
        lines += [f"  failed {name}: {message}" for name, message in tests["failures"]]
    if result.scan is not None:
        issues = result.scan["issues"]
        names = {"CODE_SMELL": ("code smell", None), "BUG": ("bug", None), "VULNERABILITY": ("vulnerability", "vulnerabilities")}
        kinds = collections.Counter(issue["kind"] for issue in issues).most_common()
        severities = collections.Counter(issue["severity"] for issue in issues)
        by_kind = ", ".join(_plural(count, *names.get(kind, (str(kind).lower(), None))) for kind, count in kinds)
        by_severity = ", ".join(f"{severities[s]} {str(s).lower()}" for s in sorted(severities, key=_severity_rank))
        detail = f" ({by_kind}; {by_severity})" if issues else ""
        lines.append(f"Static analysis (bal {result.scan['tool']}): {_plural(len(issues), 'issue')}{detail}.")
        shown = issues[:SCAN_ISSUES_LISTED]
        lines += [f"  {i['file']}:{i['line']}:{i['column']}  {i['severity']}  {i['rule_id']}  {i['rule']}" for i in shown]
        if len(issues) > len(shown):
            lines.append(f"  ... and {len(issues) - len(shown)} more in scan-report/scan_results.json")
        for rule_id in dict.fromkeys(issue["rule_id"] for issue in shown):  # each rule once, in listing order
            rule = result.scan["rules"].get(rule_id, {})
            lines.append(f"  {rule_id}: {rule.get('description') or ''} {rule.get('help') or ''}".rstrip())
    if result.vulnerabilities is not None:
        counts = result.vulnerabilities["counts"]
        detail = ", ".join(f"{count} {severity.lower()}" for severity, count in counts.items())
        packages = _plural(result.vulnerabilities["packages"], "package")
        lines.append(f"Vulnerabilities (Trivy, {packages} in {result.vulnerabilities['target']}): {detail or 'none found'}.")
        for vuln_id, severity, package, installed, fixed in result.vulnerabilities["top"]:
            lines.append(f"  {vuln_id}  {severity}  {package} {installed}" + (f", fixed in {fixed}" if fixed else ""))
        if sum(counts.values()) > len(result.vulnerabilities["top"]):
            lines.append(f"  ... and {sum(counts.values()) - len(result.vulnerabilities['top'])} more in trivy/vulnerabilities.txt")
    if result.complexity is not None:
        cx = result.complexity
        functions = cx["functions"]
        where = f"{_plural(len(functions), 'function')} in {_plural(cx['files'], 'file')}"
        if functions:
            average = sum(fn["cyclomatic"] for fn in functions) / len(functions)
            detail = (
                f"cyclomatic highest {functions[0]['cyclomatic']}, average {average:.1f}; cognitive highest"
                f" {max(fn['cognitive'] for fn in functions)}; deepest nesting {max(fn['nesting'] for fn in functions)}"
            )
            if cx["limit"] is not None:
                detail += f"; {cx['over'] or 'none'} over the limit of {cx['limit']}"
        else:
            detail = "nothing to measure"
        lines.append(f"Complexity (Ballerina {cx['ballerina']} parser, {where}): {detail}.")
        if cx["syntax_errors"]:
            lines.append(f"  the parser reported {_plural(cx['syntax_errors'], 'syntax error')}, so some values may be off")
        shown = functions[:COMPLEXITY_LISTED]
        for fn in shown:
            flag = ", over the limit" if cx["limit"] is not None and fn["cyclomatic"] > cx["limit"] else ""
            lines.append(
                f"  {fn['file']}:{fn['line']}  {fn['function']}  cyclomatic {fn['cyclomatic']}, cognitive {fn['cognitive']},"
                f" nesting {fn['nesting']}, {_plural(fn['lines'], 'line')}{flag}"
            )
        if len(functions) > len(shown):
            lines.append(f"  ... and {len(functions) - len(shown)} more in complexity/complexity.txt")
        lines += [f"  expectation not met: {mismatch}" for mismatch in cx["mismatches"]]
    if result.visualizations is not None:
        viz = result.visualizations
        names = {"overview": ("overview", None), "sequence": ("sequence diagram", None), "data-mapper": ("data mapper", None),
                 "service": ("service", None), "diagram": ("other diagram", None)}
        kinds = ", ".join(_plural(count, *names.get(kind, (kind, None))) for kind, count in viz["kinds"].most_common())
        svgs = f"; {_plural(viz['svgs'], 'SVG')} from the extension's export" if viz["svgs"] else ""
        lines.append(
            f"Visualizations (Ballerina extension {viz['extension']} in VS Code {viz['vscode']}, {_plural(viz['files'], 'file')}):"
            f" {kinds or 'nothing to draw'}{svgs}."
        )
        lines += [f"  not captured: {failure}" for failure in viz["failed"]]
        lines += [f"  PNG only: {failure}" for failure in viz["svg_failed"]]
    return lines


def format_result(result: BuildResult, output_lines: int = 0) -> str:
    if result.cancelled:
        verdict = "was cancelled"
    elif result.success or (result.problems and not result.message):
        verdict = "succeeded"  # the build did, even when a check failed
    else:
        verdict = "failed"
    exit_code = f" (exit code {result.exit_code})" if result.exit_code else ""
    problems = f", but {'; '.join(result.problems)}" if result.problems else ""
    step = result.failed_step or f"make {MAKE_TARGETS[result.mode]}"
    lines = [f"`{step}` {verdict} after {_duration(result.seconds)}{exit_code}{problems}."]
    if result.message:
        lines.append(result.message)
    if result.program_exit_code is not None:
        lines.append(f"The program exited with code {result.program_exit_code}.")
    lines += _check_lines(result)
    if result.docker_image:
        lines.append(f"Docker image: {result.docker_image}")
    lines += [f"Output: {path}" for path in result.artifacts]
    for path in result.reports:
        index = Path(path, "index.html")
        lines.append(f"Report: {index if index.is_file() else path}")
    jars = [path for path in result.artifacts if path.endswith(".jar")]
    if jars:
        lines.append(f"Run the jar with: java -jar {shlex.quote(jars[0])}")
    if result.image_tar:
        lines.append(f"Load the image with: docker load -i {shlex.quote(result.image_tar)}")
    cleanup = []
    if result.container:
        lines.append(f"Container kept: {result.container}  (shell: docker exec -it {result.container} bash)")
        cleanup.append(f"docker rm -fv {result.container}")
    if result.temp_dir:
        lines.append(f"Temporary folder kept: {result.temp_dir}  (project/ and build.log)")
        cleanup.append(f"rm -rf {shlex.quote(result.temp_dir)}")
    if cleanup:
        lines.append("Remove with: " + " && ".join(cleanup))
    else:
        lines.append("The temporary folder and the container were removed.")
    tail = result.output_tail.split("\n")[-output_lines:] if output_lines and result.output_tail else []
    if tail:
        lines += ["", f"--- last {len(tail)} lines of build output ---", *tail]
    return "\n".join(lines)


# --------------------------------------------------------------------------- MCP


def serve_mcp(template: Path, image: Optional[str], rebuild_image: bool = False) -> int:
    """Serve the build capabilities as MCP tools over stdio. Never write to stdout here."""
    if sys.version_info < (3, 10):
        sys.exit("MCP mode needs Python 3.10+ with the `mcp` package (or run: uv run bal_builder.py mcp).")
    try:
        from mcp.server.mcpserver import Context, MCPServer as Server  # mcp 2.x
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError:
        try:
            from mcp.server.fastmcp import Context, FastMCP as Server  # mcp 1.x
            from mcp.server.fastmcp.exceptions import ToolError
        except ImportError:
            sys.exit("MCP mode needs the `mcp` package: pip install mcp (or run: uv run bal_builder.py mcp).")
    import anyio
    import anyio.from_thread
    import anyio.to_thread
    from typing import Annotated

    from pydantic import Field

    # Log notifications are deprecated in newer MCP revisions but still the only stream some clients show.
    warnings.filterwarnings("ignore", message=".*logging capability is deprecated.*")

    def stop_running_builds(signum: int, frame: object) -> None:
        # Clients send SIGTERM shortly after closing stdin; don't leave build containers running.
        with _RUNNING_LOCK:
            running = dict(_RUNNING)
        for container, cancel in running.items():
            cancel.cancel()
            subprocess.run(
                ["docker", "rm", "--force", "--volumes", container],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        os._exit(128 + signum)

    signal.signal(signal.SIGTERM, stop_running_builds)

    class McpReporter(Reporter):
        """Sends each line of build output to the client while the tool call is still running."""

        def __init__(self, ctx: Context) -> None:
            self.ctx = ctx
            self.sent = 0
            self.logging = True

        def status(self, message: str) -> None:
            self._send(f"==> {message}")

        def line(self, line: str) -> None:
            if line.strip():
                self._send(line)

        def _send(self, text: str) -> None:  # called from the build's worker thread
            self.sent += 1
            try:
                anyio.from_thread.run(self._notify, self.sent, text)
            except Exception:
                pass  # the request is gone (cancelled or disconnected); the build still cleans up

        async def _notify(self, count: int, text: str) -> None:
            try:
                await self.ctx.report_progress(count, None, text)
            except Exception:
                pass
            if self.logging:
                try:
                    await self.ctx.log("info", text, logger_name=LABEL)
                except Exception:
                    self.logging = False

    async def run_tool(ctx: Context, req: BuildRequest) -> str:
        cancel = CancelToken()
        try:
            result = await anyio.to_thread.run_sync(run_build, req, McpReporter(ctx), cancel, abandon_on_cancel=True)
        except BuildError as exc:
            raise ToolError(str(exc)) from exc
        except anyio.get_cancelled_exc_class():
            cancel.cancel()  # the abandoned worker thread stops the build and removes the container
            raise
        if not result.success:
            raise ToolError(format_result(result, output_lines=TAIL_LINES))
        return format_result(result, output_lines=SUCCESS_TAIL_LINES)

    pending_rebuild = [rebuild_image]  # `mcp --rebuild-image` applies to the first build only

    def request(
        mode: str,
        bal_file: Optional[str],
        source: Optional[str],
        output_dir: Optional[str],
        run_tests: bool,
        test_files: Optional[List[str]],
        test_source: Optional[str],
        security_scan: bool,
        complexity: bool,
        max_complexity: Optional[int],
        visualize: bool,
        keep_temp: bool,
    ) -> BuildRequest:
        rebuild, pending_rebuild[0] = pending_rebuild[0], False
        return BuildRequest(
            mode=mode,
            bal_file=Path(bal_file) if bal_file else None,
            source=source,
            template=template,
            image=image,
            rebuild_image=rebuild,
            keep=keep_temp,
            output_dir=Path(output_dir) if output_dir else None,
            tests=run_tests,
            test_files=[Path(path) for path in test_files or []],
            test_source=test_source,
            scan=security_scan,
            complexity=complexity,
            max_complexity=max_complexity,
            visualize=visualize,
        )

    pins = (
        " `// dependency: org/name:version` comments in it pin the versions of the packages it uses (pulled from"
        " Ballerina Central; the build fails if it used another version)."
    )
    BalFile = Annotated[
        Optional[str],
        Field(description="Path to the .bal file to build; it replaces main.bal in a copy of the template. Give this or `source`." + pins),
    ]
    Source = Annotated[Optional[str], Field(description="Ballerina source code to build instead of a file; it becomes main.bal." + pins)]
    OutputDir = Annotated[
        Optional[str],
        Field(
            description="Folder to copy the outputs to, so they outlive the temporary copy: target/bin (the executable .jar, plus"
            " the native executable for build_graalvm), build_docker's image .tar, the reports of run_tests, security_scan"
            " and complexity, and the diagrams of visualize. Without it, build_docker saves the .tar as ./<file name>.tar and the checks are only summarised."
        ),
    ]
    RunTests = Annotated[
        bool,
        Field(
            description="Also run `make test`: bal test with a test report and code coverage (output_dir/test-report). The tests"
            " come from the template's tests/ folder, test_files and test_source."
        ),
    ]
    TestFiles = Annotated[Optional[List[str]], Field(description="Paths of test .bal files to add to tests/; implies run_tests.")]
    TestSource = Annotated[
        Optional[str],
        Field(description="Test code (import ballerina/test; @test:Config functions), written to tests/main_test.bal; implies run_tests."),
    ]
    SecurityScan = Annotated[
        bool,
        Field(
            description="Also run bal scan static analysis (output_dir/scan-report) and Trivy: known vulnerabilities in the .jar or"
            " the image plus a CycloneDX SBOM (output_dir/trivy). The first scan downloads images and vulnerability databases."
        ),
    ]
    Complexity = Annotated[
        bool,
        Field(
            description="Also measure each function's cyclomatic and cognitive complexity, deepest nesting and lines of code,"
            " with the template's own Ballerina parser (output_dir/complexity). The result lists the most complex functions."
        ),
    ]
    MaxComplexity = Annotated[
        Optional[int],
        Field(description="Fail the build when a function's cyclomatic complexity is higher than this (10 is a common limit); implies complexity.", ge=1),
    ]
    Visualize = Annotated[
        bool,
        Field(
            description="Also save the Ballerina VS Code extension's diagrams, drawn by the extension itself in a headless VS Code:"
            " each file's overview and, for each function, method, service and resource, its sequence diagram, data mapper or"
            " service view (PNG, plus the extension's SVG export of sequence diagrams) in output_dir/visualizations, with an"
            " index.html. Needs output_dir. The first run builds two images."
        ),
    ]
    KeepTemp = Annotated[
        bool,
        Field(
            description="Leave the temporary folder and the container in place and report where they are. By default both are"
            " removed after a successful build; a failed build always keeps the temporary folder."
        ),
    ]

    server = Server(
        "ballerina-builder",
        instructions=(
            f"Builds Ballerina programs inside a container, in a fresh copy of the template project {template}"
            " (the given file or source replaces its main.bal; `// dependency: org/name:version` comments in it pin"
            " package versions). compile_ballerina is a quick compile check;"
            " build_graalvm makes a native executable (takes minutes); build_docker makes a Docker image saved"
            " as a .tar. output_dir collects the .jar, native executable or image .tar and the reports;"
            " run_tests adds bal test with coverage, security_scan adds bal scan and Trivy, complexity adds"
            " complexity metrics per function, visualize adds the VS Code extension's diagrams. Build output streams"
            " as progress notifications; the result summarises the checks and ends with the last lines of output."
        ),
    )

    @server.tool(structured_output=False)
    async def compile_ballerina(
        ctx: Context,
        bal_file: BalFile = None,
        source: Source = None,
        output_dir: OutputDir = None,
        run_tests: RunTests = False,
        test_files: TestFiles = None,
        test_source: TestSource = None,
        security_scan: SecurityScan = False,
        complexity: Complexity = False,
        max_complexity: MaxComplexity = None,
        visualize: Visualize = False,
        keep_temp: KeepTemp = False,
    ) -> str:
        """Compile a Ballerina program with `make build` (bal build) in a copy of the template project, inside the
        template's container, optionally with its tests, security scans, complexity metrics and diagrams. Returns the outcome,
        the check results, and the last lines of output, including compiler errors."""
        return await run_tool(
            ctx, request(
                "compile", bal_file, source, output_dir, run_tests, test_files, test_source, security_scan, complexity,
                max_complexity, visualize, keep_temp,
            )
        )

    @server.tool(structured_output=False)
    async def build_graalvm(
        ctx: Context,
        bal_file: BalFile = None,
        source: Source = None,
        output_dir: OutputDir = None,
        run_tests: RunTests = False,
        test_files: TestFiles = None,
        test_source: TestSource = None,
        security_scan: SecurityScan = False,
        complexity: Complexity = False,
        max_complexity: MaxComplexity = None,
        visualize: Visualize = False,
        keep_temp: KeepTemp = False,
    ) -> str:
        """Build a GraalVM native executable with `make build_graalvm` (bal build --graalvm) in a copy of the template
        project, inside the template's container, optionally with its tests, security scans, complexity metrics and
        diagrams. Takes several minutes.
        The executable is a Linux binary."""
        return await run_tool(
            ctx, request(
                "graalvm", bal_file, source, output_dir, run_tests, test_files, test_source, security_scan, complexity,
                max_complexity, visualize, keep_temp,
            )
        )

    @server.tool(structured_output=False)
    async def build_docker(
        ctx: Context,
        bal_file: BalFile = None,
        source: Source = None,
        output_dir: OutputDir = None,
        run_tests: RunTests = False,
        test_files: TestFiles = None,
        test_source: TestSource = None,
        security_scan: SecurityScan = False,
        complexity: Complexity = False,
        max_complexity: MaxComplexity = None,
        visualize: Visualize = False,
        keep_temp: KeepTemp = False,
    ) -> str:
        """Build a Docker image with `make build_docker` (bal build --cloud=docker) using Docker-in-Docker inside the
        template's container, then save it with `docker save` to a .tar on this machine (load it with docker load -i),
        optionally with its tests, security scans, complexity metrics and diagrams."""
        return await run_tool(
            ctx, request(
                "docker", bal_file, source, output_dir, run_tests, test_files, test_source, security_scan, complexity,
                max_complexity, visualize, keep_temp,
            )
        )

    server.run()
    return 0


# --------------------------------------------------------------------------- CLI


def _positive_int(text: str) -> int:
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError(f"expected a whole number of 1 or more, not {text!r}")
    return int(text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bal_builder.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Build a Ballerina file in a temporary copy of a template project, inside the template's container.",
        epilog=f"""\
examples:
  bal_builder.py compile       hello.bal -o out                # the .jar -> out/
  bal_builder.py build-graalvm hello.bal -o out                # native executable + .jar -> out/
  bal_builder.py build-docker  hello.bal                       # docker save -> ./hello.tar
  bal_builder.py compile       hello.bal --test --scan -o out  # plus test and scan reports in out/
  bal_builder.py compile       hello.bal --max-complexity 10   # fail if a function is too complex
  bal_builder.py compile       hello.bal --visualize -o out    # the VS Code extension's diagrams -> out/visualizations
  bal_builder.py run           hello.bal a b                   # build, then run the program with the arguments a b

scripts: with this first line, a .bal file runs like a script (chmod +x hello.bal; ./hello.bal a b):
  #!/usr/bin/env -S bal_builder.py run --scan -- x y
  The options before -- are the build's; x y become the program's first arguments, before those of the
  command line. The line is turned into a comment before the build. Build output is hidden unless the
  build fails (-v shows it on stderr), so stdout is the program's own; the exit code is the program's.

dependencies: comments in the file pin the versions of the packages it uses (examples/main_dependencies.bal):
  // dependency: ballerina/toml:0.3.0
  They are locked in the copy's Dependencies.toml, and `bal build --sticky` pulls them from Ballerina
  Central before the Makefile target, which then builds with them. The build fails if it used another
  version; the summary lists the versions it used.

options of compile, build-graalvm, build-docker and run (see `bal_builder.py COMMAND -h`):
  -o DIR              copy the outputs (target/bin, build-docker's .tar, reports) to DIR
  --test              also run `make test`: bal test with a report and code coverage
  --test-file FILE    add a test file to tests/ (repeatable; implies --test)
  --scan              also run bal scan (in {SCAN_BALLERINA_IMAGE}) and Trivy (vulnerabilities, SBOM)
  --complexity        also measure each function's cyclomatic and cognitive complexity
  --max-complexity N  fail if a function's cyclomatic complexity is over N (implies --complexity)
  --visualize         also save the Ballerina VS Code extension's diagrams of each file and function (needs -o)
  -k, --keep          keep the temporary folder and the container
  -t DIR              template folder (default: ${TEMPLATE_ENV}, else this script's folder)

Build output streams while it runs. After a successful build the temporary folder and the
container are removed; a failed build keeps the temporary folder and prints its path.""",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    common, build = _common_options(), _build_options()
    source = argparse.ArgumentParser(add_help=False)
    source.add_argument("bal_file", type=Path, help="the Ballerina file to build; it replaces main.bal in the copy")

    commands.add_parser("compile", parents=[common, source, build], help="run `make build` (bal build)")
    commands.add_parser("build-graalvm", parents=[common, source, build], help="run `make build_graalvm` (bal build --graalvm)")
    commands.add_parser(
        "build-docker", parents=[common, source, build], help="run `make build_docker` (bal build --cloud=docker) and save the image as a .tar"
    )
    run = commands.add_parser(
        "run",
        parents=[common, _run_options(), source],
        help="run `make build`, then the program with the given arguments (also the command of a .bal script's #! line)",
    )
    run.add_argument("args", nargs=argparse.REMAINDER, help="the program's arguments")
    commands.add_parser("mcp", parents=[common], help="serve compile/build-graalvm/build-docker as MCP tools over stdio")
    return parser


def _common_options() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-t",
        "--template",
        type=Path,
        default=Path(os.environ.get(TEMPLATE_ENV) or SCRIPT_PATH.parent),
        metavar="DIR",
        help=f"template project folder (default: ${TEMPLATE_ENV}, else the folder holding this script)",
    )
    common.add_argument(
        "--image",
        default=os.environ.get(IMAGE_ENV),
        help=f"build in this image instead of the template's .devcontainer image (default: ${IMAGE_ENV})",
    )
    common.add_argument(
        "--rebuild-image",
        action="store_true",
        help="rebuild the template's .devcontainer image and pull its base, even if .devcontainer is unchanged",
    )
    return common


def _build_options() -> argparse.ArgumentParser:
    build = argparse.ArgumentParser(add_help=False)
    build.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="DIR",
        help="copy the outputs here, so they outlive the temporary copy: target/bin (the executable .jar, plus the native"
        " executable for build-graalvm), build-docker's image .tar (otherwise saved as ./<file name>.tar) and the check reports",
    )
    build.add_argument(
        "--test", action="store_true", help="also run `make test`: bal test with a test report and code coverage (-> DIR/test-report)"
    )
    build.add_argument(
        "--test-file",
        type=Path,
        action="append",
        default=[],
        metavar="FILE",
        help="add a test .bal file to tests/ (repeatable; implies --test)",
    )
    build.add_argument(
        "--scan",
        action="store_true",
        help=f"also run bal scan static analysis in {SCAN_BALLERINA_IMAGE} (-> DIR/scan-report) and Trivy: known"
        " vulnerabilities in the .jar or image plus a CycloneDX SBOM (-> DIR/trivy)",
    )
    build.add_argument(
        "--complexity",
        action="store_true",
        help="also measure each function's cyclomatic and cognitive complexity, deepest nesting and lines of code with the"
        " template's own Ballerina parser (-> DIR/complexity)",
    )
    build.add_argument(
        "--max-complexity",
        type=_positive_int,
        metavar="N",
        help="fail if a function's cyclomatic complexity is over N; 10 is a common limit (implies --complexity)",
    )
    build.add_argument(
        "--visualize",
        action="store_true",
        help="also save the diagrams the Ballerina VS Code extension draws, drawn by the extension itself in a headless VS Code:"
        " each file's overview and the sequence diagram, data mapper or service view of each function, method, service and"
        " resource, as PNG (plus the extension's SVG export of sequence diagrams) with an index.html (-> DIR/visualizations; needs -o)",
    )
    build.add_argument("-k", "--keep", action="store_true", help="leave the temporary folder and the container, and print where they are")
    return build


def _run_options() -> argparse.ArgumentParser:
    run = argparse.ArgumentParser(add_help=False, parents=[_build_options()])
    run.add_argument("-v", "--verbose", action="store_true", help="show the build's output and steps on stderr")
    return run


def _shebang_parser() -> argparse.ArgumentParser:
    """The arguments of a #! line after `run`: the build's options, then the program's first arguments."""
    parser = argparse.ArgumentParser(prog="bal_builder.py run (#! line)", parents=[_common_options(), _run_options()])
    parser.add_argument("args", nargs="*", help="the program's first arguments")
    return parser


def _shebang_args(path: str) -> Optional[List[List[str]]]:
    """The words after `run` on a script's #! line, as the system may pass them, or None without such a line.

    The first entry honours quotes, as `env -S` does with the line Linux passes it whole; the second is split
    at spaces only, as the macOS kernel splits the line itself.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            line = fh.readline(4096)
    except OSError:
        return None
    if not line.startswith("#!"):
        return None
    try:
        quoted = shlex.split(line[2:])
    except ValueError:
        quoted = line[2:].split()
    splits = []
    for words in (quoted, line[2:].split()):
        if "run" not in words[1:]:
            return None
        splits.append(words[words.index("run", 1) + 1:])
    return splits


def _script_call(argv: List[str]) -> Optional[tuple]:
    """For `run` started by a script's #! line, (#! arguments, script, command-line arguments), else None.

    The system calls the line's command with the line's own arguments first, then the script's path, then the
    arguments it was run with; the script is the argument whose #! line names exactly the arguments before it.
    The #! arguments are taken from the line itself, with its quotes, whichever way the system split it.
    """
    for i, arg in enumerate(argv):
        splits = _shebang_args(arg) if os.path.isfile(arg) else None
        if splits and argv[:i] in splits:
            return splits[0], arg, argv[i + 1:]
    return None


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    script = _script_call(argv[1:]) if argv[:1] == ["run"] else None
    if script is not None:
        line_args, path, command_line_args = script
        args = _shebang_parser().parse_args(line_args)
        args.command, args.bal_file, args.args = "run", Path(path), args.args + command_line_args
    else:
        args = _parser().parse_args(argv)
        if args.command == "run" and args.args[:1] == ["--"]:
            args.args = args.args[1:]
    if args.command == "mcp":
        return serve_mcp(args.template.expanduser().resolve(), args.image, args.rebuild_image)

    mode = {"compile": "compile", "build-graalvm": "graalvm", "build-docker": "docker", "run": "run"}[args.command]
    req = BuildRequest(
        mode=mode,
        bal_file=args.bal_file,
        template=args.template,
        image=args.image,
        rebuild_image=args.rebuild_image,
        keep=args.keep,
        output_dir=args.output,
        tests=args.test,
        test_files=args.test_file,
        scan=args.scan,
        complexity=args.complexity,
        max_complexity=args.max_complexity,
        visualize=args.visualize,
        program_args=args.args if mode == "run" else [],
    )

    def interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt  # run_build then stops the build and removes the container, as for Ctrl-C

    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupt)

    if mode != "run":
        reporter = ConsoleReporter()
    else:  # stdout is the program's: the build's output goes to stderr with -v, and nowhere otherwise
        reporter = ConsoleReporter(sys.stderr) if args.verbose else Reporter()
    try:
        result = run_build(req, reporter)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if mode != "run":
        print("\n" + format_result(result), file=sys.stderr)  # the output itself was streamed already
        return 0 if result.success else 130 if result.cancelled else 1
    if result.program_exit_code is not None and not result.cancelled:
        if args.verbose or req.output_dir or req.tests or req.scan or req.complexity or req.max_complexity or req.visualize:
            print("\n" + format_result(result), file=sys.stderr)
        return result.program_exit_code
    if result.cancelled:
        print(result.message, file=sys.stderr)
        return 130
    # The build or one of its checks failed, so the program did not run: say why, with the end of the build output.
    print(format_result(result, output_lines=0 if args.verbose else 40), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
