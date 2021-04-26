import torch
import inspect
import numbers
import typing
import enum
from typing import Any, Callable, Dict, List, Optional, Tuple, cast
from torch._jit_internal import boolean_dispatched

_manual_overrides : Dict[Callable, List[inspect.Signature]] = {}

def _nonzero_schemas():
    signatures = []

    def nonzero(self):
        pass
    signatures.append(inspect.signature(nonzero))

    def nonzero(self, *, as_tuple : bool):  # type: ignore[no-redef]
        pass
    signatures.append(inspect.signature(nonzero))

    return signatures

_manual_overrides[torch.nonzero] = _nonzero_schemas()

class _FakeGlobalNamespace:
    def __getattr__(self, name):
        if name == 'torch':
            return torch
        raise RuntimeError('Expected a torch namespace lookup')

_type_eval_globals = {'Tensor' : torch.Tensor, 'Device' : torch.device, 'Layout' : torch.layout,
                      'number' : numbers.Number, 'Future' : torch.jit.Future,
                      'AnyEnumType' : enum.Enum, 'QScheme' : torch.qscheme,
                      '__torch__': _FakeGlobalNamespace(), 'NoneType': type(None),
                      't': typing.TypeVar('t')}
for k in dir(typing):
    _type_eval_globals[k] = getattr(typing, k)

def _torchscript_type_to_python_type(ts_type : 'torch._C.JitType') -> Any:
    """
    Convert a TorchScript type to a Python type (including subtypes) via
    eval'ing the annotation_str. _type_eval_globals sets up expressions
    like "List" and "Future" to map to actual types (typing.List and jit.Future)
    """
    return eval(ts_type.annotation_str, _type_eval_globals)

def _torchscript_schema_to_signature(ts_schema : torch._C.FunctionSchema) -> inspect.Signature:
    parameters : List[inspect.Parameter] = []
    for arg in ts_schema.arguments:
        arg_type = _torchscript_type_to_python_type(arg.type)
        default = arg.default_value if arg.has_default_value() else inspect.Parameter.empty
        # TODO: Figure out if this is safe. It seems like when generating the type signatures for
        # PythonArgParser, we emit signatures with `input` instead of `self` as the first tensor
        # argument name. Downstream, if someone converts that positional argument to a keyword
        # argument, the name mismatch will break things, so here we're going to normalize the
        # name to "input"
        name = arg.name if arg.name != 'self' else 'input'
        kind = inspect.Parameter.KEYWORD_ONLY if arg.kwarg_only else inspect.Parameter.POSITIONAL_OR_KEYWORD
        parameters.append(inspect.Parameter(name=name, kind=kind, default=default, annotation=arg_type))
    return_types = [_torchscript_type_to_python_type(ret.type) for ret in ts_schema.returns]
    if len(return_types) == 0:
        return_type = None
    elif len(return_types) == 1:
        return_type = return_types[0]
    else:
        return_type = tuple(return_types)

    return inspect.Signature(parameters, return_annotation=return_type)

def get_signature_for_torch_op(op : Callable) -> Optional[List[inspect.Signature]]:
    """
    Given an operator on the `torch` namespace, return a list of `inspect.Signature`
    objects corresponding to the overloads of that op.. May return `None` if a signature
    could not be retrieved.

    Args:
        op (Callable): An operator on the `torch` namespace to look up a signature for

    Returns:
        Optional[List[inspect.Signature]]: A list of signatures for the overloads of this
            operator, or None if the operator signatures could not be retrieved.
    """
    override = _manual_overrides.get(op)
    if override:
        return override

    aten_fn = torch.jit._builtins._find_builtin(op)

    if aten_fn is None:
        return None

    schemas = torch._C._jit_get_schemas_for_operator(aten_fn)
    signatures = [_torchscript_schema_to_signature(schema) for schema in schemas]

    return signatures

