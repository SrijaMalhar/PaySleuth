"""The ASGI app for uvicorn:  uvicorn paysleuth.main:app"""

import os

from .api import create_app
from .logging_setup import configure
from .notify import sender_from_env

DSN = os.environ.get("PAYSLEUTH_DATABASE_URL", "postgresql://paysleuth:paysleuth@localhost:5432/paysleuth")

configure(os.environ.get("PAYSLEUTH_LOG_LEVEL", "INFO"))
app = create_app(DSN, sender_from_env(), pool_size=int(os.environ.get("PAYSLEUTH_POOL_SIZE", "20")),
                 metrics_token=os.environ.get("PAYSLEUTH_METRICS_TOKEN"))
