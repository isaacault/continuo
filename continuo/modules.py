"""continuo's parts (docs/design/video.md §3): core is always on; the others run when
CONTINUO_MODULES lists them. A part that's off mounts no routes, starts no background loops and
isn't imported at all, so a server that doesn't use it doesn't pay for it."""
import importlib
from types import ModuleType

from . import config

# Name -> the package that defines it: routers(), loops(), on_start(), sign_in(user, device).
KNOWN = {"music": "continuo.music", "video": "continuo.video"}


def enabled() -> list[str]:
    return list(config.settings.modules)


def is_on(name: str) -> bool:
    return name in config.settings.modules


def load() -> list[ModuleType]:
    """The enabled parts' packages, imported now (and only those)."""
    unknown = [m for m in config.settings.modules if m not in KNOWN]
    if unknown:
        raise RuntimeError(f"CONTINUO_MODULES: unknown part(s) {', '.join(unknown)}; known: {', '.join(KNOWN)}")
    return [importlib.import_module(KNOWN[m]) for m in config.settings.modules]
