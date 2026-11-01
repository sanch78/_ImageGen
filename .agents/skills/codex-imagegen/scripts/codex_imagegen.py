#!/usr/bin/env python3
"""Generate one raster image through a headless `codex exec` run.

The run uses the invoking user's Codex login and home, an empty staging
directory under the caller's tmp root and a read-only sandbox. The image is
written only to --out (never overwritten without --force); the staging
directory is removed afterwards.
"""
import argparse
import datetime
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_MODEL, DEFAULT_EFFORT = "gpt-6.1-sol", "medium"
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
EXIT_FAILED, EXIT_USAGE, EXIT_AUTH, EXIT_REFUSED, EXIT_LIMIT = 1, 2, 3, 4, 5
ASPECTS = {
    "square": "square (1:1)",
    "landscape": "landscape (wider than tall, about 3:2)",
    "portrait": "portrait (taller than wide, about 2:3)",
}
AUTH_MARKERS = ("401", "unauthorized", "token_revoked", "not logged in", "login")
LIMIT_MARKERS = ("usage_limit", "rate_limit", "quota", "429", "credits")


class Failure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--prompt", help="image request text")
    src.add_argument("--prompt-file", type=Path, help="UTF-8 file with the request")
    src.add_argument("--from", dest="source", type=Path,
                     help="place an already generated image instead of running Codex")
    src.add_argument("--from-thread", action="store_true",
                     help="inside Codex: place the newest image of $CODEX_THREAD_ID")
    p.add_argument("--note", help="request text recorded in the sidecar with --from/--from-thread")
    p.add_argument("--out", type=Path, required=True, help="output image path")
    p.add_argument("--staging-root", type=Path,
                   help="existing directory in the caller's tmp root (required with a prompt)")
    p.add_argument("--aspect", choices=sorted(ASPECTS), default="square")
    p.add_argument("--transparent", action="store_true", help="request a transparent background")
    p.add_argument("--ref", action="append", type=Path, default=[],
                   help="reference image attached to the request (repeatable)")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Codex model (default {DEFAULT_MODEL})")
    p.add_argument("--effort", choices=EFFORTS, default=DEFAULT_EFFORT,
                   help=f"Codex reasoning effort (default {DEFAULT_EFFORT})")
    p.add_argument("--timeout", type=int, default=600, help="seconds (default 600)")
    p.add_argument("--force", action="store_true", help="replace an existing --out")
    p.add_argument("--no-sidecar", action="store_true", help="skip the <out>.json metadata file")
    a = p.parse_args(argv)
    if a.prompt_file:
        a.prompt = a.prompt_file.read_text(encoding="utf-8")
    if a.prompt is not None and not a.prompt.strip():
        p.error("the prompt is empty")
    if a.from_thread and not os.environ.get("CODEX_THREAD_ID"):
        p.error("--from-thread needs CODEX_THREAD_ID (set inside a Codex session)")
    if a.out.suffix.lower() != ".png":
        p.error("--out must end in .png (Codex returns PNG)")
    if not a.out.parent.is_dir():
        p.error(f"output directory does not exist: {a.out.parent}")
    if a.out.exists() and not a.force:
        p.error(f"output exists (use --force to replace): {a.out}")
    if a.prompt is not None:
        if a.staging_root is None or not a.staging_root.is_dir():
            p.error("--staging-root must name an existing directory")
        for ref in a.ref:
            if not ref.is_file():
                p.error(f"reference image not found: {ref}")
    return a


def build_prompt(a):
    lines = [
        "This is a headless image-generation run. Call the built-in image_gen tool",
        "exactly once for the request below. Do not run shell commands, read or",
        "write files, or call any other tool.",
        f"Output shape: {ASPECTS[a.aspect]}.",
    ]
    if a.transparent:
        lines.append("Background: genuinely transparent; preserve the alpha channel.")
    if a.ref:
        lines.append("The attached images are references for the request, in the order given.")
    lines += [
        "If the tool refuses or fails, reply with IMAGEGEN_FAILED: <reason>.",
        "Otherwise reply with IMAGEGEN_OK.",
        "",
        "Request:",
        a.prompt.strip(),
    ]
    return "\n".join(lines)


def read_events(text):
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def thread_id(events):
    return next((e.get("thread_id") for e in events if e.get("type") == "thread.started"), None)


