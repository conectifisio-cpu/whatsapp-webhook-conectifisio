import os
import json
import requests
import re
from flask import Flask, request, jsonify

app = Flask(__name__)

# ==========================================
# CONFIGURAÇÕES DE SEGURANÇA (VARIÁVEIS DE AMBIENTE)
# ==========================================
# Certifique-se de que estas chaves estão no painel "Settings > Environment Variables" da Vercel
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "conectifisio_2024_seguro")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_WEBHOOK_URL = os.environ.get("WIX_WEBHOOK_URL", "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook")

def send_reply(to, text):
    """ Tenta responder no WhatsApp e loga o resultado para depuração """
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        print("!!! ERRO CRÍTICO: WHATSAPP_TOKEN ou PHONE_NUMBER_ID não configurados na Vercel !!!")
        return False

    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"Log WhatsApp: Status {res.status_code} - Resposta: {res.text}")
        return res.status_code == 200
    except Exception as e:
        print(f"Erro ao responder no WhatsApp: {e}")
        return False

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    """ Validação obrigatória para o Painel da Meta (Verify Token) """
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅ Webhook Validado com Sucesso!")
        return challenge, 200
    return "Token de Verificação Inválido", 403

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    """ Recebe as mensagens do WhatsApp e envia para o Wix CMS """
    data = request.get_json()
    
    # Log completo do que a Meta está enviando (visível nos Logs da Vercel)
    print("--- Nova Mensagem Recebida ---")
    print(json.dumps(data, indent=2))

    if not data or "entry" not in data:
        return jsonify({"status": "no_data"}), 200

    try:
        # Extração segura dos dados da Meta
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]
        
        if "messages" in value:
            message = value["messages"][0]
            phone = message["from"]
            text = message.get("text", {}).get("body", "").strip()
            
            # Identificação Automática da Unidade pelo número de destino
            display_num = value.get("metadata", {}).get("display_phone_number", "")
            unit = "Ipiranga" if "23629360" in display_num else "SCS"

            print(f"Processando contato de {phone} para a unidade {unit}")

            # 1. Sincronização com o Wix CMS
            payload_wix = {
                "from": phone, 
                "text": text, 
                "unit": unit,
                "timestamp": value.get("messages", [{}])[0].get("timestamp")
            }
            
            try:
                res_wix = requests.post(WIX_WEBHOOK_URL, json=payload_wix, timeout=10)
                print(f"Status Wix CMS: {res_wix.status_code}")
            except Exception as wix_err:
                print(f"Erro ao conectar com Wix: {wix_err}")

            # 2. Resposta de Feedback para o Paciente
            reply_text = f"Olá! Recebemos sua mensagem na unidade {unit}. Um atendente falará com você em breve."
            send_reply(phone, reply_text)

        return jsonify({"status": "success"}), 200
    except Exception as e:
        print(f"Erro Crítico no Webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# Necessário para a Vercel identificar a aplicação Flask
app.debug = False
