"""Configuration stored in Postgres: versions, validation, hot reload.

The YAML file is the starting point. Once an admin saves a configuration from the
console, versions live in Postgres (who, when, why) and every gateway applies the
newest one within seconds. Secrets never go in: they stay in environment
variables, referenced as ${NAME}.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

from .config import Config, build_config, effort_format

log = logging.getLogger("meritorder.configstore")

_ENV_REF = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}$")


class ConfigError(Exception):
    pass


def secret_problems(raw: dict) -> list[str]:
    """Literal secrets in the document (they must be ${ENV} references instead)."""
    problems = []
    for name, b in (raw.get("backends") or {}).items() if isinstance(raw.get("backends"), dict) else []:
        value = ((b or {}).get("auth") or {}).get("value")
        if value and not _ENV_REF.match(str(value)):
            problems.append(f"backends.{name}.auth.value holds a literal secret; use value_env or ${{NAME}}")
    for i, k in enumerate(((raw.get("auth") or {}).get("static_keys")) or []):
        if (k or {}).get("key") and not _ENV_REF.match(str(k["key"])):
            problems.append(f"auth.static_keys[{i}].key holds a literal key; use key_env")
    oidc = (raw.get("auth") or {}).get("oidc")
    if isinstance(oidc, dict) and oidc.get("public_client_secret") and not _ENV_REF.match(str(oidc["public_client_secret"])):
        problems.append("auth.oidc.public_client_secret holds a literal secret; use ${MERITORDER_OIDC_CLIENT_SECRET}")
    ch = (raw.get("reporting") or {}).get("clickhouse") if isinstance(raw.get("reporting"), dict) else None
    if isinstance(ch, dict):
        value = (ch.get("auth") or {}).get("value") if isinstance(ch.get("auth"), dict) else None
        if value and not _ENV_REF.match(str(value)):
            problems.append("reporting.clickhouse.auth.value holds a literal password; use value_env or value_file")
        if re.match(r"^https?://[^/]*:[^/]*@", str(ch.get("url") or "")):
            problems.append("reporting.clickhouse.url holds a password; set user, and the password with auth.value_env or value_file")
    servers = (raw.get("mcp") or {}).get("servers") if isinstance(raw.get("mcp"), dict) else None
    for name, srv in (servers.items() if isinstance(servers, dict) else enumerate(servers or [])):
        value = ((srv or {}).get("auth") or {}).get("value") if isinstance((srv or {}).get("auth"), dict) else None
        if value and not _ENV_REF.match(str(value)):
            problems.append(f"mcp.servers.{name}.auth.value holds a literal secret; use value_env (MERITORDER_MCP_<NAME>)")
    problems.extend(_secret_refs(raw, ""))
    return problems


# Where a console-saved configuration may read values from the gateway's environment and disk. An admin chooses both a
# backend's credential source (auth.value_env, value_file) and its base_url, and the backend sends what it reads to that
# URL; ${NAME} is expanded anywhere, and GET /v1/admin/config shows some expanded fields. Without these rules an admin
# could read any variable (the database URL, the SCIM token) or any readable file. The YAML file on disk is the
# operator's and isn't checked. Docs: docs/06-auth-and-spend.md, "Secrets in a saved configuration".

# Provider credentials the shipped configs and docs use. Any backend, or bill source, may read these.
PROVIDER_KEY_ENVS = frozenset({
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "AZURE_OPENAI_API_KEY",
    "AZURE_FOUNDRY_API_KEY", "XAI_API_KEY", "DEEPSEEK_API_KEY", "MISTRAL_API_KEY", "MOONSHOT_API_KEY", "ZAI_API_KEY",
    "MINIMAX_API_KEY", "ANTHROPIC_ADMIN_KEY", "OPENAI_ADMIN_KEY", "AZURE_EXPORT_SAS",
})
BACKEND_ENV_PREFIX = "MERITORDER_BACKEND_"  # any other backend credential: MERITORDER_BACKEND_<NAME>
# The gateway's own secrets, each readable only by the field that uses it (so no backend can send one to its base_url).
_OWN_SECRETS: list[tuple[re.Pattern, frozenset[str], tuple[str, ...]]] = [
    (re.compile(r"^reporting\.clickhouse\.auth\.value(_env|_file)?$"), frozenset({"MERITORDER_CLICKHOUSE_PASSWORD"}), ()),
    (re.compile(r"^compressor\.api_key_env$"), frozenset({"MERITORDER_COMPRESSOR_KEY"}), ()),
    (re.compile(r"^auth\.oidc\.google_groups\.credentials_(env|file)$"), frozenset({"MERITORDER_GOOGLE_GROUPS_CREDENTIALS"}), ()),
    (re.compile(r"^auth\.oidc\.public_client_secret$"), frozenset({"MERITORDER_OIDC_CLIENT_SECRET"}), ()),
    (re.compile(r"^auth\.oidc\.browser_client_secret_env$"), frozenset({"MERITORDER_OIDC_BROWSER_CLIENT_SECRET"}), ()),
    (re.compile(r"^batches\.(key|previous_key)_(env|file)$"), frozenset({"MERITORDER_BATCH_KEY", "MERITORDER_BATCH_KEY_PREVIOUS"}), ()),
    (re.compile(r"^response_store\.(key|old_keys)_(env|file)$"),
     frozenset({"MERITORDER_RESPONSES_KEY", "MERITORDER_RESPONSES_OLD_KEYS"}), ()),
    # An MCP server's credential (mcp.py): MERITORDER_MCP_<NAME>, sent only to that server.
    (re.compile(r"^mcp\.servers(\[\d+\]|\.[^.]+)\.auth\.value(_env|_file)?$"), frozenset(), ("MERITORDER_MCP_",)),
    # A static key's value is only compared with callers' keys, never sent or shown, so any variable may hold it.
    (re.compile(r"^auth\.static_keys\[\d+\]\.key(_env)?$"), frozenset(), ("",)),
]
_PROVIDER_PLACES = re.compile(r"^backends\.[^.]+\.auth\.value(_env|_file)?$|^billing\.sources(\[\d+\]|\.[^.]+)\.auth\.value(_env|_file)?$")
# Variables a saved configuration may reference as ${NAME} outside a secret field: the ones the shipped configs use.
PLAIN_ENVS = frozenset({
    "AWS_PROFILE", "AWS_REGION", "AZURE_FOUNDRY_ENDPOINT", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
    "AZURE_OPENAI_GPT_DEPLOYMENT", "GOOGLE_CLOUD_PROJECT", "LOCAL_VLLM_URL", "MERITORDER_ALLOW_FREE_MODELS",
    "MERITORDER_BEDROCK", "MERITORDER_CLICKHOUSE_DATABASE", "MERITORDER_CLICKHOUSE_URL", "MERITORDER_CLICKHOUSE_USER",
    "MERITORDER_COMPRESSOR_MODEL", "MERITORDER_COMPRESSOR_URL", "MERITORDER_FOUNDRY_CLAUDE", "MERITORDER_FOUNDRY_MISTRAL", "MERITORDER_GOOGLE_GROUPS",
    "MERITORDER_GPU_BUDGET_USD", "MERITORDER_KUBERNETES", "MERITORDER_LOCAL_VLLM", "MERITORDER_MAX_GPU_REPLICAS",
    "MERITORDER_MAX_GPU_USD_PER_HOUR", "MERITORDER_MAX_REQUEST_USD", "MERITORDER_OIDC_BROWSER_CLIENT_ID", "MERITORDER_OIDC_CLIENT_ID",
    "MERITORDER_OIDC_HOSTED_DOMAINS", "MERITORDER_OIDC_ISSUER", "MERITORDER_ORG_BUDGET_USD", "MERITORDER_SKYPILOT",
    "MERITORDER_SKYPILOT_SMALL", "MERITORDER_SOURCE_URL", "MERITORDER_VERTEX", "MERITORDER_VERTEX_GEMINI_NATIVE",
})
# The operator widens these lists in the gateway's environment (comma-separated names; a trailing * makes a prefix).
SECRET_ENVS_VAR = "MERITORDER_CONFIG_SECRET_ENVS"  # more variables any secret field may read
PLAIN_ENVS_VAR = "MERITORDER_CONFIG_ENVS"  # more variables ${NAME} may name outside secret fields
SECRETS_DIR_VAR = "MERITORDER_SECRETS_DIR"  # where *_file fields may read from (default: the Helm chart's mount)
DEFAULT_SECRETS_DIR = "/run/secrets/meritorder"
_ENV_ANY = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
_LITERAL_PLACES = re.compile(r"^backends\.[^.]+\.auth\.value$|^auth\.static_keys\[\d+\]\.key$|^auth\.oidc\.public_client_secret$"
                             r"|^reporting\.clickhouse\.auth\.value$|^billing\.sources(\[\d+\]|\.[^.]+)\.auth\.value$"
                             r"|^mcp\.servers(\[\d+\]|\.[^.]+)\.auth\.value$")
_NOT_SECRET_FILES = frozenset({"prices_file"})


def _operator_list(var: str) -> tuple[frozenset[str], tuple[str, ...]]:
    items = [x.strip() for x in os.environ.get(var, "").split(",") if x.strip()]
    return frozenset(x for x in items if not x.endswith("*")), tuple(x[:-1] for x in items if x.endswith("*") and len(x) > 1)


def _listed(name: str, names: frozenset[str], prefixes: tuple[str, ...]) -> bool:
    return name in names or any(name.startswith(p) for p in prefixes)


def secret_env_allowed(path: str, name: str) -> bool:
    """May the secret field at ``path`` read the variable ``name`` (or the file of that name)?"""
    if _listed(name, *_operator_list(SECRET_ENVS_VAR)):
        return True
    if _PROVIDER_PLACES.match(path):
        return name in PROVIDER_KEY_ENVS or name.startswith(BACKEND_ENV_PREFIX)
    return any(rx.match(path) and _listed(name, names, prefixes) for rx, names, prefixes in _OWN_SECRETS)


def secrets_dir() -> str:
    return os.path.realpath(os.environ.get(SECRETS_DIR_VAR) or DEFAULT_SECRETS_DIR)


def _secret_file_problem(path: str, value: str) -> str | None:
    """A *_file field: only inside the secrets directory (``..`` and symlinks resolved), named for a variable the field may
    read (the Helm chart mounts each secret as a file named for its variable)."""
    where = secrets_dir()
    if ".." in Path(value).parts or not os.path.isabs(value):
        return f"{path} must be an absolute path without '..' inside {where}"
    real = os.path.realpath(value)
    if os.path.commonpath([real, where]) != where:
        return f"{path} reads {value}, outside {where}; a saved configuration reads secret files only from there ({SECRETS_DIR_VAR})"
    if not secret_env_allowed(path, os.path.basename(real)):
        return (f"{path} reads {os.path.basename(real)}, which this field may not read; " + _allowed_hint(path))
    return None


def _allowed_hint(path: str) -> str:
    if _PROVIDER_PLACES.match(path):
        return (f"a backend's key may come from a provider's variable ({', '.join(sorted(PROVIDER_KEY_ENVS))}) or "
                f"{BACKEND_ENV_PREFIX}<NAME>, or one the operator lists in {SECRET_ENVS_VAR}")
    own = next((sorted(names) + [p + "*" for p in prefixes] for rx, names, prefixes in _OWN_SECRETS if rx.match(path)), [])
    return f"it may read {', '.join(own) or 'nothing by default'}, or a variable the operator lists in {SECRET_ENVS_VAR}"


def _secret_refs(value: Any, path: str) -> list[str]:
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _secret_refs(v, f"{path}.{k}" if path else str(k))]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _secret_refs(v, f"{path}[{i}]")]
    if not isinstance(value, str) or not value:
        return []
    field = path.rpartition(".")[2]
    if field.endswith("_env"):
        return [] if secret_env_allowed(path, value) else [f"{path} reads {value}, which this field may not read; " + _allowed_hint(path)]
    if field.endswith("_file") and field not in _NOT_SECRET_FILES:
        problem = _secret_file_problem(path, value)
        return [problem] if problem else []
    names = _ENV_ANY.findall(value)
    if _LITERAL_PLACES.match(path):
        return [f"{path} references ${{{n}}}, which this field may not read; " + _allowed_hint(path)
                for n in names if not secret_env_allowed(path, n)]
    plain = _operator_list(PLAIN_ENVS_VAR)
    return [f"{path} references ${{{n}}}; outside a secret field a saved configuration may reference only the variables the "
            f"shipped config uses, or ones the operator lists in {PLAIN_ENVS_VAR}" for n in names if not (n in PLAIN_ENVS or _listed(n, *plain))]


def prune(value: Any) -> Any:
    """Drop empty values (None, "", {}, []) so blank form fields mean "use the default"."""
    if isinstance(value, dict):
        out = {k: prune(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v is not None and v != "" and v != {} and not (isinstance(v, list) and not v and k != "routes")}
    if isinstance(value, list):
        return [prune(v) for v in value]
    return value


def parse_document(text: str | None = None, document: dict | None = None) -> dict:
    if document is not None:
        raw = document
    else:
        try:
            raw = yaml.safe_load(text or "")
        except yaml.YAMLError as e:
            raise ConfigError(f"not valid YAML: {e}")
    if not isinstance(raw, dict):
        raise ConfigError("the configuration must be a mapping")
    return prune(raw)


def parse_and_validate(text: str | None, config_dir: str, prices: dict | None, document: dict | None = None) -> tuple[dict, Config]:
    raw = parse_document(text, document)
    if not isinstance(raw, dict):
        raise ConfigError("the configuration must be a YAML mapping")
    if "include" in raw:
        raise ConfigError("`include:` isn't supported in stored configurations; paste the included sections in")
    problems = secret_problems(raw)
    if problems:
        raise ConfigError("; ".join(problems))
    try:
        cfg = build_config(raw, config_dir, prices_data=prices, quiet=True)
    except Exception as e:  # pydantic validation errors and friends
        raise ConfigError(str(e))
    return raw, cfg


def summarize(cfg: Config) -> dict[str, Any]:
    return {
        "backends_enabled": sorted(b.name for b in cfg.backends if b.enabled),
        "backends_disabled": {b.name: b.disabled_reason or "disabled in config" for b in cfg.backends if not b.enabled},
        "models": len([m for m in cfg.models if m.kind == "chat"]),
        "embedding_models": len([m for m in cfg.models if m.kind == "embedding"]),
        "unpriced_routes": sorted(f"{m.name}@{r.backend}" for m in cfg.models for r in m.routes if not r.priced),
        "teams": [t.name for t in cfg.teams],
        "effort": {b.name: effort_format(b) for b in cfg.backends if b.enabled},
    }


def to_yaml(raw: dict) -> str:
    return yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, width=120)


def token(version: int | None, prices: dict | None, router: int | None = None, success: int | None = None) -> str:
    """Changes whenever the stored configuration, prices, quality scores, the learned router's active model or the success
    predictor's fitted layer change."""
    base = f"{version or 'file'}:{(prices or {}).get('updated', '')}:{((prices or {}).get('quality') or {}).get('updated', '')}"
    return base + (f":r{router}" if router else "") + (f":s{success}" if success else "")


