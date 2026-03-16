import os
import requests
import json
import traceback
import firebase_admin
from flask import Flask, request, jsonify
from firebase_admin import credentials, firestore

app = Flask(__name__)

# Configurações simples
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "conectifisio_2024_seguro")

# Inicialização segura do Firebase
firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
db = None

if firebase_creds_json and not firebase_admin._apps:
    try:
        cred_dict = json.loads(firebase_creds_json)
        if 'private_key' in cred_dict:
            cred_dict['private_key'] = cred_dict['private_key'].replace('\\n', '\n')
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("✅ Firebase OK")
    except Exception as e:
        print(f"❌ Erro Firebase: {str(e)}")

@app.route("/api/whatsapp", methods=["GET", "POST"])
def handle_whatsapp():
    # --- VALIDAÇÃO (GET) ---
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        
        # Rota para o Dashboard ler os dados
        if request.args.get("action") == "get_patients" and db:
            docs = db.collection("PatientsKanban").stream()
            return jsonify({"items": [doc.to_dict() for doc in docs]}), 200
            
        return "Acesso Negado", 403

    # --- RECEBIMENTO (POST) ---
    try:
        data = request.get_json()
        
        # Verifica se é uma mensagem válida
        if data and "entry" in data:
            changes = data["entry"][0].get("changes", [{}])[0].get("value", {})
            if "messages" in changes:
                message = changes["messages"][0]
                phone = message["from"]
                
                # Salva no Firebase para o Dashboard ver
                if db:
                    db.collection("PatientsKanban").document(phone).set({
                        "cellphone": phone,
                        "lastInteraction": firestore.SERVER_TIMESTAMP,
                        "status": "triagem"
                    }, merge=True)

                # Resposta Direta de Teste
                url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
                headers = {
                    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
                    "Content-Type": "application/json"
                }
                payload = {
                    "messaging_product": "whatsapp",
                    "to": phone,
                    "type": "text",
                    "text": {"body": "✅ Conectifisio Online! Recebi sua mensagem. Como posso ajudar?"}
                }
                requests.post(url, json=payload, headers=headers)
                
        return jsonify({"status": "ok"}), 200

    except Exception:
        print(f"ERRO: {traceback.format_exc()}")
        return jsonify({"status": "error"}), 200

if __name__ == "__main__":
    app.run(port=5000)
