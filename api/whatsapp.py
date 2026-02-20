import osimport os
import json
import requests
import re
import time
import random
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES v35.2 - HUMANIZADO BRASIL
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

# --- FUNÇÕES DE HUMANIZAÇÃO ---

def simular_digitacao(to, segundos=None):
    """
    Cria um delay proposital antes de enviar a resposta.
    Isso faz o paciente ver o status 'digitando...' no WhatsApp.
    """
    if segundos is None:
        segundos = random.uniform(2.0, 4.0)
    time.sleep(segundos)

# --- FUNÇÕES DE ENVIO (API META INTERATIVA) ---

def enviar_texto(to, texto):
    simular_digitacao(to)
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": texto}}
    requests.post(url, json=payload, headers=headers, timeout=10)

def enviar_botoes(to, texto, lista_botoes):
    """Envia botões de resposta rápida (Máximo 3)"""
    simular_digitacao(to)
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    
    botoes_payload = []
    for i, nome_botao in enumerate(lista_botoes):
        botoes_payload.append({
            "type": "reply",
            "reply": {"id": f"btn_{i}", "title": nome_botao}
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texto},
            "action": {"buttons": botoes_payload}
        }
    }
    requests.post(url, json=payload, headers=headers, timeout=10)

def enviar_lista(to, texto, etiqueta_botao, secoes):
    """Envia um menu suspenso (Lista) com até 10 opções"""
    simular_digitacao(to)
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": "Conectifisio"},
            "body": {"text": texto},
            "footer": {"text": "Toque no botão para escolher"},
            "action": {
                "button": etiqueta_botao,
                "sections": secoes
            }
        }
    }
    requests.post(url, json=payload, headers=headers, timeout=10)

