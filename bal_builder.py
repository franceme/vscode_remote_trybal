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

  MCP server over stdio, with the tools compile_ballerina, build_graalvm and build_docker
  (Python 3.10+ and the `mcp` package; `uv run` installs it from the metadata above):
    uv run bal_builder.py mcp
    claude mcp add ballerina-builder -- uv run /absolute/path/to/bal_builder.py mcp

Every build copies the template folder (default: the folder holding this script; or --template, or
$BAL_BUILDER_TEMPLATE) to a new temporary folder, replaces main.bal with the given file, starts a
container from the template's dev-container image (.devcontainer/), copies the project in, runs the
Makefile target and streams its output while it runs. The template is re-read on every build, so
changes to it apply straight away.

-o / --output DIR (MCP: output_dir) copies a build's outputs out of the container, so they outlive the
temporary copy: target/bin (the executable .jar, plus the native executable for build-graalvm),
build-docker's image .tar (saved as ./<file name>.tar without -o) and the reports of these checks:
  --test  `make test`: bal test with a test report and code coverage                  -> test-report/
  --scan  bal scan static analysis, run in a newer Ballerina image                     -> scan-report/
          and Trivy: vulnerabilities and a CycloneDX SBOM of the .jar or the image     -> trivy/
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
from pathlib import Path
from typing import Callable, Deque, Dict, Generator, List, Optional

SCRIPT_PATH = Path(__file__).resolve()
TEMPLATE_ENV = "BAL_BUILDER_TEMPLATE"
IMAGE_ENV = "BAL_BUILDER_IMAGE"

WORKDIR = "/workspace"  # where the project lives inside the build container
LABEL = "bal-builder"  # label on every container and template image this script creates

# capability -> Makefile target in the template
MAKE_TARGETS = {"compile": "build", "graalvm": "build_graalvm", "docker": "build_docker"}

# Never copied from the template: VCS data, build output and caches at any depth ...
EXCLUDE_EVERYWHERE = (".git", "target", "__pycache__", "*.pyc", ".DS_Store")
# ... and, at the top level, this script and the places it writes its outputs by default.
EXCLUDE_AT_ROOT = (SCRIPT_PATH.name, "dist", "*.tar")

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
    tar_path: Optional[Path] = None  # docker: set by _normalized to <output_dir or .>/<file stem>.tar


