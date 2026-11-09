"""Application settings from the environment / .env."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    portainer_user: str = ""
    portainer_password: str = ""
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "servers_manager"
    default_portainer_port: int = 9443
    portainer_verify_tls: bool = False
    main_server_name: str = "Nitarti-15-f375154914"  # reference server for diff/normalize/propagate
    backup_dir: str = "backups"  # where stack compose+env backups get saved

    # local run (python run.py)
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    app_reload: bool = False
    open_browser: bool = True  # open the browser on startup


settings = Settings()
