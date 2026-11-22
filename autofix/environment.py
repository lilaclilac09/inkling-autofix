import os
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from .providers import model_config


_model_keys = ContextVar("model_keys", default=frozenset())


def command_env(api_key_env=None):
    excluded = {"TINKER_API_KEY", "INKLING_API_KEY", "OPENAI_API_KEY", *_model_keys.get()}
    if api_key_env:
        excluded.add(api_key_env)
    return {name: value for name, value in os.environ.items()
            if name not in excluded and not (name.startswith("AUTOFIX_") and "KEY" in name)}


@contextmanager
def key_environment(name):
    token = _model_keys.set(_model_keys.get() | {name})
    try:
        yield
    finally:
        _model_keys.reset(token)


def model_commands(function):
    @wraps(function)
    def wrapped(config, *args, **kwargs):
        with key_environment(model_config(config.get("model", {}))["api_key_env"]):
            return function(config, *args, **kwargs)
    return wrapped
