"""Shared helpers for the CLI front-ends."""

import argparse

from configgen import ValidationError


def arg_validator(fn):
    """Adapt a configgen validator (raises ValidationError) into an argparse `type=`.

    Preserves the validator's message in the argparse "invalid value" error, while
    keeping the library validators free of any argparse dependency.
    """

    def _type(value):
        try:
            return fn(value)
        except ValidationError as exc:
            raise argparse.ArgumentTypeError(str(exc))

    _type.__name__ = getattr(fn, "__name__", "value")
    return _type
