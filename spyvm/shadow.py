import weakref
from spyvm import model, constants, error, wrapper
from rpython.tool.pairtype import extendabletype
from rpython.rlib import rarithmetic, jit

class AbstractShadow(object):
    """A shadow is an optional extra bit of information that
    can be attached at run-time to any Smalltalk object.
    """
    _attr_ = ['_w_self']
    
    def __init__(self, space, w_self):
        self.space = space
        self._w_self = w_self
    def fetch(self, n0):
        return self.w_self()._fetch(n0)
    def store(self, n0, w_value):
        return self.w_self()._store(n0, w_value)
    def size(self):
        return self.w_self()._size()
    def w_self(self):
        return self._w_self
    def getname(self):
        return repr(self)
    def attach_shadow(self): pass
    def detach_shadow(self): pass
    def sync_shadow(self): pass
   
class AbstractCachingShadow(AbstractShadow):
    _attr_ = []

    def __init__(self, space, w_self):
        AbstractShadow.__init__(self, space, w_self)

    def detach_shadow(self):
        self.invalidate_shadow()

    def invalidate_shadow(self):
        """This should get called whenever the base Smalltalk
        object changes."""
        self._w_self.store_shadow(None)

    def attach_shadow(self):
        self.update_shadow()

    def sync_shadow(self):
        pass

    def update_shadow(self):
        self.w_self().store_shadow(self)
        self.sync_cache()

    def sync_cache(self):
        raise NotImplementedError()

    def store(self, n0, w_value):
        self.invalidate_shadow()
        AbstractShadow.store(self, n0, w_value)

# ____________________________________________________________ 

POINTERS = 0
BYTES = 1
WORDS = 2
WEAK_POINTERS = 3
COMPILED_METHOD = 4


class MethodNotFound(error.SmalltalkException):
    pass

class ClassShadowError(error.SmalltalkException):
    pass

