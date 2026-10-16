#!/bin/sh
# Provision the Foundry Memory store the learning coach mirrors validated
# notes into, and grant the runtime identities the memory RBAC roles.
#
# Symptom this fixes: LEARNING_COACH_MEMORY_STORE is set but the store does
# not exist (search/mirror calls 404), or memory writes fail with 401.
# Run it with: sh scripts/setup-memory.sh [store-name]
#
# Requires: az CLI logged in as a user with role-assignment rights on the
# account, and the text-embedding-3-small deployment provisioned (azd provision).
set -eu

SUBSCRIPTION="<subscription-id>"
RESOURCE_GROUP="<resource-group>"
ACCOUNT="<ai-account>"
PROJECT="edu-agent"
PROJECT_ENDPOINT="https://${ACCOUNT}.services.ai.azure.com/api/projects/${PROJECT}"
API_VERSION="2025-11-15-preview"
# Avoid names ending in s/x/ch: the preview search endpoint pluralizes the
# name naively and 404s ("coach" -> "coachs").
STORE_NAME="${1:-edu-agent-learning-memory}"
CHAT_MODEL="gpt-5.4-mini"
EMBEDDING_MODEL="text-embedding-3-small"
# Runtime identities that need the memory API roles:
# - the PROJECT's system-assigned managed identity (<project-identity-principal-id>): the memory
#   runtime uses it to call the chat/embedding deployments; without it writes
#   fail with "Authentication to the Azure OpenAI resource failed"
# - the account's system-assigned managed identity (<account-identity-principal-id>)
# - the hosted agent's dedicated Entra identity (refresh from
#   `azd env get-value AGENT_AGENT-FRAMEWORK-AGENT-WITH-LOCAL-TOOLS-RESPONSES_INSTANCE_IDENTITY_PRINCIPAL_ID`)
# - the HF Space demo service principal (hf-edu-agent-demo, see fix-permissions.sh)
PRINCIPALS="<project-identity-principal-id> <account-identity-principal-id> <agent-identity-principal-id> <demo-sp-object-id>"
SCOPE="/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.CognitiveServices/accounts/$ACCOUNT"
# The memory API validates the token audience as ai.azure.com (without the
# /.default suffix: az returns an empty token for that form).
AUDIENCE="https://ai.azure.com"

az account set --subscription "$SUBSCRIPTION"

# 1. Create the store if it does not exist (get-or-create).
STORES=$(az rest --method get \
    --uri "${PROJECT_ENDPOINT}/memory_stores?api-version=${API_VERSION}" \
    --resource "$AUDIENCE")
if echo "$STORES" | grep -q "\"$STORE_NAME\""; then
    echo "memory store already exists: $STORE_NAME"
else
    az rest --method post \
        --uri "${PROJECT_ENDPOINT}/memory_stores?api-version=${API_VERSION}" \
        --resource "$AUDIENCE" \
        --body "{\"name\":\"$STORE_NAME\",\"description\":\"Edu-agent learning coach semantic memory\",\"definition\":{\"kind\":\"default\",\"chat_model\":\"$CHAT_MODEL\",\"embedding_model\":\"$EMBEDDING_MODEL\",\"options\":{\"user_profile_enabled\":true,\"chat_summary_enabled\":false,\"procedural_memory_enabled\":false,\"default_ttl_seconds\":0}}}" > /dev/null
    echo "created memory store: $STORE_NAME"
fi

# 2. Grant the memory roles to each runtime identity. Role names are
# preview-era and may drift; what gets assigned is echoed so drift is visible.
# (Chat access itself uses the roles granted by fix-permissions.sh / azd.)
for PRINCIPAL in $PRINCIPALS; do
    for ROLE in "Foundry User" "Cognitive Services OpenAI User"; do
        if az role assignment list --scope "$SCOPE" \
                --query "[?principalId=='$PRINCIPAL'&&roleDefinitionName=='$ROLE']" -o tsv | grep -q .; then
            echo "already assigned: $ROLE -> $PRINCIPAL"
        else
            az role assignment create \
                --assignee-object-id "$PRINCIPAL" \
                --assignee-principal-type ServicePrincipal \
                --role "$ROLE" \
                --scope "$SCOPE" \
                --output none
            echo "assigned: $ROLE -> $PRINCIPAL"
        fi
    done
done

echo
echo "Done. Verify in the Foundry portal Memory page, or search the store:"
echo "  az rest --method post \\"
echo "    --uri '${PROJECT_ENDPOINT}/memory_stores/${STORE_NAME}:search_memories?api-version=${API_VERSION}' \\"
echo "    --resource '${AUDIENCE}' --body '{\"scope\":\"<memory-key>\"}'"
