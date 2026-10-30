# Haixin contribution rules

- Preserve existing user data, configuration, and permission boundaries.
- Use isolated fixtures for tests. Never commit credentials, runtime data, chats or backups.
- Verify actual behavior; distinguish mocked tests, real model checks and unverified behavior.
- Keep the main interface conversational and concise. Personal state should serve the conversation, not force users to assemble modules.
- Do not expose the local API publicly without implementing authentication and per-user data/tool isolation.
- Follow README for checks. Keep external dependency licenses and attribution intact.
