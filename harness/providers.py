"""Provider registry: providers.json holds providers, each holding models.

Identity is two-layered so one provider can serve many model configurations:

  * a provider owns the connection and the credentials - base_url, headers and
    the API key (keyring only). Its id is stable and is never derived from a
    name, a host or an endpoint path.
  * a model configuration owns its display name, the API model id and the
    per-model parameters. Its id is stable and unique across every provider.

Renaming either side therefore never breaks the link, and adding a second
model to a provider cannot overwrite the first. The flat helpers
(list_models / active_config / active_model_params / ...) project that tree
into the one-model-per-row shape the engine and the renderer protocol expect.

Secrets never land in providers.json - they live in the OS keyring, one entry
per provider id. Callers only ever see masked forms.
"""

import contextlib
import contextvars
import copy
import functools
import json
import secrets
import threading
from pathlib import Path
from typing import TypedDict

from . import paths

SERVICE = "harness"
FORMAT_VERSION = 2

GROUP_PREFIX = "grp-"
MODEL_PREFIX = "mdl-"

_ACTIVE_ID = contextvars.ContextVar("providers_active_id", default=None)
_EFFORT = contextvars.ContextVar("providers_effort", default=None)


class ModelPayload(TypedDict):
    """One stored model configuration: a stable id plus its parameters.

    The contract for every dict that goes into ``providers["models"]`` and
    for the payloads the renderer sends through the model ops.
    """

    id: str
    name: str
    model: str
    reasoning_effort: str | None
    temperature: float | None
    context_window: int | None
    max_output_tokens: int | None
    supports_tools: bool
    supports_reasoning: bool
    supports_images: bool
    supports_text_tools: bool
    extra_params: dict
    disabled: bool


MODEL_FIELDS = tuple(key for key in ModelPayload.__annotations__ if key != "id")


class ModelRow(TypedDict):
    """The one-model-per-row shape the engine and the renderer protocol expect."""

    id: str
    name: str
    base_url: str
    model: str
    headers: dict
    disabled: bool
    reasoning_effort: str | None
    temperature: float | None
    context_window: int | None
    max_output_tokens: int | None
    supports_tools: bool
    supports_reasoning: bool
    supports_images: bool
    supports_text_tools: bool
    extra_params: dict
    provider_id: str
    provider_name: str


MODEL_DEFAULTS = {
    "name": "",
    "model": "",
    "reasoning_effort": None,
    "temperature": None,
    "context_window": None,
    "max_output_tokens": None,
    "supports_tools": True,
    "supports_reasoning": True,
    "supports_images": True,
    "supports_text_tools": False,
    "extra_params": {},
    "disabled": False,
}

GROUP_ALIASES = ("provider_id", "group_id", "provider_name", "base_url", "headers")
RESERVED_PARAMS = ("model", "messages", "tools", "stream", "stream_options")


@contextlib.contextmanager
def scoped(active_id=None, effort=None):
    """Scope which provider + effort this context uses (one session).

    Defaults to the shared providers.json values when nothing is attached.
    """
    t1 = _ACTIVE_ID.set(active_id)
    t2 = _EFFORT.set(effort)
    try:
        yield
    finally:
        try:
            _EFFORT.reset(t2)
        except ValueError:
            pass
        try:
            _ACTIVE_ID.reset(t1)
        except ValueError:
            pass


@contextlib.contextmanager
def use(target_id) -> contextlib.AbstractContextManager:
    """Route model calls to ``target_id`` for this context, effort untouched.

    This is how role routing works without duplicating credentials: call_llm
    reads everything - base_url, key, model id, capabilities, params, context
    window - from the active context, so overriding just the active id points a
    bounded sub-loop (the plan agent, the decision checker) at a different
    configured model while it keeps the session's effort override. An empty or
    unknown id changes nothing, so a misconfiguration can never reach the
    wire as a model the registry does not hold.
    """
    resolved = target_id or _ACTIVE_ID.get()
    if not resolved or _find_model(_cached(), resolved)[1] is None:
        yield
        return
    token = _ACTIVE_ID.set(resolved)
    try:
        yield
    finally:
        try:
            _ACTIVE_ID.reset(token)
        except ValueError:
            pass


def _file():
    return paths.providers_file()


def _env_files():
    if paths.is_initialized():
        return paths.env_files()
    return [
        paths.default_project_root() / ".env",
        Path.home() / ".agents" / "env",
    ]
def _new_id(prefix):
    return f"{prefix}{secrets.token_hex(4)}"


def _unique_id(prefix, taken):
    while True:
        candidate = _new_id(prefix)
        if candidate not in taken:
            return candidate


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _as_int(value):
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value, default=True):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off")
    return bool(value)


