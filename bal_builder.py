#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.10"]
# ///
"""Build a Ballerina file in a throwaway copy of a template project, inside the template's container.

One script, two front ends that share the same build code:

  CLI (Python 3.9+, needs nothing but Docker):
    python3 bal_builder.py compile       hello.bal             # make build         (bal build)
    python3 bal_builder.py build-graalvm hello.bal -d          # make build_graalvm (bal build --graalvm), target/bin -> ./dist
    python3 bal_builder.py build-docker  hello.bal -o app.tar  # make build_docker  (bal build --cloud=docker), docker save -> app.tar

  MCP server over stdio, with the tools compile_ballerina, build_graalvm and build_docker
  (Python 3.10+ and the `mcp` package; `uv run` installs it from the metadata above):
    uv run bal_builder.py mcp
    claude mcp add ballerina-builder -- uv run /absolute/path/to/bal_builder.py mcp

Every build copies the template folder (default: the folder holding this script; or --template, or
$BAL_BUILDER_TEMPLATE) to a new temporary folder, replaces main.bal with the given file, starts a
container from the template's dev-container image (.devcontainer/), copies the project in, runs the
Makefile target and streams its output while it runs. The template is re-read on every build, so
changes to it apply straight away.

After a successful build the temporary folder and the container are removed. --keep (MCP: keep_temp)
leaves both in place and prints where they are. A failed or cancelled build always keeps the
temporary folder (project/, synced back from the container, and build.log) and prints its path; the
container is removed.
"""

import argparse
import codecs
import collections
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
from typing import Deque, Dict, List, Optional, Union

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

DOCKERD_TIMEOUT = 90  # seconds to wait for the Docker daemon inside the build container
TAIL_LINES = 150  # lines of build output kept for the result; MCP responses include them on failure ...
SUCCESS_TAIL_LINES = 20  # ... and only this many after a successful build
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
    download_dist: bool = False  # graalvm: copy target/bin out of the container ...
    dist_dir: Optional[Path] = None  # ... into this folder (default ./dist; setting it implies download)
    tar_path: Union[str, Path, None] = None  # docker: the .tar to write, or a folder for it (default ./<file stem>.tar)


@dataclasses.dataclass
class BuildResult:
    mode: str
    success: bool = False
    cancelled: bool = False
    message: str = ""
    exit_code: Optional[int] = None
    seconds: float = 0.0
    artifacts: List[str] = dataclasses.field(default_factory=list)
    docker_image: Optional[str] = None
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

    def stream(self, *args: str, env: Optional[Dict[str, str]] = None) -> int:
        """Run a docker command, passing its combined stdout/stderr on as it is produced."""
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
        try:
            while True:
                chunk = os.read(proc.stdout.fileno(), 65536)  # returns as soon as anything arrives
                if not chunk:
                    break
                self.out.output(decoder.decode(chunk))
            self.out.output(decoder.decode(b"", final=True))
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

    def start(self, image: str, name: str, docker_in_docker: bool) -> None:
        args = ["run", "--detach", "--init", "--name", name, "--label", f"{LABEL}=1"]
        if docker_in_docker:
            # Keep the image's entrypoint: in the template image it starts dockerd, which needs privileges.
            args += ["--privileged", image, "sleep", "infinity"]
        else:
            args += ["--entrypoint", "sleep", image, "infinity"]
        self.run(*args)

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


def _ignore(root: Path, directory: str, names: List[str]) -> List[str]:
    patterns = EXCLUDE_EVERYWHERE + (EXCLUDE_AT_ROOT if Path(directory) == root else ())
    return [name for name in names if _excluded(name, patterns)]


