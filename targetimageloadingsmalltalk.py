#! /usr/bin/env python
import sys, time
import os

from rpython.rlib.streamio import open_file_as_stream
from rpython.rlib import jit, rpath

from spyvm import model, interpreter, squeakimage, objspace, wrapper,\
    error, shadow, storage_logger, constants
from spyvm.tool.analyseimage import create_image
from spyvm.interpreter_proxy import VirtualMachine

def _usage(argv):
    print """
    Usage: %s <path> [-r|-m|-h] [-naPu] [-jpiS] [-tlLE]
            <path> - image path (default: Squeak.image)

          Execution mode:
            (no flags)             - Image will be normally opened.
            -r|--run <code>        - Code will be compiled and executed, result printed.
            -m|--method <selector> - Selector will be sent to a SmallInteger, result printed.
            -h|--help              - Output this and exit.

          Execution parameters:
            -n|--num <int> - Only with -m or -r, SmallInteger to be used as receiver (default: nil).
            -a|--arg <arg> - Only with -m, will be used as single String argument.
            -P|--process   - Only with -m or -r, create a high-priority Process for the context.
                             The images last active Process will be started first.
                             By default, run in headless mode. This will ignore the active process
                             in the image and execute the context directly. The image window will
                             probably not open. Good for benchmarking.
            -u             - Only with -m or -r, try to stop UI-process at startup. Can help benchmarking.

          Other parameters:
            -j|--jit <jitargs> - jitargs will be passed to the jit configuration.
            -p|--poll          - Actively poll for events. Try this if the image is not responding well.
            -i|--no-interrupts - Disable timer interrupt. Disables non-cooperative scheduling.
            -S                 - Disable specialized storage strategies; always use generic ListStorage
            
          Logging parameters:
            -t|--trace                 - Output a trace of each message, primitive, return value and process switch.
            -l|--storage-log           - Output a log of storage operations.
            -L|--storage-log-aggregate - Output an aggregated storage log at the end of execution.
            -E|--storage-log-elements  - Include classnames of elements into the storage log.

    """ % argv[0]

def get_parameter(argv, idx, arg):
    if len(argv) < idx + 1:
        raise error.Exit("Missing argument after %s" % arg)
    return argv[idx], idx + 1
    
def get_int_parameter(argv, idx, arg):
    param, idx = get_parameter(argv, idx, arg)
    try:
        result = int(param)
    except ValueError, e:
        raise error.Exit("Non-int argument after %s" % arg)
    return result, idx
    
def print_error(str):
    os.write(2, str + os.linesep)
    
prebuilt_space = objspace.ObjSpace()

def entry_point(argv):
    # == Main execution parameters
    path = None
    selector = None
    code = ""
    number = 0
    have_number = False
    stringarg = None
    headless = True
    # == Other parameters
    poll = False
    interrupts = True
    trace = False
    
    space = prebuilt_space
    idx = 1
    try:
        while idx < len(argv):
            arg = argv[idx]
            idx += 1
            if arg in ["-h", "--help"]:
                _usage(argv)
                return 0
            elif arg in ["-j", "--jit"]:
                jitarg, idx = get_parameter(argv, idx, arg)
                jit.set_user_param(interpreter.Interpreter.jit_driver, jitarg)
            elif arg in ["-n", "--number"]:
                number, idx = get_int_parameter(argv, idx, arg)
                have_number = True
            elif arg in ["-m", "--method"]:
                selector, idx = get_parameter(argv, idx, arg)
            elif arg in ["-t", "--trace"]:
                trace = True
            elif arg in ["-p", "--poll"]:
                poll = True
            elif arg in ["-a", "--arg"]:
                stringarg, idx = get_parameter(argv, idx, arg)
            elif arg in ["-r", "--run"]:
                code, idx = get_parameter(argv, idx, arg)
            elif arg in ["-i", "--no-interrupts"]:
                interrupts = False
            elif arg in ["-P", "--process"]:
                headless = False
            elif arg in ["-S"]:
                space.no_specialized_storage.set()
            elif arg in ["-u"]:
                from spyvm.plugins.vmdebugging import stop_ui_process
                stop_ui_process()
            elif arg in ["-l", "--storage-log"]:
                storage_logger.activate()
            elif arg in ["-L", "--storage-log-aggregate"]:
                storage_logger.activate(aggregate=True)
            elif arg in ["-E", "--storage-log-elements"]:
                storage_logger.activate(elements=True)
            elif path is None:
                path = arg
            else:
                _usage(argv)
                return -1
        
        if path is None:
            path = "Squeak.image"
        if code and selector:
            raise error.Exit("Cannot handle both -r and -m.")
    except error.Exit as e:
        print_error("Parameter error: %s" % e.msg)
        return 1
    
    path = rpath.rabspath(path)
    try:
        f = open_file_as_stream(path, mode="rb", buffering=0)
        try:
            imagedata = f.readall()
        finally:
            f.close()
    except OSError as e:
        print_error("%s -- %s (LoadError)" % (os.strerror(e.errno), path))
        return 1

    # Load & prepare image and environment
    image_reader = squeakimage.reader_for_image(space, squeakimage.Stream(data=imagedata))
    image = create_image(space, image_reader)
    interp = interpreter.Interpreter(space, image, image_name=path,
                trace=trace, evented=not poll,
                interrupts=interrupts)
    space.runtime_setup(argv[0])
    print_error("") # Line break after image-loading characters
    
    # Create context to be executed
    if code or selector:
        if not have_number:
            w_receiver = space.w_nil
        else:
            w_receiver = space.wrap_int(number)
        if code:
            selector = compile_code(interp, w_receiver, code)
            if selector is None:
                return -1 # Compilation failed, message is printed.
        s_frame = create_context(interp, w_receiver, selector, stringarg)
        if headless:
            space.headless.set()
            context = s_frame
        else:
            create_process(interp, s_frame)
            context = active_context(space)
    else:
        context = active_context(space)
    
    w_result = execute_context(interp, context)
    print result_string(w_result)
    storage_logger.print_aggregated_log()
    return 0

