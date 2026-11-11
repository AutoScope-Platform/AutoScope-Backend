"""Imports every ORM model so `Base.metadata` knows all tables.

Alembic (`migrations/env.py`) and the test setup import this file. When you add a
module with models, add its import here.
"""

from app.core import audit, idempotency  # noqa: F401
from app.modules.identity import models as identity_models  # noqa: F401
