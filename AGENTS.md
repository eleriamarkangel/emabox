# Emabox working preferences

- This is the canonical application code repository.
- Branch workflow: local work and validation on `test`; promote validated changes to `staging` for deployment preparation. Promote to `main` only after staging succeeds. Do not skip stages.
- Keep project notes, conversation summaries, and activity history in `C:\Users\My PC\Documents\ChatGPT\emabox`.
- Before live provider API testing, tell the user the purpose and planned request budget.
- Use offline fake-provider tests by default. Keep live testing minimal, with a maximum of 100 outbound provider API requests per feature-testing effort, including retries and token-count requests. Do not reset the budget by splitting runs. Stop at the cap unless the user explicitly changes it.
- Record live request counts and outcomes in the notes folder. Do not record secrets.
