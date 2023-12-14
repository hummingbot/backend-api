from typing import Optional

from pydantic import BaseModel


class HummingbotInstanceConfig(BaseModel):
    instance_name: str
    credentials_profile: str
    image: str = "hummingbot/hummingbot:latest"
    autostart_script: Optional[str] = None
