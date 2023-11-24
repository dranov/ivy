#
# Copyright (c) Microsoft Corporation. All Rights Reserved.
#
from typing import Any
from . import ivy_actions as ia
from . import ivy_logic as il
from . import ivy_module as im
from . import ivy_solver
from . import ivy_trace as it
from . import ivy_transrel as itr
from . import logic as lg
from . import mypyvy_syntax as pyv

from ivy import z3

logfile = None
verbose = False

# Ivy symbols have dots in them (due to the module system)
# but mypyvy doesn't allow dots in names, so we replace
# them with this string
DOT_REPLACEMENT = "_"
BRACE_REPLACEMENT = '_B_'
COLON_REPLACEMENT = "_c_"

# This is how Ivy internally represents true and false
_true = lg.And()
_false = lg.Or()

class Translation:
    '''Helper class for translating Ivy expressions to mypyvy expressions.'''
    def sort_type(ivy_sort):
        if il.is_function_sort(ivy_sort) and il.is_boolean_sort(ivy_sort.rng):
            return 'relation'
        elif il.is_function_sort(ivy_sort):
            return 'function'
        return 'individual'

    def to_pyv_sort(ivy_sort):
        if il.is_first_order_sort(ivy_sort) \
            or il.is_enumerated_sort(ivy_sort):
            return pyv.UninterpretedSort(ivy_sort.name)
        elif il.is_boolean_sort(ivy_sort):
            return pyv.BoolSort
        # Relation
        elif il.is_function_sort(ivy_sort) and il.is_boolean_sort(ivy_sort.rng):
            # FIXME: do we need the bool for rng?
            return tuple([Translation.to_pyv_sort(x) for x in ivy_sort.dom])
        elif il.is_function_sort(ivy_sort):
            return (tuple([Translation.to_pyv_sort(x) for x in ivy_sort.dom]), Translation.to_pyv_sort(ivy_sort.rng))
        else:
            raise NotImplementedError("translating sort {} to mypyvy ".format(repr(ivy_sort)))

    def to_pyv_name(ivy_name):
        if isinstance(ivy_name, str):
            name = ivy_name.replace(".", DOT_REPLACEMENT)
            name = name.replace('[', BRACE_REPLACEMENT)
            name = name.replace(']', BRACE_REPLACEMENT)
            name = name.replace(':', COLON_REPLACEMENT)
            return name
        raise Exception("cannot translate non-string name {} to mypyvy ".format(repr(ivy_name)))

    def translate_binders(binders) -> tuple[pyv.SortedVar]:
        '''Translate [var_name:var_sort] into mypyvy.'''
        vars = []
        for binder in binders:
            name = Translation.to_pyv_name(binder.name)
            sort = Translation.to_pyv_sort(binder.sort)
            vars.append(pyv.SortedVar(name, sort, None))
        return vars

    def smt_binders_to_ivy(sorts: dict[str, Any], fmla: z3.BoolRef):
        '''Convert the binders of an SMT formula to Ivy binders.
        sorts: dictionary from sort names to Ivy sorts
        '''
        assert z3.is_ast(fmla) and z3.is_expr(fmla) and z3.is_quantifier(fmla)
        num_binders = fmla.num_vars()
        def to_ivy_name(name: str) -> str:
            # For some reason, SMT vars incorporate the sort,
            # like 'X:integer'; we only want the 'X'
            return name.split(':')[0]
        binders = [lg.Var(to_ivy_name(fmla.var_name(i)), sorts[fmla.var_sort(i).name()]) for i in range(num_binders)]
        return binders

    def translate_symbol_decl(sym: il.Symbol, is_mutable=True) -> pyv.Decl:
        sort = sym.sort
        kind = Translation.sort_type(sort)
        name = Translation.to_pyv_name(sym.name)

        if kind == 'individual':
            pyv_sort = Translation.to_pyv_sort(sort)
            const = pyv.ConstantDecl(name, pyv_sort, is_mutable)
            return const
        elif kind == 'relation':
            assert sym.is_relation()
            pyv_sort = Translation.to_pyv_sort(sort)
            rel = pyv.RelationDecl(name, pyv_sort, is_mutable)
            return rel
        elif kind == 'function':
            (dom_sort, rng_sort) = Translation.to_pyv_sort(sort)
            fn = pyv.FunctionDecl(name, dom_sort, rng_sort, is_mutable)
            return fn
        else:
            raise NotImplementedError("translating symbol {} to mypyvy ".format(repr(sym)))
 
    def pyv_havoc_symbol(sym: il.Symbol) -> pyv.Expr:
        '''Return a two-state formula that havocs the given symbol.'''
        sort = sym.sort
        sym = itr.new(sym) # we want to talk about the new version
        vvar = lg.Var("V", sort.rng)

        fmla = None
        if len(sort.dom) == 0:
            # exists V in sort.rng. cst = V
            fmla = lg.Exists([vvar], lg.Eq(sym, vvar))
        else:
            # forall X0 X1 X2 in sort.dom. exists V in sort.rng. rel(X,Y,Z) = V
            uvars = [lg.Var("X{}".format(i), sort.dom[i]) for i in range(len(sort.dom))]
            ex = lg.Exists([vvar], lg.Eq(lg.Apply(sym, *uvars), vvar))
            fmla = lg.ForAll(uvars, ex)

        return Translation.translate_logic_fmla(fmla, is_twostate=True)

    def pyv_unchanged_symbol(sym: il.Symbol) -> pyv.Expr:
        '''Return a two-state formula that asserts the given symbol is unchanged.'''
        sort = sym.sort
        old_sym = sym
        sym = itr.new(sym) # we want to talk about the new version
        vvar = lg.Var("V", sort.rng)

        fmla = None
        if len(sort.dom) == 0:
            # new(cst) = cst
            fmla = lg.Eq(sym, old_sym)
        else:
            # forall X0 X1 X2 in sort.dom. new(rel(X,Y,Z)) = rel(X, Y, Z)
            uvars = [lg.Var("X{}".format(i), sort.dom[i]) for i in range(len(sort.dom))]
            eq = lg.Eq(lg.Apply(sym, *uvars), lg.Apply(old_sym, *uvars))
            fmla = lg.ForAll(uvars, eq)

        return Translation.translate_logic_fmla(fmla, is_twostate=True)
    
    def smt_to_ivy(fmla: z3.BoolRef, sorts: dict[str, Any], syms: dict[str, Any], binders=[]) -> lg.And:
        '''Convert an SMT formula to an Ivy formula.
        sorts: dict from sort names to Ivy sorts
        syms: dict from symbol names to Ivy symbols
        '''
        assert z3.is_ast(fmla) and z3.is_expr(fmla)

        # Quantifiers
        # FIXME: variables underneath have de-Bruijn indices
        # we need to convert them back to names
        # https://stackoverflow.com/questions/13357509/is-it-possible-to-access-the-name-associated-with-the-de-bruijn-index-in-z3
        if z3.is_quantifier(fmla) and fmla.is_forall():
            # How to translate the sorts of the vars?
            new_binders = Translation.smt_binders_to_ivy(sorts, fmla)
            # FIXME: wtf should happen here?
            binders = binders + list(reversed(new_binders))
            return lg.ForAll(new_binders, Translation.smt_to_ivy(fmla.body(), sorts, syms, binders))
        elif z3.is_quantifier(fmla): # strangely, there is no is_exists()
            new_binders = Translation.smt_binders_to_ivy(sorts, fmla)
            binders = binders + list(reversed(new_binders))
            return lg.Exists(new_binders, Translation.smt_to_ivy(fmla.body(), sorts, syms, binders))
        # Unary ops
        elif z3.is_not(fmla):
            return lg.Not(Translation.smt_to_ivy(fmla.children()[0], sorts, syms, binders))
        # Binary ops
        elif z3.is_and(fmla):
            return lg.And(*[Translation.smt_to_ivy(x, sorts, syms, binders) for x in fmla.children()])
        elif z3.is_or(fmla):
            return lg.Or(*[Translation.smt_to_ivy(x, sorts, syms, binders) for x in fmla.children()])
        elif z3.is_eq(fmla):
            return lg.Eq(Translation.smt_to_ivy(fmla.children()[0], sorts, syms, binders), Translation.smt_to_ivy(fmla.children()[1], sorts, syms, binders))
        elif z3.is_app_of(fmla, z3.Z3_OP_IMPLIES):
            return lg.Implies(Translation.smt_to_ivy(fmla.children()[0], sorts, syms, binders), Translation.smt_to_ivy(fmla.children()[1], sorts, syms, binders))
        elif z3.is_app_of(fmla, z3.Z3_OP_IFF):
            return lg.Iff(Translation.smt_to_ivy(fmla.children()[0], sorts, syms, binders), Translation.smt_to_ivy(fmla.children()[1], sorts, syms, binders))
        # Ternary
        elif z3.is_app_of(fmla, z3.Z3_OP_ITE):
            return lg.Ite(Translation.smt_to_ivy(fmla.children()[0], sorts, syms, binders), Translation.smt_to_ivy(fmla.children()[1], sorts, syms, binders), Translation.smt_to_ivy(fmla.children()[2], sorts, syms, binders))
        # Constants
        elif z3.is_true(fmla):
            return _true
        elif z3.is_false(fmla):
            return _false
        elif z3.is_const(fmla):
            name = fmla.decl().name()
            sort = syms[name].sort
            return lg.Const(name, sort)
        # IMPORTANT: these must come after all the other operators,
        # because it's not really specific enough.
        # Application
        elif z3.is_app(fmla) and fmla.num_args() > 0:
            name = fmla.decl().name()
            sort = syms[name].sort
            args = [Translation.smt_to_ivy(x, sorts, syms, binders) for x in fmla.children()]
            try:
                return lg.Apply(lg.Const(name, sort), *args)
            except:
                # ivy.logic.SortError: in application of new_env.auth_required, at position 3, expected sort {_mint,_transfer}, got sort function_identifier
                import pdb; pdb.set_trace()

                # Constants
        elif z3.is_var(fmla):
            # FIXME: is this correct?
            return binders[z3.get_var_index(fmla)]
        else:
            import pdb; pdb.set_trace()
            assert False, "unhandled SMT formula: {}".format(fmla)


    def translate_logic_fmla(fmla, is_twostate=False) -> pyv.Expr:
        '''Translates a logic formula (as defined in logic.py) to a
        mypyvy expression. (Note: these for some reason are not AST nodes.)'''

        if isinstance(fmla, lg.ForAll):
            # fmla.variables & fmla.body
            return pyv.Forall(Translation.translate_binders(fmla.variables), Translation.translate_logic_fmla(fmla.body, is_twostate))
        elif isinstance(fmla, lg.Exists):
            # fmla.variables & fmla.body
            return pyv.Exists(Translation.translate_binders(fmla.variables), Translation.translate_logic_fmla(fmla.body, is_twostate))
        elif isinstance(fmla, lg.Ite):
            # fmla.sort & fmla.cond & fmla.t_then, fmla.t_else
            return pyv.IfThenElse(Translation.translate_logic_fmla(fmla.cond, is_twostate), Translation.translate_logic_fmla(fmla.t_then, is_twostate), Translation.translate_logic_fmla(fmla.t_else, is_twostate))
        elif isinstance(fmla, lg.And):
            # fmla.terms
            if len(fmla.terms) == 0:
                return pyv.TrueExpr
            return pyv.And(*tuple([Translation.translate_logic_fmla(x, is_twostate) for x in fmla.terms]))
        elif isinstance(fmla, lg.Or):
            # fmla.terms
            if len(fmla.terms) == 0:
                return pyv.FalseExpr
            return pyv.Or(*tuple([Translation.translate_logic_fmla(x, is_twostate) for x in fmla.terms]))
        elif isinstance(fmla, lg.Eq):
            # fmla.t1 & fmla.t2
            return pyv.Eq(Translation.translate_logic_fmla(fmla.t1, is_twostate), Translation.translate_logic_fmla(fmla.t2, is_twostate))
        elif isinstance(fmla, lg.Implies):
            # fmla.t1 & fmla.t2
            return pyv.Implies(Translation.translate_logic_fmla(fmla.t1, is_twostate), Translation.translate_logic_fmla(fmla.t2, is_twostate))
        elif isinstance(fmla, lg.Iff):
            # fmla.t1 & fmla.t2
            return pyv.Iff(Translation.translate_logic_fmla(fmla.t1, is_twostate), Translation.translate_logic_fmla(fmla.t2, is_twostate))
        elif isinstance(fmla, lg.Not):
            # fmla.body
            return pyv.Not(Translation.translate_logic_fmla(fmla.body, is_twostate))
        elif isinstance(fmla, lg.Apply):
            # fmla.func & fmla.terms
            if is_twostate and itr.is_new(fmla.func):
                # We need to add a new() around the application and rename 'new_rel' to 'rel'
                old_name = itr.new_of(fmla.func).name
                fm = pyv.Apply(Translation.to_pyv_name(old_name), tuple([Translation.translate_logic_fmla(x, is_twostate) for x in fmla.terms]))
                return pyv.New(fm)
            else:
                return pyv.Apply(Translation.to_pyv_name(fmla.func.name), tuple([Translation.translate_logic_fmla(x, is_twostate) for x in fmla.terms]))
        elif isinstance(fmla, lg.Const):
            if is_twostate and itr.is_new(fmla):
                # We need to add a new() around the application and rename 'new_rel' to 'rel'
                old_name = itr.new_of(fmla).name
                fm = pyv.Id(Translation.to_pyv_name(old_name))
                return pyv.New(fm)
            return pyv.Id(Translation.to_pyv_name(fmla.name))
        elif isinstance(fmla, lg.Var):
            return pyv.Id(Translation.to_pyv_name(fmla.name))
        else:
            raise NotImplementedError("translating logic formula {} to mypyvy ".format(repr(fmla)))

    def globals_in_fmla(fmla) -> set[str]:
        '''Returns the set of global names that appear in a formula.
        We use this to mark constants/relations/functions as immutable
        if they appear in axioms.'''
        if isinstance(fmla, lg.ForAll) or isinstance(fmla, lg.Exists):
            return Translation.globals_in_fmla(fmla.body)
        elif isinstance(fmla, lg.Ite):
            return Translation.globals_in_fmla(fmla.cond) | Translation.globals_in_fmla(fmla.t_then) | Translation.globals_in_fmla(fmla.t_else)
        elif isinstance(fmla, lg.And) or isinstance(fmla, lg.Or):
            if len(fmla.terms) == 0:
                return set()
            return set.union(*[Translation.globals_in_fmla(x) for x in fmla.terms])
        elif isinstance(fmla, lg.Eq) or isinstance(fmla, lg.Implies) or isinstance(fmla, lg.Iff):
            return Translation.globals_in_fmla(fmla.t1) | Translation.globals_in_fmla(fmla.t2)
        elif isinstance(fmla, lg.Not):
            return Translation.globals_in_fmla(fmla.body)
        elif isinstance(fmla, lg.Apply):
            return {fmla.func.name} | set.union(*[Translation.globals_in_fmla(x) for x in fmla.terms])
        elif isinstance(fmla, lg.Const):
            return {fmla.name}
        elif isinstance(fmla, lg.Var):
            return set()
        else:
            raise NotImplementedError("constants_in_fmla: {}".format(repr(fmla)))

    def new_globals_in_pyv_fmla(globals: set[str], e: pyv.Expr, under_new=False) -> set[str]:
        '''Returns the set of global names that appear in a mypyvy formula
        under new(). Used to identify which relations/functions are modified.
        Takes as argument the set of all mutable global symbols.'''
        if isinstance(e, pyv.Bool) or isinstance(e, pyv.Int):
            return set()
        elif isinstance(e, pyv.UnaryExpr):
            if e.op == 'NEW':
                return Translation.new_globals_in_pyv_fmla(globals, e.arg, under_new=True)
            return Translation.new_globals_in_pyv_fmla(globals, e.arg, under_new)
        elif isinstance(e, pyv.BinaryExpr):
            return Translation.new_globals_in_pyv_fmla(globals, e.arg1, under_new) | Translation.new_globals_in_pyv_fmla(globals, e.arg2, under_new)
        elif isinstance(e, pyv.NaryExpr):
            return set.union(*[Translation.new_globals_in_pyv_fmla(globals, x, under_new) for x in e.args])
        elif isinstance(e, pyv.AppExpr):
            res = set.union(*[Translation.new_globals_in_pyv_fmla(globals, x, under_new) for x in e.args])
            if under_new:
                res.add(e.callee)
            return res
        elif isinstance(e, pyv.QuantifierExpr):
            return Translation.new_globals_in_pyv_fmla(globals, e.body, under_new)
        elif isinstance(e, pyv.Id):
            if under_new and e.name in globals:
                return set([e.name])
            return set()
        elif isinstance(e, pyv.IfThenElse):
            return Translation.new_globals_in_pyv_fmla(globals, e.branch, under_new) \
                | Translation.new_globals_in_pyv_fmla(globals, e.then, under_new) \
                      | Translation.new_globals_in_pyv_fmla(globals, e.els, under_new)
        elif isinstance(e, pyv.Let):
            return Translation.new_globals_in_pyv_fmla(globals, e.val, under_new) \
                | Translation.new_globals_in_pyv_fmla(globals, e.body, under_new)
        else:
            assert False, e

    def translate_initializer(init: ia.Action) -> tuple[pyv.InitDecl, set[il.Symbol]]:
        '''Translate an Ivy (combined) initializer, i.e. one that calls in
        sequence all the initializer actions, to a mypyvy initializer.
        This might include intermediate versions of relations.
        To translate these to mypyvy, we collect them and return them as
        the second return value. Our caller then must ensure these are
        defined at the top-level in the mypyvy spec.
        '''
        # This is substantially similar to translate_action, but instead
        # of defining a mypyvy transition, we explicitly add existential
        # quantifiers around the one-state formula for init.

        # We want a one-state formula in this context
        upd = it.make_vc(init).to_formula()
        # For some reason, make_vc() returns a conjuction
        # that has Not(And()) at the end. We remove that.
        # FIXME: are we supposed to negate the whole thing?
        assert isinstance(upd, lg.And) and upd.terms[-1] == lg.Not(lg.And())
        upd = lg.And(*upd.terms[:-1])
        # Add existential quantifiers for all implicitly existentially quantified variables
        exs = set(filter(itr.is_skolem, upd.symbols()))
        first_order_exs = set(filter(lambda x: il.is_first_order_sort(x.sort) | il.is_enumerated_sort(x.sort) | il.is_boolean_sort(x.sort), exs))
        second_order_exs = set(filter(lambda x: il.is_function_sort(x.sort), exs))
        assert exs == first_order_exs | second_order_exs, "exs != first_order_exs + second_order_exs: {} != {} + {}".format(exs, first_order_exs, second_order_exs)

        ex_quant = sorted(list(first_order_exs))
        # HACK: lg.Exists only takes Vars (ex_quant has Const), but mypyvy
        # does not distinguish between the two -- it's all pyv.Id, so
        # we add the existentials on the mypyvy side, rather than in Ivy.
        fmla = Translation.translate_logic_fmla(upd)
        ex_fmla = pyv.Exists(Translation.translate_binders(ex_quant), fmla)
        decl = pyv.InitDecl(None, ex_fmla)
        return (decl, second_order_exs)

    def translate_action(pyv_mutable_symbols: set[str], name: str, action: ia.Action) -> tuple[pyv.DefinitionDecl, set[il.Symbol]]:
        '''Translate an Ivy action to a mypyvy action. The transition
        relation might include temporary/intermediate versions of relations.
        To translate these to mypyvy, we collect them and return them as
        the second return value. Our caller then must ensure these are
        defined at the top-level in the mypyvy spec.'''
        # This gives us a two-state formula
        (_mod, tr, pre) = action.update(im.module,None)

        # The precondition is defined negatively, i.e. the action *fails*
        # if the precondition is true, so we negate it.
        fmla = lg.And(lg.Not(pre.to_formula()), tr.to_formula())
        symbols = {}
        for sym in fmla.symbols():
            symbols[sym.name] = sym

        # Make sure round-tripping through SMT works        
        z3_fmla = ivy_solver.formula_to_z3(fmla)
        _fmla = Translation.smt_to_ivy(z3_fmla, im.module.sig.sorts, symbols)
        assert fmla == _fmla, "Round-tripping Ivy -> SMT -> Ivy is incorrect: BEFORE:\n{}\n!=\nAFTER:\n{}".format(fmla, _fmla)

        # then simplify via SMT
        sfmla = Translation.simplify_via_smt(z3_fmla)
        _sfmla = Translation.smt_to_ivy(sfmla, im.module.sig.sorts, symbols)
        fmla = _sfmla

        # Collect all implicitly existentially quantified variables
        # ...and add them as parameters to the transition after
        # the action's own formal params
        exs = set(filter(itr.is_skolem, fmla.symbols()))
        first_order_exs = set(filter(lambda x: il.is_first_order_sort(x.sort) | il.is_enumerated_sort(x.sort) | il.is_boolean_sort(x.sort), exs))

        # We can get intermediate versions of relations and functions,
        # e.g. __m_l.a.b.balance.map(V0,V1), and we can't translate those as parameters
        # We have to collect these and define them as relations/functions at the
        # top-level, and also define an action that sets them arbitrarily.
        second_order_exs = set(filter(lambda x: il.is_function_sort(x.sort), exs))
        assert exs == first_order_exs | second_order_exs, "exs != first_order_exs + second_order_exs: {} != {} + {}".format(exs, first_order_exs, second_order_exs)

        # Add to params
        # it seems exs already contains action.formal_params
        # but we might to use action.formal_params to prettify names
        params = sorted(list(first_order_exs))
        # what to do with action.formal_returns?
        # it seems they're already existentials, so we can just ignore them

        # Generate the transition
        pyv_name = Translation.to_pyv_name(name)
        pyv_params = Translation.translate_binders(params)
        pyv_fmla = Translation.translate_logic_fmla(fmla, is_twostate=True)

        # NOTE: mypyvy is less clever than Ivy when it comes to identifying
        # what is modified. In particular, if there is a clause of the form
        # new(env_historical_auth_required(O, t__this, _approve)), where
        # t__this is an individual, it will think that t__this is modified
        # by this clause, but that's not really the case.
        #
        # In any case, because we do simplification, some symbols from the
        # original formula might have disappeared, so we can't just use
        # what Ivy thought is modified by the action.
        #
        # Rather than relying on Ivy's output, we compute the set of modified
        # symbols ourselves, by mimicking mypyvy's logic.
        pyv_new_globals: set[str] = Translation.new_globals_in_pyv_fmla(pyv_mutable_symbols, pyv_fmla)
        mods = tuple([pyv.ModifiesClause(x) for x in sorted(pyv_new_globals)])

        trans = pyv.DefinitionDecl(True, 2, pyv_name, pyv_params, mods, pyv_fmla)
        return (trans, second_order_exs)
    

    def simplify_via_smt(fmla: z3.BoolRef) -> z3.BoolRef:
        '''Simplify an SMT formula.'''
        # First, apply the contextual solver
        fmla = z3.Tactic('ctx-solver-simplify').apply(fmla).as_expr()
        # Then perform our own (further) simplifications
        # https://microsoft.github.io/z3guide/programming/Example%20Programs/Formula%20Simplification/

        # Rules for simplification:
        # (1) (if B then X else X) => X
        # (2) (X = X) => true

        return fmla