@dataclasses.dataclass
class BuildResult:
    mode: str
    success: bool = False
    cancelled: bool = False
    message: str = ""
    problems: List[str] = dataclasses.field(default_factory=list)  # failed checks (the build itself worked)
    exit_code: Optional[int] = None
    seconds: float = 0.0
    artifacts: List[str] = dataclasses.field(default_factory=list)  # files written outside the temporary folder
    reports: List[str] = dataclasses.field(default_factory=list)  # report folders written to output_dir
    docker_image: Optional[str] = None
    image_tar: Optional[str] = None
    tests: Optional[dict] = None  # summary of bal test's test_results.json
    scan: Optional[dict] = None  # summary of bal scan's scan_results.json
    vulnerabilities: Optional[dict] = None  # summary of Trivy's report
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
    def __init__(self) -> None:
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

    @staticmethod
    def _stdout(text: str) -> None:
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except BrokenPipeError:  # e.g. piped into `head`: drop further output, but finish and clean up
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


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

    def ensure_image(self, image: str) -> str:
        if self.run("image", "inspect", image, check=False).returncode != 0:
            self.out.status(f"Pulling {image}")
            if self.stream("pull", image) != 0:
                raise BuildError(f"Could not pull the image {image}.")
        return image

    def start(self, image: str, name: str, docker_in_docker: bool = False, volumes: tuple = ()) -> None:
        args = ["run", "--detach", "--init", "--name", name, "--label", f"{LABEL}=1"]
        for volume in volumes:
            args += ["--volume", volume]
        if docker_in_docker:
            # Keep the image's entrypoint: in the template image it starts dockerd, which needs privileges.
            args += ["--privileged", image, "sleep", "infinity"]
        else:
            args += ["--entrypoint", "sleep", image, "infinity"]
        self.run(*args)

    @contextlib.contextmanager
    def helper(self, image: str, kind: str, volumes: tuple = ()) -> Generator[str, None, None]:
        """A throwaway container for a check (bal scan, Trivy), removed afterwards whatever happens."""
        name = f"{LABEL}-{kind}-{uuid.uuid4().hex[:8]}"
        with _RUNNING_LOCK:
            _RUNNING[name] = self.cancel
        try:
            self.start(image, name, volumes=volumes)
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
        (project / "main.bal").write_text(req.source, encoding="utf-8")
        out.status("Wrote the given source to main.bal")
    else:
        shutil.copyfile(req.bal_file, project / "main.bal")
        out.status(f"Replaced main.bal with {req.bal_file}")
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
    test_files = [Path(path).expanduser().resolve() for path in req.test_files]
    for path in test_files:
        if not path.is_file():
            raise BuildError(f"No such test file: {path}")
    output_dir = req.output_dir.expanduser().resolve() if req.output_dir is not None else None
    if output_dir is not None and output_dir.exists() and not output_dir.is_dir():
        raise BuildError(f"The output path {output_dir} exists and is not a folder.")
    return dataclasses.replace(
        req,
        template=template,
        bal_file=bal_file,
        output_dir=output_dir,
        tests=req.tests or bool(test_files) or req.test_source is not None,
        test_files=test_files,
        tar_path=(output_dir or Path.cwd()) / f"{stem}.tar" if req.mode == "docker" else None,
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


# --------------------------------------------------------------------------- checks (--test, --scan)

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
        docker.copy_in(project, container, image)
        if req.mode == "docker":
            docker.wait_for_daemon(container)

        out.status(f"Running `make {target}` in {container}:{WORKDIR}")
        out.tail.clear()  # the result reports the build itself, not the image preparation
        result.exit_code = docker.stream("exec", "--workdir", WORKDIR, container, "make", target)
        if result.exit_code != 0:
            result.message = _failure_hint(req.mode, out.tail)
        else:
            if req.mode == "docker":
                result.docker_image = docker.built_image(container, list(out.tail))
                docker.save(container, result.docker_image, req.tar_path)
                result.image_tar = str(req.tar_path)
            reports = tmp / "reports"
            reports.mkdir()
            if req.scan:
                result.problems += _checked("bal scan", lambda: _bal_scan(req, docker, out, project, reports, tmp, result))
                result.problems += _checked("Trivy", lambda: _trivy(req, docker, out, container, reports, tmp, result))
            if req.tests:  # last, so that the end of the output shows how the tests went
                result.problems += _checked("make test", lambda: _run_tests(docker, out, container, reports, result))
            if req.output_dir is not None:
                _copy_outputs(req, docker, out, container, reports, tmp, result)
            if result.image_tar:
                result.artifacts.append(result.image_tar)
            result.success = not result.problems
    except Cancelled as exc:
        result.cancelled, result.message = True, str(exc)
    except KeyboardInterrupt:
        result.cancelled, result.message = True, "The build was interrupted."
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
    """Summaries of the --test and --scan results."""
    lines = []
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
    lines = [f"`make {MAKE_TARGETS[result.mode]}` {verdict} after {_duration(result.seconds)}{exit_code}{problems}."]
    if result.message:
        lines.append(result.message)
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
        )

    BalFile = Annotated[
        Optional[str], Field(description="Path to the .bal file to build; it replaces main.bal in a copy of the template. Give this or `source`.")
    ]
    Source = Annotated[Optional[str], Field(description="Ballerina source code to build instead of a file; it becomes main.bal.")]
    OutputDir = Annotated[
        Optional[str],
        Field(
            description="Folder to copy the outputs to, so they outlive the temporary copy: target/bin (the executable .jar, plus"
            " the native executable for build_graalvm), build_docker's image .tar and the reports of run_tests and"
            " security_scan. Without it, build_docker saves the .tar as ./<file name>.tar and the checks are only summarised."
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
            " (the given file or source replaces its main.bal). compile_ballerina is a quick compile check;"
            " build_graalvm makes a native executable (takes minutes); build_docker makes a Docker image saved"
            " as a .tar. output_dir collects the .jar, native executable or image .tar and the reports;"
            " run_tests adds bal test with coverage, security_scan adds bal scan and Trivy. Build output streams"
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
        keep_temp: KeepTemp = False,
    ) -> str:
        """Compile a Ballerina program with `make build` (bal build) in a copy of the template project, inside the
        template's container, optionally with its tests and security scans. Returns the outcome, the test and scan
        results, and the last lines of output, including compiler errors."""
        return await run_tool(
            ctx, request("compile", bal_file, source, output_dir, run_tests, test_files, test_source, security_scan, keep_temp)
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
        keep_temp: KeepTemp = False,
    ) -> str:
        """Build a GraalVM native executable with `make build_graalvm` (bal build --graalvm) in a copy of the template
        project, inside the template's container, optionally with its tests and security scans. Takes several minutes.
        The executable is a Linux binary."""
        return await run_tool(
            ctx, request("graalvm", bal_file, source, output_dir, run_tests, test_files, test_source, security_scan, keep_temp)
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
        keep_temp: KeepTemp = False,
    ) -> str:
        """Build a Docker image with `make build_docker` (bal build --cloud=docker) using Docker-in-Docker inside the
        template's container, then save it with `docker save` to a .tar on this machine (load it with docker load -i),
        optionally with its tests and security scans."""
        return await run_tool(
            ctx, request("docker", bal_file, source, output_dir, run_tests, test_files, test_source, security_scan, keep_temp)
        )

    server.run()
    return 0


# --------------------------------------------------------------------------- CLI


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

options of compile, build-graalvm and build-docker (see `bal_builder.py COMMAND -h`):
  -o DIR           copy the outputs (target/bin, build-docker's .tar, reports) to DIR
  --test           also run `make test`: bal test with a report and code coverage
  --test-file FILE add a test file to tests/ (repeatable; implies --test)
  --scan           also run bal scan (in {SCAN_BALLERINA_IMAGE}) and Trivy (vulnerabilities, SBOM)
  -k, --keep       keep the temporary folder and the container
  -t DIR           template folder (default: ${TEMPLATE_ENV}, else this script's folder)

Build output streams while it runs. After a successful build the temporary folder and the
container are removed; a failed build keeps the temporary folder and prints its path.""",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

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
    build = argparse.ArgumentParser(add_help=False)
    build.add_argument("bal_file", type=Path, help="the Ballerina file to build; it replaces main.bal in the copy")
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
    build.add_argument("-k", "--keep", action="store_true", help="leave the temporary folder and the container, and print where they are")

    commands.add_parser("compile", parents=[common, build], help="run `make build` (bal build)")
    commands.add_parser("build-graalvm", parents=[common, build], help="run `make build_graalvm` (bal build --graalvm)")
    commands.add_parser(
        "build-docker", parents=[common, build], help="run `make build_docker` (bal build --cloud=docker) and save the image as a .tar"
    )
    commands.add_parser("mcp", parents=[common], help="serve compile/build-graalvm/build-docker as MCP tools over stdio")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "mcp":
        return serve_mcp(args.template.expanduser().resolve(), args.image, args.rebuild_image)

    mode = {"compile": "compile", "build-graalvm": "graalvm", "build-docker": "docker"}[args.command]
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
    )

    def interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt  # run_build then stops the build and removes the container, as for Ctrl-C

    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupt)

    reporter = ConsoleReporter()
    try:
        result = run_build(req, reporter)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("\n" + format_result(result), file=sys.stderr)  # the output itself was streamed already
    return 0 if result.success else 130 if result.cancelled else 1


if __name__ == "__main__":
    sys.exit(main())
