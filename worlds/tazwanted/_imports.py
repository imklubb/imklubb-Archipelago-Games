"""
_imports.py -- let the modules work both inside the apworld and on their own.

Archipelago loads this folder as a package, so its modules have to import each
other relatively: `from . import taz_data`. But the same files are run directly
during development -- `python taz_data.py` self-tests, and the tools import
them plainly -- and a relative import fails outside a package.

Rather than maintain two copies, each module asks here for what it needs and
gets whichever style works.
"""

import importlib
import sys


def load(name):
    """Import a sibling module, package or not."""
    pkg = __package__ or ""
    if pkg:
        try:
            return importlib.import_module(f".{name}", pkg)
        except ImportError:
            pass
    return importlib.import_module(name)


def optional(name):
    """The same, but None when it is unavailable.

    pcsx2_mem calls sys.exit when pine is missing, which raises SystemExit
    rather than ImportError -- so generation on a server with no emulator must
    catch both or the whole world fails to load.
    """
    try:
        return load(name)
    except (Exception, SystemExit):
        return None


def data(name):
    """Read a file from the world's data/ folder, zipped or not.

    Archipelago loads an apworld straight out of a .apworld zip, so paths
    built from __file__ point inside an archive: os.path.exists returns False
    and open() raises. Every data file silently went missing when run that way
    while working perfectly from a plain folder.

    pkgutil.get_data goes through the import machinery, so it reads from
    either. The filesystem is still tried afterwards for the case where the
    tools are run outside a package.
    """
    import json
    import os
    import pkgutil

    pkg = __package__ or ""
    if pkg:
        try:
            raw = pkgutil.get_data(pkg, f"data/{name}")
            if raw:
                return json.loads(raw.decode("utf-8"))
        except Exception:
            pass

    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "data", name),
                 os.path.join(here, name),
                 name):
        try:
            if os.path.exists(cand):
                with open(cand, encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
    return None


def extract_images(names, subdir="maps"):
    """Unpack bundled images to real files, and return the folder.

    Kivy loads a texture from a PATH, and inside a .apworld that path points
    into a zip -- so the pins drew and the map behind them did not. Writing
    them out once per session is the only way it can see them.

    Cached: the same folder is reused for the life of the process.
    """
    import os
    import pkgutil
    import tempfile

    global _IMG_DIR
    if _IMG_DIR is not None:
        return _IMG_DIR

    out = os.path.join(tempfile.gettempdir(), "taz_wanted_maps")
    os.makedirs(out, exist_ok=True)
    pkg = __package__ or ""
    for name in names:
        dest = os.path.join(out, name)
        if os.path.exists(dest):
            continue
        raw = None
        if pkg:
            try:
                raw = pkgutil.get_data(pkg, f"data/{subdir}/{name}")
            except Exception:
                raw = None
        if raw is None:
            src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "data", subdir, name)
            if os.path.exists(src):
                with open(src, "rb") as f:
                    raw = f.read()
        if raw:
            with open(dest, "wb") as f:
                f.write(raw)
    _IMG_DIR = out
    return out


_IMG_DIR = None
