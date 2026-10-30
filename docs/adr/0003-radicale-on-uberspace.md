# Radicale on Uberspace as the calendar store

The calendar lives in a self-hosted Radicale instance on the same account that runs TimeTagger, reached by Apple Calendar, iOS, Thunderbird and DAVx5 over CalDAV. Radicale is deployed and upgraded outside this repo; this project ships only the bridge.

Rejected: Google Calendar as the store (data leaves the server, OAuth, quota, and duplicate semantics become Google's); iCloud or another hosted CalDAV (provider quirks, notably refusing cross-calendar moves); implementing CalDAV inside the bridge (cheapest to deploy, most expensive to keep compatible).

Radicale does not expand recurring events, which matches the no-recurrence decision, and Uberspace provides TLS and routing through `uberspace web backend`.
