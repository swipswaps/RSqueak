#! /usr/bin/env python
import sys
import os

from rsqueakvm.util import system

from rpython.config.config import to_optparse
from rpython.config.translationoption import get_combined_translation_config
from rpython.jit.codewriter.policy import JitPolicy
from rpython.rlib import objectmodel


sys.setrecursionlimit(15000)

def target(driver, args):
    driver.exe_name = "rsqueak"
    config = driver.config
    parser(config).parse_args(args)

    driver.config.translation.suggest(**{
        "jit": True,
        "jit_opencoder_model": "big",
    })
    driver.config.translation.set(gcrootfinder="shadowstack")
    if system.IS_WINDOWS:
        driver.config.translation.suggest(**{
            "icon": os.path.join(os.path.dirname(__file__), "rsqueak.ico")
        })
    config.translating = True

    system.expose_options(driver.config)

    if 'PythonPlugin' in system.optional_plugins:
        # Disable vmprof, because it causes compiling errors
        system.disabled_plugins += ',ProfilerPlugin'

    # We must not import this before the config was exposed
    from rsqueakvm.main import safe_entry_point
    if 'PythonPlugin' in system.optional_plugins:
        from rsqueakvm.plugins.python.utils import entry_point
        from pypy.tool.ann_override import PyPyAnnotatorPolicy
        ann_policy = PyPyAnnotatorPolicy()
        return entry_point, None, ann_policy
    return safe_entry_point, None, None

def jitpolicy(self):
    if "PythonPlugin" in system.optional_plugins:
        from pypy.module.pypyjit.policy import PyPyJitPolicy
        from pypy.module.pypyjit.hooks import pypy_hooks
        return PyPyJitPolicy(pypy_hooks)
    elif "JitHooks" in system.optional_plugins:
        from rsqueakvm.plugins.vmdebugging.hooks import jitiface
        return JitPolicy(jitiface)
    else:
        return JitPolicy()

def parser(config):
    return to_optparse(config, useoptions=["rsqueak.*"])

def print_help(config):
    to_optparse(config).print_help()

take_options = True

def get_additional_config_options():
    return system.translation_options()


if __name__ == '__main__':
    assert not objectmodel.we_are_translated()
    from rpython.translator.driver import TranslationDriver
    driver = TranslationDriver()
    driver.config = get_combined_translation_config(
        system.translation_options(),
        translating=False)
    if "--" in sys.argv:
        idx = sys.argv.index("--")
        configargs, args = sys.argv[0:idx], sys.argv[idx:]
    else:
        configargs, args = [], sys.argv
    f, _, _ = target(driver, configargs)
    try:
        sys.exit(f(args))
    except SystemExit:
        pass
    except:
        if hasattr(sys, 'ps1') or not sys.stderr.isatty():
            # we are in interactive mode or we don't have a tty-like
            # device, so we call the default hook
            sys.__excepthook__(type, value, tb)
        else:
            import pdb, traceback
            _type, value, tb = sys.exc_info()
            traceback.print_exception(_type, value, tb)
            pdb.post_mortem(tb)
