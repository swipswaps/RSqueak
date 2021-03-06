from rsqueakvm.plugins.foreign_language.process import W_ForeignLanguageProcess
from rsqueakvm.plugins.ruby import utils
from rsqueakvm.plugins.ruby.frame import WR_FrameObject
from rsqueakvm.plugins.ruby.model import W_RubyObject
from rsqueakvm.plugins.ruby.objspace import ruby_space

from topaz.error import RubyError, print_traceback
from topaz.executioncontext import ExecutionContext


class W_RubyProcess(W_ForeignLanguageProcess):
    _attrs_ = ['source', 'filepath', 'ec']
    repr_classname = 'W_RubyProcess'

    def __init__(self, space, w_rcvr=None, method_name='', args_w=None,
                 source='', filepath='-e',
                 is_send=False, break_on_exceptions=False):
        W_ForeignLanguageProcess.__init__(
            self, space, w_rcvr, method_name, args_w,
            is_send, break_on_exceptions)
        self.source = source
        self.filepath = filepath
        self.ec = ExecutionContext()
        self.init_runner()

    def eval(self):
        if self.source == '':
            return self.fail('Invalid Ruby eval')
        try:
            retval = ruby_space.execute(self.source, filepath=self.filepath)
            self.set_result(W_RubyObject(retval))
        except RubyError as e:
            self.set_result(W_RubyObject(e.w_value))

    def send(self):
        st_to_rb = utils.smalltalk_to_ruby
        wr_rcvr = st_to_rb(self.space(), self.w_rcvr)
        args_rw = [st_to_rb(self.space(), w_arg) for w_arg in self.args_w]
        try:
            wr_result = ruby_space.send(
                wr_rcvr, self.method_name, args_w=args_rw)
            self.set_result(W_RubyObject(wr_result))
        except RubyError as e:
            print_traceback(ruby_space, e.w_value)
            error = W_RubyObject(e.w_value)
            self.set_error(error)
            self.set_result(error)
        except Exception as e:
            # import pdb; pdb.set_trace()
            self.fail(
                'No result in send prim (wr_rcvr: %s, methodname: %s, "%s")'
                % (wr_rcvr, self.method_name, e))

    def pre_resume(self):
        ruby_space.current_ruby_process.set(self)

    def post_resume(self):
        # unset `current_ruby_process` to restore original behavior
        ruby_space.current_ruby_process.set(None)

    def w_top_frame(self):
        if self.ec is None:
            return None
        topframe = self.ec.gettoprubyframe()
        if topframe is None:
            return None
        return W_RubyObject(WR_FrameObject(topframe))

    def guess_classname(self):
        return self.repr_classname
