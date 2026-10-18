FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app
RUN pip install --no-cache-dir uv==0.13.1
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY entrypoint.sh ./entrypoint.sh
RUN uv sync --frozen --no-dev
RUN chmod 0555 /app/entrypoint.sh
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["api"]
