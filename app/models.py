"""Imports every ORM model so `Base.metadata` knows all tables.

Alembic (`migrations/env.py`) and the test setup import this file. When you add a
module with models, add its import here.
"""

from app.core import audit, idempotency  # noqa: F401
from app.modules.booking import models as booking_models  # noqa: F401
from app.modules.catalog import models as catalog_models  # noqa: F401
from app.modules.haulage import models as haulage_models  # noqa: F401
from app.modules.identity import models as identity_models  # noqa: F401
from app.modules.insurance import models as insurance_models  # noqa: F401
from app.modules.messaging import models as messaging_models  # noqa: F401
from app.modules.notifications import models as notifications_models  # noqa: F401
from app.modules.orders import models as orders_models  # noqa: F401
from app.modules.payments import models as payments_models  # noqa: F401
from app.modules.reviews import models as reviews_models  # noqa: F401