def _same_host(one, two):
    """True when two base URLs point at the same place (or when either is blank)."""
    if not one or not two:
        return True
    return one.rstrip("/").lower() == two.rstrip("/").lower()


def _clean_headers(value):
    if not isinstance(value, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in value.items()
        if isinstance(k, str) and isinstance(v, (str, int, float, bool))
    }


def _clean_params(value):
    """Extra request params: a flat dict of scalars, never our own dispatch keys."""
    if not isinstance(value, dict):
        return {}
    clean = {}
    for key, item in value.items():
        if not isinstance(key, str) or key in RESERVED_PARAMS:
            continue
        if item is None or isinstance(item, bool | int | float | str):
            clean[key] = item
    return clean


def _clean_model(payload):
    """Whitelist a model payload down to the canonical model fields."""
    clean = {}
    for key in MODEL_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if key in ("name", "model", "reasoning_effort"):
            clean[key] = _text(value)
            if key == "reasoning_effort" and not clean[key]:
                clean[key] = None
        elif key == "temperature":
            clean[key] = _as_float(value)
        elif key in ("context_window", "max_output_tokens"):
            clean[key] = _as_int(value)
        elif key in (
            "supports_tools",
            "supports_reasoning",
            "supports_images",
            "supports_text_tools",
        ):
            clean[key] = _as_bool(value)
        elif key == "extra_params":
            clean[key] = _clean_params(value)
        elif key == "disabled":
            clean[key] = _as_bool(value, default=False)
    return clean


def _read_raw():
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_current(raw):
    """Has this file already been written in the providers-with-models shape?"""
    if not isinstance(raw, dict):
        return False
    providers = raw.get("providers")
    if not isinstance(providers, list):
        return False
    if raw.get("version") == FORMAT_VERSION:
        return True
    return any(
        isinstance(p, dict) and isinstance(p.get("models"), list) for p in providers
    )


def _backup_path():
    return _file().with_name(_file().name + ".bak")


def _backup(raw):
    """Keep the pre-migration file, so nothing is ever discarded silently."""
    try:
        _backup_path().write_text(
            json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def _migrate_flat(raw):
    """Old one-model-per-provider file -> providers with models.

    Each old entry becomes one provider holding one model, so every id - and
    therefore every keyring entry - survives untouched. Entries that shared an
    id, the collision the old format allowed, end up as sibling models of one
    provider instead of overwriting each other.
    """
    groups, by_id = [], {}
    for entry in raw.get("providers", []):
        if not isinstance(entry, dict):
            continue
        group_id = _text(entry.get("id")) or _text(entry.get("base_url"))
        if not group_id:
            continue
        group = by_id.get(group_id)
        if group is None:
            group = {
                "id": group_id,
                "name": _text(entry.get("name")) or group_id,
                "base_url": _text(entry.get("base_url")),
                "headers": _clean_headers(entry.get("headers")),
                "disabled": _as_bool(entry.get("disabled"), default=False),
                "models": [],
            }
            by_id[group_id] = group
            groups.append(group)
        taken = {m["id"] for g in groups for m in g["models"]} | {
            g["id"] for g in groups
        }
        model = dict(MODEL_DEFAULTS)
        model["id"] = _unique_id(MODEL_PREFIX, taken)
        model.update(_clean_model(entry))
        if not model["name"]:
            model["name"] = model["model"] or group["id"]
        group["models"].append(model)

    active_id = _text(raw.get("active_id"))
    active_model = _text(raw.get("active_model_id"))
    if active_model and active_model not in {
        m["id"] for g in groups for m in g["models"]
    }:
        active_model = ""
    if not active_model:
        group = by_id.get(active_id)
        if group and group["models"]:
            active_model = group["models"][0]["id"]
    return {
        "version": FORMAT_VERSION,
        "active_id": active_model,
        "active_provider_id": _text(raw.get("active_provider_id")) or active_id,
        "providers": groups,
    }


def _clean_group(group):
    clean = {
        "id": _text(group.get("id")) or _new_id(GROUP_PREFIX),
        "name": _text(group.get("name")),
        "base_url": _text(group.get("base_url")),
        "headers": _clean_headers(group.get("headers")),
        "disabled": _as_bool(group.get("disabled"), default=False),
        "models": [],
    }
    seen = set()
    for model in group.get("models", []):
        if not isinstance(model, dict):
            continue
        entry = dict(MODEL_DEFAULTS)
        model_id = _text(model.get("id"))
        if not model_id or model_id in seen:
            model_id = _unique_id(MODEL_PREFIX, seen)
        entry["id"] = model_id
        seen.add(model_id)
        entry.update(_clean_model(model))
        clean["models"].append(entry)
    return clean


def _normalise(raw):
    empty = {
        "version": FORMAT_VERSION,
        "active_id": None,
        "active_provider_id": None,
        "providers": [],
    }
    if not isinstance(raw, dict):
        return empty
    providers = raw.get("providers")
    if not isinstance(providers, list):
        return empty
    if _is_current(raw):
        groups = [_clean_group(p) for p in providers if isinstance(p, dict)]
        return {
            "version": FORMAT_VERSION,
            "active_id": _text(raw.get("active_id"))
            or _text(raw.get("active_model_id")),
            "active_provider_id": _text(raw.get("active_provider_id")),
            "providers": groups,
        }
    return _migrate_flat(raw)


_CACHE = {}
_MISSING = object()


def _cached():
    try:
        stamp = _file().stat().st_mtime_ns
    except OSError:
        stamp = None
    if _CACHE.get("stamp", _MISSING) != stamp:
        _CACHE.clear()
        _CACHE["stamp"] = stamp
        _CACHE["data"] = _normalise(_read_raw())
    return _CACHE["data"]


def _write(data):
    target = _file()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "version": FORMAT_VERSION,
        "active_id": data.get("active_id"),
        "active_provider_id": data.get("active_provider_id"),
        "providers": data.get("providers", []),
    }
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    _CACHE.clear()
    return body


