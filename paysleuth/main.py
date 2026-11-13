"""The ASGI app for uvicorn:  uvicorn paysleuth.main:app"""

import os

from .api import create_app
from .notify import sender_from_env

DSN = os.environ.get("PAYSLEUTH_DATABASE_URL", "postgresql://paysleuth:paysleuth@localhost:5432/paysleuth")

app = create_app(DSN, sender_from_env())