def create_type_hint(x):
    if isinstance(x, tuple):
        if len(x) == 0:
            return Tuple[()]
        if all(type(i) is type(x[0]) for i in x):
            return Tuple[type(x[0]), ...]
        return Tuple[Any, ...]
    return type(x)

def type_matches(signature_type : Any, argument_type : Any):
    sig_origin_type = getattr(signature_type, '__origin__', signature_type)

    # Union types in signature. Given type needs to match one of the
    # contained types in the Union
    if sig_origin_type is typing.Union and signature_type != argument_type:
        sig_contained = signature_type.__args__
        return any(type_matches(c, argument_type) for c in sig_contained)

    if signature_type is List[int] and argument_type is int:
        # int can be promoted to List[int]
        return True

    def is_homogeneous_int_tuple(t):
        if not getattr(t, '__origin__', None) in {tuple, Tuple}:
            return False

        contained = t.__args__
        if t.__args__ == ((),):  # Tuple[()].__args__ == ((),) for some reason
            return True
        return all(c is int or (c is Ellipsis) for c in contained)

    if signature_type is List[int] and is_homogeneous_int_tuple(argument_type):
        # Tuple[int] is accepted for List[int] parameters
        return True

    # Dtype is an int in schemas
    if signature_type is int and argument_type is torch.dtype:
        return True

    if signature_type is numbers.Number and argument_type in {int, float}:
        return True
    return issubclass(argument_type, signature_type)

