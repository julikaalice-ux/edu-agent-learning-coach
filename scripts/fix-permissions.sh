#!/bin/sh
# Re-grant the data-plane roles the HF/local demo needs when a redeploy wipes them.
#
# Symptom this fixes: every model call fails with ChatClientException / 403
# (istio-envoy, empty body) because the service principal lost access to the
# Foundry account. Run it with: sh scripts/fix-permissions.sh
#
# Requires: az CLI logged in as a user with role-assignment rights on the account.
set -eu

SUBSCRIPTION="e4ef4f05-a6e1-4e76-a213-2670ffe7a487"
RESOURCE_GROUP="rg-agent-framework-agent-with-local-tools-responses-dev-48181787"
ACCOUNT="cog-c5e2qyhj2tb7g"
SP_OBJECT_ID="3023d782-1ed9-4f12-9cb5-2f808f490920"  # hf-edu-agent-demo
SCOPE="/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.CognitiveServices/accounts/$ACCOUNT"

az account set --subscription "$SUBSCRIPTION"

for ROLE in "Azure AI Developer" "Cognitive Services User"; do
    # Filter by principalId locally: --assignee would call Microsoft Graph,
    # which is unreliable from some networks and makes the script hang.
    if az role assignment list --scope "$SCOPE" \
            --query "[?principalId=='$SP_OBJECT_ID'&&roleDefinitionName=='$ROLE']" -o tsv | grep -q .; then
        echo "already assigned: $ROLE"
    else
        az role assignment create \
            --assignee-object-id "$SP_OBJECT_ID" \
            --assignee-principal-type ServicePrincipal \
            --role "$ROLE" \
            --scope "$SCOPE" \
            --output none
        echo "assigned: $ROLE"
    fi
done

echo
echo "Done. Propagation can take a few minutes; retry the chat if it still 403s."
