"""Configuration of SQLAlchemy database, copied from Pyramid Cookiecutter."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import attr
import zope.sqlalchemy
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, configure_mappers, sessionmaker

# import or define all models here to ensure they are attached to the
# Base.metadata prior to any initialization routines
from .collector import CollectorStat, NodeError  # noqa
from .link import Link  # noqa
from .meta import Base  # noqa
from .node import Node  # noqa

# run configure_mappers after defining all of the models to ensure
# all relationships can be setup
configure_mappers()


def get_engine(settings):
    return create_engine(**attr.asdict(settings))


def get_session_factory(engine) -> sessionmaker:
    factory = sessionmaker(engine)
    return factory


@contextlib.contextmanager
def session_scope(factory: sessionmaker) -> Iterator[Session]:
    """Yields a session wrapped in a context manager."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_tm_session(session_factory, transaction_manager, request=None):
    """Get a ``sqlalchemy.orm.Session`` instance backed by a transaction.

    This function will hook the session to the transaction manager which
    will take care of committing any changes.

    - When using pyramid_tm it will automatically be committed or aborted
      depending on whether an exception is raised.

    - When using scripts you should wrap the session in a manager yourself.
      For example:

      .. code-block:: python

          import transaction

          engine = get_engine(settings)
          session_factory = get_session_factory(engine)
          with transaction.manager:
              dbsession = get_tm_session(session_factory, transaction.manager)

    This function may be invoked with a ``request`` kwarg, such as when invoked
    by the reified ``.dbsession`` Pyramid request attribute which is configured
    via the ``includeme`` function below. The default value, for backwards
    compatibility, is ``None``.

    The ``request`` kwarg is used to populate the ``sqlalchemy.orm.Session``'s
    "info" dict.  The "info" dict is the official namespace for developers to
    stash session-specific information.  For more information, please see the
    SQLAlchemy docs:
    https://docs.sqlalchemy.org/en/stable/orm/session_api.html#sqlalchemy.orm.session.Session.params.info

    By placing the active ``request`` in the "info" dict, developers will be
    able to access the active Pyramid request from an instance of an SQLAlchemy
    object in one of two ways:

    - Modern SQLAlchemy. This uses the "Runtime Inspection API":

      .. code-block:: python

          from sqlalchemy import inspect as sa_inspect

          dbsession = sa_inspect(dbObject).session
          request = dbsession.info["request"]
    """
    dbsession = session_factory(info={"request": request})
    zope.sqlalchemy.register(dbsession, transaction_manager=transaction_manager)
    return dbsession


def includeme(config):
    """Initialize the models for a Pyramid app."""
    settings = config.get_settings()
    settings["tm.manager_hook"] = "pyramid_tm.explicit_manager"

    config.include("pyramid_tm")

    # use pyramid_retry to retry a request when transient exceptions occur
    config.include("pyramid_retry")

    # hook to share the dbengine fixture in testing
    dbengine = settings.get("dbengine")
    if not dbengine:
        dbengine = get_engine(settings["app_config"].db)

    session_factory = get_session_factory(dbengine)
    config.registry["dbsession_factory"] = session_factory

    # make request.dbsession available for use in Pyramid
    def dbsession_factory(request):
        # hook to share the dbsession fixture in testing
        dbsession = request.environ.get("app.dbsession")
        if dbsession is None:
            # request.tm is the transaction manager used by pyramid_tm
            dbsession = get_tm_session(session_factory, request.tm, request=request)
        return dbsession

    config.add_request_method(dbsession_factory, "dbsession", reify=True)