class ClassShadow(AbstractCachingShadow):
    """A shadow for Smalltalk objects that are classes
    (i.e. used as the class of another Smalltalk object).
    """

    _immutable_fields_ = ["name", "instance_size", "instance_varsized", "instance_kind", "w_methoddict", "s_methoddict", "w_superclass"]

    name = None
    def __init__(self, space, w_self):
        self.name = ""
        AbstractCachingShadow.__init__(self, space, w_self)

    def getname(self):
        return "%s class" % (self.name or '?',)

    def sync_cache(self):
        "Update the ClassShadow with data from the w_self class."

        w_self = self.w_self()
        # read and painfully decode the format
        classformat = self.space.unwrap_int(
            w_self._fetch(constants.CLASS_FORMAT_INDEX))
        # The classformat in Squeak, as an integer value, is:
        #    <2 bits=instSize//64><5 bits=cClass><4 bits=instSpec>
        #                                    <6 bits=instSize\\64><1 bit=0>
        # In Slang the value is read directly as a boxed integer, so that
        # the code gets a "pointer" whose bits are set as above, but
        # shifted one bit to the left and with the lowest bit set to 1.

        # compute the instance size (really the size, not the number of bytes)
        instsize_lo = (classformat >> 1) & 0x3F
        instsize_hi = (classformat >> (9 + 1)) & 0xC0
        self.instance_size = (instsize_lo | instsize_hi) - 1  # subtract hdr
        # decode the instSpec
        format = (classformat >> 7) & 15
        self.instance_varsized = format >= 2
        if format < 4:
            self.instance_kind = POINTERS
        elif format == 4:
            self.instance_kind = WEAK_POINTERS
        elif format == 6:
            self.instance_kind = WORDS
            if self.instance_size != 0:
                raise ClassShadowError("can't have both words and a non-zero "
                                       "base instance size")
        elif 8 <= format <= 11:
            self.instance_kind = BYTES
            if self.instance_size != 0:
                raise ClassShadowError("can't have both bytes and a non-zero "
                                       "base instance size")
        elif 12 <= format <= 15:
            self.instance_kind = COMPILED_METHOD
        else:
            raise ClassShadowError("unknown format %d" % (format,))

        self.guess_class_name()

        # read the methoddict
        w_methoddict = w_self._fetch(constants.CLASS_METHODDICT_INDEX)
        assert isinstance(w_methoddict, model.W_PointersObject)
        self.w_methoddict = w_methoddict

        w_superclass = w_self._fetch(constants.CLASS_SUPERCLASS_INDEX)
        if w_superclass.is_same_object(self.space.w_nil):
            self.w_superclass = None
        else:
            assert isinstance(w_superclass, model.W_PointersObject)
            self.w_superclass = w_superclass

    def guess_class_name(self):
        w_self = self.w_self()
        w_name = None

        # read the name
        if w_self.size() > constants.CLASS_NAME_INDEX:
            w_name = w_self._fetch(constants.CLASS_NAME_INDEX)
        else:
            # Some heuristic to find the classname
            # Only used for debugging
            # XXX This is highly experimental XXX
            # if the name-pos of class is not bytesobject,
            # we are probably holding a metaclass instead of a class.
            # metaclasses hold a pointer to the real class in the last
            # slot. This is pos 6 in mini.image and higher in squeak3.9
            w_realclass = w_self._fetch(w_self.size() - 1)
            assert isinstance(w_realclass, model.W_PointersObject)
            if w_realclass.size() > constants.CLASS_NAME_INDEX:
                # TODO ADD TEST WHICH GOES OVER THIS PART
                w_name = w_realclass._fetch(constants.CLASS_NAME_INDEX)

        if isinstance(w_name, model.W_BytesObject):
            self.name = w_name.as_string()

    def new(self, extrasize=0):
        w_cls = self.w_self()
        if self.instance_kind == POINTERS:
            w_new = model.W_PointersObject(w_cls, self.instance_size+extrasize)
        elif self.instance_kind == WORDS:
            w_new = model.W_WordsObject(w_cls, extrasize)
        elif self.instance_kind == BYTES:
            w_new = model.W_BytesObject(w_cls, extrasize)
        elif self.instance_kind == COMPILED_METHOD:
            w_new = model.W_CompiledMethod(extrasize)
        else:
            raise NotImplementedError(self.instance_kind)
        return w_new

    def s_methoddict(self):
        return jit.promote(self.w_methoddict.as_methoddict_get_shadow(self.space))

    def s_superclass(self):
        if self.w_superclass is None:
            return None
        return self.w_superclass.as_class_get_shadow(self.space)

    # _______________________________________________________________
    # Methods for querying the format word, taken from the blue book:
    #
    # included so that we can reproduce code from the reference impl
    # more easily

    def ispointers(self):
        " True if instances of this class have data stored as pointers "
        XXX   # what about weak pointers?
        return self.format == POINTERS

    def iswords(self):
        " True if instances of this class have data stored as numerical words "
        XXX   # what about weak pointers?
        return self.format in (POINTERS, WORDS)

    def isbytes(self):
        " True if instances of this class have data stored as numerical bytes "
        return self.format == BYTES

    def isvariable(self):
        " True if instances of this class have indexed inst variables "
        return self.instance_varsized

    def instsize(self):
        " Number of named instance variables for each instance of this class "
        return self.instance_size

    def inherits_from(self, s_superclass):
        classshadow = self
        while classshadow is not None:
            if classshadow is s_superclass:
                return True
            classshadow = classshadow.s_superclass()
        else:
            return False

    # _______________________________________________________________
    # Methods for querying the format word, taken from the blue book:

    def __repr__(self):
        return "<ClassShadow %s>" % (self.name or '?',)

    @jit.unroll_safe
    def lookup(self, w_selector):
        look_in_shadow = self
        jit.promote(w_selector)
        while look_in_shadow is not None:
            w_method = look_in_shadow.s_methoddict().find_selector(w_selector)
            if w_method is not None:
                return w_method.as_compiledmethod_get_shadow(self.space)
            look_in_shadow = look_in_shadow.s_superclass()
        raise MethodNotFound(self, w_selector)

    def initialize_methoddict(self):
        "NOT_RPYTHON"     # this is only for testing.
        if self.w_methoddict is None:
            self.w_methoddict = model.W_PointersObject(None, 2)
            self.w_methoddict._store(1, model.W_PointersObject(None, 0))
            self.s_methoddict().invalid = False

    def installmethod(self, w_selector, w_method):
        "NOT_RPYTHON"     # this is only for testing.
        assert not isinstance(w_selector, str)
        self.initialize_methoddict()
        self.s_methoddict().methoddict[w_selector] = w_method
        if isinstance(w_method, model.W_CompiledMethod):
            method = w_method.as_compiledmethod_get_shadow(self.space)
            method.w_compiledin = self.w_self()

class MethodDictionaryShadow(AbstractCachingShadow):

    @jit.elidable
    def find_selector(self, w_selector):
        return self.methoddict.get(w_selector, None)

    def sync_cache(self):
        w_values = self.w_self()._fetch(constants.METHODDICT_VALUES_INDEX)
        assert isinstance(w_values, model.W_PointersObject)
        s_values = w_values.get_shadow(self.space)
        # XXX Should add!
        # s_values.notifyinvalid(self)
        size = self.w_self().size() - constants.METHODDICT_NAMES_INDEX
        self.methoddict = {}
        for i in range(size):
            w_selector = self.w_self()._fetch(constants.METHODDICT_NAMES_INDEX+i)
            if not w_selector.is_same_object(self.space.w_nil):
                if not isinstance(w_selector, model.W_BytesObject):
                    raise ClassShadowError("bogus selector in method dict")
                w_compiledmethod = w_values._fetch(i)
                if not isinstance(w_compiledmethod, model.W_CompiledMethod):
                    raise ClassShadowError("the methoddict must contain "
                                           "CompiledMethods only for now")
                self.methoddict[w_selector] = w_compiledmethod
                selector = w_selector.as_string()
                w_compiledmethod._likely_methodname = selector


