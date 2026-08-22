# Running paperless-ngx

## Prerequisites

- Docker + Docker Compose plugin
- An Authentik instance if you want OIDC login (optional)

## 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set:

- `PAPERLESS_SECRET_KEY` — generate with `python3 -c "import secrets; print(secrets.token_urlsafe(64))"`
- `PAPERLESS_URL` — the public URL this instance will be served from
- `PAPERLESS_ALLOWED_HOSTS` / `PAPERLESS_CSRF_TRUSTED_ORIGINS` — matching hostnames
- `USERMAP_UID` / `USERMAP_GID` — match your host user so bind-mounted files (`./consume`, `./export`) aren't owned by root

## 2. Start the stack

```bash
docker compose -f compose.yml up -d
```

This brings up `db` (Postgres), `broker` (Redis), `gotenberg` + `tika` (Office doc conversion/OCR), and `webserver` (paperless-ngx itself) on port 8000.

Create the initial admin user:

```bash
docker compose -f compose.yml exec webserver python3 manage.py createsuperuser
```

Then visit `http://localhost:8000` (or your configured `PAPERLESS_URL`).

## 3. (Optional) Set up OIDC login via Authentik

1. In Authentik, create an **OAuth2/OpenID Provider**:
   - Redirect URI: `<PAPERLESS_URL>/accounts/oidc/authentik/login/callback/`
   - Note the generated **Client ID** and **Client Secret**.
2. Create an **Application** in Authentik bound to that provider, and note its **slug**.
3. In `.env`, fill in the OIDC block:
   - Replace `<authentik.example.com>` with your Authentik host
   - Replace `<app-slug>` with the application slug from step 2
   - Set `client_id` and `secret` to the values from step 1
4. Restart the stack: `docker compose -f compose.yml up -d`
5. On the paperless-ngx login page, a "Authentik" SSO option will now appear.

Optional flags in `.env`:

- `PAPERLESS_SOCIAL_AUTO_SIGNUP=true` — skip the "confirm details" step on first SSO login
- `PAPERLESS_DISABLE_REGULAR_LOGIN=true` — disable username/password login, SSO only
- `PAPERLESS_REDIRECT_LOGIN_TO_SSO=true` — skip the login page and go straight to Authentik

## Stopping / updating

```bash
docker compose -f compose.yml down        # stop
docker compose -f compose.yml pull        # pull new images
docker compose -f compose.yml up -d       # recreate with new images
```

Data persists in the `data`, `media`, `pgdata`, and `redisdata` named volumes.