# ==========================================
# WEBHOOK PRINCIPAL (LÓGICA DE ATENDIMENTO)
# ==========================================

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "entry" not in data: return jsonify({"status": "ok"}), 200

    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" not in value: return jsonify({"status": "not_msg"}), 200

        message = value["messages"][0]
        phone = message["from"]
        
        # Identifica se a entrada veio por texto livre ou por clique em botão/lista
        msg_recebida = ""
        if message["type"] == "text":
            msg_recebida = message["text"]["body"].strip()
        elif message["type"] == "interactive":
            inter = message["interactive"]
            if inter["type"] == "button_reply":
                msg_recebida = inter["button_reply"]["title"]
            elif inter["type"] == "list_reply":
                msg_recebida = inter["list_reply"]["title"]

        unit = "Ipiranga" if "23629360" in value.get("metadata", {}).get("display_phone_number", "") else "SCS"

        # 1. SINCRONIZAÇÃO COM O WIX CMS (Versão Otimizada)
        try:
            res_wix = requests.post(WIX_URL, json={"from": phone, "text": msg_recebida, "unit": unit}, timeout=15)
            info = res_wix.json()
        except:
            info = {"currentStatus": "triagem"}

        status = info.get("currentStatus", "triagem")

        # --- FLUXO DE CONVERSA HUMANIZADO (BRASIL) ---

        if status == "triagem":
            enviar_botoes(phone, 
                f"Olá! ✨ Seja muito bem-vindo à Conectifisio unidade {unit}.\n\nPara iniciarmos seu atendimento com toda atenção, você já é nosso paciente?",
                ["Sim, já sou", "Não, primeira vez"]
            )
            requests.post(WIX_URL, json={"from": phone, "status": "aguardando_identificacao"})

        elif status == "aguardando_identificacao":
            if "Sim" in msg_recebida:
                enviar_texto(phone, "Que bom ter você de volta! 😊 Para localizarmos sua ficha rapidamente, como gostaria de ser chamado(a)?")
            else:
                enviar_texto(phone, "Seja bem-vindo! ✨ Para darmos início ao seu cadastro e agendamento, como gostaria de ser chamado(a)?")
            requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome"})

        elif status == "cadastrando_nome":
            nome = msg_recebida.title()
            enviar_texto(phone, f"Prazer em te conhecer, {nome}! 😊\n\nConte-me um pouco: o que te trouxe à nossa clínica hoje? Qual sua principal queixa ou dor?")
            requests.post(WIX_URL, json={"from": phone, "name": nome, "status": "cadastrando_queixa"})

        elif status == "cadastrando_queixa":
            secoes = [{
                "title": "Nossas Especialidades",
                "rows": [
                    {"id": "s1", "title": "Fisio Ortopédica"},
                    {"id": "s2", "title": "Fisio Neurológica"},
                    {"id": "s3", "title": "Fisio Pélvica"},
                    {"id": "s4", "title": "Acupuntura"},
                    {"id": "s5", "title": "Pilates Studio"},
                    {"id": "s6", "title": "Outros / Recovery"}
                ]
            }]
            enviar_lista(phone, 
                "Entendido. Vamos cuidar disso! Qual dessas especialidades você procura hoje?", 
                "Ver Especialidades", 
                secoes
            )
            requests.post(WIX_URL, json={"from": phone, "queixa": msg_recebida, "status": "escolha_especialidade"})

        elif status == "escolha_especialidade":
            if "Neurológica" in msg_recebida:
                texto_neuro = (
                    "Olá! Tudo bem? 😊 Para darmos sequência ao seu agendamento de fisioterapia, "
                    "precisamos entender melhor seu grau de independência nas atividades do dia a dia.\n\n"
                    "Em qual dessas opções você se enquadra?\n\n"
                    "🔹 *Independente:* Realizo as atividades de forma autônoma e com segurança.\n\n"
                    "🤝 *Semidependente:* Consigo fazer algumas atividades sozinho(a), mas preciso de ajuda parcial ou de dispositivos auxiliares (bengala, andador).\n\n"
                    "👨‍🦽 *Dependente:* Preciso de ajuda total para me locomover e realizar atividades diárias."
                )
                enviar_botoes(phone, texto_neuro, ["Independente", "Semidependente", "Dependente"])
                requests.post(WIX_URL, json={"from": phone, "status": "triagem_neuro", "servico": "Neurologia"})
            else:
                enviar_botoes(phone,
                    "Perfeito! ✅ Como você deseja realizar o seu atendimento?",
                    ["Convênio", "Particular"]
                )
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade", "servico": msg_recebida})

        elif status == "triagem_neuro":
            if "Dependente" in msg_recebida:
                enviar_texto(phone, "Entendido. Como seu caso exige uma atenção especial, nosso fisioterapeuta responsável assumirá este contato agora para te dar suporte total. 👨‍⚕️")
                requests.post(WIX_URL, json={"from": phone, "status": "atendimento_humano"})
            else:
                enviar_botoes(phone, "Certo! ✅ Você deseja realizar o atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", ["Convênio", "Particular"])
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade"})

        elif status == "escolha_modalidade":
            modalidade = "particular" if "Particular" in msg_recebida else "convenio"
            if modalidade == "particular":
                enviar_texto(phone, "No atendimento particular focamos na sua evolução total, com tempo e especialistas dedicados. 😊\n\nPor favor, digite seu CPF (apenas números) para seu registro.")
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_cpf", "modalidade": "particular"})
            else:
                enviar_texto(phone, "Ótimo! Qual o nome do seu CONVÊNIO?")
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_convenio", "modalidade": "convenio"})

        elif status == "cadastrando_convenio":
            enviar_texto(phone, "Anotado! Agora, por favor, digite o seu CPF (apenas números).")
            requests.post(WIX_URL, json={"from": phone, "convenio": msg_recebida, "status": "cadastrando_cpf"})

        elif status == "cadastrando_cpf":
            enviar_botoes(phone, "CPF recebido! Qual o período da sua preferência para o agendamento?", ["Manhã", "Tarde"])
            requests.post(WIX_URL, json={"from": phone, "cpf": msg_recebida, "status": "agendando"})

        elif status == "agendando":
            enviar_texto(phone, "Recebido! 🎉 Nossa equipe já recebeu seus dados e entrará em contato em instantes para confirmar o horário exato. Até já!")
            requests.post(WIX_URL, json={"from": phone, "status": "finalizado"})

        return jsonify({"status": "success"}), 200

    except Exception as e:
        return jsonify({"status": "error"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Erro", 403
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
