"""
Reads the title/key_points that pipeline/step02_script.py's generate_script()
already writes to job_dir/script.json, instead of re-extracting the PDF text
a second time just to build a video description.
"""

import json
from pathlib import Path


def load_script_title_and_points(job_dir: Path) -> tuple[str, str]:
    script_path = Path(job_dir) / "script.json"
    with open(script_path, encoding="utf-8") as f:
        script = json.load(f)

    title = script.get("title", "")
    key_points = "\n".join(f"- {p}" for p in script.get("key_points", []))
    return title, key_points
