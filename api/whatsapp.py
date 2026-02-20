import os
import json
import requests
import re
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES v33.5 - ULTRA ESTÁVEL
# ==========================================
# Estas variáveis devem estar configuradas no painel da Vercel
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
# URL do Webhook do Wix (Backend)
WIX_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

# --- MENUS E TEXTOS ESTRATÉGICOS ---
SERVICOS_MENU = (
    "1. Fisioterapia Ortopédica\n"
    "2. Fisioterapia Neurológica\n"
    "3. Fisioterapia Pélvica\n"
    "4. Acupuntura\n"
    "5. Pilates Studio\n"
    "6. Recovery / Liberação Miofascial"
)

# --- FUNÇÕES AUXILIARES ---
def send_whatsapp(to, text):
    """Envia uma mensagem de texto através da API Cloud do WhatsApp (Meta)"""
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
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"DEBUG META: Status {response.status_code}")
    except Exception as e:
        print(f"ERRO AO ENVIAR WHATSAPP: {e}")

def clean_cpf(text):
    """Remove caracteres não numéricos e valida se tem 11 dígitos"""
    nums = re.sub(r'\D', '', text)
    return nums if len(nums) == 11 else None

# ==========================================
# WEBHOOK PRINCIPAL
# ==========================================

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    
    # Validação inicial para evitar processar notificações de sistema/leitura
    if not data or "entry" not in data:
        return jsonify({"status": "no_data"}), 200

    try:
        entry = data["entry"][0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        
        if "messages" not in value:
            return jsonify({"status": "not_a_message"}), 200

        message = value["messages"][0]
        phone = message["from"]
        text = message.get("text", {}).get("body", "").strip()
        
        # Identificação da Unidade (Ipiranga ou SCS) baseada no número que recebeu a mensagem
        display_phone = value.get("metadata", {}).get("display_phone_number", "")
        unit = "Ipiranga" if "23629360" in display_phone else "SCS"

        # 1. COMUNICAÇÃO COM O WIX (Sincronização de Estado)
        # Enviamos a mensagem do paciente e recebemos o status actual e o nome (se existir)
        try:
            res_wix = requests.post(WIX_URL, json={"from": phone, "text": text, "unit": unit}, timeout=15)
            info = res_wix.json()
        except Exception as e:
            print(f"ERRO DE LIGAÇÃO AO WIX: {e}")
            return jsonify({"status": "wix_error"}), 200

        status = info.get("currentStatus", "triagem")
        p_name = info.get("patientName", "")
        p_modalidade = info.get("modalidade", "particular")

        # Se o Dashboard marcou como 'atendimento_humano', o robô silencia-se
        if status == "atendimento_humano":
            return jsonify({"status": "human_mode_active"}), 200

        # --- LÓGICA DA MÁQUINA DE ESTADOS (ACOLHIMENTO) ---
        reply = ""

        if status == "triagem":
            # Verifica se já temos o nome do paciente no banco de dados
            if p_name and p_name != "Paciente Novo":
                reply = f"Olá, {p_name}! Que bom falar consigo novamente na Conectifisio unidade {unit}! 😊\n\nDeseja iniciar um novo Plano de Tratamento hoje?"
                requests.post(WIX_URL, json={"from": phone, "status": "menu_veterano"})
            else:
                reply = f"Olá! ✨ Seja bem-vindo à Conectifisio unidade {unit}. Para iniciarmos o seu atendimento, como gostaria de ser chamado(a)?"
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome"})

        elif status == "cadastrando_nome":
            nome_limpo = text.title()
            reply = f"Prazer, {nome_limpo}! 😊 O que o(a) trouxe à clínica hoje? (Qual a sua dor ou queixa principal?)"
            requests.post(WIX_URL, json={"from": phone, "name": nome_limpo, "status": "cadastrando_queixa"})

        elif status == "cadastrando_queixa":
            # Captura a queixa (Escuta Ativa) e apresenta o menu de serviços
            reply = f"Entendido. Vamos analisar a melhor forma de ajudar. Qual serviço procura hoje?\n\n{SERVICOS_MENU}"
            requests.post(WIX_URL, json={"from": phone, "queixa": text, "status": "escolha_especialidade"})

        elif status == "escolha_especialidade":
            # Verificação de Triagem Neuro
            if "2" in text or "neuro" in text.lower():
                reply = "Para casos de Neurologia, como está a mobilidade do paciente? (Independente, Semidependente ou Dependente)"
                requests.post(WIX_URL, json={"from": phone, "status": "triagem_neuro", "servico": "Neurologia"})
            else:
                reply = "Perfeito! ✅ Deseja realizar o atendimento pelo seu CONVÉNIO ou de forma PARTICULAR?"
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade", "servico": text})

        elif status == "triagem_neuro":
            if "independente" in text.lower():
                reply = "Certo! ✅ Deseja atendimento pelo seu CONVÉNIO ou de forma PARTICULAR?"
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade"})
            else:
                # Silencia o robô para casos complexos
                reply = "O nosso fisioterapeuta responsável assumirá o contacto agora para lhe dar atenção total. 👨‍⚕️"
                requests.post(WIX_URL, json={"from": phone, "status": "atendimento_humano"})

        elif status == "escolha_modalidade":
            is_particular = "particular" in text.lower()
            modalidade = "particular" if is_particular else "convenio"
            
            if is_particular:
                reply = "No atendimento particular focamos na sua evolução total, com tempo e especialistas dedicados. Digite o seu CPF (apenas números)."
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_cpf", "modalidade": "particular"})
            else:
                reply = "Qual o nome do seu CONVÉNIO?"
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_convenio", "modalidade": "convenio"})

        elif status == "cadastrando_convenio":
            reply = "Anotado! Agora, por favor, digite o seu CPF (apenas números)."
            requests.post(WIX_URL, json={"from": phone, "convenio": text, "status": "cadastrando_cpf"})

        elif status == "cadastrando_cpf":
            cpf = clean_cpf(text)
            if cpf:
                reply = "CPF validado! Qual o período da sua preferência para agendamento: Manhã ou Tarde? 🕒"
                requests.post(WIX_URL, json={"from": phone, "cpf": cpf, "status": "agendando"})
            else:
                reply = "CPF inválido. Por favor, envie os 11 números novamente."

        elif status == "agendando":
            reply = "Recebido! 🎉 A nossa equipa entrará em contacto em instantes para confirmar o seu horário exacto. Até já!"
            send_whatsapp(phone, reply)
            requests.post(WIX_URL, json={"from": phone, "status": "finalizado"})

        # Envia a resposta final se houver
        if reply:
            send_whatsapp(phone, reply)
            
        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"ERRO CRÍTICO NO WEBHOOK: {e}")
        return jsonify({"status": "error_handled"}), 200

# Endpoint de Verificação (GET) exigido pela Meta para activar o Webhook
@app.route("/api/whatsapp", methods=["GET"])
def verify():
    # O Verify Token deve ser o mesmo configurado no painel de Developers da Meta
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Erro de Verificação", 403

if __name__ == "__main__":
    app.run(port=5000)
