#!/usr/bin/env bash
set -euo pipefail

# Setup step `keycloak-setup` of process-compose.yaml: bring the local Keycloak's `nannos` realm
# to what local development needs. One attempt; process-compose restarts the step on failure while
# Keycloak is still booting (a cold boot takes ~110 s), so there are no retry loops here.
# Nothing to do against a remote IdP (NANNOS_OIDC_MODE != local).
#
# Idempotent, and safe for two stacks at once: every write sets a fixed value.

[[ "${NANNOS_OIDC_MODE:-local}" == local ]] || { echo "Remote IdP: nothing to set up locally"; exit 0; }

KC=http://localhost:8180
curl -sf "$KC/realms/nannos/.well-known/openid-configuration" >/dev/null \
  || { echo "Keycloak (realm nannos) is not ready yet"; exit 1; }

cd "$(dirname "${BASH_SOURCE[0]}")"

admin_token() {
  curl -s -X POST "$KC/realms/master/protocol/openid-connect/token" \
    -d grant_type=password -d client_id=admin-cli -d username=admin -d password=admin \
    | python3 -c 'import json, sys; print(json.load(sys.stdin).get("access_token", ""))' 2>/dev/null || true
}
TOKEN="$(admin_token)"
if [[ -z "$TOKEN" ]]; then
  # Keycloak 26.x defaults the master realm to sslRequired!=NONE, blocking HTTP admin token
  # requests from the host. Disable it via kcadm.sh (internal HTTP) — once: it starts a JVM in the
  # container, which takes minutes on a busy machine. exec reads stdin: give it none.
  docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh update realms/master \
    -s sslRequired=NONE --server http://localhost:8080 --realm master --user admin --password admin \
    </dev/null >/dev/null 2>&1 || echo "⚠ Could not disable master realm SSL (may already be NONE)"
  TOKEN="$(admin_token)"
fi
[[ -n "$TOKEN" ]] || { echo "No Keycloak admin token yet"; exit 1; }

api() {  # method path [json]
  curl -sf -X "$1" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    "$KC/admin/realms/nannos/$2" ${3:+-d "$3"}
}
client_uuid() { api GET "clients?clientId=$1" | python3 -c 'import json, sys; print(json.load(sys.stdin)[0]["id"])'; }

# Keycloak ignores the "secret" field in realm exports and generates random ones: every
# confidential client gets "local-secret", what the stack is configured with.
for client in agent-console orchestrator nannos-admin; do
  api PUT "clients/$(client_uuid "$client")" '{"secret": "local-secret"}' >/dev/null
done
echo "✓ Keycloak client secrets configured"

# The realm export doesn't include service account role mappings: let the backend manage groups
# and users through nannos-admin.
SA_USER_ID=$(api GET "clients/$(client_uuid nannos-admin)/service-account-user" \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])')
REALM_MGMT_UUID=$(client_uuid realm-management)
ROLES=$(api GET "clients/$REALM_MGMT_UUID/roles" | python3 -c '
import json, sys
needed = {"manage-users", "view-users", "query-groups", "query-users"}
print(json.dumps([r for r in json.load(sys.stdin) if r["name"] in needed]))')
api POST "users/$SA_USER_ID/role-mappings/clients/$REALM_MGMT_UUID" "$ROLES" >/dev/null
echo "✓ nannos-admin service account roles granted"

# Every stack on this machine signs in through agent-console: slot 0 on :5001 and slot N on
# :4N001. This is the dev realm only, so its redirect URIs are a localhost wildcard instead of one
# entry per slot (realm-export.json has it; a Keycloak imported before that gets it here).
AC_UUID=$(client_uuid agent-console)
PATCH=$(api GET "clients/$AC_UUID" | python3 -c '
import json, sys
client = json.load(sys.stdin)
wanted = ["http://localhost*", "http://127.0.0.1*"]
uris = client.get("redirectUris") or []
attrs = client.get("attributes") or {}
logout = [u for u in attrs.get("post.logout.redirect.uris", "").split("##") if u]
if all(u in uris for u in wanted) and all(u in logout for u in wanted):
    sys.exit()
attrs["post.logout.redirect.uris"] = "##".join(logout + [u for u in wanted if u not in logout])
print(json.dumps({"redirectUris": uris + [u for u in wanted if u not in uris], "attributes": attrs}))')
if [[ -n "$PATCH" ]]; then
  api PUT "clients/$AC_UUID" "$PATCH" >/dev/null
  echo "✓ agent-console accepts every local stack (http://localhost*)"
fi
