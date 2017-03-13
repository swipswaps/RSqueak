from rsqueakvm.plugins.foreign_language.model import (
    W_ForeignLanguageObject, ForeignLanguageClassShadow)
from rsqueakvm.plugins.ruby.objspace import ruby_space


class W_RubyObject(W_ForeignLanguageObject):
    _attrs_ = ['wr_object', 's_class']
    _immutable_fields_ = ['wr_object', 's_class?']
    repr_classname = 'W_RubyObject'

    def __init__(self, wr_object):
        W_ForeignLanguageObject.__init__(self)
        self.wr_object = wr_object
        # self.w_pyID = None
        self.s_class = None

    def getclass(self, space):
        return W_RubyObject(self.wr_object.getclass(ruby_space))

    def getforeignclass(self, space):
        return ruby_space.getclass(self.wr_object)

    def is_same_object(self, other):
        return (isinstance(other, W_RubyObject) and
                other.wr_object is self.wr_object)

    def make_class_shadow(self, space):
        return RubyClassShadow(space, self.getforeignclass(space))


class RubyClassShadow(ForeignLanguageClassShadow):
    _attrs_ = ['wr_class']
    _immutable_fields_ = ['wr_class']

    def __init__(self, space, wr_class):
        self.wr_class = wr_class
        self.name = wr_class.name
        ForeignLanguageClassShadow.__init__(self, space)

    def method_exists(self, w_selector):
        methodname = self.space.unwrap_string(w_selector)
        idx = methodname.find(':')
        if idx > 0:
            methodname = methodname[0:idx]
        ruby_method = self.wr_class.find_method(ruby_space, methodname)
        return ruby_method is not None
