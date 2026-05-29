"""Step-7b gate: the Streamlit app loads and the adapter loader works.

Skips entirely if streamlit (the `ui` extra) isn't installed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from v4sim.app import streamlit_app  # noqa: E402
from v4sim.strategies.hook_adapter import HookAdapter  # noqa: E402

APP_FILE = streamlit_app.__file__


def test_app_loads_without_exception():
    at = AppTest.from_file(APP_FILE, default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    assert at.title and "LP returns" in at.title[0].value


def test_adapter_loader_accepts_hookadapter_subclass():
    src = (
        b"from v4sim.strategies.hook_adapter import HookAdapter\n"
        b"class Adapter(HookAdapter):\n"
        b"    def rebalance(self, env, key, position, truth_sqrt_price_x96):\n"
        b"        return None\n"
    )
    adapter = streamlit_app.load_adapter_from_bytes(src, "my_adapter.py")
    assert isinstance(adapter, HookAdapter)


def test_adapter_loader_rejects_missing_class():
    with pytest.raises(RuntimeError, match="class named `Adapter`"):
        streamlit_app.load_adapter_from_bytes(b"x = 1\n", "bad.py")


def test_adapter_loader_rejects_non_subclass():
    src = b"class Adapter:\n    pass\n"
    with pytest.raises(RuntimeError, match="must subclass"):
        streamlit_app.load_adapter_from_bytes(src, "notsub.py")
