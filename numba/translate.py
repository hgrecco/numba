import opcode
import sys
import types
import __builtin__

import numpy as np

import llvm.core as lc
import llvm.passes as lp
import llvm.ee as le
from ._ext import make_ufunc

if sys.maxint > 2**33:
    _plat_bits = 64
else:
    _plat_bits = 32

_int32 = lc.Type.int(32)
_intp = lc.Type.int(_plat_bits)
_intp_star = lc.Type.pointer(_intp)
_void_star = lc.Type.pointer(lc.Type.int(8))

_pyobject_head = [_intp, lc.Type.pointer(lc.Type.int(32))]
if hasattr(sys, 'getobjects'):
    _trace_refs_ = True
    _pyobject_head = [lc.Type.pointer(lc.Type.int(32)),
                      lc.Type.pointer(lc.Type.int(32))] + \
                      _pyobject_head
else:
    _trace_refs_ = False

_head_len = len(_pyobject_head)
_numpy_struct = lc.Type.struct(_pyobject_head+\
      [_void_star,          # data
       lc.Type.int(32),     # nd
       _intp_star,          # dimensions
       _intp_star,          # strides
       _void_star,          # base
       _void_star,          # descr
       lc.Type.int(32),     # flags
       _void_star,          # weakreflist 
       _void_star,          # maskna_dtype
       _void_star,          # maskna_data
       _intp_star,          # masna_strides
      ])
_numpy_array = lc.Type.pointer(_numpy_struct)

_BASE_ARRAY_FIELD_OFS = len(_pyobject_head)

_numpy_array_field_ofs = {
    'data' : _BASE_ARRAY_FIELD_OFS,
    'ndim' : _BASE_ARRAY_FIELD_OFS + 1,
    'shape' : _BASE_ARRAY_FIELD_OFS + 2,
    'strides' : _BASE_ARRAY_FIELD_OFS + 3,
}

# Translate Python bytecode to LLVM IR

# For type-inference we need a mapping showing what the output type
# is from any operation and the input types.  We can assume if it is
# not in this table that the output type is the same as the input types 

typemaps = {
}

#hasconst
#hasname
#hasjrel
#haslocal
#hascompare
#hasfree

def itercode(code):
    """Return a generator of byte-offset, opcode, and argument 
    from a byte-code-string
    """
    i = 0
    extended_arg = 0
    n = len(code)
    while i < n:
        c = code[i]
        num = i
        op = ord(c)
        i = i + 1
        oparg = None
        if op >= opcode.HAVE_ARGUMENT:
            oparg = ord(code[i]) + ord(code[i+1])*256 + extended_arg
            extended_arg = 0
            i = i + 2
            if op == opcode.EXTENDED_ARG:
                extended_arg = oparg*65536L

        yield num, op, oparg



# Convert llvm Type object to kind-bits string
def llvmtype_to_strtype(typ):
    if typ.kind == lc.TYPE_FLOAT:
        return 'f32'
    elif typ.kind == lc.TYPE_DOUBLE:
        return 'f64'
    elif typ.kind == lc.TYPE_INTEGER:
        return 'i%d' % typ.width
    elif typ.kind == lc.TYPE_POINTER and \
         typ.pointee.kind == lc.TYPE_FUNCTION:
        return ['func'] + typ.pointee.args

# We don't support all types....
def pythontype_to_strtype(typ):
    if issubclass(typ, float):
        return 'f64'
    elif issubclass(typ, int):
        return 'i%d' % _plat_bits
    elif issubclass(typ, (types.BuiltinFunctionType, types.FunctionType)):
        return ["func"]

def map_to_function(func, typs, mod):
    typs = [str_to_llvmtype(x) if isinstance(x, str) else x for x in typs]
    INTR = getattr(lc, 'INTR_%s' % func.__name__.upper())
    return lc.Function.intrinsic(mod, INTR, typs)

