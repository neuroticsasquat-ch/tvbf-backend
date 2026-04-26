# tvbf-backend/infra

OpenTofu config for the TV Binge Friend backend. Owns:

- The `tvbf` database + `tvbf_user` role on the brand-shared Postgres server.
- The Linux Web App (container) on the brand-shared App Service Plan.
- App settings / connection string / GHCR image config.
- Diagnostic settings routing logs to the brand-shared Log Analytics workspace.

The custom domain `api.tvbingefriend.com` is managed outside tofu for now — wire
up the CNAME at the registrar once the Web App's default hostname exists, then
add the hostname binding + managed cert via the Azure portal or `az webapp config
hostname add`. Can be imported into tofu later if desired.

Reads brand-shared resources (resource group, App Service Plan, Postgres server,
Log Analytics workspace) from `sasquatch-infra/` via Azure data sources keyed by
name. State lives in the same storage account as `sasquatch-infra/`, but under a
different `key` (`tvbf-backend.tfstate`) so applies are isolated.

## Prereqs

- `sasquatch-infra/` has been applied (the shared resources exist).
- The `pg_trgm`, `citext`, and `pgcrypto` extensions are preloaded at the server
  level by `sasquatch-infra/postgres.tf` (`azure.extensions` server config).
- A GitHub Personal Access Token with `read:packages` scope, used by the Web App
  to pull the container image from GHCR. Store as `ghcr_pat` in tfvars or env.

## Apply

```bash
tofu init
tofu plan -var-file=terraform.tfvars
tofu apply -var-file=terraform.tfvars
```

## Per-deploy flow (handled by GitHub Actions, not by tofu)

1. CI builds + pushes `ghcr.io/<owner>/tvbf-backend:<sha>` and `:main`.
2. CI runs `alembic upgrade head` against the prod DB (one-shot container).
3. CI calls `az webapp config container set --docker-custom-image-name ghcr.io/.../tvbf-backend:<sha>`
   to update the running image. (We pin the image tag here so tofu plans don't
   churn on every deploy.)

`tofu apply` handles infra changes (env vars, scaling, custom domain). Image tag
updates happen out-of-band via the deploy workflow.