def _copy_template(req: BuildRequest, project: Path, out: _Output) -> None:
    out.status(f"Copying template {req.template}")
    shutil.copytree(req.template, project, symlinks=True, ignore=functools.partial(_ignore, req.template))
    main_bal = project / "main.bal"
    if main_bal.is_symlink():  # write a new file rather than through a link back into the template
        main_bal.unlink()
    if req.source is not None:
        main_bal.write_text(req.source, encoding="utf-8")
        out.status("Wrote the given source to main.bal")
    else:
        shutil.copyfile(req.bal_file, main_bal)
        out.status(f"Replaced main.bal with {req.bal_file}")


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
    dist_dir = tar_path = None
    if req.mode == "graalvm" and (req.download_dist or req.dist_dir is not None):
        dist_dir = (req.dist_dir or Path("dist")).expanduser().resolve()
    if req.mode == "docker":
        given = os.fspath(req.tar_path) if req.tar_path is not None else ""
        tar_path = Path(given or f"{stem}.tar").expanduser().resolve()
        if given.endswith(("/", os.sep)) or tar_path.is_dir():  # a folder: put <stem>.tar inside it
            tar_path = tar_path / f"{stem}.tar"
    return dataclasses.replace(
        req, template=template, bal_file=bal_file, download_dist=dist_dir is not None, dist_dir=dist_dir, tar_path=tar_path
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
    if mode == "graalvm" and any("Error 137" in line or line.strip() == "Killed" for line in tail):
        return "native-image was killed, most likely for lack of memory: give Docker more memory and retry."
    return ""


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
            if req.mode == "graalvm" and req.dist_dir is not None:
                result.artifacts = docker.copy_out(container, f"{WORKDIR}/target/bin", req.dist_dir)
            elif req.mode == "docker":
                result.docker_image = docker.built_image(container, list(out.tail))
                docker.save(container, result.docker_image, req.tar_path)
                result.artifacts = [str(req.tar_path)]
            result.success = True
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


def format_result(result: BuildResult, output_lines: int = 0) -> str:
    verdict = "succeeded" if result.success else "was cancelled" if result.cancelled else "failed"
    exit_code = f" (exit code {result.exit_code})" if result.exit_code else ""
    lines = [f"`make {MAKE_TARGETS[result.mode]}` {verdict} after {_duration(result.seconds)}{exit_code}."]
    if result.message:
        lines.append(result.message)
    if result.docker_image:
        lines.append(f"Docker image: {result.docker_image}")
    lines += [f"Output: {path}" for path in result.artifacts]
    if result.docker_image and result.artifacts:
        lines.append(f"Load it with: docker load -i {shlex.quote(result.artifacts[0])}")
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

    def request(mode: str, bal_file: Optional[str], source: Optional[str], keep_temp: bool, **extra) -> BuildRequest:
        rebuild, pending_rebuild[0] = pending_rebuild[0], False
        return BuildRequest(
            mode=mode,
            bal_file=Path(bal_file) if bal_file else None,
            source=source,
            template=template,
            image=image,
            rebuild_image=rebuild,
            keep=keep_temp,
            **extra,
        )

    BalFile = Annotated[
        Optional[str], Field(description="Path to the .bal file to build; it replaces main.bal in a copy of the template. Give this or `source`.")
    ]
    Source = Annotated[Optional[str], Field(description="Ballerina source code to build instead of a file; it becomes main.bal.")]
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
            " as a .tar. Build output streams as progress notifications; the result ends with its last lines."
        ),
    )

    @server.tool(structured_output=False)
    async def compile_ballerina(ctx: Context, bal_file: BalFile = None, source: Source = None, keep_temp: KeepTemp = False) -> str:
        """Compile a Ballerina program with `make build` (bal build) in a copy of the template project, inside the
        template's container. Returns the outcome and the last lines of build output, including compiler errors."""
        return await run_tool(ctx, request("compile", bal_file, source, keep_temp))

    @server.tool(structured_output=False)
    async def build_graalvm(
        ctx: Context,
        bal_file: BalFile = None,
        source: Source = None,
        download_dist: Annotated[bool, Field(description="Copy target/bin (native executable and jar) to dist_dir afterwards.")] = False,
        dist_dir: Annotated[Optional[str], Field(description="Folder to copy target/bin into (default ./dist); implies download_dist.")] = None,
        keep_temp: KeepTemp = False,
    ) -> str:
        """Build a GraalVM native executable with `make build_graalvm` (bal build --graalvm) in a copy of the template
        project, inside the template's container. Takes several minutes. The executable is a Linux binary."""
        return await run_tool(
            ctx,
            request("graalvm", bal_file, source, keep_temp, download_dist=download_dist, dist_dir=Path(dist_dir) if dist_dir else None),
        )

    @server.tool(structured_output=False)
    async def build_docker(
        ctx: Context,
        bal_file: BalFile = None,
        source: Source = None,
        output_tar: Annotated[
            Optional[str], Field(description="Where to write the saved image (default ./<file name>.tar); a folder puts the .tar inside it.")
        ] = None,
        keep_temp: KeepTemp = False,
    ) -> str:
        """Build a Docker image with `make build_docker` (bal build --cloud=docker) using Docker-in-Docker inside the
        template's container, then save it with `docker save` to a .tar on this machine (load it with docker load -i)."""
        return await run_tool(ctx, request("docker", bal_file, source, keep_temp, tar_path=output_tar or None))

    server.run()
    return 0


# --------------------------------------------------------------------------- CLI


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bal_builder.py",
        description="Build a Ballerina file in a temporary copy of a template project, inside the template's container.",
        epilog="Build output streams while it runs. After a successful build the temporary folder and the container are"
        " removed (use --keep to leave both); a failed build keeps the temporary folder and prints its path.",
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
    build.add_argument("-k", "--keep", action="store_true", help="leave the temporary folder and the container, and print where they are")

    commands.add_parser("compile", parents=[common, build], help="run `make build` (bal build)")
    graalvm = commands.add_parser("build-graalvm", parents=[common, build], help="run `make build_graalvm` (bal build --graalvm)")
    graalvm.add_argument("-d", "--download-dist", action="store_true", help="copy target/bin (native executable and jar) out afterwards")
    graalvm.add_argument("--dist", type=Path, metavar="DIR", help="folder to copy target/bin into (default: ./dist); implies -d")
    docker = commands.add_parser(
        "build-docker", parents=[common, build], help="run `make build_docker` (bal build --cloud=docker) and save the image as a .tar"
    )
    docker.add_argument(
        "-o", "--output", metavar="PATH", help="the .tar to write (default: ./<file name>.tar); a folder (or a path ending in /) puts it inside"
    )
    commands.add_parser("mcp", parents=[common], help="serve compile/build-graalvm/build-docker as MCP tools over stdio")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "mcp":
        return serve_mcp(args.template.expanduser().resolve(), args.image, args.rebuild_image)

    mode = {"compile": "compile", "build-graalvm": "graalvm", "build-docker": "docker"}[args.command]
    req = BuildRequest(
        mode=mode, bal_file=args.bal_file, template=args.template, image=args.image, rebuild_image=args.rebuild_image, keep=args.keep
    )
    if mode == "graalvm":
        req.download_dist, req.dist_dir = args.download_dist, args.dist
    elif mode == "docker":
        req.tar_path = args.output

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
