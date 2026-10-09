"""Provider registry: non-secrets in providers.json, secrets in OS keyring.

Resolution for the engine (active_config):
  1. providers.json active entry -> keyring secret, or plaintext .env fallback
  2. legacy single-provider setup from config (env / ./.env / ~/.agents/env)

The GUI never receives full secrets - only masked forms and key status.
"""

import contextlib
import contextvars
import json
from pathlib import Path

from . import paths

PROVIDERS_FILE = Path.cwd() / "providers.json"
_DEFAULT_FILE = Path(PROVIDERS_FILE)

SERVICE = "harness"

LEGACY_FILES = (Path.cwd() / ".env", Path.home() / ".agents" / "env")

_ACTIVE_ID = contextvars.ContextVar("providers_active_id", default=None)
_EFFORT = contextvars.ContextVar("providers_effort", default=None)


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


def _file():
    if paths.is_initialized() and Path(PROVIDERS_FILE) == _DEFAULT_FILE:
        return paths.providers_file()
    return Path(PROVIDERS_FILE)


def _env_files():
    if paths.is_initialized():
        return paths.env_files()
    return list(LEGACY_FILES)


def _read_providers_file():
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"active_id": None, "providers": []}
    if not isinstance(data.get("providers"), list):
        data["providers"] = []
    return data


def _write_providers_file(data):
    target = _file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def list_providers():
    return _read_providers_file()["providers"]


def get_provider(provider_id):
    for p in list_providers():
        if p.get("id") == provider_id:
            return p
    return None


def save_provider(entry):
    """Create or replace a non-secret provider entry. Never put secrets here."""
    entry = {k: entry.get(k) for k in ("id", "name", "base_url", "model", "headers", "disabled",
                                       "reasoning_effort", "temperature")}
    data = _read_providers_file()
    data["providers"] = [p for p in data["providers"] if p.get("id") != entry["id"]]
    data["providers"].append(entry)
    if not data.get("active_id"):
        data["active_id"] = entry["id"]
    _write_providers_file(data)
    return entry


def delete_provider(provider_id):
    data = _read_providers_file()
    data["providers"] = [p for p in data["providers"] if p.get("id") != provider_id]
    if data.get("active_id") == provider_id:
        data["active_id"] = data["providers"][0]["id"] if data["providers"] else None
    _write_providers_file(data)
    try:
        import keyring
        keyring.delete_password(SERVICE, provider_id)
    except Exception:  # noqa: BLE001 - missing secret is not an error
        pass


def get_active_id():
    override = _ACTIVE_ID.get()
    if override is not None:
        return override
    return _read_providers_file().get("active_id")


def set_active(provider_id):
    if provider_id is not None and get_provider(provider_id) is None:
        return False
    data = _read_providers_file()
    data["active_id"] = provider_id
    _write_providers_file(data)
    return True


def set_key(provider_id, secret):
    import keyring
    keyring.set_password(SERVICE, provider_id, secret)


def has_key(provider_id):
    try:
        import keyring
        return keyring.get_password(SERVICE, provider_id) is not None
    except Exception:  # noqa: BLE001 - no backend means no key
        return False


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


def active_model_params():
    """Extra chat-call params for the active provider: effort and temperature."""
    active_id = get_active_id()
    entry = get_provider(active_id) if active_id else None
    params = {}
    if entry:
        if entry.get("reasoning_effort"):
            params["reasoning_effort"] = entry["reasoning_effort"]
        if entry.get("temperature") is not None:
            try:
                params["temperature"] = float(entry["temperature"])
            except (TypeError, ValueError):
                pass
    override = _EFFORT.get()
    if override is not None and override != "default":
        params["reasoning_effort"] = override
    return params


def set_effort(provider_id, effort):
    data = _read_providers_file()
    for entry in data["providers"]:
        if entry.get("id") == provider_id:
            entry["reasoning_effort"] = effort or None
    _write_providers_file(data)


def active_config():
    """(base_url, api_key, model, source). Source names the credential origin."""
    from . import config

    active_id = get_active_id()
    if active_id:
        entry = get_provider(active_id)
        if entry and not entry.get("disabled"):
            try:
                import keyring
                secret = keyring.get_password(SERVICE, active_id)
            except Exception:  # noqa: BLE001
                secret = None
            if secret:
                return entry["base_url"], secret, entry["model"], "keyring"
            legacy = _legacy_env()
            if legacy.get("API_KEY"):
                return entry["base_url"], legacy["API_KEY"], entry["model"], "plaintext-env"

    return config.BASE_URL, config.API_KEY, config.MODEL, "legacy-env"


