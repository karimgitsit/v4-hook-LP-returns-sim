"""Load forge-built contract artifacts from contracts/v4-core/out/.

The harness expects `forge build` has been run inside contracts/v4-core/ so
that JSON artifacts exist under contracts/v4-core/out/<file>.sol/<name>.json.

Bytecode is returned as raw bytes; ABI as the parsed list.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_V4_CORE_OUT = _REPO_ROOT / "contracts" / "v4-core" / "out"


class ArtifactNotFound(FileNotFoundError):
    """Raised when a requested artifact is missing — usually means `forge build` wasn't run."""


def _load(file_sol: str, contract_name: str) -> dict:
    path = _V4_CORE_OUT / file_sol / f"{contract_name}.json"
    if not path.exists():
        raise ArtifactNotFound(
            f"missing artifact {path} — run `forge build` inside contracts/v4-core/ first"
        )
    with path.open() as f:
        return json.load(f)


@cache
def get(file_sol: str, contract_name: str | None = None) -> dict:
    """Return parsed artifact JSON for contracts/v4-core/out/<file_sol>/<contract>.json.

    If contract_name is omitted, derived by stripping the .sol suffix from file_sol.
    """
    if contract_name is None:
        if not file_sol.endswith(".sol"):
            raise ValueError(f"file_sol must end with .sol (got {file_sol!r})")
        contract_name = file_sol[:-4]
    return _load(file_sol, contract_name)


def bytecode(file_sol: str, contract_name: str | None = None) -> bytes:
    art = get(file_sol, contract_name)
    obj = art["bytecode"]["object"]
    if obj.startswith("0x"):
        obj = obj[2:]
    return bytes.fromhex(obj)


def abi(file_sol: str, contract_name: str | None = None) -> list:
    return get(file_sol, contract_name)["abi"]
