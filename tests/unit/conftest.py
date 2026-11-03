"""Import protocol/transport without importing the HA setup entry point."""

import sys
from pathlib import Path
from types import ModuleType

package = ModuleType("_rest_unit")
package.__path__ = [str(Path(__file__).parents[2] / "custom_components/hatch_rest_ble")]
sys.modules[package.__name__] = package
