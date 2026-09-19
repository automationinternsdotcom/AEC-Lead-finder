# Gmail delegation for GitHub article reports

A Workspace super administrator must authorize a dedicated service account's OAuth
client ID for exactly these scopes:

- `https://www.googleapis.com/auth/gmail.readonly`
- `https://www.googleapis.com/auth/gmail.send`

Enable the Gmail API in its Cloud project. Store the service-account JSON only in
the GitHub Actions repository secret `GMAIL_SERVICE_ACCOUNT_JSON`. Never print the
JSON/private key/access token or place it in a repository, local .env, or artifact.
The runtime impersonates `akhil@automationinterns.com`; it needs no Drive, Admin,
gmail.modify, or full-mail scope. A normal mailbox login does not grant domain-wide
delegation administration privileges.

The only allowed envelopes are To: jw@aetherclean.com and a separate To:
jon@automationinterns.com, akhil@automationinterns.com. CC/BCC must be absent.
The runtime checks exact Subject/From/To/CC/BCC headers in Gmail Sent before it
records success. A sent message is not proof of recipient inbox delivery.
Never use jordan@aethercommercialcleaning.us or any prospect for report/test sends.

After both Gmail and TREG secrets are configured, dispatch one controlled test in
GitHub Actions. Do not fall back to a local sender or browser email automation.
