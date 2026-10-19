from importlib.metadata import version

import neoson


def test_package_exposes_installed_version() -> None:
    assert neoson.__version__ == version("neoson")
