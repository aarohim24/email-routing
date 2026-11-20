from .config import Settings
from .api import App
from .logging_config import configure_logging

configure_logging()

application = App(Settings.load())
