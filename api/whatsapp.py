import os, requests, json, traceback, firebase_admin
from flask import Flask, request, jsonify
from firebase_admin import credentials, firestore

app = Flask(__name__)

# Configurações
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "conectifisio_2024_seguro")

# Inicialização do Firebase (Correção para evitar o erro 403)
firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
db = None
if firebase_creds_json:
    try:
        if not firebase_admin._apps:
            cred_dict = json.loads(firebase_creds_json)
            if 'private_key' in cred_dict:
                cred_dict['private_key'] = cred_dict['private_key'].replace('\\n', '\n')
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("✅ Firebase Conectado com Sucesso!")
    except Exception as e:
        print(f"❌ Erro Firebase: {str(e)}")

@app.route("/api/whatsapp", methods=["GET", "POST"])
def handle_whatsapp():
    # --- DASHBOARD E VALIDAÇÃO (GET) ---
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        
        if request.args.get("action") == "get_patients" and db:
            docs = db.collection("PatientsKanban").stream()
            return jsonify({"items": [doc.to_dict() for doc in docs]}), 200
            
        return "Acesso Negado", 403

    # --- RECEBIMENTO E RESPOSTA (POST) ---
    try:
        data = request.get_json()
        print(f"📥 MENSAGEM RECEBIDA: {json.dumps(data)}") # Log para ver o que chega
        
        if data and "entry" in data:
            changes = data["entry"][0].get("changes", [{}])[0].get("value", {})
            if "messages" in changes:
                message = changes["messages"][0]
                phone = message["from"]
                
                # 1. Salvar no Firebase
                if db:
                    db.collection("PatientsKanban").document(phone).set({
                        "cellphone": phone, "lastInteraction": firestore.SERVER_TIMESTAMP, "status": "triagem"
                    }, merge=True)

                # 2. Tentar responder
                url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
                payload = {
                    "messaging_product": "whatsapp", "to": phone, "type": "text",
                    "text": {"body": "✅ Conectifisio Online! Recebi sua mensagem. Como posso ajudar?"}
                }
                res = requests.post(url, json=payload, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"})
                
                # LOG CRUCIAL: Ver o que a Meta diz sobre o nosso envio
                print(f"📤 RESPOSTA DA META AO ENVIO: {res.status_code} - {res.text}")
                
        return jsonify({"status": "ok"}), 200
    except:
        print(f"❌ ERRO NO PROCESSAMENTO: {traceback.format_exc()}")
        return jsonify({"status": "error"}), 200

if __name__ == "__main__":
    app.run(port=5000)