def _save(data):
    """Persist a normalised tree, backing up a not-yet-migrated file first."""
    raw = _read_raw()
    if raw is not None and not _is_current(raw) and not _backup_path().exists():
        _backup(raw)
    return _write(data)


_WRITE_LOCK = threading.RLock()


def _transaction(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with _WRITE_LOCK:
            return function(*args, **kwargs)

    return wrapper


def _find_group(data, group_id):
    for group in data["providers"]:
        if group["id"] == group_id:
            return group
    return None


def _find_model(data, model_id):
    for group in data["providers"]:
        for model in group["models"]:
            if model["id"] == model_id:
                return group, model
    return None, None


def _live(group):
    models = [m for m in group["models"] if not m.get("disabled")] if group else []
    return models or (group["models"] if group else [])


def _resolve(data, target_id):
    """(group, model) for an id that may name a model or a whole provider."""
    if not target_id:
        return None, None
    group, model = _find_model(data, target_id)
    if model is not None:
        return group, model
    group = _find_group(data, target_id)
    if group is None:
        return None, None
    models = _live(group)
    return group, (models[0] if models else None)


def _active(data):
    """(group, model) the engine should use, from the file or the context.

    A disabled model is never served: the active provider falls back to its
    first live model, and if none of those are live the first live model in the
    registry is used rather than silently dropping to the .env provider.
    """
    override = _ACTIVE_ID.get()
    group, model = _find_model(data, override or _text(data.get("active_id")))
    if model is not None and not (model.get("disabled") or group.get("disabled")):
        return group, model
    if group is None:
        group = _find_group(data, _text(data.get("active_provider_id")))
    if group is None or not _live(group):
        for candidate in data["providers"]:
            if _live(candidate):
                group = candidate
                break
    models = _live(group)
    return group, (models[0] if models else None)


def list_groups():
    return copy.deepcopy(_cached()["providers"])


def get_group(group_id):
    """One provider, copied: the cache is shared, callers must not mutate it."""
    return copy.deepcopy(_find_group(_cached(), group_id))


def get_model(model_id) -> ModelPayload | None:
    """One model row, copied: the cache is shared, callers must not mutate it."""
    return copy.deepcopy(_resolve(_cached(), model_id)[1])


def group_of(target_id) -> dict | None:
    return copy.deepcopy(_resolve(_cached(), target_id)[0])


def active_pair():
    return tuple(copy.deepcopy(part) for part in _active(_cached()))


@_transaction
def save_group(group):
    """Create or replace a provider. Never put secrets here."""
    data = _cached()
    group_id = _text(group.get("id")) or _unique_id(
        GROUP_PREFIX, {g["id"] for g in data["providers"]}
    )
    clean = {
        "id": group_id,
        "name": _text(group.get("name")) or "Provider",
        "base_url": _text(group.get("base_url")),
        "headers": _clean_headers(group.get("headers")),
        "disabled": _as_bool(group.get("disabled"), default=False),
        "models": [],
    }
    existing = _find_group(data, group_id)
    if existing is not None:
        existing.clear()
        existing.update(clean)
        target = existing
    else:
        data["providers"].append(clean)
        target = clean
    _save(data)
    return target


@_transaction
def save_model(model, group_id=None) -> ModelPayload:
    """Create or update one model, leaving its siblings and provider alone.

    A payload that names a different provider moves the model to that provider;
    anything else about the provider is left untouched unless it was sent.
    """
    data = _cached()
    model = dict(model or {})
    model_id = _text(model.get("id"))
    taken = {m["id"] for g in data["providers"] for m in g["models"]}
    if not model_id:
        model_id = _unique_id(MODEL_PREFIX, taken)

    group, existing = _find_model(data, model_id)
    if existing is not None:
        target_group, target = group, existing
        wanted = (
            _text(group_id)
            or _text(model.get("provider_id"))
            or _text(model.get("group_id"))
        )
        destination = _find_group(data, wanted) if wanted else None
        if destination is not None and destination is not group:
            target_group["models"] = [
                m for m in target_group["models"] if m["id"] != model_id
            ]
            destination["models"].append(existing)
            target_group = destination
    else:
        explicit = (
            _text(group_id)
            or _text(model.get("provider_id"))
            or _text(model.get("group_id"))
        )
        if not explicit:
            explicit = (
                model_id
                if _find_group(data, model_id) is None
                else _unique_id(GROUP_PREFIX, {g["id"] for g in data["providers"]})
            )
        target_group = _find_group(data, explicit)
        if target_group is None:
            target_group = save_group(
                {
                    "id": explicit,
                    "name": _text(model.get("provider_name"))
                    or _text(model.get("name"))
                    or "Provider",
                    "base_url": _text(model.get("base_url")),
                    "headers": _clean_headers(model.get("headers")),
                    "disabled": _as_bool(model.get("disabled"), default=False),
                }
            )
            data = _cached()
            target_group = _find_group(data, target_group["id"])
        target = None

    wanted_model = _text(model.get("model"))
    if wanted_model:
        self_id = target["id"] if target is not None else model_id
        clash = next(
            (
                m
                for m in target_group["models"]
                if m.get("model") == wanted_model and m["id"] != self_id
            ),
            None,
        )
        if clash is not None:
            raise ValueError(
                f"a model with id '{wanted_model}' already exists in this provider"
            )

    if target is None:
        target = dict(MODEL_DEFAULTS)
        target["id"] = model_id
        target_group["models"].append(target)
    target.update(_clean_model(model))
    if not target["name"]:
        target["name"] = target["model"] or model_id

    patch = {}
    if "provider_name" in model:
        patch["name"] = _text(model["provider_name"]) or "Provider"
    if "base_url" in model:
        patch["base_url"] = _text(model["base_url"])
    if "headers" in model:
        patch["headers"] = _clean_headers(model["headers"])
    if patch:
        target_group.update(patch)

    _save(data)
    return target


@_transaction
def save_provider(entry):
    """Compat entry point: a flat payload carrying provider and model fields."""
    entry = dict(entry or {})
    group_id = _text(entry.get("provider_id")) or _text(entry.get("group_id")) or None
    return save_model(entry, group_id)


def _forget_key(group_id):
    try:
        import keyring

        keyring.delete_password(SERVICE, group_id)
    except Exception:  # noqa: BLE001
        pass


@_transaction
def delete_group(group_id):
    data = _cached()
    group = _find_group(data, group_id)
    if group is None:
        return False
    data["providers"] = [g for g in data["providers"] if g["id"] != group_id]
    if data.get("active_provider_id") == group_id:
        data["active_provider_id"] = (
            data["providers"][0]["id"] if data["providers"] else None
        )
    if data.get("active_id") and _find_model(data, data["active_id"])[1] is None:
        data["active_id"] = None
    body = _save(data)
    _forget_key(group_id)
    return _find_group(body, group_id) is None


@_transaction
def delete_model(model_id) -> bool:
    """Drop one model. The provider lives on while any other model does."""
    data = _cached()
    group, model = _find_model(data, model_id)
    if model is None:
        group = _find_group(data, model_id)
        return delete_group(group["id"]) if group else False
    group["models"] = [m for m in group["models"] if m["id"] != model_id]
    emptied = not group["models"]
    if emptied:
        data["providers"] = [g for g in data["providers"] if g["id"] != group["id"]]
        if data.get("active_provider_id") == group["id"]:
            data["active_provider_id"] = None
    if data.get("active_id") == model_id:
        data["active_id"] = None
    body = _save(data)
    if emptied:
        _forget_key(group["id"])
    return _find_model(body, model_id)[1] is None


@_transaction
def delete_provider(target_id):
    """Accept either kind of id: a model, or a whole provider."""
    data = _cached()
    if _find_model(data, target_id)[1] is not None:
        return delete_model(target_id)
    return delete_group(target_id)


@_transaction
def set_active(target_id):
    """Select a model by its own id, or a provider by its first live model."""
    data = _cached()
    group, model = _resolve(data, target_id)
    if model is None:
        return False
    data["active_id"] = model["id"]
    data["active_provider_id"] = group["id"]
    _save(data)
    return True


def get_active_model_id():
    override = _ACTIVE_ID.get()
    if override:
        return override
    model = _active(_cached())[1]
    return model["id"] if model else None


def get_active_provider_id():
    group = _active(_cached())[0]
    return group["id"] if group else None


@_transaction
def set_effort(target_id, effort):
    """Set reasoning effort on one model; a provider id targets its live model."""
    data = _cached()
    group, model = _resolve(data, target_id)
    if model is None:
        return False
    model["reasoning_effort"] = _text(effort) or None
    _save(data)
    return True


def _group_id_for(target_id):
    group = _resolve(_cached(), target_id)[0]
    return group["id"] if group else None


def set_key(target_id, secret):
    """Store a secret against the provider that owns this model."""
    group_id = _group_id_for(target_id)
    if not group_id:
        return False
    import keyring

    keyring.set_password(SERVICE, group_id, secret)
    return True


def _key_for_group(group_id):
    try:
        import keyring

        return keyring.get_password(SERVICE, group_id)
    except Exception:  # noqa: BLE001
        return None


def has_key(target_id):
    group_id = _group_id_for(target_id)
    return bool(group_id) and _key_for_group(group_id) is not None


def mask(secret):
    if not secret:
        return "(missing)"
    return secret[:3] + "..." + secret[-3:] if len(secret) > 8 else "***"


def _legacy_env():
    values = {}
    for path in _env_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return values


def _flat(group, model):
    """Project one stored model into the ModelRow the protocol expects."""
    if not (group and model):
        return None
    row: ModelRow = {
        "id": model["id"],
        "name": model.get("name") or model.get("model") or "",
        "base_url": group.get("base_url", ""),
        "model": model.get("model", ""),
        "headers": dict(group.get("headers") or {}),
        "disabled": bool(group.get("disabled") or model.get("disabled")),
        "reasoning_effort": model.get("reasoning_effort"),
        "temperature": model.get("temperature"),
        "context_window": model.get("context_window"),
        "max_output_tokens": model.get("max_output_tokens"),
        "supports_tools": bool(model.get("supports_tools", True)),
        "supports_reasoning": bool(model.get("supports_reasoning", True)),
        "supports_images": bool(model.get("supports_images", True)),
        "supports_text_tools": bool(model.get("supports_text_tools", False)),
        "extra_params": dict(model.get("extra_params") or {}),
        "provider_id": group["id"],
        "provider_name": group.get("name") or group["id"],
    }
    return row


def list_models(group_id=None) -> list[ModelRow]:
    """Every model configuration, optionally within a single provider."""
    out = []
    for group in _cached()["providers"]:
        if group_id is not None and group["id"] != group_id:
            continue
        for model in group["models"]:
            flat = _flat(group, model)
            if flat:
                out.append(flat)
    return out


def get_provider(target_id) -> ModelRow | None:
    group, model = _resolve(_cached(), target_id)
    return _flat(group, model)


def active_model_entry() -> ModelRow | None:
    return _flat(*active_pair())


def active_model_params():
    """Extra chat-call params for the active model, only where it supports them."""
    group, model = active_pair()
    params = {}
    if not (group and model):
        return params
    effort = model.get("reasoning_effort")
    override = _EFFORT.get()
    if override is not None and override != "default":
        effort = override
    if effort and model.get("supports_reasoning", True):
        params["reasoning_effort"] = effort
    if model.get("temperature") is not None:
        params["temperature"] = float(model["temperature"])
    if model.get("max_output_tokens"):
        params["max_tokens"] = int(model["max_output_tokens"])
    for key, value in (model.get("extra_params") or {}).items():
        params.setdefault(key, value)
    return params


def active_model_capabilities():
    """What the active model can do, so callers never assume too much."""
    group, model = active_pair()
    if not (group and model):
        return {
            "supports_tools": True,
            "supports_reasoning": True,
            "supports_images": True,
            "supports_text_tools": False,
            "context_window": None,
            "max_output_tokens": None,
            "model": None,
            "name": None,
            "provider_id": None,
            "provider_name": None,
        }
    return {
        "supports_tools": bool(model.get("supports_tools", True)),
        "supports_reasoning": bool(model.get("supports_reasoning", True)),
        "supports_images": bool(model.get("supports_images", True)),
        "supports_text_tools": bool(model.get("supports_text_tools", False)),
        "context_window": model.get("context_window"),
        "max_output_tokens": model.get("max_output_tokens"),
        "model": model.get("model"),
        "name": model.get("name") or model.get("model"),
        "provider_id": group["id"],
        "provider_name": group.get("name") or group["id"],
    }


def active_context_window():
    from . import config

    window = active_model_capabilities()["context_window"]
    return int(window) if window else config.context_window()


def _credential_state(group):
    """(source, secret_or_None) for one provider group."""
    secret = _key_for_group(group["id"])
    if secret:
        return "keyring", secret
    legacy = _legacy_env()

    if legacy.get("API_KEY") and _same_host(
        legacy.get("BASE_URL"), group.get("base_url")
    ):
        return "plaintext-env", legacy["API_KEY"]
    return "missing", None


def _credential_for(group):
    """(base_url, api_key, source) for a provider. Never returns the key twice."""
    if group is None:
        from . import config

        return config.base_url(), config.api_key(), "legacy-env"
    source, secret = _credential_state(group)
    return group.get("base_url", ""), secret, source


def active_config() -> tuple[str | None, str | None, str, str]:
    """(base_url, api_key, model, source). Source names the credential origin."""
    group, model = active_pair()
    if not (group and model):
        from . import config

        return config.base_url(), config.api_key(), config.model(), "legacy-env"
    base_url, api_key, source = _credential_for(group)
    return base_url, api_key, model.get("model", ""), source


def active_config_for(target_id) -> tuple[str | None, str | None, str, str]:
    """(base_url, api_key, model, source) for one model or provider id."""
    group, model = _resolve(_cached(), target_id)
    if not (group and model):
        return active_config()
    base_url, api_key, source = _credential_for(group)
    if group.get("disabled") or model.get("disabled"):
        return base_url, api_key, model.get("model", ""), "disabled"
    return base_url, api_key, model.get("model", ""), source


def provider_status(target_id):
    """renderer-safe summary: masked key state, never the secret."""
    data = _cached()

    group, model = _find_model(data, target_id)
    if model is None:
        group, model = _resolve(data, target_id)
    if not (group and model):
        return None
    flat = _flat(group, model)
    state, _ = _credential_state(group)
    flat.update({"key": state, "active": model["id"] == get_active_model_id()})
    return flat


def _migration_marker():
    return _backup_path().with_name(_backup_path().name + ".reported")


def migration_report():
    """What the last format migration did, for the Settings UI to show.

    The counts describe the migration itself - read from the backup, not from
    the live registry, which keeps growing as the user adds models. The note
    is shown once: the backup file stays on disk forever, so without a marker
    the banner would reappear on every launch.
    """
    backup = _backup_path()
    if not backup.exists() or _migration_marker().exists():
        return []
    try:
        raw = json.loads(backup.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [
            "an earlier providers.json could not be read; it is kept as providers.json.bak"
        ]
    entries = raw.get("providers") if isinstance(raw.get("providers"), list) else []
    if not entries:
        return []

    group_ids = {
        (_text(entry.get("id")) or _text(entry.get("base_url")))
        for entry in entries
        if isinstance(entry, dict)
    }
    group_ids.discard("")
    count = len(entries)
    try:
        _migration_marker().write_text("shown\n", encoding="utf-8")
    except OSError:
        pass
    return [
        f"moved {count} legacy entr{'y' if count == 1 else 'ies'} into "
        f"{count} model(s) under {len(group_ids)} provider(s); "
        "the original file is kept as providers.json.bak"
    ]


def _import_target(base_url, model=""):
    """A provider for an imported entry: reuse a group, else create one."""
    data = _cached()
    for group in data["providers"]:
        if group.get("base_url") == base_url and group["models"]:
            return group
    group_id = base_url
    suffix = 2
    while _find_group(data, group_id) is not None:
        group_id = f"{base_url}#{suffix}"
        suffix += 1
    new_model_id = _unique_id(
        MODEL_PREFIX, {m["id"] for g in data["providers"] for m in g["models"]}
    )
    save_model(
        {
            "id": new_model_id,
            "provider_id": group_id,
            "provider_name": base_url,
            "name": model or base_url,
            "model": model,
            "base_url": base_url,
        }
    )
    return _find_group(_cached(), group_id)


@_transaction
def migrate_from_settings():
    """One-time import of settings.json/.env keys into keyring. Returns count."""
    try:
        settings = json.loads(
            (paths.project_root() / "settings.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        settings = {}
    entries = settings.get("providers") if isinstance(settings, dict) else None
    imported = 0
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        key = entry.get("api_key") or ""
        auth = entry.get("headers") or {}
        if not key and isinstance(auth, dict):
            bearer = auth.get("Authorization") or ""
            if isinstance(bearer, str) and bearer.startswith("Bearer "):
                key = bearer[len("Bearer ") :]
        base_url = _text(entry.get("base_url"))
        if not (base_url and key):
            continue

        group = _import_target(base_url, _text(entry.get("model")))
        if group and not _key_for_group(group["id"]):
            set_key(group["id"], key)
            imported += 1
    legacy = _legacy_env()
    env_base = legacy.get("BASE_URL")
    if env_base and not list_models():
        save_model(
            {
                "id": env_base,
                "name": legacy.get("MODEL", env_base),
                "base_url": env_base,
                "model": legacy.get("MODEL", ""),
            }
        )
    if get_active_model_id() is None and list_models():
        set_active(list_models()[0]["id"])
    return imported


PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "harness_selftest",
        "description": "Return the probe value. Always call this tool when asked.",
        "parameters": {
            "type": "object",
            "properties": {
                "probe": {"type": "string", "description": "The word harness"},
            },
            "required": ["probe"],
        },
    },
}


def _tool_probe(client, model):
    """(ok, note) for one tool-calling probe. Never raises."""
    try:
        message = (
            client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a test. Use the tools you are given.",
                    },
                    {
                        "role": "user",
                        "content": "Call the harness_selftest tool with probe='harness'.",
                    },
                ],
                tools=[PROBE_TOOL],
            )
            .choices[0]
            .message
        )
    except Exception as failure:  # noqa: BLE001
        from . import errors as errors_mod

        return False, errors_mod.explain(failure, model=model)
    calls = getattr(message, "tool_calls", None) or []
    names = [getattr(getattr(call, "function", None), "name", "") for call in calls]
    if "harness_selftest" in names:
        return True, "tool call ok"
    return False, "no tool call returned"


