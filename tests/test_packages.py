from importlib.metadata import version
from types import ModuleType

import pytest

import neoson_contracts
import neoson_core
import neoson_gateway
import neoson_robot


@pytest.mark.parametrize(
    ("module", "distribution"),
    [
        (neoson_contracts, "neoson-contracts"),
        (neoson_core, "neoson-core"),
        (neoson_gateway, "neoson-gateway"),
        (neoson_robot, "neoson-robot"),
    ],
)
def test_package_exposes_installed_version(
    module: ModuleType, distribution: str
) -> None:
    assert module.__version__ == version(distribution)
