from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_CORS_ALLOWED_ORIGINS = (
    "http://localhost:3118",
    "http://127.0.0.1:3118",
    "http://tauri.localhost",
    "https://tauri.localhost",
)


class CorsSettings(BaseSettings):
    """CORS is loaded separately so app creation does not require service credentials."""

    cors_allowed_origins: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def allowed_origins(self) -> list[str]:
        configured = (origin.strip().rstrip("/") for origin in self.cors_allowed_origins.split(","))
        return list(dict.fromkeys((*DEFAULT_CORS_ALLOWED_ORIGINS, *(origin for origin in configured if origin))))


class Settings(BaseSettings):
    r2_endpoint_url: str
    r2_bucket_name: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_region: str = "auto"
    google_oauth_client_id: str = ""
    google_oauth_client_ids: str = ""
    cloudflare_account_id: str
    cloudflare_d1_database_id: str
    cloudflare_d1_api_token: str
    librarian_webhook_secret: str = ""
    require_raw_source: bool = False
    librarian_model: str = "google/gemini-3.1-flash-lite"
    librarian_thinking_level: str = "minimal"
    librarian_provider_order: str = "google,vertex"
    librarian_fallback_model: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def google_oauth_audiences(self) -> list[str]:
        configured = (
            audience.strip()
            for audience in self.google_oauth_client_ids.split(",")
        )
        audiences = [audience for audience in configured if audience]
        if self.google_oauth_client_id.strip():
            audiences.append(self.google_oauth_client_id.strip())
        return list(dict.fromkeys(audiences))
