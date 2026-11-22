TINKER_BASE_URL = "https://tinker.thinkingmachines.dev/services/tinker-prod/oai/api/v1"


def model_config(model):
    defaults = {"api_key_env": "INKLING_API_KEY", "tool_mode": "native"}
    provider = model.get("provider", "openai")
    if provider == "tinker":
        defaults.update(base_url=TINKER_BASE_URL, api_key_env="TINKER_API_KEY")
    elif provider != "openai":
        raise ValueError(f"Unknown provider: {provider}")
    result = {**defaults, **model}
    if result["tool_mode"] not in {"native", "json"}:
        raise ValueError("tool_mode must be native or json")
    return result
