import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from oped.media import ToolsMissing, find_tools


@pytest.fixture(scope="session")
def tools():
    try:
        return find_tools()
    except ToolsMissing as e:
        pytest.skip(str(e))
