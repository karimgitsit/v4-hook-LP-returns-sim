"""`v4sim-app` — launch the Streamlit UI.

Thin shim so users can run the app without remembering the file path:

    v4sim-app            # == streamlit run src/v4sim/app/streamlit_app.py

Any extra args are forwarded to ``streamlit run``.
"""

from __future__ import annotations

import sys
from pathlib import Path

APP_PATH = Path(__file__).resolve().parent / "app" / "streamlit_app.py"


def cli(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        from streamlit.web import cli as st_cli
    except ImportError:
        sys.stderr.write(
            "Streamlit is not installed. Install the UI extra:\n"
            '    pip install -e ".[ui]"\n'
        )
        return 1
    sys.argv = ["streamlit", "run", str(APP_PATH), *argv]
    return st_cli.main()  # type: ignore[no-any-return]


if __name__ == "__main__":
    raise SystemExit(cli())
