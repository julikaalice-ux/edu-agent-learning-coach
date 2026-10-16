"""Assemble the Hugging Face Space folder.

The Space runs space/app.py next to the learning_coach package, flat:

    build/space/
        app.py  Dockerfile  requirements.txt  README.md
        learning_coach/  agents/  instructions.md  response.schema.json

Usage:  python scripts/build_space.py [output-folder]
Then upload the folder to the Space (see space/README.md). Nothing secret is
copied: .env files, data folders and caches are left out.
"""

from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src/agent-framework-agent-with-local-tools-responses"
SPACE_FILES = ("app.py", "Dockerfile", "requirements.txt", "README.md")
RUNTIME_FILES = ("instructions.md", "response.schema.json")
RUNTIME_DIRS = ("learning_coach", "agents")
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", ".env", "*.env", ".learning-coach", ".DS_Store")


def build(out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name in SPACE_FILES:
        shutil.copy2(ROOT / "space" / name, out / name)
    for name in RUNTIME_FILES:
        shutil.copy2(SRC / name, out / name)
    for name in RUNTIME_DIRS:
        shutil.copytree(SRC / name, out / name, ignore=SKIP)
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build/space"
    folder = build(target.resolve())
    count = sum(1 for path in folder.rglob("*") if path.is_file())
    print(f"Space folder ready: {folder} ({count} files)")
