import json
import requests

url = "https://bot-production-3033.up.railway.app/v1/context"

with open("dataset/merchants_seed.json", "r", encoding="utf-8") as f:
    payload = json.load(f)["merchants"][0]

body = {
    "scope": "merchant",
    "context_id": "m_001_drmeera_dentist_delhi",
    "version": 1,
    "payload": payload
}

res = requests.post(url, json=body)

print(res.text)