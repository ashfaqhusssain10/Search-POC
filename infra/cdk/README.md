# SearchPOC v7 — Lambda + API Gateway deployment

CDK app that deploys the v7 search Lambda behind a REST API with API key auth.

## Prerequisites

- AWS CLI configured (default profile, region `ap-south-1`).
- Docker running locally (CDK builds the Lambda container image via Docker).
- Node.js + `aws-cdk` CLI installed: `npm install -g aws-cdk`.
- DDB table `Item-Item-Similarity-Search` populated (run `python -m scripts.upload_resolution_to_ddb`).
- S3 bucket `search-item-item-poc` populated (run `python -m scripts.build_runtime_artifacts`).

## One-time setup

```bash
cd infra/cdk
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cdk bootstrap   # only the first time, per account/region
```

## Deploy

```bash
cd infra/cdk
source .venv/bin/activate
cdk deploy
```

Outputs at end of deploy:
- `ApiEndpoint` — base URL of the REST API (path `/search`).
- `ApiKeyId` — the API key ID; retrieve the secret value with:
  ```bash
  aws apigateway get-api-key --api-key <ApiKeyId> --include-value --region ap-south-1
  ```
- `LambdaName` — Lambda function name (useful for `aws logs tail`).

## Test the deployed API

```bash
API="<ApiEndpoint>search"
KEY="<api-key-value>"

curl -X POST "$API" \
  -H "x-api-key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"dishes": ["Garlic Naan", "Bagara Rice", "Gulab Jamun"], "top_n": 5}'
```

## Updating

After changing Lambda code:
```bash
cdk deploy   # rebuilds image, pushes, updates Lambda
```

After changing the resolution table or S3 artifacts:
- DDB / S3 changes are picked up on the next Lambda cold start. To force a refresh of warm containers, redeploy the function (`cdk deploy` with no changes is a no-op — use `aws lambda update-function-code` or bump a NO-OP env var).

## Destroy

```bash
cdk destroy
```
Note: the DDB resolution table and S3 bucket are NOT managed by this stack (created by `bootstrap_aws.py`); destroying the stack will not delete them.