def test_provider(target_id, timeout=20):
    """Run the harness-ok probe, then a tool-calling probe. Never exposes the key."""
    import time

    from openai import OpenAI

    from . import errors as errors_mod

    base_url, api_key, model, source = active_config_for(target_id)
    if source == "disabled":
        return False, "this model is disabled"
    if not api_key:
        return False, f"no key stored ({source})"
    started = time.monotonic()
    try:
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        message = (
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a test."},
                    {"role": "user", "content": "Reply with exactly: harness-ok"},
                ],
            )
            .choices[0]
            .message
        )
        elapsed = time.monotonic() - started
        content = message.content or ""
        if "harness-ok" not in content:
            return False, f"{model}: unexpected reply in {elapsed:.1f}s"
        detail = f"{model}: ok in {elapsed:.1f}s via {source}"

        group, entry = _resolve(_cached(), target_id)
        if entry is None or entry.get("supports_tools", True):
            tools_ok, note = _tool_probe(client, model)
            detail += f" · {note}"
            if tools_ok:
                detail += " (native tool calling works)"
        return True, detail
    except Exception as failure:  # noqa: BLE001
        detail = errors_mod.explain(failure, model=model)
        return False, detail


def _first_int(*values):
    """The first usable positive integer among the candidates, or None."""
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def _normalize_models_entry(raw):
    """Map one /models entry onto our model fields. Only derivable keys land
    here; a bare OpenAI-style entry (id/created/owned_by) yields nothing."""
    if not isinstance(raw, dict):
        return {}
    info = {}
    top = raw.get("top_provider")
    top = top if isinstance(top, dict) else {}
    window = _first_int(
        raw.get("context_length"),
        raw.get("context_window"),
        raw.get("max_model_len"),
        top.get("context_length"),
    )
    if window:
        info["context_window"] = window
    max_out = _first_int(
        top.get("max_completion_tokens"),
        raw.get("max_completion_tokens"),
        raw.get("max_output_tokens"),
    )
    if max_out:
        info["max_output_tokens"] = max_out
    name = _text(raw.get("name"))
    if name and name != raw.get("id"):
        info["name"] = name
    arch = raw.get("architecture")
    arch = arch if isinstance(arch, dict) else {}
    modalities = raw.get("input_modalities")
    if modalities is None:
        modalities = arch.get("input_modalities")
    if modalities is None:
        modalities = raw.get("modalities")
    if isinstance(modalities, (list, tuple)):
        has_images = any(str(m).lower() == "image" for m in modalities)
    elif isinstance(modalities, str):
        has_images = "image" in modalities.lower()
    else:
        has_images = None
    if has_images is False:
        info["supports_images"] = False
    params = raw.get("supported_parameters")
    if isinstance(params, (list, tuple)) and params:
        lowered = {str(p).lower() for p in params}
        if not lowered & {"tools", "tool_choice"}:
            info["supports_tools"] = False
        if not lowered & {"reasoning", "include_reasoning", "reasoning_effort"}:
            info["supports_reasoning"] = False
    return info


