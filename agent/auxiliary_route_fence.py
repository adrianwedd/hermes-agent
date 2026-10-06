"""Scoped route restriction for admitted, off-turn Kanban intake calls."""
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlparse

_route = ContextVar("admitted_auxiliary_route", default=None)


@contextmanager
def admitted_route(route):
    token = _route.set(dict(route))
    try:
        yield
    finally:
        _route.reset(token)


def fallback_allowed():
    return _route.get() is None


def validate_call(client, kwargs, provider):
    route = _route.get()
    if route is None:
        return
    from hermes_cli.kanban_provider_admission import remote_route
    actual = {"provider": provider, "model": kwargs.get("model"),
              "base_url": str(getattr(client, "base_url", "") or "")}
    endpoint = urlparse(actual["base_url"])
    host = {"openai-codex": "chatgpt.com", "ollama-cloud": "ollama.com"}.get(provider)
    if (actual["provider"] != route["provider"] or actual["model"] != route["model"]
            or endpoint.scheme != "https" or endpoint.hostname != host or not remote_route(actual)):
        raise RuntimeError("Auxiliary intake resolved outside its admitted provider/model route")
