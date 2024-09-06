ARG PYTHON_BASE=3.11-slim-bookworm
# build stage
FROM python:$PYTHON_BASE AS builder

# define whether we are building a production or development image
ARG DEVEL=no

# install RRDtool development libraries
RUN apt-get update && apt-get install --no-install-recommends -y \
    build-essential \
    librrd-dev \
    && rm -rf /var/lib/apt/lists/*

# install PDM
RUN pip --disable-pip-version-check install -U pdm
# disable update check
ENV PDM_CHECK_UPDATE=false
# copy files
COPY pyproject.toml pdm.lock README.rst /mesh-info/
COPY meshinfo/ /mesh-info/meshinfo

# install dependencies and project into the local packages directory
WORKDIR /mesh-info
RUN pdm install --check --prod --no-editable

# run stage
FROM python:$PYTHON_BASE

RUN apt-get update && apt-get install --no-install-recommends -y \
    librrd8 \
    && rm -rf /var/lib/apt/lists/*

# retrieve packages from build stage
COPY --from=builder /mesh-info/.venv/ /mesh-info/.venv
ENV PATH="/mesh-info/.venv/bin:$PATH"

#EXPOSE 8000

# set command/entrypoint, adapt to fit your needs
COPY alembic/ /mesh-info/alembic
COPY alembic.ini /mesh-info/
COPY meshinfo/ /mesh-info/meshinfo
WORKDIR /mesh-info