class AbstractRedirectingShadow(AbstractShadow):
    _attr_ = ['_w_self_size']

    def __init__(self, space, w_self):
        AbstractShadow.__init__(self, space, w_self)
        if w_self is not None:
            self._w_self_size = w_self.size()
        else:
            self._w_self_size = 0

    def fetch(self, n0):
        raise NotImplementedError()
    def store(self, n0, w_value):
        raise NotImplementedError()
    def size(self):
        return self._w_self_size

    def attach_shadow(self):
        AbstractShadow.attach_shadow(self)
        w_self = self.w_self()
        assert isinstance(w_self, model.W_PointersObject)
        for i in range(self._w_self_size):
            self.copy_from_w_self(i)
        w_self._vars = None

    def detach_shadow(self):
        w_self = self.w_self()
        assert isinstance(w_self, model.W_PointersObject)
        w_self._vars = [self.space.w_nil] * self._w_self_size
        for i in range(self._w_self_size):
            self.copy_to_w_self(i)

    def copy_from_w_self(self, n0):
        self.store(n0, self.w_self()._fetch(n0))
    def copy_to_w_self(self, n0):
        self.w_self()._store(n0, self.fetch(n0))
 
class ContextPartShadow(AbstractRedirectingShadow):

    __metaclass__ = extendabletype
    _attr_ = ['_s_sender', '_pc', '_temps_and_stack', 
            '_stack_ptr', 'instances_w']

    _virtualizable2_ = [
        "_s_sender", "_pc",
        "_temps_and_stack[*]", "_stack_ptr",
        "_w_self", "_w_self_size"
    ]

    def __init__(self, space, w_self):
        self._s_sender = None
        AbstractRedirectingShadow.__init__(self, space, w_self)
        self.instances_w = None

    @staticmethod
    def is_block_context(w_pointers, space):
        method_or_argc = w_pointers.fetch(space, constants.MTHDCTX_METHOD)
        return method_or_argc.getclass(space).is_same_object(
            space.w_SmallInteger)

    def fetch(self, n0):
        if n0 == constants.CTXPART_SENDER_INDEX:
            return self.w_sender()
        if n0 == constants.CTXPART_PC_INDEX:
            return self.wrap_pc()
        if n0 == constants.CTXPART_STACKP_INDEX:
            return self.wrap_stackpointer()
        if self.stackstart() <= n0 < self.external_stackpointer():
            temp_i = self.stackdepth() - (n0-self.stackstart()) - 1
            assert temp_i >= 0
            return self.peek(temp_i)
        if self.external_stackpointer() <= n0 < self.stackend():
            return self.space.w_nil
        else:
            # XXX later should store tail out of known context part as well
            raise error.WrapperException("Index in context out of bounds")

    def store(self, n0, w_value):
        if n0 == constants.CTXPART_SENDER_INDEX:
            return self.store_w_sender(w_value)
        if n0 == constants.CTXPART_PC_INDEX:
            return self.store_unwrap_pc(w_value)
        if n0 == constants.CTXPART_STACKP_INDEX:
            return self.unwrap_store_stackpointer(w_value)
        if self.stackstart() <= n0 < self.external_stackpointer(): # XXX can be simplified?
            temp_i = self.stackdepth() - (n0-self.stackstart()) - 1
            assert temp_i >= 0
            return self.set_top(w_value, temp_i)
        if self.external_stackpointer() <= n0 < self.stackend():
            return
        else:
            # XXX later should store tail out of known context part as well
            raise error.WrapperException("Index in context out of bounds")

    def unwrap_store_stackpointer(self, w_sp1):
        # the stackpointer in the W_PointersObject starts counting at the
        # tempframe start
        # Stackpointer from smalltalk world == stacksize in python world
        self.store_stackpointer(self.space.unwrap_int(w_sp1) -
                                self.tempsize())

    def store_stackpointer(self, size):
        depth = self.stackdepth()
        if size < depth:
            # TODO Warn back to user
            assert size >= 0
            self.pop_n(depth - size)
        else:
            for i in range(depth, size):
                self.push(self.space.w_nil)

    def wrap_stackpointer(self):
        return self.space.wrap_int(self.stackdepth() + 
                                   self.tempsize())

    def external_stackpointer(self):
        return self.stackdepth() + self.stackstart()

    def w_home(self):
        raise NotImplementedError()

    def s_home(self):
        return self.w_home().as_methodcontext_get_shadow(self.space)
    
    def stackstart(self):
        raise NotImplementedError()

    def stackpointer_offset(self):
        raise NotImplementedError()

    def w_receiver(self):
        " Return self of the method, or the method that contains the block "
        return self.s_home().w_receiver()

    def store_s_sender(self, s_sender):
        assert s_sender is None or isinstance(s_sender, ContextPartShadow)
        self._s_sender = s_sender

    def store_w_sender(self, w_sender):
        assert isinstance(w_sender, model.W_PointersObject)
        if w_sender.is_same_object(self.space.w_nil):
            self._s_sender = None
        else:
            self._s_sender = w_sender.as_context_get_shadow(self.space)

    def w_sender(self):
        if self._s_sender is None:
            return self.space.w_nil
        return self._s_sender.w_self()

    def s_sender(self):
        return self._s_sender

    def store_unwrap_pc(self, w_pc):
        if w_pc.is_same_object(self.space.w_nil):
            return
        pc = self.space.unwrap_int(w_pc)
        pc -= self.s_method().bytecodeoffset
        pc -= 1
        self.store_pc(pc)

    def wrap_pc(self):
        pc = self.pc()
        pc += 1
        pc += self.s_method().bytecodeoffset
        return self.space.wrap_int(pc)

    def pc(self):
        return self._pc

    def store_pc(self, newpc):
        self._pc = newpc

    def stackpointer_offset(self):
        raise NotImplementedError()

    def mark_returned(self):
        self.store_pc(-1)
        self.store_s_sender(None)

    def is_returned(self):
        return self.pc() == -1 and self.w_sender is self.space.w_nil

    # ______________________________________________________________________
    # Method that contains the bytecode for this method/block context

    def w_method(self):
        retval = self.s_home().w_method()
        assert isinstance(retval, model.W_CompiledMethod)
        return retval

    def s_method(self):
        w_method = jit.promote(self.w_method())
        return jit.promote(
            w_method.as_compiledmethod_get_shadow(self.space)
        )

    def getbytecode(self):
        jit.promote(self._pc)
        assert self._pc >= 0
        bytecode = self.s_method().bytecode[self._pc]
        currentBytecode = ord(bytecode)
        self._pc += 1
        return currentBytecode

    # ______________________________________________________________________
    # Temporary Variables
    #
    # Are always fetched relative to the home method context.
    
    def gettemp(self, index):
        return self.s_home().gettemp(index)

    def settemp(self, index, w_value):
        self.s_home().settemp(index, w_value)

    @jit.unroll_safe
    def init_stack_and_temps(self):
        stacksize = self.stackend() - self.stackstart()
        tempsize = self.tempsize()
        self._temps_and_stack = [None] * (stacksize + tempsize)
        for i in range(tempsize):
            self._temps_and_stack[i] = self.space.w_nil
        self._stack_ptr = rarithmetic.r_uint(tempsize) # we point after the last element

    # ______________________________________________________________________
    # Stack Manipulation
    
    def stack(self):
        """NOT_RPYTHON""" # purely for testing
        return self._temps_and_stack[self.tempsize():self._stack_ptr]

    def pop(self):
        #assert self._stack_ptr > self.tempsize()
        ptr = jit.promote(self._stack_ptr) - 1
        ret = self._temps_and_stack[ptr]   # you get OverflowError if the stack is empty
        self._temps_and_stack[ptr] = None
        self._stack_ptr = ptr
        return ret

    def push(self, w_v):
        #assert self._stack_ptr >= self.tempsize()
        #assert self._stack_ptr < self.stackend() - self.stackstart() + self.tempsize()
        ptr = jit.promote(self._stack_ptr)
        self._temps_and_stack[ptr] = w_v
        self._stack_ptr = ptr + 1

    @jit.unroll_safe
    def push_all(self, lst):
        for elt in lst:
            self.push(elt)

    def top(self):
        return self.peek(0)

    def set_top(self, value, position=0):
        rpos = rarithmetic.r_uint(position)
        self._temps_and_stack[self._stack_ptr + ~rpos] = value

    def peek(self, idx):
        rpos = rarithmetic.r_uint(idx)
        return self._temps_and_stack[jit.promote(self._stack_ptr) + ~rpos]

    @jit.unroll_safe
    def pop_n(self, n):
        #assert n == 0 or self._stack_ptr - n >= self.tempsize()
        jit.promote(self._stack_ptr)
        while n > 0:
            n -= 1
            self._stack_ptr -= 1
            self._temps_and_stack[self._stack_ptr] = None

    def stackdepth(self):
        return rarithmetic.intmask(self._stack_ptr - self.tempsize())

    @jit.unroll_safe
    def pop_and_return_n(self, n):
        result = [self.peek(i) for i in range(n - 1, -1, -1)]
        self.pop_n(n)
        return result

    def stackend(self):
        # XXX this is incorrect when there is subclassing
        return self._w_self_size

    def tempsize(self):
        raise NotImplementedError()
    # ______________________________________________________________________
    # Marriage of Context Shadows with PointerObjects only when required
    
    def w_self(self):
        if self._w_self is not None:
            return self._w_self
        else:
            size = self.size() - self.space.w_MethodContext.as_class_get_shadow(self.space).instance_size
            space = self.space
            w_self = space.w_MethodContext.as_class_get_shadow(space).new(size)
            w_self.store_shadow(self)
            self._w_self = w_self
            self._w_self_size = w_self.size()
            return w_self

    def store_instances_array(self, list_w):
        # used for primitives 77 & 78
        self.instances_w = list_w

    def instances_array(self):
        return self.instances_w


