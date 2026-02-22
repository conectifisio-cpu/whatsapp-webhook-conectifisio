import os
import json
import requests
import time
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# CONFIGURAÇÕES DE AMBIENTE (Devem estar na Vercel)
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
# URL Direta para evitar erro de variável 'None'
WIX_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"
GEMINI_API_KEY = "" # A Vercel injeta a chave automaticamente no ambiente

def chamar_gemini(query, system_prompt):
    """
    Integração com Gemini 2.5 Flash para análise de NLP (Queixas e FAQ).
    Implementa retentativas com backoff exponencial.
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": query}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]}
    }
    
    for delay in [1, 2, 4, 8, 16]:
        try:
            res = requests.post(url, json=payload, timeout=15)
            if res.status_code == 200:
                result = res.json()
                return result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
        except Exception:
            time.sleep(delay)
    return None

def enviar_whatsapp(to, payload_msg):
    """Função genérica para enviar mensagens via WhatsApp Cloud API"""
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        **payload_msg
    }
    return requests.post(url, json=payload, headers=headers, timeout=10)

def enviar_texto(to, texto):
    return enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "entry" not in data:
        return jsonify({"status": "ok"}), 200

    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" not in value:
            return jsonify({"status": "not_a_message"}), 200

        message = value["messages"][0]
        phone = message["from"]
        msg_type = message.get("type")
        
        # Extração da mensagem (texto ou botão)
        msg_recebida = ""
        if msg_type == "text":
            msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))

        # 1. SINCRONIZAÇÃO COM O WIX (Ponte de Dados)
        # O Wix retorna o status atual do paciente e se ele é veterano
        res_wix = requests.post(WIX_URL, json={"from": phone, "text": msg_recebida}, timeout=15)
        info_paciente = res_wix.json()
        status = info_paciente.get("currentStatus", "triagem")
        is_veteran = info_paciente.get("isVeteran", False)
        nome_paciente = info_paciente.get("patientName", "Paciente")

        # 2. LÓGICA DE IA: ANÁLISE DE QUEIXA (NLP)
        if status == "cadastrando_queixa":
            system_prompt = f"""
            Você é o assistente de triagem clínica da Conectifisio.
            O paciente {nome_paciente} está relatando um sintoma.
            Sua tarefa:
            1. Seja empático e acolhedor.
            2. Identifique a possível área (Coluna, Joelho, Neuro, etc).
            3. Responda apenas com a frase de acolhimento.
            """
            resumo_ia = chamar_gemini(msg_recebida, system_prompt)
            
            # Atualiza o Wix com a análise da IA
            requests.post(WIX_URL, json={
                "from": phone, 
                "queixa": msg_recebida, 
                "queixa_ia": resumo_ia, 
                "status": "escolha_especialidade"
            })
            
            enviar_texto(phone, resumo_ia if resumo_ia else "Entendi perfeitamente. Vamos cuidar disso agora.")
            # Aqui dispararia o menu de especialidades...

        # 3. LÓGICA DE FAQ / CONVERSA LIVRE
        elif status == "atendimento_humano":
            # Se estiver em atendimento humano, o robô não interfere a menos que seja um comando de reset
            if msg_recebida.lower() in ["reset", "recomeçar", "menu"]:
                requests.post(WIX_URL, json={"from": phone, "status": "triagem"})
                enviar_texto(phone, "Entendido! Vamos recomeçar o seu atendimento. 😊")
            return jsonify({"status": "human_mode"}), 200

        # ... (Restante da lógica de fluxo estruturado v100) ...

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"Erro Crítico: {e}")
        return jsonify({"status": "error", "message": str(e)}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    """Verificação do Webhook pela Meta"""
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Token Inválido", 403

if __name__ == "__main__":
    app.run(port=5000)
