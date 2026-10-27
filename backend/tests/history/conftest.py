import pytest
from sqlalchemy import text


@pytest.fixture
def second_admin_allowed(db):
    """Lift the one-Administrator index for a test that needs two admins.

    The schema allows a single Administrator, but the sole admin can be
    replaced by another account, and one admin's draft history must never
    reach the next. These tests model both accounts side by side.
    """
    db.session.execute(text("DROP INDEX IF EXISTS uq_single_admin"))
    db.session.commit()
