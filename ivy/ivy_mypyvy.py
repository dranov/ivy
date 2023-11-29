#
# Copyright (c) Microsoft Corporation. All Rights Reserved.
#
from typing import Any
from . import ivy_actions as ia
from . import ivy_ast as ast
from . import ivy_logic as il
from . import ivy_module as im
from . import ivy_proof as pf
from . import ivy_solver
from . import ivy_trace as it
from . import ivy_transrel as itr
from . import logic as lg
from . import mypyvy_syntax as pyv

import time
from ivy.z3 import z3

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

IVY_TEMPORARY_INDICATOR = '__m_'

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

    def smt_binder_name_to_ivy_name(name: str) -> str:
        # For some reason, SMT vars incorporate the sort,
        # like 'X:integer'; we only want the 'X'
        return name.split(':')[0]

    def smt_binders_to_ivy(sorts: dict[str, Any], fmla: z3.BoolRef):
        '''Convert the binders of an SMT formula to Ivy binders.
        sorts: dictionary from sort names to Ivy sorts
        '''
        assert z3.is_ast(fmla) and z3.is_expr(fmla) and z3.is_quantifier(fmla)
        num_binders = fmla.num_vars()
        binders = [lg.Var(Translation.smt_binder_name_to_ivy_name(fmla.var_name(i)), sorts[fmla.var_sort(i).name()]) for i in range(num_binders)]
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
        # https://stackoverflow.com/questions/13357509/is-it-possible-to-access-the-name-associated-with-the-de-bruijn-index-in-z3
        if z3.is_quantifier(fmla) and fmla.is_forall():
            # How to translate the sorts of the vars?
            new_binders = Translation.smt_binders_to_ivy(sorts, fmla)
            # XXX: this seems to work, but I'm not sure it's correct
            binders = list(reversed(new_binders)) + binders
            return lg.ForAll(new_binders, Translation.smt_to_ivy(fmla.body(), sorts, syms, binders))
        elif z3.is_quantifier(fmla): # strangely, there is no is_exists()
            new_binders = Translation.smt_binders_to_ivy(sorts, fmla)
            binders = list(reversed(new_binders)) + binders
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

    def pyv_globals_under_new(globals: set[str], e: pyv.Expr, under_new=False) -> set[str]:
        '''Returns the set of global names that appear in a mypyvy formula
        under new(). Used to identify which relations/functions we need to
        declare as modified. Takes as argument the set of all mutable global symbols.
        IMPORTANT: because of mypyvy's conservative logic, some of these variables
        might not actually be modified. We need to identify those separately.'''
        if isinstance(e, pyv.Bool) or isinstance(e, pyv.Int):
            return set()
        elif isinstance(e, pyv.UnaryExpr):
            if e.op == 'NEW':
                return Translation.pyv_globals_under_new(globals, e.arg, under_new=True)
            return Translation.pyv_globals_under_new(globals, e.arg, under_new)
        elif isinstance(e, pyv.BinaryExpr):
            return Translation.pyv_globals_under_new(globals, e.arg1, under_new) | Translation.pyv_globals_under_new(globals, e.arg2, under_new)
        elif isinstance(e, pyv.NaryExpr):
            return set.union(*[Translation.pyv_globals_under_new(globals, x, under_new) for x in e.args])
        elif isinstance(e, pyv.AppExpr):
            res = set.union(*[Translation.pyv_globals_under_new(globals, x, under_new) for x in e.args])
            if under_new:
                res.add(e.callee)
            return res
        elif isinstance(e, pyv.QuantifierExpr):
            return Translation.pyv_globals_under_new(globals, e.body, under_new)
        elif isinstance(e, pyv.Id):
            if under_new and e.name in globals:
                return set([e.name])
            return set()
        elif isinstance(e, pyv.IfThenElse):
            return Translation.pyv_globals_under_new(globals, e.branch, under_new) \
                | Translation.pyv_globals_under_new(globals, e.then, under_new) \
                      | Translation.pyv_globals_under_new(globals, e.els, under_new)
        elif isinstance(e, pyv.Let):
            return Translation.pyv_globals_under_new(globals, e.val, under_new) \
                | Translation.pyv_globals_under_new(globals, e.body, under_new)
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
        print("Translating initializer... ", end='', flush=True)
        _start = time.monotonic()
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

        symbols = {}
        for sym in upd.symbols():
            symbols[sym.name] = sym
        # Simplify via SMT
        z3_fmla = ivy_solver.formula_to_z3(upd)
        sfmla = Translation.simplify_via_smt(z3_fmla, symbols)
        _sfmla = Translation.smt_to_ivy(sfmla, im.module.sig.sorts, symbols)
        _upd = upd # Save original formula
        upd = _sfmla

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
        _end = time.monotonic()
        print("done in {:.2f}s! ({:.1f}% of original size)".format(_end - _start, ((len(str(_sfmla)) / len(str(_upd))) * 100)))
        return (decl, second_order_exs)

    def translate_action(pyv_mutable_symbols: set[str], name: str, action: ia.Action) -> tuple[pyv.DefinitionDecl, set[il.Symbol]]:
        '''Translate an Ivy action to a mypyvy action. The transition
        relation might include temporary/intermediate versions of relations.
        To translate these to mypyvy, we collect them and return them as
        the second return value. Our caller then must ensure these are
        defined at the top-level in the mypyvy spec.'''
        print("Translating action `{}`... ".format(name), end='', flush=True)
        _start = time.monotonic()
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
        sfmla = Translation.simplify_via_smt(z3_fmla, symbols)
        _sfmla = Translation.smt_to_ivy(sfmla, im.module.sig.sorts, symbols)
        _fmla = fmla # Save the original fmla
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
        pyv_supposedly_modified: set[str] = Translation.pyv_globals_under_new(pyv_mutable_symbols, pyv_fmla)
        mods = tuple([pyv.ModifiesClause(x) for x in sorted(pyv_supposedly_modified)])

        # We still need to identify which relations are not really modified,
        # but only caught in mypyvy's conservative logic.
        # What we do is we look at the simplified formula (`fmla`) and see
        # which symbols start with new_. These should actually match
        # the Ivy modified symbols in the original formula.
        actually_modified = set(map(itr.new_of, filter(itr.is_new, fmla.symbols())))

        # Sanity check: simplification shouldn't have changed the set of
        # modified symbols.
        orig_modified = set(map(lambda x: x.name, _mod))
        simpl_modified = set(map(lambda x: x.name, actually_modified))
        assert orig_modified == simpl_modified, "orig_modified != simpl_modified: {} != {}".format(orig_modified, simpl_modified)

        # For each not actually modified relation, add a clause that
        # it hasn't changed (otherwise it will get havoc'ed).
        pyv_actually_modified: set[str] = set(map(Translation.to_pyv_name, simpl_modified))
        pyv_not_actually_modified = pyv_supposedly_modified - pyv_actually_modified
        if len(pyv_not_actually_modified) > 0:
            ivy_not_actually_modified = set(filter(lambda x: Translation.to_pyv_name(x.name) in pyv_not_actually_modified, fmla.symbols()))
            noop_clauses = [Translation.pyv_unchanged_symbol(x) for x in ivy_not_actually_modified]
            noop_fmla = pyv.And(*noop_clauses)
            pyv_fmla = pyv.And(pyv_fmla, noop_fmla)

        trans = pyv.DefinitionDecl(True, 2, pyv_name, pyv_params, mods, pyv_fmla)
        _end = time.monotonic()
        print("done in {:.2f}s! ({:.1f}% of original size)".format(_end - _start, (len(str(_sfmla)) / len(str(_fmla)) * 100)))
        return (trans, second_order_exs)
    

    def simplify_via_smt(fmla: z3.BoolRef, syms: dict[str, Any]) -> z3.BoolRef:
        '''Simplify an SMT formula.'''
        orig_fmla = fmla

        # Perform our own simplifications.
        # https://microsoft.github.io/z3guide/programming/Example%20Programs/Formula%20Simplification/

        # We would want to apply the macro-finder tactic and apply it
        # only for the Skolem relations, but it seems that's not possible
        # without modifying Z3 internals. Instead, we'll do it ourselves.
        # Inspiration:
        # https://github.com/Z3Prover/z3/blob/3422f44cea4e73572d1e22d1c483a960ec788771/src/ast/macros/macro_finder.cpp
        # https://github.com/Z3Prover/z3/blob/3422f44cea4e73572d1e22d1c483a960ec788771/src/ast/macros/macro_util.cpp

        # TODO: rRfactor this to perform the macro identification
        # on the Ivy side, rather than relying on Z3.

        def is_macro(f) -> bool:
            '''Returns true if the given formula is a macro.
            Implemented by calling Z3.'''
            if not z3.is_bool(f):
                return False

            s = z3.Tactic('macro-finder').apply(f).as_expr()
            # NOTE: this also returns True if `f` is a conjunction of
            # macros; we don't want that, but it isn't an issue given
            # how we call `is_macro` in this context.
            return z3.is_true(s)

        def is_skolem_macro(f) -> bool:
            '''Returns true if the given formula is a macro
            that defines a Skolem relation.'''
            if not is_macro(f):
                return False

            # at the very least, it can identify when _rel is on the RHS
            # Type of macros we support:
            #  - ForAll(binders, Iff/Eq(_rel(binders), definition))
            if z3.is_quantifier(f) and f.is_forall():
                # Identify if _rel(binders) is on LHS or RHS of Iff/Eq
                num_binders = f.num_vars()
                sides = [0, 1] # LHS, RHS
                for side in sides:
                    if not f.body().children()[side].num_args() == num_binders:
                        continue
                    # name of quantified relation
                    rel_name = f.body().children()[side].decl().name()
                    return rel_name.startswith(IVY_TEMPORARY_INDICATOR)

            return False

        class Macro:
            def __init__(self, head, body, num_args, full_fmla):
                self.head = head
                self.body = body
                self.num_args = num_args
                self.full_fmla = full_fmla

            def __str__(self):
                return self.full_fmla.__str__()

            def is_application(self, fmla):
                '''Is fmla an application of this macro?'''
                if not z3.is_app(fmla):
                    return False
                if not fmla.decl() == self.head.decl():
                    return False
                return True

        def parse_macro(f):
            '''Splits a formula identified as a macro into a macro_head
            and a macro_body. Macro heads can then be identified in other formulas
            and replaced with the body.'''
            assert z3.is_quantifier(f) and f.is_forall(), f"Unsupported macro type: {f}"
            # Identify if _rel(binders) is on LHS or RHS of Iff/Eq
            num_binders = f.num_vars()
            sides = [0, 1] # LHS, RHS
            for side in sides:
                if not f.body().children()[side].num_args() == num_binders:
                    continue
                macro_head = f.body().children()[side]
                macro_body = f.body().children()[1 - side]
                return Macro(macro_head, macro_body, num_binders, f)
            assert False, f"Macro could not be parsed: {f}"

        def subterms(t):
            seen = {}
            def subterms_rec(t):
                if z3.is_app(t):
                    for ch in t.children():
                        if ch in seen:
                            continue
                        seen[ch] = True
                        # Return smaller subterms first
                        yield from subterms_rec(ch)
                        yield ch
                # TODO: look under ForAll & Exists?
            return { s for s in subterms_rec(t) }

        # We want to substitute m.head with m.bodies in fmla
        # This seems very similar to `ivy_proof.ivy:unfold_fmla()`, which
        # does this on the Ivy side.
        def remove_macro_definition(t, macro):
            def simplify_rec(t):
                # Remove the macro definition
                if z3.eq(t, macro.full_fmla):
                    return True
                # Replace the macro application with the body
                # if macro.is_application(t):
                    # return macro.unfold_definition_for(t)
                chs = [simplify_rec(ch) for ch in t.children()]
                # ForAll and Exists
                if z3.is_quantifier(t):
                    assert len(chs) == 1
                    vs = [z3.Const(t.var_name(i), t.var_sort(i)) for i in range(t.num_vars())]
                    if t.is_forall():
                        return z3.ForAll(vs, chs[0])
                    else:
                        return z3.Exists(vs, chs[0])
                # And() and Or() should be the same as applications
                # but there is some arity check in z3.py that fails
                # if they are treated as applications (their arity is supposedly 2).
                if z3.is_and(t):
                    return z3.And(chs)
                if z3.is_or(t):
                    return z3.Or(chs)
                if z3.is_app(t):
                    return t.decl()(chs)
                if z3.is_const(t) or z3.is_var(t):
                    return t
                else:
                    raise NotImplementedError(f"unhandled case: {t}")
            return simplify_rec(t)

        while True:
            remaining_macros = map(parse_macro, filter(is_skolem_macro, subterms(fmla)))
            # If there are no remaining macros to rewrite, we're done
            # https://stackoverflow.com/a/21525143
            _exhausted = object()
            macro = next(remaining_macros, _exhausted)
            if macro is _exhausted:
                break

            # Otherwise we reduce with this macro.
            # This also replaces the macro definition itself with 'True'
            _fmla = remove_macro_definition(fmla, macro)
            iv_fmla = Translation.smt_to_ivy(_fmla, im.module.sig.sorts, syms)
            macro_fmla = Translation.smt_to_ivy(macro.full_fmla, im.module.sig.sorts, syms)
            dfn = ast.LabeledFormula(ast.Atom('interm_macro'), macro_fmla)
            _iv_fmla = pf.unfold_fmla(iv_fmla, [[dfn]])
            _sm_fmla = ivy_solver.formula_to_z3(_iv_fmla)

            fmla = _sm_fmla

        # Check that we produced an equivalent formula
        s = z3.Solver()
        s.add(z3.Not(orig_fmla == fmla))
        res = s.check
        assert res != z3.unsat, f"Simplification equivalence: {res} produced a non-equivalent formula: {orig_fmla}\nis not equivalent to\n{fmla}"

        # Then perform Z3 simplifications
        fmla = z3.Tactic('ctx-solver-simplify').apply(fmla).as_expr()
        fmla = z3.Tactic('propagate-values').apply(fmla).as_expr()

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

    def translate_ivy_sig(self, mod: im.Module, sig: il.Sig = None):
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

        # If we are explicitly passed a signature to use, use that one
        sig: il.Sig = sig or mod.sig
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

    # FIXME: Is using mod.old_sig correct?
    # An isolate's (call it X) signature does not contain symbols used internally
    # by isolates associated with it (e.g. Y) (e.g. via Ivy's `with` mechanism),
    # but such symbols might appear when we translate X's actions to mypyvy,
    # if X calls actions from Y.
    prog.translate_ivy_sig(mod, mod.old_sig)

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
    print("The translation performs simplification via SMT. It might take on the order of minutes!")
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
