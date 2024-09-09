from __future__ import annotations

import pendulum
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.schema import MetaData
from sqlalchemy.types import TIMESTAMP, TypeDecorator

# Recommended naming convention used by Alembic, as various different database
# providers will autogenerate vastly different names making migrations more
# difficult. See: http://alembic.zzzcomputing.com/en/latest/naming.html
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class PDateTime(TypeDecorator):
    """SQLAlchemy type to wrap `pendulum.datetime` instead of `datetime.datetime`."""

    impl = TIMESTAMP(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            if not isinstance(value, pendulum.DateTime):
                # Pendulum enforces non-naive datetime objects
                raise TypeError(f"Expected pendulum.datetime, not {value!r}")
            value = value.in_tz("UTC")
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            value = pendulum.instance(value)
        return value


class Base(DeclarativeBase):
    metadata = metadata

    type_annotation_map = {
        pendulum.DateTime: PDateTime,
    }
