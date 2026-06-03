FROM public.ecr.aws/lambda/python:3.12-arm64

# boto3 ships with the Lambda base image. If we ever need a newer version,
# pin it here and the Lambda runtime will pick up the COPY-ed copy first.
COPY lambda/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt --target "${LAMBDA_TASK_ROOT}"

# Copy only what the runtime path imports. Keeps the image small and avoids
# accidentally bundling Qdrant/Neo4j-dependent modules.
COPY core/__init__.py        ${LAMBDA_TASK_ROOT}/core/__init__.py
COPY core/categories.py      ${LAMBDA_TASK_ROOT}/core/categories.py
COPY core/platter_cache.py   ${LAMBDA_TASK_ROOT}/core/platter_cache.py
COPY core/rds_client.py      ${LAMBDA_TASK_ROOT}/core/rds_client.py
COPY core/runtime_index.py   ${LAMBDA_TASK_ROOT}/core/runtime_index.py
COPY scripts/__init__.py     ${LAMBDA_TASK_ROOT}/scripts/__init__.py
COPY scripts/search_v7.py    ${LAMBDA_TASK_ROOT}/scripts/search_v7.py
COPY lambda/handler.py       ${LAMBDA_TASK_ROOT}/handler.py
COPY lambda/server.py        ${LAMBDA_TASK_ROOT}/server.py

# Lambda default entrypoint. ECS overrides this in the task definition to:
#   ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
CMD ["handler.handler"]
