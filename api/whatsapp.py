import os
import json
import requests
import re
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES E TEXTOS ESTRATÉGICOS v33.0
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_URL = os.environ.get("WIX_WEBHOOK_URL")

# --- MENUS DE OPÇÕES ---
SERVICOS_MENU = (
    "1. Fisioterapia Ortopédica\n"
    "2. Fisioterapia Neurológica\n"
    "3. Fisioterapia Pélvica\n"
    "4. Acupuntura\n"
    "5. Pilates Studio\n"
    "6. Recovery / Liberação Miofascial"
)

# Estratégia de Valor (Não Preço)
MSG_VALOR_PARTICULAR = (
    "Entendi perfeitamente sua queixa; vamos avaliar a melhor forma de ajudar você. 😊\n\n"
    "Nosso foco é que você volte a se movimentar sem dor, com segurança e qualidade de vida. "
    "Nos atendimentos particulares conseguimos um plano individualizado, com atenção total à sua evolução. "
    "Trabalhamos com especialistas e tecnologia moderna.\n\n"
    "Trabalhamos com sessões avulsas e pacotes flexíveis. Quer que eu te mostre como funciona na prática?"
)

# Recomendações Finais (Convênio)
MSG_FINAL_CONVENIO = (
    "Sessão agendada! ✅\n\n"
    "Traga o pedido médico original e chegue com 15 minutos de antecedência.\n\n"
    "Em alguns convênios pode ser necessário o token de validação, que você recebe pelo celular "
    "após a solicitação do procedimento no plano de saúde.\n\n"
    "Qualquer dúvida, estou à disposição 😊"
)

# --- FUNÇÕES UTILITÁRIAS ---
def send_whatsapp(to, text):
    """Envia mensagem via API do WhatsApp Cloud"""
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
        requests.post(url, json=payload, headers=headers, timeout=10)
    except Exception as e:
        print(f"Erro ao enviar WhatsApp: {e}")

def extract_cpf(text):
    """Limpa o texto e verifica se contém 11 dígitos"""
    nums = re.sub(r'\D', '', text)
    return nums if len(nums) == 11 else None

