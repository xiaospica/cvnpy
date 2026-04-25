# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Callable, Dict, Type

from .base import SimBarSource

_REGISTRY: Dict[str, Type[SimBarSource]] = {}


def register_bar_source(name: str) -> Callable[[Type[SimBarSource]], Type[SimBarSource]]:
    def deco(cls: Type[SimBarSource]) -> Type[SimBarSource]:
        if name in _REGISTRY and _REGISTRY[name] is not cls:
            raise ValueError(f"bar_source name {name!r} already registered by {_REGISTRY[name]}")
        _REGISTRY[name] = cls
        return cls
    return deco


def build_bar_source(name: str, **kwargs) -> SimBarSource:
    if name not in _REGISTRY:
        raise ValueError(f"unknown bar_source: {name!r}. registered: {sorted(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)


def registered_names() -> list[str]:
    return sorted(_REGISTRY)


def unregister_bar_source(name: str) -> None:
    _REGISTRY.pop(name, None)
