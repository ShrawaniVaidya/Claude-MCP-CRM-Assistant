import requests

response = requests.post(
    "http://localhost:8000/api/ai-analyze",
    json={
        "question": "How many hot leads do we have?"
    }
)

print("Status:", response.status_code)
print("Response:")
print(response.text)