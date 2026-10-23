# Project notes for Claude

## Git remotes and pull requests

This checkout is a fork. `origin` is `mlloyd5/automatic-ripping-machine` (the user's fork) and
`upstream` is `automatic-ripping-machine/automatic-ripping-machine` (the public project).

- Open pull requests against the fork only. Always pass
  `--repo mlloyd5/automatic-ripping-machine` to `gh pr create` and to any other `gh pr` or
  `gh issue` command, even though the gh default repo is set to the fork, so the target is
  explicit in the command.
- Never open a pull request or issue against `upstream` unless the user asks for an upstream
  contribution in that message. If unsure, ask before creating anything on upstream.
- Push branches to `origin` only.
