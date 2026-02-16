import os
import json
import requests
import re
from flask import Flask, request, jsonify

app = Flask(__name__)

# ==========================================
# CONFIGURAÇÕES DE SEGURANÇA E CONEXÃO
# ==========================================

# 1. Use este token exatamente assim no painel "Meta Developers"
VERIFY_TOKEN = "conectifisio_2024_seguro"

# 2. URL Final do Wix (apontando para o domínio principal para evitar redirecionamentos)
WIX_WEBHOOK_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

def clean_cpf(cpf_str):
    """ Remove pontos e traços, deixando apenas os 11 números do CPF """
    return re.sub(r'\D', '', cpf_str)

def sync_to_wix(data):
    """ Envia os dados capturados para o Kanban do Wix CMS """
    try:
        print(f"Enviando dados de {data.get('from')} para o Wix...")
        response = requests.post(WIX_WEBHOOK_URL, json=data, timeout=15)
        
        if response.status_code == 200:
            print(f"Sucesso Wix: {response.json()}")
            return True
        else:
            print(f"Erro Wix {response.status_code}: Verifique se o site foi PUBLICADO.")
            return False
    except Exception as e:
        print(f"Falha de rede com Wix: {e}")
        return False

# ==========================================
# ROTA DE VERIFICAÇÃO (GET) - Para o Painel da Meta
# ==========================================

@app.route("/whatsapp", methods=["GET"])
def verify():
    """ Valida o webhook quando você clica em 'Verificar e Salvar' na Meta """
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("Webhook validado com sucesso pela Meta!")
        return challenge, 200
    
    print("Falha na validação do token Meta.")
    return "Token de verificação inválido", 403

# ==========================================
# ROTA DE MENSAGENS (POST) - O Cérebro do Bot
# ==========================================

@app.route("/whatsapp", methods=["POST"])
def webhook():
    """ Recebe as mensagens reais do WhatsApp e processa a Etapa 2 (Cadastro) """
    data = request.get_json()
    
    if not data or "entry" not in data:
        return jsonify({"status": "no_data"}), 200

    try:
        for entry in data["entry"]:
            for change in entry["changes"]:
                value = change["value"]
                
                if "messages" in value:
                    message = value["messages"][0]
                    phone = message["from"]
                    text = message.get("text", {}).get("body", "").strip()
                    
                    # 1. Identificar Unidade pelo número de destino
                    display_num = value["metadata"]["display_phone_number"]
                    unit = "Ipiranga" if "23629360" in display_num else "SCS"

                    # 2. Payload base para o Kanban
                    payload = {
                        "from": phone,
                        "text": text,
                        "unit": unit,
                        "status": "triagem" # Status padrão inicial
                    }

                    # 3. LÓGICA DE CAPTURA INTELIGENTE (Etapa 2)
                    
                    # DETECTAR CPF (Procura 11 números no texto)
                    cpf_candidato = clean_cpf(text)
                    if len(cpf_candidato) == 11:
                        payload["cpf"] = cpf_candidato
                        payload["status"] = "cadastro" # Move o card no Kanban automaticamente
                        print(f"CPF detectado: {cpf_candidato}")
                    
                    # DETECTAR EMAIL
                    if "@" in text and "." in text:
                        payload["email"] = text.lower()
                        print(f"Email detectado: {text}")

                    # 4. Sincronizar com o Wix
                    sync_to_wix(payload)

        return jsonify({"status": "received"}), 200

    except Exception as e:
        print(f"Erro crítico no processamento: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(port=5000)
