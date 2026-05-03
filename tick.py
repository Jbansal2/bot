import requests

res = requests.post(
    "https://bot-production-3033.up.railway.app/v1/tick",
    json={
        "now": "2026-05-03T21:00:00Z",
        "available_triggers": ["trg_003_recall_due_priya"]
    }
)

print(res.text)