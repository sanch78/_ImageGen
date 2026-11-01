"""Offline tests for codex_imagegen.py: no network, no Codex quota.

Run: python3 -m unittest discover -s tests
"""
import contextlib
import io
import json
import os
import stat
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / ".agents/skills/codex-imagegen/scripts"))
import codex_imagegen as ci  # noqa: E402


def png(w, h, color_type=2):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", b"") + chunk(b"IEND", b"")


STUB = """#!/usr/bin/env python3
import os, pathlib, sys
if sys.argv[1:2] == ["--version"]:
    print("codex-cli 0.0.0-stub"); sys.exit(0)
mode = os.environ["STUB_MODE"]
print(pathlib.Path(os.environ["STUB_EVENTS"]).read_text(), end="")
if mode == "ok":
    d = pathlib.Path(os.environ["CODEX_HOME"]) / "generated_images" / os.environ["STUB_THREAD"]
    d.mkdir(parents=True)
    (d / "exec-1.png").write_bytes(pathlib.Path(os.environ["STUB_PNG"]).read_bytes())
pathlib.Path(os.environ["STUB_SEEN"]).write_text(os.getcwd() + "\\n" + " ".join(sys.argv[1:]))
sys.exit(0 if mode == "ok" else 1)
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="codex-imagegen-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.stage = self.tmp / "stage-root"
        self.stage.mkdir()
        self.home = self.tmp / "codex-home"
        self.out = self.tmp / "out.png"

    def run_main(self, *args, env=None):
        err = io.StringIO()
        with mock.patch.dict(os.environ, env or {}), contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                code = ci.main(list(args))
            except SystemExit as e:
                code = e.code
        return code, err.getvalue()


class Arguments(Base):
    def test_requires_png_output(self):
        code, err = self.run_main("--prompt", "x", "--staging-root", str(self.stage),
                                  "--out", str(self.tmp / "a.jpg"))
        self.assertEqual(code, 2)
        self.assertIn(".png", err)

    def test_refuses_existing_output_without_force(self):
        self.out.write_bytes(b"keep")
        code, err = self.run_main("--prompt", "x", "--staging-root", str(self.stage), "--out", str(self.out))
        self.assertEqual(code, 2)
        self.assertIn("--force", err)
        self.assertEqual(self.out.read_bytes(), b"keep")

    def test_requires_existing_staging_root(self):
        code, _ = self.run_main("--prompt", "x", "--staging-root", str(self.tmp / "missing"),
                                "--out", str(self.out))
        self.assertEqual(code, 2)

    def test_rejects_empty_prompt_and_missing_parent(self):
        self.assertEqual(self.run_main("--prompt", " ", "--staging-root", str(self.stage),
                                       "--out", str(self.out))[0], 2)
        self.assertEqual(self.run_main("--prompt", "x", "--staging-root", str(self.stage),
                                       "--out", str(self.tmp / "no/out.png"))[0], 2)

    def test_from_thread_needs_codex_thread(self):
        env = {k: v for k, v in os.environ.items() if k != "CODEX_THREAD_ID"}
        with mock.patch.dict(os.environ, env, clear=True):
            code, err = self.run_main("--from-thread", "--out", str(self.out))
        self.assertEqual(code, 2)
        self.assertIn("CODEX_THREAD_ID", err)

    def test_prompt_mentions_aspect_transparency_and_refs(self):
        a = ci.parse_args(["--prompt", "a red fox", "--aspect", "portrait", "--transparent",
                           "--staging-root", str(self.stage), "--out", str(self.out)])
        text = ci.build_prompt(a)
        self.assertIn("portrait", text)
        self.assertIn("transparent", text)
        self.assertTrue(text.endswith("a red fox"))


class Events(unittest.TestCase):
    def load(self, name):
        return ci.read_events((FIXTURES / name).read_text())

    def test_success_sample_exposes_only_the_thread(self):
        events = self.load("exec-ok.jsonl")
        self.assertEqual(ci.thread_id(events), "01a19704-7867-76d2-b2c7-1bd771e44c57")
        self.assertEqual(events[-1]["type"], "turn.completed")

    def test_logged_out_sample_is_an_auth_failure(self):
        code, message = ci.classify(self.load("exec-logged-out.jsonl"), "")
        self.assertEqual(code, ci.EXIT_AUTH)
        self.assertIn("codex login", message)

    def test_limit_refusal_and_generic_failure(self):
        failed = [{"type": "turn.failed", "error": {"message": "usage_limit_exceeded"}}]
        self.assertEqual(ci.classify(failed, "")[0], ci.EXIT_LIMIT)
        self.assertEqual(ci.classify([], "IMAGEGEN_FAILED: policy")[0], ci.EXIT_REFUSED)
        other = [{"type": "turn.failed", "error": {"message": "stream disconnected"}}]
        self.assertEqual(ci.classify(other, "")[0], ci.EXIT_FAILED)

    def test_ignores_non_json_lines(self):
        self.assertEqual(ci.read_events('noise\n{"type":"turn.started"}\n{bad\n'),
                         [{"type": "turn.started"}])


class Images(Base):
    def test_png_size_alpha_and_aspect(self):
        self.assertEqual(ci.png_info(png(1536, 1024)), (1536, 1024, False))
        self.assertEqual(ci.png_info(png(64, 64, 6))[2], True)
        self.assertIsNone(ci.png_info(b"GIF89a" + b"\0" * 30))
        self.assertTrue(ci.aspect_ok("square", 1254, 1254))
        self.assertFalse(ci.aspect_ok("landscape", 1024, 1536))
        self.assertTrue(ci.aspect_ok("portrait", 1024, 1536))

    def test_thread_image_picks_newest_png_of_that_thread(self):
        d = self.home / "generated_images" / "t1"
        d.mkdir(parents=True)
        (d / "a.png").write_bytes(b"1")
        (d / "b.png").write_bytes(b"2")
        os.utime(d / "a.png", (1, 1))
        self.assertEqual(ci.thread_image("t1", self.home).name, "b.png")
        self.assertIsNone(ci.thread_image("t2", self.home))
        self.assertIsNone(ci.thread_image(None, self.home))

    def test_from_places_validates_and_writes_sidecar(self):
        src = self.tmp / "src.png"
        src.write_bytes(png(1024, 1536))
        code, _ = self.run_main("--from", str(src), "--aspect", "portrait", "--note", "n", "--out", str(self.out))
        self.assertEqual(code, 0)
        self.assertEqual(self.out.read_bytes(), src.read_bytes())
        meta = json.loads(Path(f"{self.out}.json").read_text())
        self.assertEqual((meta["width"], meta["height"], meta["prompt"]), (1024, 1536, "n"))
        self.assertEqual(list(meta), sorted(meta))
        self.assertIsNone(meta["effort"])

    def test_rejects_unknown_effort(self):
        code, _ = self.run_main("--prompt", "x", "--effort", "extreme", "--staging-root", str(self.stage),
                                "--out", str(self.out))
        self.assertEqual(code, 2)

    def test_wrong_aspect_or_missing_alpha_writes_nothing(self):
        src = self.tmp / "src.png"
        src.write_bytes(png(1024, 1024))
        self.assertEqual(self.run_main("--from", str(src), "--aspect", "landscape", "--out", str(self.out))[0], 1)
        self.assertEqual(self.run_main("--from", str(src), "--transparent", "--out", str(self.out))[0], 1)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["src.png", "stage-root"])

    def test_from_thread_uses_codex_thread_id(self):
        d = self.home / "generated_images" / "t9"
        d.mkdir(parents=True)
        (d / "x.png").write_bytes(png(800, 800, 6))
        env = {"CODEX_HOME": str(self.home), "CODEX_THREAD_ID": "t9"}
        code, _ = self.run_main("--from-thread", "--transparent", "--out", str(self.out), env=env)
        self.assertEqual(code, 0)
        self.assertTrue(self.out.exists())


class Run(Base):
    """Drive run_codex against a stub `codex` on PATH."""

    def stub_env(self, mode, events):
        bindir = self.tmp / "bin"
        bindir.mkdir(exist_ok=True)
        stub = bindir / "codex"
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        image = self.tmp / "gen.png"
        image.write_bytes(png(1536, 1024))
        self.seen = self.tmp / "seen.txt"
        return {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "CODEX_HOME": str(self.home),
            "STUB_MODE": mode,
            "STUB_EVENTS": str(FIXTURES / events),
            "STUB_THREAD": "01a19704-7867-76d2-b2c7-1bd771e44c57",
            "STUB_PNG": str(image),
            "STUB_SEEN": str(self.seen),
        }

    def test_success_copies_thread_image_and_removes_staging(self):
        env = self.stub_env("ok", "exec-ok.jsonl")
        code, err = self.run_main("--prompt", "hero", "--aspect", "landscape",
                                  "--staging-root", str(self.stage), "--out", str(self.out), env=env)
        self.assertEqual(code, 0, err)
        self.assertEqual(ci.png_info(self.out.read_bytes()), (1536, 1024, False))
        self.assertEqual(json.loads(Path(f"{self.out}.json").read_text())["codex_version"],
                         "codex-cli 0.0.0-stub")
        cwd, argv = self.seen.read_text().split("\n", 1)
        self.assertEqual(Path(cwd).parent, self.stage)
        self.assertIn("-s read-only", argv)
        self.assertIn('-m gpt-6.1-sol -c model_reasoning_effort="medium"', argv)
        self.assertNotIn("dangerously", argv)
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_model_and_effort_reach_codex_and_sidecar(self):
        env = self.stub_env("ok", "exec-ok.jsonl")
        code, err = self.run_main("--prompt", "hero", "--aspect", "landscape", "--model", "m2",
                                  "--effort", "high", "--staging-root", str(self.stage),
                                  "--out", str(self.out), env=env)
        self.assertEqual(code, 0, err)
        self.assertIn('-m m2 -c model_reasoning_effort="high"', self.seen.read_text())
        meta = json.loads(Path(f"{self.out}.json").read_text())
        self.assertEqual((meta["model"], meta["effort"]), ("m2", "high"))

    def test_logged_out_exits_3_without_output(self):
        env = self.stub_env("fail", "exec-logged-out.jsonl")
        code, err = self.run_main("--prompt", "hero", "--staging-root", str(self.stage),
                                  "--out", str(self.out), env=env)
        self.assertEqual(code, ci.EXIT_AUTH)
        self.assertIn("codex login", err)
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.stage.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
