from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    bot_token: str = ""
    database_url: str = "sqlite:///./data/vpnshop.db"
    public_base_url: str = "http://localhost:8000"
    admin_user: str = "admin"
    admin_password: str = "change-me"
    admin_session_secret: str = "change-me"
    lava_api_key: str = ""
    lava_offer_id: str = ""
    lava_webhook_key: str = ""
    xui_base_url: str = ""
    xui_username: str = ""
    xui_password: str = ""
    xui_api_token: str = ""
    xui_inbound_id: int = 1
    happ_subscription_base: str = ""
    reminder_days: str = "3,1"
    timezone: str = "Asia/Novosibirsk"

    @property
    def reminder_offsets(self) -> list[int]:
        return [int(value.strip()) for value in self.reminder_days.split(",") if value.strip()]


settings = Settings()