def _match_models_entry(entries, model_id):
    """The /models entry for our model id: exact, then case-insensitive."""
    if not _text(model_id):
        return None
    for raw in entries:
        if isinstance(raw, dict) and raw.get("id") == model_id:
            return raw
    lowered = model_id.lower()
    for raw in entries:
        if isinstance(raw, dict) and str(raw.get("id", "")).lower() == lowered:
            return raw
    return None


def apply_model_info(payload, info, include_bools=False):
    """Fill only the fields the payload left empty, in place. Capability
    switches move only when asked, and only ever to false. Returns the
    human-readable labels of what changed."""
    if not isinstance(info, dict) or not info:
        return []
    applied = []
    if info.get("context_window") and payload.get("context_window") is None:
        payload["context_window"] = int(info["context_window"])
        applied.append(f"context window {info['context_window']}")
    if info.get("max_output_tokens") and payload.get("max_output_tokens") is None:
        payload["max_output_tokens"] = int(info["max_output_tokens"])
        applied.append(f"max output {info['max_output_tokens']}")
    name = _text(info.get("name"))
    if name and not _text(payload.get("name")):
        payload["name"] = name
        applied.append(f'display name "{name}"')
    if include_bools:
        for key, label in (
            ("supports_images", "images off"),
            ("supports_tools", "tool calling off"),
            ("supports_reasoning", "reasoning off"),
        ):
            if info.get(key) is False and payload.get(key, True):
                payload[key] = False
                applied.append(label)
    return applied