async def _success_version(store) -> int | None:
    from .success import VERSION_SETTING

    return ((await store.get_setting(VERSION_SETTING)) or {}).get("version")


async def current_token(store, prices: dict | None) -> str:
    """The token for what's stored now, with these prices: what a process publishes after changing one of them."""
    row = await store.active_config()
    return token(row["id"] if row else None, prices, await store.router_model_version(), await _success_version(store))


async def load_active(gw, publish: bool = False) -> bool:
    """Apply the newest stored configuration and prices unless already applied, and follow the learned router's active
    model. Returns True if a configuration was applied."""
    row = await gw.store.active_config()
    prices = await gw.store.get_setting("prices")
    router = await gw.store.router_model_version()
    success = await _success_version(gw.store)
    tok = token(row["id"] if row else None, prices, router, success)
    if tok == getattr(gw, "config_token", None):
        return False
    if row is None and prices is None:
        gw.config_token = tok  # nothing stored: the YAML file stays in effect
        await gw.load_learned(router)
        await gw.load_success(success)
        if publish:
            await gw.state.set_config_token(tok)
        return False
    raw = row["body"] if row else gw.file_raw
    if raw is None:
        return False
    cfg = build_config(raw, gw.file_cfg.config_dir, prices_data=prices)
    await gw.apply_config(cfg, row["id"] if row else None)
    await gw.load_learned(router)
    await gw.load_success(success)
    gw.config_token = tok
    if publish:
        await gw.state.set_config_token(tok)
    return True


class ConfigFollower:
    """The active configuration for processes without a gateway (worker, controller), reloaded when it changes.

    Without this they would keep using the YAML file and miss budgets and safeguards edited in the console.
    """

    def __init__(self, store, path=None, cfg=None):
        from .config import default_config_path, load_raw_config

        self.store = store
        if cfg is not None:
            self.file_raw, self.file_cfg = None, cfg
        else:
            p = Path(path) if path else default_config_path()
            self.file_raw = load_raw_config(p)
            self.file_cfg = build_config(self.file_raw, p.parent)
        self.cfg = self.file_cfg
        self.token: str | None = None

    async def current(self):
        row = await self.store.active_config()
        prices = await self.store.get_setting("prices")
        tok = token(row["id"] if row else None, prices)
        if tok == self.token:
            return self.cfg
        raw = row["body"] if row else self.file_raw
        if raw is not None and (row is not None or prices is not None):
            try:
                self.cfg = build_config(raw, self.file_cfg.config_dir, prices_data=prices, quiet=True)
                log.info("using configuration version %s", row["id"] if row else "file (stored prices)")
            except Exception:
                log.exception("stored configuration doesn't load; keeping the previous one")
        self.token = tok
        return self.cfg
