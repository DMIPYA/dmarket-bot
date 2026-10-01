"""Конфигурация из переменных окружения (.env)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str
    telegram_chat_id: str
    dmarket_public_key: str
    dmarket_secret_key: str

    market_name: str = "Dreams & Nightmares Case"
    game_id: str = "a8db"  # CS2; TF2 = "tf2"
    monitor_interval: float = 5.0
    auto_sell: bool = False
    sell_currency: str = "USD"


settings = Settings()
