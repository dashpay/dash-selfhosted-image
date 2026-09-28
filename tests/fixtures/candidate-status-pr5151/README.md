# Candidate status API regression fixtures

Public responses captured 2026-09-28 20:25:40 UTC, using GitHub REST API version
`2022-11-28` (the controller's configured version).

- Repository: `dashpay/platform`
- PR: <https://github.com/dashpay/platform/pull/5151>
- Exact head: `a02b1460736e18b6345bb4722c622e55e787371d`
- Status ID: `55116170875`
- Context: `Runner image candidate / PR 5151`
- Successful publisher: <https://github.com/dashpay/platform/actions/runs/36474975257>

## Sources and projections

- `combined.json`: `GET repos/dashpay/platform/commits/a02b1460736e18b6345bb4722c622e55e787371d/status?per_page=100&page=1`.
  Retains the combined response envelope's state, SHA, count and URL fields,
  and the matching status object unchanged. Unrelated statuses and repository
  metadata are omitted. `total_count` remains the original combined count,
  not the length of this fixture's filtered status array.
- `statuses.json`: `GET repos/dashpay/platform/commits/a02b1460736e18b6345bb4722c622e55e787371d/statuses?per_page=100&page=1`.
  Retains the full-list array shape and matching status object unchanged;
  unrelated contexts are omitted.
- `publisher-run.json`: `GET repos/dashpay/platform/actions/runs/36474975257`.
  Retains the run ID, path, event, status, conclusion, exact head, URL,
  PR associations and repository full name; other run metadata is omitted.

The combined endpoint genuinely omits `creator`: it was not removed by this
projection. The full status includes `creator.login = github-actions[bot]`
and `creator.id = 41898282`. No credential material is included.

Tests derive historical, untrusted and paginated cases from these fixtures;
those mutations are synthetic, not additional claims about the live PR.

GitHub's [commit-status API documentation](https://docs.github.com/en/rest/commits/statuses?apiVersion=2022-11-28)
distinguishes the combined endpoint's `Simple Commit Status` objects (without
`creator`) from full `Status` objects (with nullable `creator`). The list
endpoint returns statuses in reverse chronological order, with at most 100
entries per page; contexts are case-insensitive. The resolver must select the
newest matching context before validating it, not search history for a usable
success.

## Fixture SHA-256

- `combined.json`: `2dde97023e3a7849aeec30157a2378691814f75dccd665fbd7f9e23e02c6bdd9`
- `statuses.json`: `9ef6372a7034b9ec7caac4d54b374b3f62cbe84cce80951bcdef1bc2181dc485`
- `publisher-run.json`: `8add3bcbe7b625b3573b159899c03a4d52f8b8c753b76c78df0816a271492758`
