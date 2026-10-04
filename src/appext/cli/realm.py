"""`appext keycloak export`: a realm import for a **local** Keycloak.

For development without the platform's own stack: a fresh Keycloak (`start-dev
--import-realm`) that knows the extension's client, the scopes its manifest asks
for and the dev key's public half. The real thing is the platform's App Store, which
provisions the same objects through the Keycloak Admin API when a reviewer approves
(docs/platform.md); this is its stand-in on a laptop. Only useful where the
platform's OAuth service is Keycloak.

Three things in the output are deliberate:

* `basic` and `acr` are declared. A realm file that lists `clientScopes` *replaces*
  Keycloak's built-in set; without `basic` no token carries `sub`. Everything else the built-in set
  offers (profile, e-mail, roles …) is left out on purpose: it would show up on the
  consent screen without the extension needing it.
* Every audience gets a **client of its own** (a bearer-only stand-in for the target
  service). Keycloak's token exchange resolves `audience` to a client of the realm and
  answers `invalid_client (Audience not found)` for a name it does not know. On the
  platform the target services exist; in a fresh local realm nothing else would create them.
* Redirect URIs cover both `localhost` and `127.0.0.1`, plus the host app's own return
  address (`app_redirect_uri` of the platform file), so the extension works in a browser
  and in the host app's WebView.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from .context import CliError, Context
from .keys import JWK_FILE, ensure_dev_key, load_public_jwk
from .project import Project, load_project, refuse_link
from .settings import resolve

DEV_USER = ("dev@localhost.invalid", "dev")

BASIC_SCOPE = {
    "name": "basic",
    "description": "Basic claims: sub and auth_time",
    "protocol": "openid-connect",
    "attributes": {"include.in.token.scope": "false", "display.on.consent.screen": "false"},
    "protocolMappers": [
        {"name": "sub", "protocol": "openid-connect", "protocolMapper": "oidc-sub-mapper", "consentRequired": False,
         "config": {"access.token.claim": "true", "introspection.token.claim": "true"}},
        {"name": "auth_time", "protocol": "openid-connect", "protocolMapper": "oidc-usersessionmodel-note-mapper",
         "consentRequired": False,
         "config": {"user.session.note": "AUTH_TIME", "id.token.claim": "true", "introspection.token.claim": "true",
                    "access.token.claim": "true", "claim.name": "auth_time", "jsonType.label": "long"}},
    ],
}
ACR_SCOPE = {
    "name": "acr",
    "description": "Authentication context class reference",
    "protocol": "openid-connect",
    "attributes": {"include.in.token.scope": "false", "display.on.consent.screen": "false"},
    "protocolMappers": [
        {"name": "acr loa level", "protocol": "openid-connect", "protocolMapper": "oidc-acr-mapper", "consentRequired": False,
         "config": {"id.token.claim": "true", "introspection.token.claim": "true", "access.token.claim": "true"}},
    ],
}


def audience_scope(name: str, audience: str) -> dict:
    """A scope the person consents to, whose token carries `aud: <audience>` – without the
    mapper the token says `aud: account` and the target service rejects it."""
    return {
        "name": name,
        "description": f"Extension scope for {audience}",
        "protocol": "openid-connect",
        "attributes": {
            "include.in.token.scope": "true",
            "display.on.consent.screen": "true",
            "consent.screen.text": f"Access to {audience}: {name}",
        },
        "protocolMappers": [{
            "name": f"{audience}-audience",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "consentRequired": False,
            "config": {"included.client.audience": audience, "id.token.claim": "false",
                       "access.token.claim": "true", "introspection.token.claim": "true"},
        }],
    }


def scope_audiences(project: Project, consent_audience: str) -> dict[str, str]:
    audiences = {scope: consent_audience for scope in project.consent_scopes}
    for service in project.services:
        audiences.update({scope: service.audience for scope in service.scopes})
    return audiences


def audience_client(audience: str) -> dict:
    return {
        "clientId": audience,
        "name": f"{audience} (stand-in for local development)",
        "enabled": True,
        "protocol": "openid-connect",
        "bearerOnly": True,
        "publicClient": False,
        "standardFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "implicitFlowEnabled": False,
    }


def build_client(project: Project, jwk: dict, *, backchannel_url: str, app_redirect_uri: str | None = None,
                 platform_name: str = "") -> dict:
    origins = [f"http://localhost:{project.dev_port}", f"http://127.0.0.1:{project.dev_port}"]
    attributes = {
        "pkce.code.challenge.method": "S256",
        "standard.token.exchange.enabled": "true",
        "display.on.consent.screen": "true",
        "consent.screen.text": f"Extension for {platform_name or 'the platform'}: {project.name}",
        "post.logout.redirect.uris": "##".join(f"{o}/" for o in origins),
        "backchannel.logout.url": backchannel_url,
        "backchannel.logout.session.required": "true",
        "frontchannel.logout.enabled": "false",
    }
    client = {
        "clientId": project.client_id,
        "name": f"Extension: {project.name}",
        "description": project.manifest.description,
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "standardFlowEnabled": True,
        "directAccessGrantsEnabled": False,
        "implicitFlowEnabled": False,
        "serviceAccountsEnabled": any(s.mode == "service" for s in project.services),
        "frontchannelLogout": False,
        "fullScopeAllowed": False,
        "consentRequired": True,
        "redirectUris": [f"{o}/auth/callback" for o in origins] + ([app_redirect_uri] if app_redirect_uri else []),
        "webOrigins": [],
        "defaultClientScopes": ["basic", "acr", *project.consent_scopes],
        "optionalClientScopes": [scope for service in project.services for scope in service.scopes],
        "attributes": attributes,
    }
    if project.client_auth == "private_key_jwt":
        client["clientAuthenticatorType"] = "client-jwt"
        attributes.update({
            "use.jwks.string": "true",
            "jwks.string": json.dumps({"keys": [jwk]}),
            "token.endpoint.auth.signing.alg": jwk["alg"],
        })
    else:
        client["clientAuthenticatorType"] = "client-secret"  # Keycloak generates the secret on import
    return client


def build_realm(project: Project, jwk: dict, *, realm: str, consent_audience: str,
                backchannel_url: str, dev_user: bool, app_redirect_uri: str | None = None,
                platform_name: str = "") -> dict:
    scopes = [audience_scope(name, audience) for name, audience in scope_audiences(project, consent_audience).items()]
    document = {
        "realm": realm,
        "enabled": True,
        "displayName": f"{realm} (local development – generated by appext)",
        "sslRequired": "none",  # a local realm over plain http
        "registrationAllowed": False,
        "loginWithEmailAllowed": True,
        "clientScopes": [BASIC_SCOPE, ACR_SCOPE, *scopes],
        "clients": [
            build_client(project, jwk, backchannel_url=backchannel_url, app_redirect_uri=app_redirect_uri,
                         platform_name=platform_name),
            *(audience_client(a) for a in sorted(set(scope_audiences(project, consent_audience).values()))),
        ],
    }
    if dev_user:
        email, password = DEV_USER
        document["users"] = [{
            "username": email, "email": email, "emailVerified": True, "enabled": True,
            "firstName": "Dev", "lastName": "User",
            "credentials": [{"type": "password", "value": password, "temporary": False}],
        }]
    return document


def realm_name(issuer: str | None) -> str:
    """The realm of a Keycloak issuer URL (`…/realms/<name>`), else `local`."""
    parts = [p for p in urlsplit(issuer or "").path.split("/") if p]
    return parts[parts.index("realms") + 1] if "realms" in parts[:-1] else "local"


def export(args, ctx: Context) -> int:
    project = load_project(ctx, args.path)
    refuse_link(project, "no Keycloak client to export")
    platform = resolve(args, ctx, project_dir=project.root)
    consent_audience = args.consent_audience or platform.starter.audience
    if args.key:
        jwk_file = ctx.path(args.key)
    else:
        _, jwk_file, created = ensure_dev_key(project.root / ".appext")
        if created:
            ctx.warn(f"no dev key yet: created {ctx.show(jwk_file.parent)}/ (the private key stays there, out of git)")
    if project.client_auth == "private_key_jwt":
        jwk = load_public_jwk(jwk_file)
        jwk.setdefault("alg", "RS256" if jwk["kty"] == "RSA" else "ES256")
    else:
        jwk = {}
    backchannel_url = args.backchannel_url or f"http://{args.backchannel_host}:{project.dev_port}/auth/backchannel-logout"
    document = build_realm(project, jwk, realm=args.realm or realm_name(platform.issuer),
                           consent_audience=consent_audience, backchannel_url=backchannel_url,
                           dev_user=args.dev_user, app_redirect_uri=platform.app_redirect_uri,
                           platform_name=platform.name)
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if not args.out:
        ctx.out.write(text)
        return 0
    out = ctx.path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    ctx.say(f"Realm import written to {ctx.show(out)}.")
    ctx.say("Use it with a fresh local Keycloak (start-dev --import-realm); an already running realm is left alone by the import.")
    if args.dev_user:
        ctx.say(f"Test user for that realm: {DEV_USER[0]} / {DEV_USER[1]} (local development only).")
    return 0
