"""The earlier name of the `cairnmind` package, kept for one release.

Hooks and MCP entries written by DevBrain 0.8.0 into existing projects start
`python -m devbrain ...`; they must keep working until `cairnmind install` rewrites them.
Importing `devbrain.<module>` gives the very same module object as `cairnmind.<module>`,
so no state is ever duplicated. New code imports `cairnmind`.
"""
from importlib import import_module
from importlib.abc import Loader, MetaPathFinder
from importlib.util import find_spec, spec_from_loader
import sys

from cairnmind import __version__

__all__ = ["__version__"]


class _SameModule(Loader):
    def __init__(self, real):
        self.real = real
        self.real_spec = None

    def create_module(self, spec):
        module = import_module(self.real)
        self.real_spec = module.__spec__
        return module

    def exec_module(self, module):
        # The module is already executed under its real name. The import machinery has
        # just put the alias spec on it; give it back its own, or `reload` and `runpy`
        # would be led here again instead of to the real loader.
        module.__spec__ = self.real_spec

    def get_code(self, fullname):
        # `python -m devbrain.<module>` runs the real module's code as `__main__`.
        return find_spec(self.real).loader.get_code(self.real)


class _Redirect(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("devbrain.") or fullname == "devbrain.__main__":
            return None
        real = "cairnmind" + fullname[len("devbrain"):]
        try:
            import_module(real)
        except ModuleNotFoundError as exc:
            if exc.name == real:
                return None
            raise
        return spec_from_loader(fullname, _SameModule(real))


if not any(isinstance(finder, _Redirect) for finder in sys.meta_path):
    sys.meta_path.insert(0, _Redirect())