class DelayedObj(object):
    def __init__(self, base, args):
        self.base = base
        self.args = args


# Variables placed on the stack. 
#  They allow an indirection
#  So, that when used in an operation, the correct
#  LLVM type can be inserted.  
class Variable(object):
    def __init__(self, val):
        if isinstance(val, Variable):
            self.val = val.val
            self._llvm = val._llvm
            self.typ = val.typ
            return 
        self.val = val
        if isinstance(val, lc.Value):
            self._llvm = val
            self.typ = llvmtype_to_strtype(val.type)
        else:
            self._llvm = None
            self.typ = pythontype_to_strtype(type(val))

    def __repr__(self):
        return '<Variable(val=%r, _llvm=%r, typ=%r)>' % (self.val, self._llvm, self.typ)

    def llvm(self, typ=None, mod=None):
        if self._llvm:
            print typ, self.typ
            if typ is not None and typ != self.typ:
                raise ValueError, "type mismatch"
            return self._llvm
        else:
            if typ is None:
                typ = 'f64'
            if typ == 'f64':
                res = lc.Constant.real(lc.Type.double(), float(self.val))
            elif typ == 'f32':
                res = lc.Constant.real(lc.Type.float(), float(self.val))
            elif typ[0] == 'i':
                res = lc.Constant.int(lc.Type.int(int(typ[1:])), 
                                      int(self.val))
            elif typ[0] == 'func':
                res = map_to_function(self.val, typ[1:], mod)
            return res

    def is_phi(self):
        return isinstance(self._llvm, lc.PHINode)

# Add complex, unsigned, and bool 
def str_to_llvmtype(str):
    if str[0] == 'f':
        if str[1:] == '32':
            return lc.Type.float()
        elif str[1:] == '64':
            return lc.Type.double()
    elif str[0] == 'i':
        num = int(str[1:])
        return lc.Type.int(num)
    raise TypeError, "Invalid Type"

def convert_to_llvmtype(typ):
    if isinstance(typ, list):
        return _numpy_array
    dt = np.dtype(typ)
    return str_to_llvmtype("%s%s" % (dt.kind, 8*dt.itemsize))

def convert_to_ctypes(typ):
    import ctypes
    from numpy.ctypeslib import _typecodes
    if isinstance(typ, list):
        crnt_elem = typ[0]
        dimcount = 1
        while isinstance(crnt_elem, list):
            crnt_elem = crnt_elem[0]
            dimcount += 1
        return np.ctypeslib.ndpointer(dtype = np.dtype(crnt_elem),
                                      ndim = dimcount,
                                      flags = 'C_CONTIGUOUS')
    return _typecodes[np.dtype(typ).str]

# Add complex, unsigned, and bool
def typcmp(type1, type2):
    if type1==type2:
        return 0
    kind1 = type1[0]
    kind2 = type2[0]
    if kind1 == kind2:
        return cmp(int(type1[1:]),int(type2[1:]))
    if kind1 == 'f':
        return 1
    else:
        return -1

# Both inputs are Variable objects
#  Resolves types on one of them. 
#  Won't work if both need resolving
#  Does not up-cast llvm types
def resolve_type(arg1, arg2):
    if arg1._llvm is not None:
        typ = arg1.typ
    elif arg2._llvm is not None:
        typ = arg2.typ
    else:
        try:
            str_to_llvmtype(arg1.typ)
            typ = arg1.typ
        except TypeError:
            try:
                str_to_llvmtype(arg2.typ)
                typ = arg2.typ
            except TypeError:
                raise TypeError, "Both types not valid"
    return typ, arg1.llvm(typ), arg2.llvm(typ)

