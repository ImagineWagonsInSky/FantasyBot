# FantasyBot

Automation for LaLiga Fantasy: keeps a LaLiga login alive unattended, talks
to the Fantasy API through a typed client, and checks every day that the API
still looks the way the code expects.

## Status

- **Auth** (`fantasybot auth …`): refresh-token flow against LaLiga's B2C
  sign-in policy, `id_token` as the bearer, the refresh token rotated and
  stored encrypted, writes frozen and an alert sent when the session can't
  be renewed.

## Credits and license

This product includes ‘LaLigaApp’ (c) 2025–present Externoak —
<https://github.com/Externoak/LaLigaApp/>. FantasyBot ports its auth flow,
API endpoint map and response adapters to Python. This attribution does not
imply endorsement by the original authors.

FantasyBot is licensed under GPL-3.0-or-later, including LaLigaApp's
additional attribution term (GPLv3 §7). See [LICENSE](LICENSE) and
[NOTICE](NOTICE).