def normalize_function(
        target: Callable, args: Tuple[Any], kwargs : Optional[Dict[str, Any]] = None, arg_types : Optional[Tuple[Any]] = None,
        kwarg_types : Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Returns normalized arguments to PyTorch functions. This means that
    `args/kwargs` will be matched up to the functional's
    signature and return exclusively kwargs in positional order.
    Also populates default values. Does not support positional-only
    parameters or varargs parameters (*args, **kwargs). Does not support modules.

    May require `arg_types` and `kwarg_types` in order to disambiguate overloads.

    Args:
        target (Callable): Function that we are normalizing
        args (Tuple[Any]): Tuple of args to the function
        kwargs (Optional[Dict[str, Any]]): Dict of kwargs to the function
        arg_types (Optional[Tuple[Any]]): Tuple of arg types for the args
        kwarg_types (Optional[Dict[str, Any]]): Dict of arg types for the kwargs

    Returns:

        Returns normalized_kwargs, or `None` if not successful.
    """
    if kwargs is None:
        kwargs = {}
    new_kwargs = None
    if target in boolean_dispatched or target.__module__ in ['torch.nn.functional', 'torch.functional']:
        target_for_analysis = target
        if target in boolean_dispatched:
            # HACK: `boolean_dispatch` as used in `torch.nn.functional` makes it so that we have
            # a 2-way dispatch based on a boolean value. Here we check that the `true` and `false`
            # branches of the dispatch have exactly the same signature. If they do, use the `true`
            # branch signature for analysis. Otherwise, leave this un-normalized
            assert not isinstance(target, str)
            dispatched = boolean_dispatched[target]
            if_true, if_false = dispatched['if_true'], dispatched['if_false']
            if inspect.signature(if_true).parameters != inspect.signature(if_false).parameters:
                return None
            target_for_analysis = if_true

        assert callable(target_for_analysis)
        sig = inspect.signature(inspect.unwrap(target_for_analysis))
        new_kwargs = _args_kwargs_to_normalized_kwargs(sig, args, kwargs)
    else:
        assert callable(target)
        torch_op_schemas = get_signature_for_torch_op(target)
        matched_schemas = []
        if torch_op_schemas:
            # Iterate through all of the schema until we find one that matches
            # If one matches, populate `new_kwargs` with the combined args/kwargs
            # values. If none matches, `new_kwargs` will be None
            for candidate_signature in torch_op_schemas:
                try:
                    candidate_signature.bind(*args, **kwargs)
                    matched_schemas.append(candidate_signature)
                except TypeError as e:
                    continue

            if len(matched_schemas) == 0:
                # Did not match any schema. Cannot normalize
                pass
            elif len(matched_schemas) == 1:
                # Matched exactly one schema, unambiguous
                new_kwargs = _args_kwargs_to_normalized_kwargs(matched_schemas[0], args, kwargs)
            else:
                if arg_types is not None or kwarg_types is not None:
                    arg_types = arg_types if arg_types else cast(Tuple[Any], ())
                    kwarg_types = kwarg_types if kwarg_types else {}
                    for candidate_signature in torch_op_schemas:
                        sig_matches = True
                        try:
                            bound_types = candidate_signature.bind(*arg_types, **kwarg_types)
                            for arg_name, arg_type in bound_types.arguments.items():
                                param = candidate_signature.parameters[arg_name]
                                sig_matches = sig_matches and type_matches(param.annotation, arg_type)
                        except TypeError as e:
                            sig_matches = False
                        if sig_matches:
                            new_kwargs = _args_kwargs_to_normalized_kwargs(candidate_signature, args, kwargs)
                            break
                else:
                    # Matched more than one schema. In this situation, the caller must provide the types of
                    # the arguments of the overload they expect.
                    schema_printouts = '\n'.join(str(schema) for schema in matched_schemas)
                    raise RuntimeError(f'Tried to normalize arguments to {torch.typename(target)} but '
                                       f'the schema match was ambiguous! Please provide argument types to '
                                       f'the normalize_arguments() call. Available schemas:\n{schema_printouts}')

    return new_kwargs

def normalize_module(
        root: torch.nn.Module, target: str, args: Tuple[Any], kwargs : Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Returns normalized arguments to PyTorch modules. This means that
    `args/kwargs` will be matched up to the functional's
    signature and return exclusively kwargs in positional order.
    Also populates default values. Does not support positional-only
    parameters or varargs parameters (*args, **kwargs).

    Args:
        root (nn.Module): root module upon which we query modules
        target (Callable): Function that we are normalizing
        args (Tuple[Any]): Tuple of args to the function
        kwargs (Optional[Dict[str, Any]]): Dict of kwargs to the function

    Returns:

        Returns normalized_kwargs, or `None` if not successful.
    """
    try:
        submod = root.get_submodule(target)
    except AttributeError:
        raise RuntimeError(f"Tried to normalize node with target {target} but root did not "
                           f"have that target!")
    if hasattr(submod.__class__, '__name__'):
        classname = submod.__class__.__name__
        if getattr(torch.nn, classname, None) == submod.__class__:
            sig = inspect.signature(inspect.unwrap(submod.forward))
            if kwargs is None:
                kwargs = {}
            new_kwargs = _args_kwargs_to_normalized_kwargs(sig, args, kwargs)
            return new_kwargs
    return None

def _args_kwargs_to_normalized_kwargs(sig : inspect.Signature, args : Tuple[Any, ...],
                                      kwargs : Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Given a call target, args, and kwargs, return the arguments normalized into
    a single kwargs dict, or None if the type signature is not supported by
    this normalization.

    Args:

        target (inspect.Signature): Signature object for the target
        args (Tuple): Arguments that appear at the callsite for `target`
        kwargs (Dict): Keyword arugments that appear at the callsite for `target`

    Returns:

        Optional[Dict]: Normalized kwargs for `target`, or `None` if this target is not
            supported
    """

    # Don't currently support positional-only
    # or varargs (*args, **kwargs) signatures
    supported_parameter_types = {
        inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    if any(p.kind not in supported_parameter_types for p in sig.parameters.values()):
        return None

    bound_args = sig.bind(*args, **kwargs)
    bound_args.apply_defaults()

    new_kwargs : Dict[str, Any] = {}
    for param in sig.parameters:
        new_kwargs[param] = bound_args.arguments[param]

    return new_kwargs