class BlockContextShadow(ContextPartShadow):
    _attr_ = ['_w_home', '_initialip', '_eargc']

    @staticmethod
    def make_context(space, w_home, s_sender, argcnt, initialip):
        # create and attach a shadow manually, to not have to carefully put things
        # into the right places in the W_PointersObject
        # XXX could hack some more to never have to create the _vars of w_result
        contextsize = w_home.as_methodcontext_get_shadow(space).myblocksize()
        w_result = model.W_PointersObject(space.w_BlockContext, contextsize)
        s_result = BlockContextShadow(space, w_result)
        s_result_non_fresh = s_result # XXX: find a better solution to translation err
        s_result = jit.hint(s_result, access_directly=True, fresh_virtualizable=True)
        w_result.store_shadow(s_result)
        s_result.store_expected_argument_count(argcnt)
        s_result.store_initialip(initialip)
        s_result.store_w_home(w_home)
        s_result.store_pc(initialip)
        s_result.init_stack_and_temps()
        return s_result_non_fresh

    def fetch(self, n0):
        if n0 == constants.BLKCTX_HOME_INDEX:
            return self.w_home()
        if n0 == constants.BLKCTX_INITIAL_IP_INDEX:
            return self.wrap_initialip()
        if n0 == constants.BLKCTX_BLOCK_ARGUMENT_COUNT_INDEX:
            return self.wrap_eargc()
        else:
            return ContextPartShadow.fetch(self, n0)

    def store(self, n0, w_value):
        if n0 == constants.BLKCTX_HOME_INDEX:
            return self.store_w_home(w_value)
        if n0 == constants.BLKCTX_INITIAL_IP_INDEX:
            return self.unwrap_store_initialip(w_value)
        if n0 == constants.BLKCTX_BLOCK_ARGUMENT_COUNT_INDEX:
            return self.unwrap_store_eargc(w_value)
        else:
            return ContextPartShadow.store(self, n0, w_value)

    @jit.dont_look_inside
    def attach_shadow(self):
        # Make sure the home context is updated first
        self.copy_from_w_self(constants.BLKCTX_HOME_INDEX)
        self.init_stack_and_temps()
        ContextPartShadow.attach_shadow(self)

    def unwrap_store_initialip(self, w_value):
        initialip = self.space.unwrap_int(w_value)
        initialip -= 1 + self.s_method().literalsize
        self.store_initialip(initialip)

    def wrap_initialip(self):
        initialip = self.initialip()
        initialip += 1 + self.s_method().literalsize
        return self.space.wrap_int(initialip)

    def unwrap_store_eargc(self, w_value):
        self.store_expected_argument_count(self.space.unwrap_int(w_value))
    
    def wrap_eargc(self):
        return self.space.wrap_int(self.expected_argument_count())

    def expected_argument_count(self):
        return self._eargc

    def store_expected_argument_count(self, argc):
        self._eargc = argc

    def initialip(self):
        return self._initialip
        
    def store_initialip(self, initialip):
        self._initialip = initialip
        
    def store_w_home(self, w_home):
        assert isinstance(w_home, model.W_PointersObject)
        self._w_home = w_home

    def w_home(self):
        return self._w_home

    def reset_stack(self):
        self.pop_n(self.stackdepth())

    def stackstart(self):
        return constants.BLKCTX_STACK_START

    def stackpointer_offset(self):
        return constants.BLKCTX_STACK_START

    def tempsize(self):
        # A blockcontext doesn't have any temps
        return 0

    def short_str(self):
        return 'BlockContext of %s (%s) [%i]' % (
            self.w_method().get_identifier_string(),
            self.w_receiver(),
            self.pc() + 1
        )

