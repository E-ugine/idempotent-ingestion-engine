from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/ingestion_engine"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="")


settings = Settings()
