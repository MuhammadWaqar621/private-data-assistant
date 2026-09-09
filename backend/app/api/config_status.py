"""
Config-status endpoint.

Exposes which groups of environment-driven configuration are fully
populated. The frontend uses this to show "configuration missing" banners
for features that depend on secrets which aren't provided yet. A group is
only reported as configured (`true`) when every variable it needs is set to
a non-empty value.

Groups:

  - `connections_llm` - everything needed to register a connection and
    chat about it: embeddings (always Azure OpenAI, `AZURE_EM_*`) AND
    whichever chat provider `LLM_PROVIDER` currently selects ("groq", the
    default, needs `GROQ_API_KEY`; "azure" needs `LLM_ENDPOINT` /
    `LLM_ENDPOINT_APIKEY` / `LLM_MODEL_NAME`) - never both providers'
    credentials at once. Same combined-check logic as the sibling
    project's `rag` group, renamed for what it gates here.
  - `encryption` - `ENCRYPTION_KEY` is set AND is a usable Fernet key. A
    connection's password cannot be stored without it, so
    `POST /api/connections` returns 503 until this is true. Checked by
    actually constructing a Fernet, so a truthy-but-malformed key reports
    `false` here instead of 500ing at save time.
  - `smtp` - forgot-password email delivery.

`AZURE_EM_DIMENSIONS` is deliberately NOT part of `connections_llm`: it has
a sensible code default (1536) in
`app/engine/azure_client.get_embedding_dimensions()`, so a deployment that
leaves it blank is still fully configured.
"""

from fastapi import APIRouter

from app.core.config import Settings, get_settings
from app.core.crypto import encryption_configured

router = APIRouter(prefix="/api/config", tags=["config"])


def _all_set(settings: Settings, var_names: list[str]) -> bool:
    """Return True only if every named setting is a non-empty value."""
    for name in var_names:
        value = getattr(settings, name, None)
        if value is None:
            return False
        if isinstance(value, str) and value.strip() == "":
            return False
    return True


def _normalized_llm_provider(settings: Settings) -> str:
    value = (settings.LLM_PROVIDER or "").strip().lower()
    return "azure" if value == "azure" else "groq"


def _connections_llm_configured(settings: Settings) -> bool:
    embeddings_ok = _all_set(
        settings,
        ["AZURE_EM_ENDPOINT", "AZURE_EM_API_KEY", "AZURE_EM_API_VERSION", "AZURE_EM_MODEL"],
    )
    if not embeddings_ok:
        return False
    if _normalized_llm_provider(settings) == "azure":
        return _all_set(settings, ["LLM_ENDPOINT", "LLM_ENDPOINT_APIKEY", "LLM_MODEL_NAME"])
    return _all_set(settings, ["GROQ_API_KEY"])


# Group -> the settings attributes that must ALL be non-empty for the
# group to be considered configured. `connections_llm` and `encryption`
# are handled separately above since neither is a plain "all of these" check.
CONFIG_GROUPS: dict[str, list[str]] = {
    "smtp": [
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_FROM_EMAIL",
    ],
}


@router.get("/status")
def get_config_status() -> dict[str, bool | str]:
    """Return which configuration groups are fully populated, plus which
    chat provider is currently active.

    Example response:
        {"connections_llm": true, "encryption": true, "smtp": false,
         "llm_provider": "groq"}
    """
    settings = get_settings()
    result: dict[str, bool | str] = {
        "connections_llm": _connections_llm_configured(settings),
        "encryption": encryption_configured(settings.ENCRYPTION_KEY or ""),
        **{group: _all_set(settings, var_names) for group, var_names in CONFIG_GROUPS.items()},
        "llm_provider": _normalized_llm_provider(settings),
    }
    return result
