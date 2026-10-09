#!/bin/sh
# Re-grant the data-plane roles the HF/local demo needs when a redeploy wipes them.
#
# Symptom this fixes: every model call fails with ChatClientException / 403
# (istio-envoy, empty body) because the service principal lost access to the
# Foundry account. Run it with: sh scripts/fix-permissions.sh
#
# Requires: az CLI logged in as a user with role-assignment rights on the account.
set -eu

SUBSCRIPTION="<subscription-id>"
RESOURCE_GROUP="<resource-group>"
ACCOUNT="<ai-account>"
SP_OBJECT_ID="<demo-sp-object-id>"  # hf-edu-agent-demo
SCOPE="/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.CognitiveServices/accounts/$ACCOUNT"

az account set --subscription "$SUBSCRIPTION"

for ROLE in "Azure AI Developer" "Cognitive Services User"; do
    if az role assignment list --scope "$SCOPE" --assignee "$SP_OBJECT_ID" --role "$ROLE" --query "[0]" -o tsv | grep -q .; then
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
