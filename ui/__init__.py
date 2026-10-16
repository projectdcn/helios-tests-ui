"""helios-tests web UI (stage 1 + inventory editor / quarantine): ``python -m ui`` on the tester's Mac, bound to
127.0.0.1. Browser -> this controller -> execution (local pytest for a directly reachable rack, or the jump host over
SSH for the full rack). Standard library only (http.server + threading); no build step for the static page."""
