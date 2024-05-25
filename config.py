import os

from dotenv import load_dotenv

load_dotenv()

CONTROLLERS_PATH = "bots/conf/controllers"
CONTROLLERS_MODULE = "bots.controllers"
BROKER_HOST = os.getenv("BROKER_HOST", "localhost")
BROKER_PORT = int(os.getenv("BROKER_PORT", 1883))
BROKER_USERNAME = os.getenv("BROKER_USERNAME", "admin")
BROKER_PASSWORD = os.getenv("BROKER_PASSWORD", "password")