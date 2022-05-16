FROM python:3.10-bullseye

# pgsql tests fail in container without `postgresql`,
# but I don't really want to install that in this container
# (need to figure out another way to test this locally w/ containers - start another service?)
RUN apt-get update && \
    apt-get install -y libpq-dev librrd-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/mesh-info/src

ENV VIRTUAL_ENV=/opt/mesh-info
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

RUN pip --no-cache-dir --disable-pip-version-check install --upgrade pip setuptools wheel

COPY requirements.txt /tmp/requirements.txt
COPY dev-requirements.txt /tmp/dev-requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt -r /tmp/dev-requirements.txt

COPY . .

EXPOSE 8000

# Note: this is command for development
CMD ["gunicorn", "--workers=1",  "--reload", "--bind=0.0.0.0:8000", "meshinfo.web:create_app()"]