class MethodContextShadow(ContextPartShadow):
    _attr_ = ['w_closure_or_nil', '_w_receiver', '__w_method']

    def __init__(self, space, w_self):
        self.w_closure_or_nil = space.w_nil
        self._w_receiver = space.w_nil
        self.__w_method = None
        ContextPartShadow.__init__(self, space, w_self)

    @staticmethod
    @jit.unroll_safe
    def make_context(space, s_method, w_receiver,
                     arguments, s_sender=None, closure=None, pc=0):
        # From blue book: normal mc have place for 12 temps+maxstack
        # mc for methods with islarge flag turned on 32
        size = (12 + s_method.islarge * 20 + s_method.argsize 
            + space.w_MethodContext.as_class_get_shadow(space).instance_size)
        # The last summand is needed, because we calculate i.a. our stackdepth relative of the size of w_self.

        s_new_context = MethodContextShadow(space, None)
        s_new_context._w_self_size = size
        s_new_context_non_fresh = s_new_context # XXX: find a better solution to translation err
        s_new_context = jit.hint(s_new_context, access_directly=True, fresh_virtualizable=True)
        
        if closure is not None: 
            s_new_context.w_closure_or_nil = closure._w_self
        
        s_new_context.store_w_method(s_method.w_self())
        if s_sender:
            s_new_context.store_s_sender(s_sender)
        s_new_context.store_w_receiver(w_receiver)
        s_new_context.store_pc(pc)
        s_new_context.init_stack_and_temps()
        
        argc = len(arguments)
        for i0 in range(argc):
            s_new_context.settemp(i0, arguments[i0])
        if closure is not None: 
            for i0 in range(closure.size()):
                s_new_context.settemp(i0+argc, closure.at0(i0))
        return s_new_context_non_fresh

    def fetch(self, n0):
        if n0 == constants.MTHDCTX_METHOD:
            return self.w_method()
        if n0 == constants.MTHDCTX_CLOSURE_OR_NIL:
            return self.w_closure_or_nil
        if n0 == constants.MTHDCTX_RECEIVER:
            return self.w_receiver()
        temp_i = n0-constants.MTHDCTX_TEMP_FRAME_START
        if (0 <= temp_i < self.tempsize()):
            return self.gettemp(temp_i)
        else:
            return ContextPartShadow.fetch(self, n0)

    def store(self, n0, w_value):
        if n0 == constants.MTHDCTX_METHOD:
            return self.store_w_method(w_value)
        if n0 == constants.MTHDCTX_CLOSURE_OR_NIL:
            self.w_closure_or_nil = w_value
            return
        if n0 == constants.MTHDCTX_RECEIVER:
            self.store_w_receiver(w_value)
            return
        temp_i = n0-constants.MTHDCTX_TEMP_FRAME_START
        if (0 <=  temp_i < self.tempsize()):
            return self.settemp(temp_i, w_value)
        else:
            return ContextPartShadow.store(self, n0, w_value)
    
    @jit.dont_look_inside
    def attach_shadow(self):
        # Make sure the method is updated first
        self.copy_from_w_self(constants.MTHDCTX_METHOD)
        self.init_stack_and_temps()
        ContextPartShadow.attach_shadow(self)

    def tempsize(self):
        if not self.is_closure_context():
            return self.s_method().tempsize
        else:
            return wrapper.BlockClosureWrapper(self.space, 
                                self.w_closure_or_nil).tempsize()

    def w_method(self):
        retval = self.__w_method
        assert isinstance(retval, model.W_CompiledMethod)
        return retval

    def store_w_method(self, w_method):
        assert isinstance(w_method, model.W_CompiledMethod)
        self.__w_method = w_method

    def w_receiver(self):
        return self._w_receiver

    def store_w_receiver(self, w_receiver):
        self._w_receiver = w_receiver

    def gettemp(self, index0):
        return self._temps_and_stack[index0]

    def settemp(self, index0, w_value):
        self._temps_and_stack[index0] = w_value

    def w_home(self):
        return self.w_self()

    def s_home(self):
        return self

    def stackpointer_offset(self):
        return constants.MTHDCTX_TEMP_FRAME_START

    def stackstart(self):
        return (constants.MTHDCTX_TEMP_FRAME_START +
                self.tempsize())

    def myblocksize(self):
        return self.size() - self.tempsize()

    def returnTopFromMethod(self, interp, current_bytecode):
        if self.is_closure_context():
            # this is a context for a blockClosure
            w_outerContext = self.w_closure_or_nil.fetch(self.space, 
                constants.BLKCLSR_OUTER_CONTEXT)
            assert isinstance(w_outerContext, model.W_PointersObject)
            s_outerContext = w_outerContext.as_context_get_shadow(self.space)
            # XXX check whether we can actually return from that context
            if s_outerContext.pc() == -1:
                raise error.BlockCannotReturnError()
            return_to_context = s_outerContext.s_home().s_sender()
        else:
            return_to_context = self.s_home().s_sender()
        return self._return(self.pop(), interp, return_to_context)

    def is_closure_context(self):
        return self.w_closure_or_nil is not self.space.w_nil

    def __str__(self):
        retval = '\nMethodContext of:'
        retval += self.w_method().as_string(markBytecode=self.pc() + 1)
        retval += "Stackptr: %i (this is an empty ascending stack)" % (self._stack_ptr - self.tempsize())
        retval += "\nStack   : " + str(self.stack())
        return retval

    def short_str(self):
        block = '[] of' if self.is_closure_context() else ''
        return '%s %s (%s) [%i]' % (
            block,
            self.w_method().get_identifier_string(),
            self.w_receiver(),
            self.pc() + 1
        )