def _models_entries(base_url, api_key, timeout):
    """The raw /models data list. Raises on transport and auth errors."""
    from openai import OpenAI

    client = OpenAI(
        base_url=base_url, api_key=api_key or "missing-key", timeout=timeout
    )
    return [item.model_dump() for item in client.models.list()]


def fetch_model_config(model, provider_id=None, base_url=None, key=None, timeout=6):
    """{found, info, note} for the model-form prefill op. Never raises."""
    resolved_base = _text(base_url)
    resolved_key = _text(key)
    if provider_id:
        group = _find_group(_cached(), provider_id)
        if group is not None:
            if not resolved_base:
                resolved_base = _text(group.get("base_url"))
            if not resolved_key:
                source, secret = _credential_state(group)
                resolved_key = secret or ""
    if not _text(model):
        return {"found": False, "info": None, "note": "no model id to look up"}
    if not resolved_base:
        return {"found": False, "info": None, "note": "no base url to query"}
    try:
        entries = _models_entries(resolved_base, resolved_key, timeout)
    except Exception as failure:  # noqa: BLE001
        from . import errors as errors_mod

        return {
            "found": False,
            "info": None,
            "note": errors_mod.explain(failure, model=model),
        }
    raw = _match_models_entry(entries, model)
    if raw is None:
        return {
            "found": False,
            "info": None,
            "note": f"{model} is not in the /models list ({len(entries)} entries)",
        }
    info = _normalize_models_entry(raw)
    if not info:
        return {
            "found": True,
            "info": None,
            "note": f"{model} is listed by /models but publishes no settings to fill",
        }
    return {"found": True, "info": info, "note": ""}


