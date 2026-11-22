import os

TINKER_BASE_URL = "https://tinker.thinkingmachines.dev/services/tinker-prod/oai/api/v1"
KEYSHIELD_BASE_URL = "https://keyshield-production.up.railway.app"
# Path from KeyShield's /vproxy/<upstream> to each upstream's OpenAI-compatible root.
KEYSHIELD_UPSTREAM_PATHS = {
    "tinker": "",
    "openai": "/v1",
    "openrouter": "/api/v1",
    "groq": "/openai/v1",
    "mistral": "/v1",
}


def keyshield_base_url(model):
    upstream = model.get("keyshield_upstream", "tinker")
    path = model.get("keyshield_path", KEYSHIELD_UPSTREAM_PATHS.get(upstream))
    if path is None:
        raise ValueError(f"Set keyshield_path for KeyShield upstream: {upstream}")
    base = model.get("keyshield_base") or os.environ.get("KS_BASE") or KEYSHIELD_BASE_URL
    return f"{base.rstrip('/')}/vproxy/{upstream}{path}"


def model_config(model):
    defaults = {"api_key_env": "INKLING_API_KEY", "tool_mode": "native"}
    provider = model.get("provider", "openai")
    if provider == "tinker":
        defaults.update(base_url=TINKER_BASE_URL, api_key_env="TINKER_API_KEY")
    elif provider == "keyshield":
        base_url = keyshield_base_url(model)
        if model.get("base_url", base_url) != base_url:
            raise ValueError("Use keyshield_base, not base_url, with provider keyshield")
        defaults.update(base_url=base_url, api_key_env="KS_TOKEN")
    elif provider != "openai":
        raise ValueError(f"Unknown provider: {provider}")
    result = {**defaults, **model}
    if result["tool_mode"] not in {"native", "json"}:
        raise ValueError("tool_mode must be native or json")
    return result
