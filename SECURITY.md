# Security and privacy

Do not report leaked credentials in a public issue. Revoke the affected WebVPN/CAS session first, then contact the repository owner privately.

The following files are secrets or personal data and must never be attached to an issue or committed: browser profiles, Playwright storage state, `.env`, diagnostic keys, SQLite databases, logs, exported ranklists, generated images, and statistics CSV files.

This project does not bypass authentication. Deploy it only where you are authorized to access the source system and process the resulting student data. Keep VNC and noVNC bound to localhost and expose them only through an authenticated SSH tunnel.
