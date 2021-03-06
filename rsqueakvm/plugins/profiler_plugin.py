import os

from rsqueakvm.error import PrimitiveFailedError
from rsqueakvm.model.compiled_methods import W_CompiledMethod
from rsqueakvm.plugins.plugin import Plugin
from rsqueakvm.primitives import expose_primitive, expose_also_as
from rsqueakvm.primitives.constants import (
    VM_CLEAR_PROFILE, VM_DUMP_PROFILE, VM_START_PROFILING, VM_STOP_PROFILING)
from rsqueakvm.util import system

from rpython.rlib import jit


def patch_interpreter():
    from rpython.rlib import rvmprof
    from rsqueakvm.interpreter import Interpreter

    def _get_code(interp, s_frame, s_sender, may_context_switch):
        return s_frame.w_method()
    _decorator = rvmprof.vmprof_execute_code("rsqueak", _get_code)
    _my_stack_frame = _decorator(Interpreter.stack_frame)
    Interpreter.stack_frame = _my_stack_frame
    print "Interpreter was patched for vmprof"


def _safe(s):
    if len(s) > 200:
        s = s[:197] + '...'
    return s.replace(':', ';')


def _get_full_name(w_cm):
    # must not be longer than 255 chars
    return "st:%s:0:/img" % _safe(w_cm.safe_identifier_string())


def patch_compiled_method():
    from rpython.rlib import rvmprof
    def _my_post_init(self):
        rvmprof.register_code(self, _get_full_name)
    W_CompiledMethod.post_init = _my_post_init

# ____________________________________________________________


class ProfilerPlugin(Plugin):

    def is_optional(self):
        return True

    def setup(self):
        from rpython.rlib import rvmprof
        rvmprof.register_code_object_class(W_CompiledMethod, _get_full_name)

    def patch(self):
        patch_interpreter()
        patch_compiled_method()

plugin = ProfilerPlugin()


@plugin.expose_primitive(unwrap_spec=[object, int, float])
@jit.dont_look_inside
def enableProfiler(interp, s_frame, w_rcvr, fileno, period):
    from rpython.rlib import rvmprof
    try:
        rvmprof.enable(fileno, period)
    except rvmprof.VMProfError as e:
        print e.msg
        raise PrimitiveFailedError
    return w_rcvr


@plugin.expose_primitive(unwrap_spec=[object])
@jit.dont_look_inside
def disableProfiler(interp, s_frame, w_rcvr):
    from rpython.rlib import rvmprof
    try:
        rvmprof.disable()
    except rvmprof.VMProfError as e:
        print e.msg
        raise PrimitiveFailedError
    return w_rcvr


@plugin.expose_primitive(unwrap_spec=[object, int])
@jit.dont_look_inside
def enableJitlog(interp, s_frame, w_rcvr, fileno):
    from rpython.rlib.rjitlog import rjitlog
    try:
        rjitlog.enable_jitlog(fileno)
    except rjitlog.JitlogError as e:
        print e.msg
        raise PrimitiveFailedError
    return w_rcvr


@plugin.expose_primitive(unwrap_spec=[object])
@jit.dont_look_inside
def disableJitlog(interp, s_frame, w_rcvr):
    from rpython.rlib.rjitlog import rjitlog
    rjitlog.disable_jitlog()
    return w_rcvr


O_BINARY = 0
if system.IS_WINDOWS:
    O_BINARY = os.O_BINARY
class LogFile(object):
    _attrs_ = ["fd"]
    def __init__(self): self.fd = -1
    def fileno(self): return self.fd
    def isopen(self): return self.fd > 0
    def open(self, name):
        self.fd = os.open(name, os.O_RDWR | os.O_CREAT | O_BINARY, 0666)
    def close(self):
        if not self.isopen(): return
        os.close(self.fd)
        self.fd = -1
jitlogfile = LogFile()
vmproflogfile = LogFile()


@expose_also_as(VM_DUMP_PROFILE, VM_CLEAR_PROFILE)
@expose_primitive(VM_STOP_PROFILING, unwrap_spec=[object])
@jit.dont_look_inside
def func(interp, s_frame, w_rcvr):
    from rpython.rlib.rjitlog import rjitlog
    from rpython.rlib import rvmprof
    from rsqueakvm.plugins.profiler_plugin import vmproflogfile, jitlogfile
    if vmproflogfile.isopen():
        try:
            rvmprof.disable()
        except rvmprof.VMProfError as e:
            print "Failure disabling vmprof: %s" % e.msg
    if jitlogfile.isopen():
        rjitlog.disable_jitlog()
    vmproflogfile.close()
    jitlogfile.close()
    return w_rcvr

DEFAULT_PERIOD = 0.001
@expose_primitive(VM_START_PROFILING, unwrap_spec=[object])
@jit.dont_look_inside
def func(interp, s_frame, w_rcvr):
    from rpython.rlib.rjitlog import rjitlog
    from rpython.rlib import rvmprof
    from rsqueakvm.plugins.profiler_plugin import vmproflogfile, jitlogfile
    if not vmproflogfile.isopen():
        vmproflogfile.open("SqueakProfile")
        try:
            rvmprof.enable(vmproflogfile.fileno(), DEFAULT_PERIOD)
        except rvmprof.VMProfError as e:
            print "Failed to start vmprof: %s" % e.msg
            vmproflogfile.close()
    if not jitlogfile.isopen():
        jitlogfile.open("SqueakProfile.jitlog")
        try:
            rjitlog.enable_jitlog(jitlogfile.fileno())
        except rjitlog.JitlogError as e:
            print "Failed to start jitlog: %s" % e.msg
            jitlogfile.close()
    return w_rcvr
