# One image, two roles: the logger polls ecobee, the dashboard serves the page.
# docker-compose.yml picks the role via `command:`.
FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyecobee/ pyecobee/
COPY app/ app/
COPY tools/ tools/

# Ship the license notices with the code: pyecobee/ is Nolan Gilley's MIT-licensed
# python-ecobee-api, and MIT requires his notice in every copy we distribute.
COPY LICENSE LICENSE-python-ecobee-api.txt ./

# Run as a non-root user; the data volume is chowned to this uid in compose.
RUN useradd --create-home --uid 10001 ecobee && chown -R ecobee:ecobee /srv
USER ecobee

EXPOSE 8321
CMD ["python", "app/ecobee_dashboard.py", "--host", "0.0.0.0", "--no-open"]
