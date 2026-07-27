# SINGLE WORKER ONLY — do not add `--workers N` or scale this to >1 dyno.
# rate_limiter.py keeps the S1 auth-throttle windows in process memory, so N
# workers means N x the brute-force allowance. See .env.example "Deploy posture"
# and docs/SECURITY_ARCHITECTURE_DECISIONS.md §3.
web: cd lexy-app && uvicorn backend.main:app --host 0.0.0.0 --port $PORT
