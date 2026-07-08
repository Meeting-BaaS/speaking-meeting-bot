"""Export the current FastAPI OpenAPI schema to the repo snapshot."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = (ROOT / "openapi.json", ROOT / "speaking-bot-openapi.json")

sys.path.insert(0, str(ROOT))

from app.main import create_app  # noqa: E402


def main() -> None:
    schema = create_app().openapi()
    serialized = json.dumps(schema, indent=2) + "\n"
    for output in OUTPUTS:
        output.write_text(serialized, encoding="utf-8")
        print(f"Wrote {output.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