# ==========================================
# WEBHOOK PRINCIPAL (MÁQUINA DE ESTADOS)
# ==========================================

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "entry" not in data:
        return jsonify({"status": "ok"}), 200

    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" in value:
            message = value["messages"][0]
            phone = message["from"]
            text = message.get("text", {}).get("body", "").strip()
            
            # Identificação da Unidade pelo número de destino
            display_phone = value.get("metadata", {}).get("display_phone_number", "")
            unit = "Ipiranga" if "23629360" in display_phone else "SCS"

            # 1. SINCRONIA COM WIX (Obtém estado atual do paciente)
            res_wix = requests.post(WIX_URL, json={"from": phone, "text": text, "unit": unit}, timeout=10)
            info = res_wix.json()
            
            status = info.get("currentStatus", "triagem")
            p_name = info.get("patientName", "")
            p_modalidade = info.get("modalidade", "particular")

            # Trava para Intervenção Humana (Dashboard)
            if status == "atendimento_humano":
                return jsonify({"status": "human_active"}), 200

            # --- FLUXO DE LÓGICA POR ESTADOS ---
            reply = ""

            # PASSO 1: BOAS-VINDAS / RECONHECIMENTO
            if status == "triagem":
                if p_name and p_name != "Paciente Novo":
                    reply = f"Olá, {p_name}! Que bom falar com você novamente aqui na Conectifisio unidade {unit}! 😊\n\nVocê já está em tratamento conosco no momento ou deseja iniciar um novo Plano de Tratamento?"
                    requests.post(WIX_URL, json={"from": phone, "status": "menu_veterano"})
                else:
                    reply = f"Olá! Tudo bem? ✨ Seja muito bem-vindo à Conectifisio unidade {unit}. Para iniciarmos seu atendimento, você já é paciente da nossa clínica?"
                    requests.post(WIX_URL, json={"from": phone, "status": "aguardando_identificacao"})

            # PASSO 2: IDENTIFICAÇÃO (SOU VETERANO OU NOVO)
            elif status == "aguardando_identificacao":
                if "sim" in text.lower() or "já" in text.lower() or "sou" in text.lower():
                    reply = "Que bom ter você de volta! 😊 Para localizarmos seu cadastro, como você gostaria de ser chamado(a)?"
                    requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome"})
                else:
                    reply = "Seja bem-vindo pela primeira vez! ✨ Para darmos início ao seu atendimento, como gostaria de ser chamado(a)?"
                    requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome"})

            # PASSO 3: ESCUTA ATIVA (A QUEIXA)
            elif status == "cadastrando_nome" or (status == "menu_veterano" and ("novo" in text.lower() or "iniciar" in text.lower())):
                nome_capturado = text.title() if status == "cadastrando_nome" else p_name
                reply = f"Prazer em conhecer você, {nome_capturado}! 😊 Me conte um pouco: o que te trouxe à Conectifisio hoje? (Qual sua dor ou queixa principal?)"
                requests.post(WIX_URL, json={"from": phone, "name": nome_capturado, "status": "cadastrando_queixa"})

            # PASSO 4: ESCOLHA DO SERVIÇO
            elif status == "cadastrando_queixa":
                reply = f"Entendi. Para te ajudarmos da melhor forma com essa questão, qual serviço você procura hoje na unidade {unit}?\n\n{SERVICOS_MENU}"
                requests.post(WIX_URL, json={"from": phone, "queixa": text, "status": "escolha_especialidade"})

            # PASSO 5: TRIAGEM NEURO E MODALIDADE
            elif status == "escolha_especialidade":
                servico_escolhido = text
                if "2" in text or "neuro" in text.lower():
                    reply = "Para direcionarmos para o especialista correto, como está a mobilidade do paciente? (Independente, Semidependente ou Dependente)"
                    requests.post(WIX_URL, json={"from": phone, "status": "triagem_neuro", "servico": "Neurologia"})
                else:
                    reply = "Entendido! ✅ Deseja realizar o atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?"
                    requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade", "servico": servico_escolhido})

            # PASSO 5.1: SEGURANÇA NEURO
            elif status == "triagem_neuro":
                if "independente" in text.lower():
                    reply = "Perfeito! ✅ Você deseja atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?"
                    requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade"})
                else:
                    reply = "Para casos que exigem suporte especializado, nosso fisioterapeuta responsável assumirá o contato agora para te dar atenção total. 👨‍⚕️"
                    requests.post(WIX_URL, json={"from": phone, "status": "atendimento_humano"})

            # PASSO 6: CONVÊNIO OU PARTICULAR (VALOR)
            elif status == "escolha_modalidade":
                if "particular" in text.lower():
                    reply = MSG_VALOR_PARTICULAR
                    requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_cpf", "modalidade": "particular"})
                else:
                    reply = "Combinado! Qual o nome do seu CONVÊNIO?"
                    requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_convenio", "modalidade": "convenio"})

            elif status == "cadastrando_convenio":
                reply = f"Certo, anotamos o convênio {text}. Agora, por favor, digite seu CPF (apenas números)."
                requests.post(WIX_URL, json={"from": phone, "convenio": text, "status": "cadastrando_cpf"})

            # PASSO 7: CPF E DADOS FINAIS
            elif status == "cadastrando_cpf":
                cpf_limpo = extract_cpf(text)
                if cpf_limpo:
                    if p_modalidade == "convenio":
                        reply = "CPF anotado! Para validarmos a cobertura, por favor, envie primeiro uma foto da sua CARTEIRINHA."
                        requests.post(WIX_URL, json={"from": phone, "cpf": cpf_limpo, "status": "aguardando_carteirinha"})
                    else:
                        reply = "CPF anotado! Qual o período da sua preferência: Manhã ou Tarde? 🕒"
                        requests.post(WIX_URL, json={"from": phone, "cpf": cpf_limpo, "status": "agendando"})
                else:
                    reply = "Não consegui validar o CPF. Pode enviar os 11 números novamente?"

            elif status == "aguardando_carteirinha":
                reply = "Obrigado! Agora, envie também uma foto do seu PEDIDO MÉDICO (emitido há até 60 dias)."
                requests.post(WIX_URL, json={"from": phone, "status": "aguardando_pedido"})

            elif status == "aguardando_pedido":
                reply = "Documentos recebidos! Para vermos a disponibilidade na agenda, qual sua preferência: Manhã ou Tarde? 🕒"
                requests.post(WIX_URL, json={"from": phone, "status": "agendando"})

            # PASSO 8: FINALIZAÇÃO
            elif status == "agendando":
                if p_modalidade == "convenio":
                    reply = MSG_FINAL_CONVENIO
                else:
                    reply = "Agendamento pré-confirmado! 🎉 Nossa equipe já recebeu seus dados e entrará em contato em instantes para confirmar o horário exato. Até já!"
                
                send_whatsapp(phone, reply)
                requests.post(WIX_URL, json={"from": phone, "status": "finalizado"})
                return jsonify({"status": "finished"}), 200

            # ENVIO DA RESPOSTA FINAL
            if reply:
                send_whatsapp(phone, reply)

        return jsonify({"status": "success"}), 200
    except Exception as e:
        print(f"Erro Crítico no Webhook: {e}")
        return jsonify({"error": str(e)}), 500

# Endpoint para verificação da Meta
@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Token de Verificação Inválido", 403
