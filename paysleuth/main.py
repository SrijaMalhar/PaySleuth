"""The ASGI app for uvicorn:  uvicorn paysleuth.main:app"""

import os

from .api import create_app
from .logging_setup import configure
from .notify import sender_from_env
from .ratelimit import DEFAULT_BURST, DEFAULT_RATE

DSN = os.environ.get("PAYSLEUTH_DATABASE_URL", "postgresql://paysleuth:paysleuth@localhost:5432/paysleuth")

configure(os.environ.get("PAYSLEUTH_LOG_LEVEL", "INFO"),
          fmt=os.environ.get("PAYSLEUTH_LOG_FORMAT", "json"))
app = create_app(DSN, sender_from_env(), pool_size=int(os.environ.get("PAYSLEUTH_POOL_SIZE", "20")),
                 metrics_token=os.environ.get("PAYSLEUTH_METRICS_TOKEN"),
                 rate=int(os.environ.get("PAYSLEUTH_RATE_PER_MINUTE", DEFAULT_RATE)),
                 burst=int(os.environ.get("PAYSLEUTH_RATE_BURST", DEFAULT_BURST)))