@_transaction
def _apply_model_info_saved(target_id, info):
    data = _cached()
    group, stored = _resolve(data, target_id)
    if stored is None:
        return []
    applied = apply_model_info(stored, info)
    if applied:
        _save(data)
    return applied


def autoconfigure_from_models(target_id, timeout=6):
    """Save-time backstop: fill the stored model's empty numeric fields from
    its /models endpoint. Capability switches are never touched here, so a
    deliberate override survives every edit-save. Never raises."""
    base_url, api_key, model, source = active_config_for(target_id)
    if source == "disabled" or not model:
        return {"found": False, "applied": [], "note": "model unavailable for lookup"}
    if not base_url:
        return {"found": False, "applied": [], "note": "no base url to query"}
    try:
        entries = _models_entries(base_url, api_key, timeout)
    except Exception as failure:  # noqa: BLE001
        from . import errors as errors_mod

        return {
            "found": False,
            "applied": [],
            "note": errors_mod.explain(failure, model=model),
        }
    raw = _match_models_entry(entries, model)
    if raw is None:
        return {
            "found": False,
            "applied": [],
            "note": f"no /models metadata for {model}",
        }
    info = _normalize_models_entry(raw)
    if not info:
        return {
            "found": True,
            "applied": [],
            "note": f"{model} is listed by /models but publishes no settings to fill",
        }
    applied = _apply_model_info_saved(target_id, info)
    return {"found": True, "applied": applied, "note": ""}