# This won't convert any llvm types.  It assumes 
#  the llvm types in args are either fixed or not-yet specified.
def func_resolve_type(mod, func, args):
    # already an llvm function
    if func.val and func.val is func._llvm:
        typs = [llvmtype_to_strtype(x) for x in func._llvm.type.pointee.args]
        lfunc = func._llvm
    else:
        # we need to generate the function including the types
        typs = [arg.typ if arg._llvm is not None else '' for arg in args]
        # pick first one as choice
        choicetype = None
        for typ in typs:
            if typ is not None:
                choicetype = typ
                break
        if choicetype is None:
            raise TypeError, "All types are unspecified"
        typs = [choicetype if x is None else x for x in typs]
        lfunc = map_to_function(func.val, typs, mod)

    llvm_args = [arg.llvm(typ) for typ, arg in zip(typs, args)]
    return lfunc, llvm_args

_compare_mapping_float = {'>':lc.FCMP_OGT,
                           '<':lc.FCMP_OLT,
                           '==':lc.FCMP_OEQ,
                           '>=':lc.FCMP_OGE,
                           '<=':lc.FCMP_OLE,
                           '!=':lc.FCMP_ONE}

_compare_mapping_sint = {'>':lc.ICMP_SGT,
                          '<':lc.ICMP_SLT,
                          '==':lc.ICMP_EQ,
                          '>=':lc.ICMP_SGE,
                          '<=':lc.ICMP_SLE,
                          '!=':lc.ICMP_NE}

_compare_mapping_uint = {'>':lc.ICMP_UGT,
                          '<':lc.ICMP_ULT,
                          '==':lc.ICMP_EQ,
                          '>=':lc.ICMP_UGE,
                          '<=':lc.ICMP_ULE,
                          '!=':lc.ICMP_NE}