def provider_status(provider_id):
    """GUI-safe summary: masked key state, never the secret."""
    entry = get_provider(provider_id)
    if entry is None:
        return None
    keyring_key = has_key(provider_id)
    return {
        "id": entry["id"],
        "name": entry.get("name"),
        "base_url": entry.get("base_url"),
        "model": entry.get("model"),
        "disabled": bool(entry.get("disabled")),
        "active": provider_id == get_active_id(),
        "key": "keyring" if keyring_key else ("plaintext-env" if _legacy_env().get("API_KEY") else "missing"),
    }


def migrate_from_settings():
    """One-time import of settings.json/.env keys into keyring. Returns count."""
    try:
        settings = json.loads((paths.project_root() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    seen = {}
    for entry in settings.get("providers", []):
        key = entry.get("api_key", "")
        auth = (entry.get("headers") or {}).get("Authorization", "")
        if not key and auth.startswith("Bearer "):
            key = auth[len("Bearer "):]
        if entry.get("base_url") and key:
            seen[entry["base_url"]] = (entry, key)
    legacy = _legacy_env()
    imported = 0
    for base_url, (entry, key) in seen.items():
        provider_id = base_url
        if get_provider(provider_id) is None:
            save_provider({
                "id": provider_id,
                "name": entry.get("name", base_url),
                "base_url": base_url,
                "model": entry.get("model", ""),
                "headers": {},
                "disabled": bool(entry.get("disabled", False)),
            })
        if not has_key(provider_id):
            set_key(provider_id, key)
            imported += 1
    if legacy.get("BASE_URL") and get_provider(legacy["BASE_URL"]) is None:
        save_provider({
            "id": legacy["BASE_URL"],
            "name": legacy.get("MODEL", legacy["BASE_URL"]),
            "base_url": legacy["BASE_URL"],
            "model": legacy.get("MODEL", ""),
            "headers": {},
            "disabled": False,
        })
    if get_active_id() is None and list_providers():
        set_active(list_providers()[0]["id"])
    return imported


def test_provider(provider_id, timeout=20):
    """Run the harness-ok probe. Returns (ok, detail). Never exposes the key."""
    import time
    from openai import OpenAI

    from . import config

    entry = get_provider(provider_id)
    if entry is None:
        return False, "unknown provider"
    base_url, api_key, model, source = active_config_for(entry)
    if not api_key:
        return False, f"no key stored ({source})"
    started = time.monotonic()
    try:
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        message = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a test."},
                {"role": "user", "content": "Reply with exactly: harness-ok"},
            ],
        ).choices[0].message
        elapsed = time.monotonic() - started
        content = (message.content or "")
        if "harness-ok" in content:
            return True, f"ok in {elapsed:.1f}s via {source}"
        return False, f"unexpected reply in {elapsed:.1f}s"
    except Exception as failure:  # noqa: BLE001 - the GUI shows this string
        return False, f"{type(failure).__name__}: {failure}"


def active_config_for(entry):
    """Resolve credentials for one provider entry (used by the test probe)."""
    from . import config  # noqa: F401 - keeps resolution rules in one place

    try:
        import keyring
        secret = keyring.get_password(SERVICE, entry["id"])
    except Exception:  # noqa: BLE001
        secret = None
    if secret:
        return entry["base_url"], secret, entry["model"], "keyring"
    legacy = _legacy_env()
    if legacy.get("API_KEY"):
        return entry["base_url"], legacy["API_KEY"], entry["model"], "plaintext-env"
    return entry["base_url"], None, entry["model"], "missing"


def suggest_title(first_text, timeout=15):
    """Name a session in a few words. Returns None on any failure."""
    from openai import OpenAI

    base_url, api_key, model, _ = active_config()
    if not api_key or not (first_text or "").strip():
        return None
    try:
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        reply = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": (
                "Title this coding task in 4 words max. Plain words only, "
                f"no quotes: {first_text[:300]}")}],
        ).choices[0].message.content or ""
        title = " ".join(reply.strip().strip("\"'").split())
        return title[:40] or None
    except Exception:  # noqa: BLE001 - titles are best effort
        return None
