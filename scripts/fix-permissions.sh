#!/bin/sh
# Re-grant the data-plane roles the HF/local demo needs when a redeploy wipes them.
#
# Symptom this fixes: every model call fails with ChatClientException / 403
# (istio-envoy, empty body) because the service principal lost access to the
# Foundry account. Run it with: sh scripts/fix-permissions.sh
#
# Requires: az CLI logged in as a user with role-assignment rights on the
# account, and azd environment configured (values come from `azd env`).
set -eu

SUBSCRIPTION="$(azd env get-value AZURE_SUBSCRIPTION_ID)"
RESOURCE_GROUP="$(azd env get-value AZURE_RESOURCE_GROUP)"
ACCOUNT="$(azd env get-value AZURE_AI_ACCOUNT_NAME)"
# The HF Space demo service principal, resolved by name: no IDs in source.
SP_OBJECT_ID="$(az ad sp show --id hf-edu-agent-demo --query id -o tsv)"
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
