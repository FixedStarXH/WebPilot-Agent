from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WEBPILOT_", env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://webpilot:webpilot@127.0.0.1:5432/webpilot"
    max_tasks: int = 100
    lease_seconds: int = 60


settings = Settings()
