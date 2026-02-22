import os
import json
import requests
import time
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES DE AMBIENTE
# ==========================================
# O ambiente fornece estas chaves automaticamente
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
API_KEY = "" # Chave de API injetada no runtime
WIX_WEBHOOK_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

# ==========================================
# FUNÇÕES DE APOIO (IA E COMUNICAÇÃO)
# ==========================================

def chamar_gemini(query, system_prompt):
    """
    Integração com Gemini 2.5 Flash para análise de NLP.
    Retorna o texto processado ou None em caso de falha.
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": query}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]}
    }
    
    # Implementação de backoff exponencial conforme requisitos
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
    """Envia requisição para a Meta Cloud API"""
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        **payload_msg
    }
    return requests.post(url, json=payload, headers=headers, timeout=10)

def responder_texto(to, texto):
    return enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})

# ==========================================
# WEBHOOK PRINCIPAL
# ==========================================

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
        
        # 1. CAPTURA DA MENSAGEM (Texto ou Botão)
        msg_recebida = ""
        if msg_type == "text":
            msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))

        # 2. SINCRONIZAÇÃO COM WIX (O Wix é o cérebro do estado)
        # Enviamos a mensagem para o Wix e ele nos diz o status atual do paciente
        sync_payload = {"from": phone, "text": msg_recebida}
        res_wix = requests.post(WIX_WEBHOOK_URL, json=sync_payload, timeout=15)
        
        if res_wix.status_code != 200:
            return jsonify({"status": "wix_error"}), 200
            
        info = res_wix.json()
        status = info.get("currentStatus", "triagem")
        is_veteran = info.get("isVeteran", False)
        nome_paciente = info.get("patientName", "Paciente")

        # 3. LÓGICA DE INTELIGÊNCIA ARTIFICIAL (NLP)
        # Se o paciente estiver na fase de contar o que sente (Queixa)
        if status == "cadastrando_queixa":
            prompt_ia = f"""
            Você é o assistente clínico da Conectifisio. O paciente {nome_paciente} relatou: "{msg_recebida}".
            Sua tarefa:
            1. Analise a queixa e seja extremamente empático e acolhedor.
            2. Confirme que entendeu o local da dor (ex: "Sinto muito que sua lombar esteja incomodando").
            3. Não forneça diagnósticos.
            4. Responda em apenas uma frase curta e humana.
            """
            analise_ia = chamar_gemini(msg_recebida, prompt_ia)
            
            # Devolvemos ao Wix o resumo da IA para o Dashboard
            requests.post(WIX_WEBHOOK_URL, json={
                "from": phone, 
                "queixa": msg_recebida, 
                "queixa_ia": analise_ia, 
                "status": "escolha_especialidade"
            })
            
            responder_texto(phone, analise_ia if analise_ia else "Entendido. Vamos cuidar disso agora mesmo.")
            # O robô seguirá para o menu de especialidades no próximo passo

        # 4. LÓGICA DE FAQ / CONVERSA LIVRE (FALLBACK)
        elif status == "atendimento_humano":
            # Se um humano assumiu, o robô só obedece ao comando de RESET
            if msg_recebida.lower() in ["resetar", "recomeçar", "menu"]:
                requests.post(WIX_WEBHOOK_URL, json={"from": phone, "status": "triagem"})
                responder_texto(phone, "Entendido! Vamos recomeçar o seu atendimento. 😊")
            return jsonify({"status": "human_mode"}), 200

        # Caso contrário, o robô segue a estrutura de botões (Fases 1 a 6)
        # ... (O restante da lógica de botões é processada aqui) ...

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"DEBUG ERRO: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    """Verificação obrigatória do Webhook pela Meta"""
    hub_mode = request.args.get("hub.mode")
    hub_token = request.args.get("hub.verify_token")
    hub_challenge = request.args.get("hub.challenge")
    
    if hub_mode == "subscribe" and hub_token == "conectifisio_2024_seguro":
        return hub_challenge, 200
    return "Token Inválido", 403

if __name__ == "__main__":
    app.run(port=5000)
