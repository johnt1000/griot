# Platforms

`griot index platform` reads pull requests (merge requests on GitLab),
releases and issues from the platform that hosts each repository. How a point
from it is keyed: [indexing-model.md](indexing-model.md).

## The five adapters

**Five platforms** — GitHub, GitLab (incl. self-hosted), Bitbucket Cloud,
Azure DevOps and Gitea/Forgejo adapters for PRs/releases/issues, detected from
each repo's `origin` remote.

## What has run against a real platform

These are built against each provider's documented API and covered by tests
with mocked HTTP; the GitHub path has been exercised against a live account,
and GitLab against gitlab.com, with a token and without one. Self-hosted
GitLab has not had a real run, and the other three have only ever been
exercised against mocked HTTP, with one exception: the path that reads a
Gitea/Forgejo release's author was checked against codeberg.org's public API
(#111).

## Tokens

Platform tokens (only needed for `griot index platform`): `GITHUB_TOKEN`,
`GITLAB_PERSONAL_ACCESS_TOKEN`, `BITBUCKET_ACCESS_TOKEN`, `AZURE_DEVOPS_PAT`,
`GITEA_TOKEN`. GitLab is the exception: without
`GITLAB_PERSONAL_ACCESS_TOKEN`, griot reads a public project's merge requests,
releases and issues anonymously and says so in the run's output; a private
project then fails that run with an error naming the token and
`griot auth set gitlab`.

How each is stored: [Credentials](credentials.md). `GRIOT_GITLAB_API_BASE`
(self-hosted GitLab) and `GRIOT_GITEA_HOSTS` (Gitea/Forgejo hosts) are in
[Configuration](configuration.md#environment-variables).