# Our own class, which will be used to generate a mypyvy `Program`
class MypyvyProgram:
    # sort -> pyv.SortDecl
    # individual -> pyv.ConstantDecl (immutable)
    # axiom -> pyv.AxiomDecl

    def __init__(self):
        self.immutable_symbols: set[str] = set()

        self.actions = []
        self.axioms = []
        self.constants = []
        self.functions = []
        self.initializers = []
        self.invariants = []
        self.relations = []
        self.sorts = []
        # These are translation artifacts: declarations of intermediate relations/functions
        # and the action that sets them arbitrarily.
        self.second_order_existentials = set() # collects the names
        self.intermediate = [] # declarations
        self.havoc_action = [] # declarations

    def add_constant_if_not_exists(self, cst):
        if cst.name not in [x.name for x in self.constants]:
            self.constants.append(cst)

    def add_sort(self, sort):
        # i.e. UninterpretedSort
        if il.is_first_order_sort(sort):
            decl = pyv.SortDecl(sort.name)
            self.sorts.append(decl)
        elif il.is_enumerated_sort(sort):
            # Declare the sort
            decl = pyv.SortDecl(sort.name)
            pyv_sort = Translation.to_pyv_sort(sort)
            self.sorts.append(decl)

            # Add constants (individuals) for each enum value
            # For some reason, not all enum variants show up in sig.symbols,
            # so we cannot add them in `translate_ivy_sig`
            for enum_value in sort.defines():
                const = pyv.ConstantDecl(enum_value, pyv_sort, False)
                self.constants.append(const)

            # Add distinct axioms (if there are >=2 enum values)
            individuals = [pyv.Id(name) for name in sort.defines()]
            if len(individuals) >= 2:
                op = pyv.NaryExpr("DISTINCT", tuple(individuals))
                axiom = pyv.AxiomDecl("{}_distinct".format(sort.name), op)
                self.axioms.append(axiom)
        elif il.is_boolean_sort(sort):
            # No need to declare the bool sort
            pass
        else:
            # print("unhandled sort: {}".format(sort))
            raise NotImplementedError("sort {} not supported".format(sort))

    def mutable_symbols(self) -> set[str]:
        '''Returns the set of mutable symbols.'''
        mut = set()
        for c in self.constants + self.relations + self.functions:
            if c.mutable:
                mut.add(c.name)
        return mut

    def translate_ivy_sig(self, mod: im.Module):
        '''Translate a module signature to the sorts, constants,
        relations, and functions of a mypyvy specification.
        '''
        # Identify immutable symbols: those which appear in axioms
        # and those which are functionally axioms in this isolate
        # (i.e. properties that are assumed to be true)
        for ax in mod.axioms:
            self.immutable_symbols |= Translation.globals_in_fmla(ax)
        for prop in mod.labeled_props:
            if prop.assumed:
                self.immutable_symbols |= Translation.globals_in_fmla(prop.formula)

        sig: il.Sig = mod.sig
        # Add sorts
        for (_sort_name, sort) in sig.sorts.items():
            self.add_sort(sort)

        # # Add symbols, replacing "." with DOT_REPLACEMENT
        for _sym_name, sym in sig.symbols.items():
            assert _sym_name == sym.name, "symbol name mismatch: {} != {}".format(_sym_name, sym.name)
            kind = Translation.sort_type(sym.sort)
            is_mutable = (sym.name not in self.immutable_symbols)
            pyv_sym_decl = Translation.translate_symbol_decl(sym, is_mutable)
            if kind == 'individual':
                self.add_constant_if_not_exists(pyv_sym_decl)
            elif kind == 'relation':
                assert sym.is_relation()
                self.relations.append(pyv_sym_decl)
            elif kind == 'function':
                self.functions.append(pyv_sym_decl)
            else:
                raise NotImplementedError("translating symbol {} to mypyvy ".format(repr(sym)))

    def add_axioms_and_props(self, mod: im.Module):
        '''Add axioms and properties to the mypyvy program.'''
        # Add axioms
        # For some reason, these are directly formulas, rather than AST nodes
        for ax in mod.axioms:
            # ...and therefore don't have axiom names
            fmla = Translation.translate_logic_fmla(ax)
            axiom = pyv.AxiomDecl(None, fmla)
            self.axioms.append(axiom)

        # Add properties that are assumed to be true in this isolate
        for prop in mod.labeled_props:
            if prop.assumed:
                fmla = Translation.translate_logic_fmla(prop.formula)
                axiom = pyv.AxiomDecl(Translation.to_pyv_name(prop.label.relname), fmla)
                self.axioms.append(axiom)

    def add_conjectures(self, mod: im.Module):
        '''Add conjectures (claimed invariants) to the mypyvy program.'''
        # Add conjectures
        for conj in mod.labeled_conjs:
            fmla = Translation.translate_logic_fmla(conj.formula)
            inv = pyv.InvariantDecl(Translation.to_pyv_name(conj.label.relname), fmla, False, False)
            self.invariants.append(inv)

    def add_initializers(self, mod: im.Module):
        '''Add initializers to the mypyvy program. Note that we CANNOT
        translate initializers one-by-one, because (at least in Ivy 1.8)
        they are stateful: the second initializer might depend on the state
        modified by the first. Therefore, we create an artificial action
        that combines all initializers in sequence, and translate that.'''
        inits = list(map(lambda x: x[1], mod.initializers)) # get the actions
        init_action = ia.Sequence(*inits)
        (decl, sec_ord_exs) = Translation.translate_initializer(init_action)
        self.second_order_existentials |= sec_ord_exs
        self.initializers.append(decl)

    def add_public_actions(self, mod: im.Module):
        '''Add public actions to the mypyvy program.'''
        public_actions = filter(lambda x: x[0] in mod.public_actions, mod.actions.items())
        mutable_symbols = self.mutable_symbols()
        for (name, action) in public_actions:
            (decl, sec_ord_exs) = Translation.translate_action(mutable_symbols, name, action)
            self.second_order_existentials |= sec_ord_exs
            self.actions.append(decl)

    def add_intermediate_rels_fn_and_havoc_action(self, mod: im.Module):
        '''Declares relations and functions for the intermediate versions
        of variables, and defines an action that sets them arbitrarily.'''
        # Define second order existentials as (mutable) relations/functions
        for se_ex in self.second_order_existentials:
            pyv_decl = Translation.translate_symbol_decl(se_ex, True)
            self.intermediate.append(pyv_decl)

        # Create a havoc action that sets all second order existentials arbitrarily
        if len(self.second_order_existentials) > 0:
            modified = sorted([Translation.to_pyv_name(x.name) for x in self.second_order_existentials])
            mods = tuple([pyv.ModifiesClause(x) for x in modified])
            havoc_clauses: list[pyv.Expr] = [Translation.pyv_havoc_symbol(x) for x in self.second_order_existentials]
            havoc_fmla = pyv.And(*havoc_clauses)
            act = pyv.DefinitionDecl(True, 2, "_havoc_intermediaries", [], mods, havoc_fmla)
            self.havoc_action.append(act)

    def to_program(self) -> pyv.Program:
        decls = self.sorts + self.constants + self.relations + \
            self.functions + self.axioms + \
            self.intermediate + self.havoc_action + \
            self.initializers + self.actions + self.invariants
        return pyv.Program(decls)


def check_isolate():
    mod = im.module
    prog = MypyvyProgram()

    # FIXME: do we need to handle mod.aliases? (type aliases)

    # STEP 1: parse mod.sig to determine
    # sorts, relations, functions, and individuals
    # mod.sig.sorts & mod.sig.symbols
    prog.translate_ivy_sig(mod)

    # STEP 2: add axioms and conjectures
    # mod.axioms
    # mod.labeled_props -> properties (become axioms once checked)
    # mod.labeled_conjs -> invariants/conjectures (to be checked)
    prog.add_axioms_and_props(mod)
    prog.add_conjectures(mod)

    # STEP 3: generate actions
    # - collect all implicitly existentially quantified variables (those starting with __)
    # mod.initializers -> after init
    # mod.public_actions
    # mod.actions
    prog.add_initializers(mod)
    prog.add_public_actions(mod)
    prog.add_intermediate_rels_fn_and_havoc_action(mod)

    #  Generate the program
    pyv_prog = prog.to_program()

    out_file = "{}.pyv".format(mod.name)
    with open(out_file, "w") as f:
        f.write(str(pyv_prog))
        print("output written to {}".format(out_file))

    exit(0)
