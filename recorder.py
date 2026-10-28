def app_access_token() -> str:
    if _token_cache.get("token") and _token_cache["expires"] > time.time() + 300:  # запас 5 минут
        return _token_cache["token"]
    # ... остальной код без изменений