import os, requests, json, firebase_admin
from flask import Flask, request, jsonify
from flask_cors import CORS
from firebase_admin import credentials, firestore

app = Flask(__name__)
CORS(app)

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "conectifisio_2024_seguro")

# Firebase simples
firebase_creds = os.environ.get("FIREBASE_CREDENTIALS")
if firebase_creds and not firebase_admin._apps:
    cred = credentials.Certificate(json.loads(firebase_creds))
    firebase_admin.initialize_app(cred)
db = firestore.client() if firebase_admin._apps else None

@app.route("/api/whatsapp", methods=["GET", "POST"])
def handle_whatsapp():
    if request.method == "GET":
        # Handshake da Meta
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        
        # Comando de Inscrição (WABA)
        if request.args.get("subscribe_app") == "sim":
            waba_id = request.args.get("waba_id")
            res = requests.post(f"https://graph.facebook.com/v19.0/{waba_id}/subscribed_apps", 
                                headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"})
            return jsonify(res.json()), 200

        return "Acesso Negado", 403

    # --- RECEBENDO MENSAGEM ---
    data = request.get_json()
    print(f"DEBUG DATA: {json.dumps(data)}") # Isso vai mostrar a mensagem real nos logs

    try:
        # Tenta pegar o número de quem enviou
        message_obj = data["entry"][0]["changes"][0]["value"]["messages"][0]
        phone = message_obj["from"]
        
        # Resposta de Teste "Pé na Porta" (Ignora filtros)
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "text",
            "text": {"body": "✅ Conectifisio Online! O cano está desentupido. O que deseja?"}
        }
        
        res = requests.post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",
                            json=payload, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"})
        
        print(f"RESPOSTA DA META AO ENVIO: {res.text}")
        return jsonify({"status": "sent"}), 200
    except Exception as e:
        print(f"ERRO NO PROCESSAMENTO: {str(e)}")
        return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(port=5000)
