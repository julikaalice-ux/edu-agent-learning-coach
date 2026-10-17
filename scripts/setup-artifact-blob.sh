#!/bin/sh
# Provision the public blob container the hosted entry serves interactive
# visuals from, grant the runtime identities Storage Blob Data Contributor,
# and install a lifecycle rule deleting artifacts after ~2 days.
#
# Symptom this fixes: hosted replies link to http://localhost:8088/artifacts/...
# which the Foundry gateway never forwards.
# Run it with: sh scripts/setup-artifact-blob.sh
#
# Requires: az CLI logged in as a user with owner/contributor rights on the RG
# and Microsoft.Storage/storageAccounts/listKeys on the account.
set -eu

SUBSCRIPTION="e4ef4f05-a6e1-4e76-a213-2670ffe7a487"
RESOURCE_GROUP="rg-agent-framework-agent-with-local-tools-responses-dev-48181787"
LOCATION="francecentral"
# Deterministic globally-unique name: "eduartifacts" + first 8 hex of
# sha256(SUBSCRIPTION) so azure.yaml can hardcode it and re-runs stay idempotent.
ACCOUNT="eduartifacts5044621b"
CONTAINER="artifacts"
# Runtime identities that upload artifacts:
# - the hosted agent's dedicated Entra identity
# - the HF Space demo service principal (hf-edu-agent-demo) for local parity
PRINCIPALS="8285c8ba-7ac1-43ae-969d-022537c56981 3023d782-1ed9-4f12-9cb5-2f808f490920"
SCOPE="/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Storage/storageAccounts/$ACCOUNT"
# Local artifact TTL is 24h; the lifecycle rule runs at least daily, so 2
# days here bounds the anonymous-read window without racing local expiry.
POLICY='{"rules":[{"enabled":true,"name":"delete-expired-artifacts","type":"Lifecycle","definition":{"actions":{"baseBlob":{"delete":{"daysAfterModificationGreaterThan":2}}},"filters":{"blobTypes":["blockBlob"],"prefixMatch":["artifacts/"]}}}]}'

az account set --subscription "$SUBSCRIPTION"

# 1. Storage account (get-or-create). allowBlobPublicAccess is false by
# default on new accounts since Aug 2023; set it on create AND update so
# re-runs also repair a reverted flag.
if az storage account show --name "$ACCOUNT" --resource-group "$RESOURCE_GROUP" -o none 2>/dev/null; then
    echo "storage account already exists: $ACCOUNT"
    az storage account update --name "$ACCOUNT" --resource-group "$RESOURCE_GROUP" \
        --allow-blob-public-access true --output none
else
    az storage account create --name "$ACCOUNT" --resource-group "$RESOURCE_GROUP" \
        --location "$LOCATION" --sku Standard_LRS --kind StorageV2 \
        --allow-blob-public-access true --min-tls-version TLS1_2 --output none
    echo "created storage account: $ACCOUNT"
fi

# 2. Container with public-access=blob (anonymous read of blobs, no listing).
# Set Container ACL requires account-key auth: Entra ID is not supported here.
KEY=$(az storage account keys list --account-name "$ACCOUNT" --resource-group "$RESOURCE_GROUP" \
    --query "[0].value" -o tsv)
az storage container create --name "$CONTAINER" --account-name "$ACCOUNT" --account-key "$KEY" \
    --auth-mode key --public-access blob --fail-on-exist false --output none
az storage container set-permission --name "$CONTAINER" --account-name "$ACCOUNT" \
    --account-key "$KEY" --auth-mode key --public-access blob --output none
echo "container ready: $CONTAINER (public-access=blob)"

# 3. Lifecycle rule: delete artifacts older than 2 days (idempotent PUT).
az storage account management-policy create --account-name "$ACCOUNT" \
    --resource-group "$RESOURCE_GROUP" --policy "$POLICY" --output none
echo "lifecycle policy installed: delete artifacts after 2 days"

# 4. RBAC for the upload identities at account scope.
for PRINCIPAL in $PRINCIPALS; do
    if az role assignment list --scope "$SCOPE" \
            --query "[?principalId=='$PRINCIPAL'&&roleDefinitionName=='Storage Blob Data Contributor']" -o tsv | grep -q .; then
        echo "already assigned: Storage Blob Data Contributor -> $PRINCIPAL"
    else
        az role assignment create --assignee-object-id "$PRINCIPAL" \
            --assignee-principal-type ServicePrincipal --role "Storage Blob Data Contributor" \
            --scope "$SCOPE" --output none
        echo "assigned: Storage Blob Data Contributor -> $PRINCIPAL"
    fi
done

echo
echo "Container URL (azure.yaml env LEARNING_COACH_ARTIFACT_BLOB_URL):"
echo "  https://${ACCOUNT}.blob.core.windows.net/${CONTAINER}"
echo "Verify anonymous read with:"
echo "  az storage blob upload --account-name $ACCOUNT --account-key <key> \\"
echo "    --container-name $CONTAINER --name probe.html --file <any.html> --auth-mode key"
echo "  curl -I https://${ACCOUNT}.blob.core.windows.net/${CONTAINER}/probe.html   # expect 200 text/html"
