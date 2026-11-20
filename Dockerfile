FROM node:22-slim AS addin-build
WORKDIR /source/addin
COPY addin/package.json addin/package-lock.json ./
RUN npm ci --ignore-scripts
COPY addin/build.mjs ./
COPY addin/src ./src
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATABASE_PATH=/app/data/alumni.db
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home alumni && mkdir /app/data && chown alumni:alumni /app/data
COPY --chown=alumni:alumni alumni ./alumni
COPY --chown=alumni:alumni migrations ./migrations
COPY --from=addin-build --chown=alumni:alumni /source/alumni/static ./alumni/static
USER alumni
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", "alumni.wsgi:application"]
