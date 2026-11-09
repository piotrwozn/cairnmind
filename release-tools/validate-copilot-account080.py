"""Gated real Copilot account controller; see validate-native-account080.py.

The SDK reuses the existing account's private runtime/OAuth cache. Only its own
normal token refresh is permitted there; Store, engine and projects are synthetic.
"""
from pathlib import Path
import importlib.util


if __name__ == "__main__":
    spec = importlib.util.spec_from_file_location("account080_controller", Path(__file__).with_name("validate-native-account080.py"))
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    raise SystemExit(controller.main("copilot"))
