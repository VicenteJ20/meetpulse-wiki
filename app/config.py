from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    r2_endpoint_url: str
    r2_bucket_name: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_region: str = "auto"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
