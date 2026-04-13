import requests

url = "https://graph.facebook.com/v19.0/1059746060556447/messages"
headers = {
    "Authorization": "Bearer SEU_TOKEN_AQUI",
    "Content-Type": "application/json"
}
payload = {
    "messaging_product": "whatsapp",
    "to": "SEU_NUMERO_PESSOAL", # coloque seu cel com 55...
    "type": "text",
    "text": {"body": "Teste de conexão Conectifisio! 🚀"}
}

res = requests.post(url, json=payload, headers=headers)
print(res.status_code)
print(res.json())
