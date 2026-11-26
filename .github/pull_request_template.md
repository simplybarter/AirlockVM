## Summary
Provide a concise summary of the changes and link any related issue (e.g., `Closes #123`).

## Type of Change
- [ ] Bug fix (`fix`)
- [ ] New feature (`feat`)
- [ ] Refactor / performance (`refactor` / `perf`)
- [ ] Documentation (`docs`)
- [ ] Tests / CI / maintenance (`test` / `chore`)

## Verification Checklist
- [ ] Ran `.venv/bin/python -m unittest discover -s tests -v` with zero failures.
- [ ] Ran `.venv/bin/python .agents/skills/airlock_configure/scripts/verify_config.py` and `.venv/bin/python .agents/skills/airlock_inventory/scripts/verify_config.py`.
- [ ] Verified portability invariants (no hardcoded local home paths, private hostnames, or secrets).
- [ ] Updated relevant documentation and `CHANGELOG.md` (`[Unreleased]`) if user-facing behavior changed.
- [ ] Followed Conventional Commits (`<type>(<scope>): <subject>`).