class Translate(object):
    def __init__(self, func, ret_type='d', arg_types=['d']):
        self.func = func
        self.fco = func.func_code
        self.names = self.fco.co_names
        self.varnames = self.fco.co_varnames
        self.constants = self.fco.co_consts
        self.costr = func.func_code.co_code
        # Just the globals we will use
        self._myglobals = {}
        for name in self.names:
            try:
                self._myglobals[name] = func.func_globals[name]
            except KeyError:
                # Assumption here is that any name not in globals or
                # builtins is an attribtue.
                self._myglobals[name] = getattr(__builtin__, name, None)

        self.mod = lc.Module.new(func.func_name+'_mod')        

        self._delaylist = [range, xrange, enumerate, len]
        self.ret_type = ret_type
        self.arg_types = arg_types
        self.setup_func()
        self.ee = None

    def setup_func(self):
        # The return type will not be known until the return
        #   function is created.   So, we will need to 
        #   walk through the code twice....
        #   Once to get the type of the return, and again to 
        #   emit the instructions.
        # For now, we assume the function has been called already
        #   or the return type is otherwise known and passed in
        self.ret_ltype = convert_to_llvmtype(self.ret_type)
        # The arg_ltypes we will be able to get from what is passed in
        argnames = self.fco.co_varnames[:self.fco.co_argcount]
        self.arg_ltypes = [convert_to_llvmtype(x) for x in self.arg_types]
        ty_func = lc.Type.function(self.ret_ltype, self.arg_ltypes)        
        self.lfunc = self.mod.add_function(ty_func, self.func.func_name)
        self.nlocals = len(self.fco.co_varnames)
        self._locals = [None] * self.nlocals
        for i, name in enumerate(argnames):
            self.lfunc.args[i].name = name
            # Store away arguments in locals
            self._locals[i] = Variable(self.lfunc.args[i])
        entry = self.lfunc.append_basic_block('Entry')
        self.blocks = {0:entry}
        self.blocks_in = {0 : set()}
        self.blocks_out = {0 : set()}
        self.blocks_locals_read = {0 : set()}
        self.blocks_locals_write = {0 : set(range(self.fco.co_argcount))}
        self.blocks_locals = {}
        self.stack = []
        self.loop_stack = []

    def add_block(self, instruction_index):
        if instruction_index not in self.blocks:
            self.blocks[instruction_index] = self.lfunc.append_basic_block(
                'BLOCK_%d' % instruction_index)
            self.blocks_in[instruction_index] = set()
            self.blocks_out[instruction_index] = set()
            self.blocks_locals_read[instruction_index] = set()
            self.blocks_locals_write[instruction_index] = set()

    def build_cfg(self):
        # FIXME: Make sure this list is exhaustive.
        opmap = opcode.opmap
        JUMP_ABSOLUTE = opmap['JUMP_ABSOLUTE']
        JUMP_FORWARD = opmap['JUMP_FORWARD']
        JUMP_IF_FALSE_OR_POP = opmap['JUMP_IF_FALSE_OR_POP']
        JUMP_IF_TRUE_OR_POP = opmap['JUMP_IF_TRUE_OR_POP']
        LOAD_FAST = opmap['LOAD_FAST']
        POP_JUMP_IF_FALSE = opmap['POP_JUMP_IF_FALSE']
        POP_JUMP_IF_TRUE = opmap['POP_JUMP_IF_TRUE']
        RETURN_VALUE = opmap['RETURN_VALUE']
        SETUP_LOOP = opmap['SETUP_LOOP']
        STORE_FAST = opmap['STORE_FAST']
        crnt_block = 0
        last_was_jump = True # At start there is no prior basic block
                             # to link up with, so skip building a
                             # fallthrough edge.
        for i, op, arg in itercode(self.costr):
            if i in self.blocks:
                if not last_was_jump:
                    print crnt_block, i
                    self.blocks_in[i].add(crnt_block)
                    self.blocks_out[crnt_block].add(i)
                crnt_block = i
            last_was_jump = False
            if op == JUMP_ABSOLUTE:
                self.add_block(arg)
                self.blocks_in[arg].add(crnt_block)
                self.blocks_out[crnt_block].add(arg)
                self.add_block(i + 3)
                last_was_jump = True
            elif op == JUMP_FORWARD:
                target = i + arg + 3
                self.add_block(target)
                self.blocks_in[target].add(crnt_block)
                self.blocks_out[crnt_block].add(target)
                self.add_block(i + 3)
                last_was_jump = True
            elif op in (JUMP_IF_FALSE_OR_POP, JUMP_IF_TRUE_OR_POP):
                raise NotImplementedError('FIXME')
            elif op == LOAD_FAST:
                # Not sure if we care about local variable users at
                # the moment (more concerned with reaches analysis).
                # Might help us eliminate unneeded phi nodes...
                self.blocks_locals_read[crnt_block].add(arg)
            elif op in (POP_JUMP_IF_FALSE, POP_JUMP_IF_TRUE):
                self.add_block(i + 3)
                self.add_block(arg)
                self.blocks_in[i + 3].add(crnt_block)
                self.blocks_in[arg].add(crnt_block)
                self.blocks_out[crnt_block].add(i + 3)
                self.blocks_out[crnt_block].add(arg)
                last_was_jump = True
            elif op == RETURN_VALUE:
                if i + 1 < len(self.costr):
                    self.add_block(i + 1)
                last_was_jump = True
            elif op == SETUP_LOOP:
                self.add_block(i + 3)
                self.blocks_in[i + 3].add(crnt_block)
                self.blocks_out[crnt_block].add(i + 3)
                last_was_jump = True # Not technically true, but we've
                                     # already built the proper CFG
                                     # edges, so skip the fallthrough
                                     # plumbing.
            elif op == STORE_FAST:
                self.blocks_locals_write[crnt_block].add(arg)

    def translate(self):
        """Translate the function
        """
        self.build_cfg()
        for i, op, arg in itercode(self.costr):
            name = opcode.opname[op]
            # Change the builder if the line-number 
            # is in the list of blocks.
            if i in self.blocks.keys():
                # Emit a branch to link blocks up if the previous
                # block was not explicitly branched out of...
                if i > 0:
                    bb_instrs = self.builder.basic_block.instructions
                    if ((len(bb_instrs) == 0) or
                        (not bb_instrs[-1].is_terminator)):
                        self.builder.branch(self.blocks[i])
                    self.blocks_locals[crnt_block] = self._locals[:]
                crnt_block = i
                self.builder = lc.Builder.new(self.blocks[i])
                self.build_phi_nodes(crnt_block)
            getattr(self, 'op_'+name)(i, op, arg)

        # Perform code optimization
        fpm = lp.FunctionPassManager.new(self.mod)
        fpm.initialize()
        fpm.add(lp.PASS_DEAD_CODE_ELIMINATION)
        fpm.run(self.lfunc)
        fpm.finalize()

        print self.lfunc

    def build_phi_nodes(self, crnt_block):
        '''Determine if any phi nodes need to be created, and if so,
        do it.'''
        if len(self.blocks_in[crnt_block]) > 1:
            for local in xrange(self.nlocals):
                if self._locals[local] is not None:
                    oldlocal = self._locals[local]
                    print "local, oldlocal =", local, oldlocal
                    newlocal = self.builder.phi(
                        str_to_llvmtype(oldlocal.typ))
                    self._locals[local] = Variable(newlocal)
                    for pred in self.blocks_in[crnt_block]:
                        if pred in self.blocks_locals:
                            pred_locals = self.blocks_locals[pred]
                            print pred_locals[local]
                            if pred_locals[local] is not None:
                                newlocal.add_incoming(
                                    pred_locals[local].llvm(
                                        llvmtype_to_strtype(newlocal.type)),
                                    self.blocks[pred])

    def get_ctypes_func(self, llvm=True):
        if self.ee is None:
            self.ee = le.ExecutionEngine.new(self.mod)
        import ctypes
        prototype = ctypes.CFUNCTYPE(convert_to_ctypes(self.ret_type),
                                     *[convert_to_ctypes(x) for x in self.arg_types])
        if llvm:
            return prototype(self.ee.get_pointer_to_function(self.lfunc))
        else:
            return prototype(self.func)
        

    def make_ufunc(self, name=None):
        if self.ee is None:
            self.ee = le.ExecutionEngine.new(self.mod)
        if name is None:
            name = self.func.func_name
        return make_ufunc(self.ee.get_pointer_to_function(self.lfunc), 
                                name)

    # This won't convert any llvm types.  It assumes 
    #  the llvm types in args are either fixed or not-yet specified.
    def func_resolve_type(self, func, args):
        # already an llvm function
        if func.val and func.val is func._llvm:
            typs = [llvmtype_to_strtype(x) for x in func._llvm.type.pointee.args]
            lfunc = func._llvm
        # The function is one of the delayed list
        elif func.val in self._delaylist:
            return None, DelayedObj(func.val, args)
        else:
            # we need to generate the function including the types
            typs = [arg.typ if arg._llvm is not None else '' for arg in args]
            # pick first one as choice
            choicetype = None
            for typ in typs:
                if typ is not None:
                    choicetype = typ
                    break
            if choicetype is None:
                raise TypeError, "All types are unspecified"
            typs = [choicetype if x is None else x for x in typs]
            lfunc = map_to_function(func.val, typs, self.mod)

        llvm_args = [arg.llvm(typ) for typ, arg in zip(typs, args)]
        return lfunc, llvm_args

    def op_LOAD_FAST(self, i, op, arg):
        self.stack.append(Variable(self._locals[arg]))

    def op_STORE_FAST(self, i, op, arg):
        # FIXME: if the current basic block dominates any previously
        # created phi nodes, we'll have to patch them to also use this
        # value.
        self._locals[arg] = self.stack.pop(-1)

    def op_LOAD_GLOBAL(self, i, op, arg):
        self.stack.append(Variable(self._myglobals[self.names[arg]]))

    def op_LOAD_CONST(self, i, op, arg):
        const = Variable(self.constants[arg])
        self.stack.append(const)        
    
    def op_BINARY_ADD(self, i, op, arg):
        arg2 = self.stack.pop(-1)
        arg1 = self.stack.pop(-1)
        print arg1, arg2
        typ, arg1, arg2 = resolve_type(arg1, arg2)
        if typ[0] == 'f':
            res = self.builder.fadd(arg1, arg2)
        else: # typ[0] == 'i'
            res = self.builder.add(arg1, arg2)
        self.stack.append(Variable(res))

    def op_INPLACE_ADD(self, i, op, arg):
        # FIXME: Trivial inspection seems to illustrate a mostly
        # identical semantics to BINARY_ADD for numerical inputs.
        # Verify this, or figure out what the corner cases are that
        # require a separate symbolic execution procedure.
        return self.op_BINARY_ADD(i, op, arg)
  
    def op_BINARY_SUBTRACT(self, i, op, arg):
        arg2 = self.stack.pop(-1)
        arg1 = self.stack.pop(-1)
        typ, arg1, arg2 = resolve_type(arg1, arg2)
        if typ[0] == 'f':
            res = self.builder.fsub(arg1, arg2)
        else: # typ[0] == 'i'
            res = self.builder.sub(arg1, arg2)
        self.stack.append(Variable(res))
    
    def op_BINARY_MULTIPLY(self, i, op, arg):
        arg2 = self.stack.pop(-1)
        arg1 = self.stack.pop(-1)
        typ, arg1, arg2 = resolve_type(arg1, arg2)
        if typ[0] == 'f':
            res = self.builder.fmul(arg1, arg2)
        else: # typ[0] == 'i'
            res = self.builder.mul(arg1, arg2)
        self.stack.append(Variable(res))

    def op_BINARY_DIVIDE(self, i, op, arg):
        arg2 = self.stack.pop(-1)
        arg1 = self.stack.pop(-1)
        typ, arg1, arg2 = resolve_type(arg1, arg2)
        if typ[0] == 'f':
            res = self.builder.fdiv(arg1, arg2)
        else: # typ[0] == 'i'
            res = self.builder.sdiv(arg1, arg2)
            # XXX: FIXME-need udiv as
        self.stack.append(Variable(res))

    def op_BINARY_MODULO(self, i, op, arg):
        arg2 = self.stack.pop(-1)
        arg1 = self.stack.pop(-1)
        typ, arg1, arg2 = resolve_type(arg1, arg2)
        if typ[0] == 'f':
            res = self.builder.frem(arg1, arg2)
        else: # typ[0] == 'i'
            res = self.builder.srem(arg1, arg2)
            # FIXME:  Add urem
        self.stack.append(Variable(res))

    def op_BINARY_POWER(self, i, op, arg):
        arg2 = self.stack.pop(-1)
        arg1 = self.stack.pop(-1)
        args = [arg1.llvm(arg1.typ), arg2.llvm(arg2.typ)]
        if arg2.typ[0] == 'i':
            INTR = getattr(lc, 'INTR_POWI')
        else: # make sure it's float
            INTR = getattr(lc, 'INTR_POW')
        typs = [str_to_llvmtype(x.typ) for x in [arg1, arg2]]
        func = lc.Function.intrinsic(self.mod, INTR, typs)
        res = self.builder.call(func, args)
        self.stack.append(Variable(res))
        

    def op_RETURN_VALUE(self, i, op, arg):
        val = self.stack.pop(-1)
        print "RETURN_VALUE", val, val.val
        if val.val is None:
            self.builder.ret(lc.Constant.real(self.ret_ltype, 0))
        else:
            self.builder.ret(val.llvm(llvmtype_to_strtype(self.ret_ltype)))
        # Add a new block at the next instruction if not at end
        if i+1 < len(self.costr) and i+1 not in self.blocks.keys():
            blk = self.lfunc.append_basic_block("RETURN_%d" % i)
            self.blocks[i+1] = blk


    def op_COMPARE_OP(self, i, op, arg):
        cmpop = opcode.cmp_op[arg]
        arg2 = self.stack.pop(-1)
        arg1 = self.stack.pop(-1)
        typ, arg1, arg2 = resolve_type(arg1, arg2)
        if typ[0] == 'f':
            res = self.builder.fcmp(_compare_mapping_float[cmpop], 
                                    arg1, arg2)
        else: # integer FIXME: need unsigned as well...
            res = self.builder.icmp(_compare_mapping_sint[cmpop], 
                                    arg1, arg2)
        self.stack.append(Variable(res))

    def op_POP_JUMP_IF_FALSE(self, i, op, arg):
        # We need to create two blocks.
        #  One for the next instruction (just past the jump)
        #  and another for the block to be jumped to.
        if (i + 3) not in self.blocks:
            cont = self.lfunc.append_basic_block("CONT_%d"% i )
            self.blocks[i+3]=cont
        else:
            cont = self.blocks[i+3]
        if arg not in self.blocks:
            if_false = self.lfunc.append_basic_block("IF_FALSE_%d" % i)
            self.blocks[arg]=if_false
        else:
            if_false = self.blocks[arg]
        arg1 = self.stack.pop(-1)
        self.builder.cbranch(arg1.llvm(), cont, if_false)

    def op_CALL_FUNCTION(self, i, op, arg):
        # number of arguments is arg
        args = [self.stack[-i] for i in range(arg,0,-1)]
        if arg > 0:
            self.stack = self.stack[:-arg]
        func = self.stack.pop(-1)
        func, args = self.func_resolve_type(func, args)
        if func is None: # A delayed-result (i.e. range or xrange)
            res = args
        else:
            res = self.builder.call(func, args)
        self.stack.append(Variable(res))

    def op_GET_ITER(self, i, op, arg):
        raise NotImplementedError('FIXME')

    def op_SETUP_LOOP(self, i, op, arg):
        print "SETUP_LOOP", i, op, arg
        self.loop_stack.append((i, arg))
        if (i + 3) not in self.blocks:
            loop_entry = self.lfunc.append_basic_block("LOOP_%d" % i)
            self.blocks[i+3] = loop_entry
            # Connect blocks up if this was not an anticipated change
            # in the basic block structure.
            predecessor = self.builder.block
            self.builder.position_at_end(predecessor)
            self.builder.branch(loop_entry)
            self.builder.position_at_end(loop_entry)
        else:
            loop_entry = self.blocks[i+3]

    def op_LOAD_ATTR(self, i, op, arg):
        objarg = self.stack.pop(-1)
        # Make this a map on types in the future (thinking this is
        # what typemap was destined to do...)
        objarg_llvm_val = objarg.llvm()
        print i, op, self.names[arg], objarg, objarg.typ, objarg_llvm_val.type,
        if objarg_llvm_val.type == _numpy_array:
            field_index = _numpy_array_field_ofs[self.names[arg]]
        else:
            raise NotImplementedError('LOAD_ATTR only supported for Numpy '
                                      'arrays.')
        res_addr = self.builder.gep(objarg_llvm_val, 
                                    [lc.Constant.int(_int32, 0),
                                     lc.Constant.int(_int32, field_index)])
        res = self.builder.load(res_addr)
        self.stack.append(Variable(res))

    def op_JUMP_ABSOLUTE(self, i, op, arg):
        print "JUMP_ABSOLUTE", i, op, arg
        self.builder.branch(self.blocks[arg])

    def op_POP_BLOCK(self, i, op, arg):
        self.loop_stack.pop(-1)

    def op_JUMP_FORWARD(self, i, op, arg):
        target_i = i + arg + 3
        if target_i not in self.blocks:
            target = self.lfunc.append_basic_block("TARGET_%d" % target_i)
            self.blocks[target_i] = target
        else:
            target = self.blocks[target_i]
        self.builder.branch(target)