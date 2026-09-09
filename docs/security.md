# Runtime security

The mutating API routes require `BEEHAIIVE_API_KEY`, a configured project allowlist, and a worker lease token where a run is involved. Keep the API key and provider token in the server environment. The API does not return the provider token.

Provision `GITHUB_TOKEN` or `GH_TOKEN` as a GitHub App installation token or fine-grained personal access token. Limit it to the repositories linked to the selected ProjectV2. Grant only the permissions required by this provider:

- read access to the selected ProjectV2 and linked issue metadata
- repository metadata read access
- Contents write access for branch creation
- Pull requests write access for pull-request creation and lookup

Do not use a classic broad `repo` token or grant administration, workflow, secret, or organization-management permissions. GitHub controls token permissions outside the application, so deployment configuration must enforce this boundary. See [GitHub's fine-grained token permissions](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens) when provisioning the token.
