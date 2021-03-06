from rsqueakvm import error
from rsqueakvm.model.variable import W_BytesObject
from rsqueakvm.plugins.plugin import Plugin


class VMDebugging(Plugin):
    pass

plugin = VMDebugging()
plugin.userdata['stop_ui'] = False


def stop_ui_process():
    plugin.userdata['stop_ui'] = True

# @plugin.expose_primitive(unwrap_spec=[object])
# def trace(interp, s_frame, w_rcvr):
#     interp.trace = True
#     return w_rcvr

# @plugin.expose_primitive(unwrap_spec=[object])
# def untrace(interp, s_frame, w_rcvr):
#     interp.trace = False
#     return w_rcvr


@plugin.expose_primitive(unwrap_spec=[object])
def halt(interp, s_frame, w_rcvr):
    from rpython.rlib.debug import attach_gdb
    print s_frame.print_stack()
    attach_gdb()


@plugin.expose_primitive(unwrap_spec=[object])
def isRSqueak(interp, s_frame, w_rcvr):
    return interp.space.w_true


@plugin.expose_primitive(unwrap_spec=[object, object, list])
def performHeadless(interp, s_frame, w_rcvr, w_selector, arguments_w):
    # XXX, heul, if this entire thing just avoids over-specializing and thus
    # generating lare-stage annotation changes in vmdebugging.hooks
    from rsqueakvm import constants
    if not isinstance(w_selector, W_BytesObject):
        raise error.PrimitiveFailedError
    newi = interp.space.interp.get()
    if newi.image.special(constants.SO_NIL) is not interp.space.w_nil:
        raise error.PrimitiveFailedError
    return newi.perform_headless(w_rcvr, w_selector, arguments_w)


@plugin.expose_primitive(unwrap_spec=[object])
def isVMTranslated(interp, s_frame, w_rcvr):
    from rpython.rlib.objectmodel import we_are_translated
    if we_are_translated():
        return interp.space.w_true
    else:
        return interp.space.w_false


@plugin.expose_primitive(unwrap_spec=[object, object])
def debugPrint(interp, s_frame, w_rcvr, w_string):
    if not isinstance(w_string, W_BytesObject):
        raise error.PrimitiveFailedError
    print interp.space.unwrap_string(w_string).replace('\r', '\n')
    return w_rcvr


@plugin.expose_primitive(unwrap_spec=[object])
def stopUIProcess(interp, s_frame, w_rcvr):
    if plugin.userdata.get('stop_ui', False):
        return interp.space.w_true
    else:
        return interp.space.w_false