def result_string(w_result):
    # This will also print contents of strings/symbols/numbers
    if not w_result:
        return ""
    return w_result.as_repr_string().replace('\r', '\n')

def compile_code(interp, w_receiver, code):
    selector = "DoIt%d" % int(time.time())
    space = interp.space
    w_receiver_class = w_receiver.getclass(space)
    
    # The suppress_process_switch flag is a hack/workaround to enable compiling code
    # before having initialized the image cleanly. The problem is that the TimingSemaphore is not yet
    # registered (primitive 136 not called), so the idle process will never be left once it is entered.
    # TODO - Find a way to cleanly initialize the image, without executing the active_context of the image.
    # Instead, we want to execute our own context. Then remove this flag (and all references to it)
    space.suppress_process_switch.set()
    try:
        w_result = interp.perform(
            w_receiver_class,
            "compile:classified:notifying:",
            w_arguments = [space.wrap_string("%s\r\n%s" % (selector, code)),
            space.wrap_string("spy-run-code"),
            space.w_nil]
        )
        
        # TODO - is this expected in every image?
        if not isinstance(w_result, model.W_BytesObject) or w_result.as_string() != selector:
            print_error("Compilation failed, unexpected result: %s" % result_string(w_result))
            return None
    except error.Exit, e:
        print_error("Exited while compiling code: %s" % e.msg)
        return None
    finally:
            space.suppress_process_switch.unset()
    w_receiver_class.as_class_get_shadow(space).s_methoddict().sync_method_cache()
    return selector

def create_context(interp, w_receiver, selector, stringarg):
    args = []
    if stringarg:
        args.append(interp.space.wrap_string(stringarg))
    return interp.create_toplevel_context(w_receiver, selector, w_arguments = args)

def create_process(interp, s_frame):
    space = interp.space
    w_active_process = wrapper.scheduler(space).active_process()
    assert isinstance(w_active_process, model.W_PointersObject)
    w_benchmark_proc = model.W_PointersObject(
        space, w_active_process.getclass(space), w_active_process.size()
    )
    if interp.image.version.has_closures:
        # Priorities below 10 are not allowed in newer versions of Squeak.
        active_priority = space.unwrap_int(w_active_process.fetch(space, 2))
        priority = active_priority / 2 + 1
        priority = max(11, priority)
    else:
        priority = 7
    w_benchmark_proc.store(space, 1, s_frame.w_self())
    w_benchmark_proc.store(space, 2, space.wrap_int(priority))

    # Make process eligible for scheduling
    wrapper.ProcessWrapper(space, w_benchmark_proc).put_to_sleep()

def active_context(space):
    w_active_process = wrapper.scheduler(space).active_process()
    active_process = wrapper.ProcessWrapper(space, w_active_process)
    w_active_context = active_process.suspended_context()
    assert isinstance(w_active_context, model.W_PointersObject)
    active_process.store_suspended_context(space.w_nil)
    return w_active_context.as_context_get_shadow(space)

def execute_context(interp, s_frame, measure=False):
    try:
        return interp.interpret_toplevel(s_frame.w_self())
    except error.Exit, e:
        print_error("Exited: %s" % e.msg)
        return None

# _____ Target and Main _____

def target(driver, *args):
    # driver.config.translation.gc = "stmgc"
    # driver.config.translation.gcrootfinder = "stm"
    from rpython.rlib import rgc
    if hasattr(rgc, "stm_is_enabled"):
        driver.config.translation.stm = True
        driver.config.translation.thread = True
    return entry_point, None

def jitpolicy(self):
    from rpython.jit.codewriter.policy import JitPolicy
    return JitPolicy()

if __name__ == "__main__":
    entry_point(sys.argv)
