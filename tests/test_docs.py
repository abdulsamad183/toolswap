import re
import shlex
import subprocess
import sys
from pathlib import Path


def test_readme_links_and_cli_examples() -> None:
    readme = Path("README.md").read_text()
    for relative in re.findall(r"\]\(([^)]+\.md)\)", readme):
        assert Path(relative).is_file(), relative
    commands = re.findall(r"^uv run --locked toolswap (.+)$", readme, flags=re.MULTILINE)
    assert len(commands) == 3
    for arguments in commands:
        result = subprocess.run(
            [sys.executable, "-m", "toolswap", *shlex.split(arguments)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip()
