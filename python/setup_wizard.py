"""Compatibility facade for the setup wizard.

The implementation lives in the `setupwiz` package (core helpers, base
steps, model steps, GGUF steps, menu). The wizard entry point stays here so
setup.bat / setup.sh and the smoke tests keep working unchanged; writes to
DRY_RUN / AUTO / ENV_FILE (as tests do) are forwarded to `setupwiz.core`.
"""
from __future__ import annotations

import sys
import types

if __package__ in (None, ""):
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import setupwiz.core as _core

# names server.py and tests/e2e_smoke.py use
from setupwiz.core import (BIN_DIR, CORE_PKGS, DEFAULT_GROUPS, ENV_FILE,
                           GGUF_ASR_BASE, KOKORO_BASE, KOKORO_FILES,
                           KOKORO_VOICES, LLM_GGUF_DIR, OPTIONAL_GROUPS,
                           PIPER_BASE, PIPER_VOICES, ROOT, USER_DATA,
                           VENV_DIR, WHISPER_BASE, WHISPER_ORDER,
                           WHISPER_SIZES_MB, YAML_FILE, ask, ask_secret,
                           download_resumable, hf_list_files, hr,
                           install_group, pick_hf_endpoint, pip_installed,
                           read_env, run, venv_python, whisper_repo,
                           write_env)

# forwarding module: attribute writes land on setupwiz.core
_FORWARDED_WRITES = {"DRY_RUN", "AUTO", "ENV_FILE"}


class _Facade(types.ModuleType):
    def __setattr__(self, name, value):
        if name in _FORWARDED_WRITES:
            setattr(_core, name, value)
        else:
            super().__setattr__(name, value)


sys.modules[__name__].__class__ = _Facade


def __getattr__(name):  # PEP 562
    try:
        return getattr(_core, name)
    except AttributeError:
        pass
    import setupwiz.menu as _menu

    try:
        return getattr(_menu, name)
    except AttributeError:
        raise AttributeError(f"module 'setup_wizard' has no attribute "
                             f"{name!r}") from None


if __name__ == "__main__":
    from setupwiz.menu import main

    main()
