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
    google_oauth_client_id: str
    cloudflare_account_id: str
    cloudflare_d1_database_id: str
    cloudflare_d1_api_token: str

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