def thread_image(thread, home):
    """Return the newest image Codex saved for a thread, else None."""
    if not thread:
        return None
    files = [f for f in (home / "generated_images" / thread).glob("*.png") if f.is_file()]
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def classify(events, last_message):
    """Map a run without an image to (exit code, message)."""
    errors = [e.get("error", {}).get("message", "") for e in events if e.get("type") == "turn.failed"]
    errors += [e.get("message", "") for e in events if e.get("type") == "error"]
    text = " ".join(errors).lower()
    reason = (errors[0] if errors else last_message.strip()) or "no image was produced"
    if any(m in text for m in AUTH_MARKERS):
        return EXIT_AUTH, f"Codex login is missing or expired ({reason}); run `codex login` as this user"
    if any(m in text for m in LIMIT_MARKERS):
        return EXIT_LIMIT, f"Codex usage limit reached: {reason}"
    if errors:
        return EXIT_FAILED, f"Codex run failed: {reason}"
    return EXIT_REFUSED, f"image generation refused or failed: {reason}"


def png_info(data):
    """Return (width, height, has_alpha) for PNG bytes, else None."""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", data[16:24])
    return w, h, data[25] in (4, 6) or b"tRNS" in data[:data.find(b"IDAT")]


def aspect_ok(aspect, w, h):
    if aspect == "square":
        return abs(w - h) <= 0.02 * max(w, h)
    return w > h if aspect == "landscape" else h > w


def check_image(data, aspect, transparent):
    info = png_info(data)
    if info is None:
        raise Failure(EXIT_FAILED, "the generated file is not a PNG image")
    w, h, alpha = info
    if not aspect_ok(aspect, w, h):
        raise Failure(EXIT_FAILED, f"generated {w}x{h}, which is not {aspect}")
    if transparent and not alpha:
        raise Failure(EXIT_FAILED, "a transparent background was requested but the image has no alpha")
    return info


def write_new(path, data, force):
    """Write data to path atomically; refuse to replace unless force."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o666 & ~current_umask())
        if force:
            os.replace(tmp, path)
        else:
            os.link(tmp, path)
    except FileExistsError:
        raise Failure(EXIT_USAGE, f"output exists (use --force to replace): {path}")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def current_umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


def codex_version():
    try:
        r = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=30)
        return r.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def run_codex(a):
    stage = Path(tempfile.mkdtemp(prefix="codex-imagegen-", dir=a.staging_root))
    try:
        last = stage / "last-message.txt"
        cmd = ["codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
               "-s", "read-only", "-C", str(stage), "-o", str(last),
               "-m", a.model, "-c", f'model_reasoning_effort="{a.effort}"']
        for ref in a.ref:
            cmd += ["-i", str(ref.resolve())]
        cmd += ["--", build_prompt(a)]
        try:
            r = subprocess.run(cmd, cwd=stage, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=a.timeout)
        except FileNotFoundError:
            raise Failure(EXIT_FAILED, "the codex command was not found on PATH")
        except subprocess.TimeoutExpired:
            raise Failure(EXIT_FAILED, f"Codex did not finish within {a.timeout} s")
        events = read_events(r.stdout)
        message = last.read_text(encoding="utf-8") if last.exists() else ""
        image = thread_image(thread_id(events), codex_home())
        if image is None:
            code, text = classify(events, message)
            raise Failure(code, text)
        return image.read_bytes()
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main(argv=None):
    a = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if a.from_thread:
            a.source = thread_image(os.environ["CODEX_THREAD_ID"], codex_home())
            if a.source is None:
                raise Failure(EXIT_USAGE, "no generated image found for this Codex thread")
        if a.source is not None:
            if not a.source.is_file():
                raise Failure(EXIT_USAGE, f"--from file not found: {a.source}")
            data = a.source.read_bytes()
        else:
            data = run_codex(a)
        w, h, alpha = check_image(data, a.aspect, a.transparent)
        write_new(a.out, data, a.force)
        generated_elsewhere = a.source is not None
        if not a.no_sidecar:
            meta = {
                "aspect": a.aspect,
                "codex_version": codex_version(),
                "effort": None if generated_elsewhere else a.effort,
                "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "alpha": alpha,
                "height": h,
                "model": None if generated_elsewhere else a.model,
                "prompt": a.prompt or a.note,
                "references": [str(r) for r in a.ref],
                "transparent": a.transparent,
                "width": w,
            }
            try:
                write_new(Path(f"{a.out}.json"),
                          (json.dumps(meta, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode(), True)
            except OSError:
                a.out.unlink(missing_ok=True)
                raise
    except Failure as e:
        print(f"codex-imagegen: {e}", file=sys.stderr)
        return e.code
    print(f"{a.out} ({w}x{h})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
