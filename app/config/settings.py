from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "clubos"
    DB_USER: str = "clubos_user"
    DB_PASSWORD: str
    COOKIE_SECURE: bool = False
    APP_ENV: Literal["development", "test", "production"] = "development"
    ATTACHMENT_STORAGE_BACKEND: Literal["local"] = "local"
    ATTACHMENT_STORAGE_DIRECTORY: Path = Path(".clubos-attachments")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
