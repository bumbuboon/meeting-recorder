#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "Resources/Scripts/interpret_codex.sh"


class InterpretCodexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.prompt = self.run_dir / "prompt.json"
        self.prompt.write_text(
            json.dumps({"segments": [{"start": 0, "end": 12, "text": "決定事項です"}]}),
            encoding="utf-8",
        )
        self.output = self.run_dir / "sections.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def fake_codex(self, body: str) -> Path:
        path = self.bin / "codex"
        path.write_text("#!/bin/bash\nset -euo pipefail\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_script(self, **extra_environment: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(extra_environment)
        environment["PATH"] = f"{self.bin}:{environment['PATH']}"
        return subprocess.run(
            [str(SCRIPT), str(self.prompt), str(self.output)],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

    def test_contract_schema_cwd_and_retry(self) -> None:
        args_log = self.root / "args.json"
        cwd_log = self.root / "cwd"
        attempts = self.root / "attempts"
        self.fake_codex(
            f"""
            printf '%s\\n' "$PWD" > {cwd_log!s}
            printf '%s\\n' "$@" > {args_log!s}
            input=$(cat)
            grep -q 'roughly one section per 5 to 10 minutes' <<<"$input"
            grep -q '機能検証だと自称' <<<"$input"
            count=0
            [ ! -f {attempts!s} ] || count=$(cat {attempts!s})
            count=$((count + 1))
            printf '%s' "$count" > {attempts!s}
            [ "$count" -gt 1 ] || exit 42
            args=("$@")
            for ((i=0; i<${{#args[@]}}; i++)); do
              if [ "${{args[$i]}}" = "-o" ]; then
                out="${{args[$((i + 1))]}}"
              fi
            done
            printf '%s\\n' '{{"meeting_title":"新薬導入方針の検討","disposition":"keep","confidence":0.93,"reason":"導入条件を具体的に相談しています","sections":[{{"title":"決定","timestamp":0,"capture_timestamp":5,"summary":"要約","bullets":["実行する"]}}]}}' > "$out"
            """
        )

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(attempts.read_text(), "2")
        self.assertEqual(Path(cwd_log.read_text().strip()).resolve(), self.run_dir.resolve())
        args = args_log.read_text().splitlines()
        for expected in ("exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only", "-m", "gpt-5.6-luna", "-C", str(self.run_dir), "--output-schema", "-o", "-"):
            self.assertIn(expected, args)
        schema = Path(args[args.index("--output-schema") + 1])
        self.assertEqual(
            set(json.loads(schema.read_text())["required"]),
            {"meeting_title", "disposition", "confidence", "reason", "sections"},
        )
        output = json.loads(self.output.read_text())
        self.assertEqual(output["sections"][0]["capture_timestamp"], 5)
        self.assertEqual(output["meeting_title"], "新薬導入方針の検討")
        self.assertEqual(output["disposition"], "keep")

    def test_invalid_schema_retries_then_fails_without_output(self) -> None:
        attempts = self.root / "attempts"
        self.fake_codex(
            f"""
            count=0
            [ ! -f {attempts!s} ] || count=$(cat {attempts!s})
            count=$((count + 1))
            printf '%s' "$count" > {attempts!s}
            args=("$@")
            for ((i=0; i<${{#args[@]}}; i++)); do
              [ "${{args[$i]}}" != "-o" ] || out="${{args[$((i + 1))]}}"
            done
            printf '%s\\n' '{{"sections":[{{"title":"missing fields"}}]}}' > "$out"
            """
        )

        result = self.run_script()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(attempts.read_text(), "2")
        self.assertFalse(self.output.exists())
        self.assertIn("failed after retry", result.stderr)


if __name__ == "__main__":
    unittest.main()