class CompiledMethodShadow(object):
    _immutable_fields_ = ["_w_self", "bytecode",
                          "literals[*]", "bytecodeoffset",
                          "literalsize", "tempsize", "primitive",
                          "argsize", "islarge",
                          "w_compiledin"]

    def __init__(self, w_compiledmethod):
        self._w_self = w_compiledmethod
        self.bytecode = "".join(w_compiledmethod.bytes)
        self.literals = w_compiledmethod.literals
        self.bytecodeoffset = w_compiledmethod.bytecodeoffset()
        self.literalsize = w_compiledmethod.getliteralsize()
        self.tempsize = w_compiledmethod.gettempsize()
        self.primitive = w_compiledmethod.primitive
        self.argsize = w_compiledmethod.argsize
        self.islarge = w_compiledmethod.islarge

        self.w_compiledin = None
        if self.literals:
            # (Blue book, p 607) All CompiledMethods that contain
            # extended-super bytecodes have the clain which they are found as
            # their last literal variable.   
            # Last of the literals is an association with compiledin
            # as a class
            w_association = self.literals[-1]
            if isinstance(w_association, model.W_PointersObject) and w_association.size() >= 2:
                # XXX XXX XXX where to get a space from here
                association = wrapper.AssociationWrapper(None, w_association)
                self.w_compiledin = association.value()

    def w_self(self):
        return self._w_self

    def getliteral(self, index):
        return self.literals[index]

    def getliteralsymbol(self, index):
        w_literal = self.getliteral(index)
        assert isinstance(w_literal, model.W_BytesObject)
        return w_literal.as_string()    # XXX performance issue here

    def create_frame(self, space, receiver, arguments, sender = None):
        assert len(arguments) == self.argsize
        s_new = MethodContextShadow.make_context(
                space, self, receiver, arguments, sender)
        return s_new

class Version:
    pass

class CachedObjectShadow(AbstractCachingShadow):
    _immutable_fields_ = ['version?']

    def __init__(self, space, w_self):
        AbstractCachingShadow.__init__(self, space, w_self)
        self.version = Version()

    def fetch(self, n0):
        jit.promote(self)
        version = self.version
        jit.promote(version)
        return self.safe_fetch(n0, version)

    @jit.elidable
    def safe_fetch(self, n0, version):
        assert version is self.version
        return self._w_self._fetch(n0)

    def store(self, n0, w_value):
        self.version = Version()
        return self._w_self._store(n0, w_value)

    def update_shadow(self):
        self.version = Version()


