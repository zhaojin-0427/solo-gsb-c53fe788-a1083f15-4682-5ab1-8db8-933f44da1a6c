"""运行期配置：全部可由环境变量覆盖（docker-compose 已给出默认值）。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://billing:billing@localhost:5432/billing"
    init_schema: bool = True
    schema_file: str = "db/schema.sql"
    currency_scale: int = 2  # 账单金额按 0.01 固化；计价轨迹保留原始精度


settings = Settings()