class BitBltShadow(AbstractCachingShadow):
    _attrs_ = [# From BitBlt
               "dest_form", "source_form", "halftone_form",
               "combination_rule", "dest_x", "dest_y", "width",
               "height", "source_x", "source_y", "clip_x", "clip_y",
               "clip_width", "clip_height", "color_map",
               # From BitBltSimulation
               "w", "h", "sx", "sy", "dx", "dy",
               "dest_bits", "dest_raster", "source_bits", "source_raster",
               "halftone_bits", "skew", "mask1", "mask2", "skew_mask",
               "n_words", "h_dir", "v_dir", "preload", "source_index",
               "dest_index", "source_delta", "dest_delta"]

    WordSize = 32
    RightMasks = [0]
    for i in xrange(WordSize):
        RightMasks.append((2 ** (i + 1)) - 1)
    AllOnes = (2 ** WordSize) - 1

    def sync_cache(self):
        try:
            self.dest_form = self.fetch(0).as_form_get_shadow(self.space)
        except error.PrimitiveFailedError, e:
            self.detach_shadow()
            raise e
        w_source_form = self.fetch(1)
        if w_source_form is self.space.w_nil:
            self.source_form = self.dest_form
        else:
            try:
                self.source_form = w_source_form.as_form_get_shadow(self.space)
            except error.PrimitiveFailedError, e:
                self.detach_shadow()
                raise e
        w_halftone_form = self.fetch(2)
        if w_halftone_form is not self.space.w_nil:
            if isinstance(w_halftone_form, model.W_WordsObject):
                # Already a bitmap
                self.halftone_bits = w_halftone_form.words
            else:
                self.halftone_bits = w_halftone_form.as_form_get_shadow(self.space).bits
        else:
            self.halftone_bits = None
        self.combination_rule = self.space.unwrap_int(self.fetch(3))
        self.dest_x = self.space.unwrap_int(self.fetch(4))
        self.dest_y = self.space.unwrap_int(self.fetch(5))
        self.width = self.space.unwrap_int(self.fetch(6))
        self.height = self.space.unwrap_int(self.fetch(7))
        self.source_x = self.space.unwrap_int(self.fetch(8))
        self.source_y = self.space.unwrap_int(self.fetch(9))
        self.clip_x = self.space.unwrap_int(self.fetch(10))
        self.clip_y = self.space.unwrap_int(self.fetch(11))
        self.clip_width = self.space.unwrap_int(self.fetch(12))
        self.clip_height = self.space.unwrap_int(self.fetch(13))
        self.color_map = self.fetch(14)

    def clip_range(self):
        if self.dest_x >= self.clip_x:
            self.sx = self.source_x
            self.dx = self.dest_x
            self.w = self.width
        else:
            self.sx = self.source_x + (self.clip_x - self.dest_x)
            self.w = self.width - (self.clip_x - self.dest_x)
            self.dx = self.clip_x
        if self.dx + self.w > self.clip_x + self.clip_width:
            self.w = self.w - (self.dx + self.w - (self.clip_x + self.clip_width))
        if self.dest_x >= self.clip_y:
            self.sy = self.source_y
            self.dy = self.dest_y
            self.h = self.height
        else:
            self.sy = self.source_y + self.clip_y - self.dest_y
            self.h = self.height - (self.clip_y - self.dest_y)
            self.dy = self.clip_y
        if self.dy + self.h > self.clip_y + self.clip_height:
            self.h = self.h - (self.dy + self.h - (self.clip_y + self.clip_height))
        if self.sx < 0:
            self.dx = self.dx - self.sx
            self.w = self.w + self.sx
            self.sx = 0
        if self.sx + self.w > self.source_form.width:
            self.w = self.w - (self.sx + self.w - self.source_form.width)
        if self.sy < 0:
            self.dy = self.dy - self.sy
            self.h = self.h + self.sy
            self.sy = 0
        if self.sy + self.h > self.source_form.height:
            self.h = self.h - (self.sy + self.h - self.source_form.height)

    def compute_masks(self):
        self.dest_bits = self.dest_form.bits
        self.dest_raster = (self.dest_form.width - 1) / BitBltShadow.WordSize + 1
        self.source_bits = self.source_form.bits
        self.source_raster = (self.source_form.width - 1) / BitBltShadow.WordSize + 1
        self.skew = (self.sx - self.dx) & (BitBltShadow.WordSize - 1)
        start_bits = BitBltShadow.WordSize - (self.dx & (BitBltShadow.WordSize - 1))
        self.mask1 = BitBltShadow.RightMasks[start_bits]
        end_bits = (BitBltShadow.WordSize - 1) - ((self.dx + self.w - 1) & (BitBltShadow.WordSize - 1))
        self.mask2 = ~BitBltShadow.RightMasks[end_bits]
        if self.skew == 0:
            self.skew_mask = 0
        else:
            self.skew_mask = BitBltShadow.RightMasks[BitBltShadow.WordSize - self.skew]
        if self.w < start_bits:
            self.mask1 = self.mask1 & self.mask2
            self.mask2 = 0
            self.n_words = 1
        else:
            self.n_words = (self.w - start_bits - 1) / BitBltShadow.WordSize + 2

    def check_overlap(self):
        self.h_dir = 1
        self.v_dir = 1
        if (self.source_form.w_self().is_same_object(self.dest_form.w_self()) and
            self.dy >= self.sy):
            if self.dy > self.sy:
                self.v_dir = -1
                self.sy = self.sy + self.h - 1
                self.dy = self.dy + self.h - 1
            elif self.dx > self.sx:
                self.h_dir = -1
                self.sx = self.sx + self.w - 1
                self.dx = self.dx + self.w - 1
                self.skew_mask = ~self.skew_mask
                self.mask1, self.mask2 = self.mask2, self.mask1

    def calculate_offsets(self):
        self.preload = (self.skew_mask != 0 and
                        self.skew <= (self.sx & (BitBltShadow.WordSize - 1)))
        if self.h_dir < 0:
            self.preload = not self.preload
        self.source_index = self.sy * self.source_raster + self.sx / BitBltShadow.WordSize
        self.dest_index = self.dy * self.dest_raster + self.dx / BitBltShadow.WordSize
        self.source_delta = ((self.source_raster *
                             self.v_dir -
                             (self.n_words + (1 if self.preload else 0))) *
                             self.h_dir)
        self.dest_delta = self.dest_raster * self.v_dir - self.n_words * self.h_dir

    def copy_loop(self):
        for i in xrange(self.h - 1):
            if self.halftone_bits:
                halftone_word = self.halftone_bits[(self.dy & (BitBltShadow.WordSize - 1)) % len(self.halftone_bits)]
                self.dy = self.dy + self.v_dir
            else:
                halftone_word = BitBltShadow.AllOnes
            skew_word = halftone_word
            if self.preload:
                prev_word = self.source_bits[self.source_index]
                self.source_index = self.source_index + self.h_dir
            else:
                prev_word = 0
            merge_mask = self.mask1
            for word in xrange(self.n_words - 1):
                prev_word = prev_word & self.skew_mask
                this_word = self.source_bits[self.source_index]
                skew_word = prev_word | (this_word & ~self.skew_mask)
                prev_word = this_word
                skew_word = (self.bit_shift(skew_word, self.skew) |
                             self.bit_shift(skew_word, self.skew - 16))
                merge_word = self.merge(
                    skew_word & halftone_word,
                    self.dest_bits[self.dest_index]
                )
                self.dest_bits[self.dest_index] = (
                    (merge_mask & merge_word) |
                    (~merge_mask & self.dest_bits[self.dest_index])
                )
                self.source_index = self.source_index + self.h_dir
                self.dest_index = self.dest_index + self.h_dir
                if word == (self.n_words - 1):
                    merge_mask = self.mask2
                else:
                    merge_mask = BitBltShadow.AllOnes
            self.source_index = self.source_index + self.source_delta
            self.dest_index = self.dest_index + self.dest_delta
        self.dest_form.replace_bits(self.dest_bits)

    def bit_shift(self, target, amount):
        if amount > 0:
            return target << amount
        else:
            return target >> -amount

    def merge(self, source_word, dest_word):
        if self.combination_rule == 0:
            return 0
        elif self.combination_rule == 1:
            return source_word & dest_word
        elif self.combination_rule == 2:
            return source_word & ~dest_word
        elif self.combination_rule == 3:
            return source_word
        elif self.combination_rule == 4:
            return ~source_word & dest_word
        elif self.combination_rule == 5:
            return dest_word
        elif self.combination_rule == 6:
            return source_word ^ dest_word
        elif self.combination_rule == 7:
            return source_word | dest_word
        elif self.combination_rule == 8:
            return ~source_word & ~dest_word
        elif self.combination_rule == 9:
            return ~source_word ^ dest_word
        elif self.combination_rule == 10:
            return ~dest_word
        elif self.combination_rule == 11:
            return source_word | ~dest_word
        elif self.combination_rule == 12:
            return ~source_word
        elif self.combination_rule == 13:
            return ~source_word | dest_word
        elif self.combination_rule == 14:
            return ~source_word | ~dest_word
        elif self.combination_rule == 15:
            return BitBltShadow.AllOnes


class FormShadow(AbstractCachingShadow):
    _attrs_ = ["bits", "width", "height", "depth", "offset_x", "offset_y"]

    def sync_cache(self):
        self.w_bits = self.fetch(0)
        if isinstance(self.w_bits, model.W_WordsObject):
            self.bits = self.w_bits.words
        else:
            self.detach_shadow()
            raise error.PrimitiveFailedError
        self.width = self.space.unwrap_int(self.fetch(1))
        self.height = self.space.unwrap_int(self.fetch(2))
        self.depth = self.space.unwrap_int(self.fetch(3))
        w_offset = self.fetch(4)
        if not w_offset is self.space.w_nil:
            self.offset_x = self.space.unwrap_int(w_offset._fetch(0))
            self.offset_y = self.space.unwrap_int(w_offset._fetch(1))

    def replace_bits(self, bits):
        if isinstance(self.w_bits, model.W_WordsObject):
            self.w_bits.words[:] = bits
        else:
            self.detach_shadow()
            raise error.PrimitiveFailedError
