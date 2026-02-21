import os
import json
import requests
import time
import random
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES v43.1 - FOCO EM HUMANIZAÇÃO
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

# --- MOTOR DE HUMANIZAÇÃO (SIMULAÇÃO DE DIGITAÇÃO) ---

def simular_digitacao(to):
    """
    Simula o tempo que um humano levaria para digitar e enviar a mensagem.
    Isso reduz a percepção de 'robô' e gera mais proximidade.
    """
    # Atraso entre 2.5 e 4.5 segundos
    atraso = random.uniform(2.5, 4.5)
    time.sleep(atraso)

# --- FUNÇÕES DE ENVIO (API META INTERATIVA) ---

def enviar_texto(to, texto):
    simular_digitacao(to)
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": texto}}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
    except: pass

def enviar_botoes(to, texto, lista_botoes):
    simular_digitacao(to)
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    btns = [{"type": "reply", "reply": {"id": f"btn_{i}", "title": b}} for i, b in enumerate(lista_botoes)]
    payload = {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {"type": "button", "body": {"text": texto}, "action": {"buttons": btns}}
    }
    requests.post(url, json=payload, headers=headers)

def enviar_lista(to, texto, label, secoes):
    simular_digitacao(to)
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {
            "type": "list", "header": {"type": "text", "text": "Conectifisio"}, 
            "body": {"text": texto}, "action": { "button": label, "sections": secoes }
        }
    }
    requests.post(url, json=payload, headers=headers)

# ==========================================
# WEBHOOK PRINCIPAL (ESTRITO AO MAPA DR. ISSA)
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
        msg_type = message.get("type")
        
        # Extração inteligente do conteúdo recebido
        msg_recebida = ""
        if msg_type == "text":
            msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))
        elif msg_type == "image":
            msg_recebida = "[FOTO_DOCUMENTO]"

        unit = "Ipiranga" if "23629360" in value.get("metadata", {}).get("display_phone_number", "") else "SCS"

        # Sincronização com o Wix para obter o estado e dados do paciente
        res_wix = requests.post(WIX_URL, json={"from": phone, "text": msg_recebida, "unit": unit}, timeout=15)
        info = res_wix.json()
        
        status = info.get("currentStatus", "triagem")
        p_name = info.get("patientName", "")
        p_modalidade = info.get("modalidade", "").lower()

        # --- FLUXO DE ATENDIMENTO v43.1 ---

        # 1. SAUDAÇÃO INICIAL (PASSO 1 DO MAPA)
        if status == "triagem":
            enviar_texto(phone, f"Olá! ✨ Seja muito bem-vindo à Conectifisio unidade {unit}.\n\nPara começarmos o seu atendimento da melhor forma, como você gostaria de ser chamado(a)?")
            requests.post(WIX_URL, json={"from": phone, "status": "aguardando_nome_inicial"})

        # 2. IDENTIFICAÇÃO (PASSO 2 DO MAPA)
        elif status == "aguardando_nome_inicial":
            nome_curto = msg_recebida.title()
            enviar_botoes(phone, 
                f"Prazer, {nome_curto}! 😊\n\nVocê já é nosso paciente ou é a sua primeira vez conosco?",
                ["Sim, já sou", "Não, primeira vez"]
            )
            requests.post(WIX_URL, json={"from": phone, "name": nome_curto, "status": "aguardando_identificacao"})

        # 3. BIFURCAÇÃO (NOVO VS VETERANO)
        elif status == "aguardando_identificacao":
            if "Sim" in msg_recebida:
                # VETERANO: Reconhece nome salvo no banco
                nome_db = p_name if p_name and p_name != "Paciente Novo" else "Amigo(a)"
                enviar_botoes(phone, 
                    f"Que bom ter você de volta, {nome_db}! 😊 Como posso te ajudar hoje?",
                    ["Retomar tratamento", "Novo pacote", "Outro assunto"]
                )
                requests.post(WIX_URL, json={"from": phone, "status": "menu_veterano"})
            else:
                # NOVO PACIENTE: Valor primeiro (Especialidades separadas)
                secoes = [{"title": "Nossos Serviços", "rows": [
                    {"id": "s1", "title": "Fisio Ortopédica"}, {"id": "s2", "title": "Fisio Neurológica"},
                    {"id": "s3", "title": "Fisio Pélvica"}, {"id": "s4", "title": "Pilates Studio"},
                    {"id": "s5", "title": "Recovery"}, {"id": "s6", "title": "Liberação Miofascial"}
                ]}]
                enviar_lista(phone, "Seja muito bem-vindo! ✨ Qual desses serviços você procura hoje?", "Ver Opções", secoes)
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_especialidade"})

        elif status == "menu_veterano":
            enviar_botoes(phone, "Entendido! Em qual período você tem preferência para agendar o seu retorno?", ["Manhã", "Tarde"])
            requests.post(WIX_URL, json={"from": phone, "status": "agendando", "servico": msg_recebida})

        # 4. MODALIDADE (CONVÊNIO OU PARTICULAR)
        elif status == "escolha_especialidade":
            servico = msg_recebida
            if "Neurológica" in servico:
                texto_neuro = ("Excelente escolha. 😊 Para sua segurança, como está a mobilidade do paciente?\n\n🔹 *Independente*\n🤝 *Semidependente*\n👨‍🦽 *Dependente*")
                enviar_botoes(phone, texto_neuro, ["Independente", "Semidependente", "Dependente"])
                requests.post(WIX_URL, json={"from": phone, "status": "triagem_neuro", "servico": "Neurologia"})
            elif servico in ["Recovery", "Liberação Miofascial"]:
                # Serviços exclusivamente particulares
                enviar_texto(phone, f"Ótima escolha! O serviço de {servico} é focado em performance e bem-estar. 😊")
                enviar_texto(phone, "Vamos agora realizar seu cadastro rápido para o agendamento.\n\nPor favor, digite seu **NOME COMPLETO** (conforme documento):")
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome_completo", "modalidade": "particular", "servico": servico})
            else:
                enviar_botoes(phone, f"Perfeito! ✅ Você deseja atendimento de {servico} pelo seu CONVÊNIO ou de forma PARTICULAR?", ["Convênio", "Particular"])
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade", "servico": servico})

        elif status == "triagem_neuro":
            if "Dependente" in msg_recebida:
                enviar_texto(phone, "Nosso fisioterapeuta responsável assumirá este contato agora para te dar atenção total e orientar os próximos passos. 👨‍⚕️")
                requests.post(WIX_URL, json={"from": phone, "status": "atendimento_humano"})
            else:
                enviar_botoes(phone, "Certo! ✅ Você deseja realizar o atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", ["Convênio", "Particular"])
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade"})

        elif status == "escolha_modalidade":
            mod_limpa = "convenio" if "Convênio" in msg_recebida else "particular"
            enviar_texto(phone, "Entendido! Vamos realizar seu cadastro rápido para o agendamento.\n\nPor favor, digite agora o seu **NOME COMPLETO** (conforme documento):")
            requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome_completo", "modalidade": mod_limpa})

        # 5. CADASTRO (QUEBRA DE RESISTÊNCIA)
        elif status == "cadastrando_nome_completo":
            enviar_texto(phone, "Qual a sua DATA DE NASCIMENTO? (Ex: 15/05/1980)")
            requests.post(WIX_URL, json={"from": phone, "name": msg_recebida.title(), "status": "cadastrando_data"})

        elif status == "cadastrando_data":
            enviar_texto(phone, "Qual o seu melhor E-MAIL para enviarmos os lembretes das sessões?")
            requests.post(WIX_URL, json={"from": phone, "birthDate": msg_recebida, "status": "cadastrando_email"})

        elif status == "cadastrando_email":
            enviar_texto(phone, "Conte-me um pouco: o que te trouxe à nossa clínica hoje? (Qual sua dor ou queixa?)")
            requests.post(WIX_URL, json={"from": phone, "email": msg_recebida, "status": "cadastrando_queixa"})

        elif status == "cadastrando_queixa":
            enviar_texto(phone, "Obrigado por compartilhar! 😊 Agora, digite seu CPF (apenas números) para registro.")
            requests.post(WIX_URL, json={"from": phone, "queixa": msg_recebida, "status": "cadastrando_cpf"})

        # 6. ELEGIBILIDADE E FINALIZAÇÃO
        elif status == "cadastrando_cpf":
            if p_modalidade == "convenio":
                enviar_texto(phone, "CPF recebido! Qual o nome do seu CONVÊNIO?")
                requests.post(WIX_URL, json={"from": phone, "cpf": msg_recebida, "status": "cadastrando_convenio"})
            else:
                enviar_botoes(phone, "Cadastro concluído! Qual período você prefere para o agendamento?", ["Manhã", "Tarde"])
                requests.post(WIX_URL, json={"from": phone, "cpf": msg_recebida, "status": "agendando"})

        elif status == "cadastrando_convenio":
            enviar_texto(phone, "Para agilizarmos sua autorização, digite o NÚMERO DA SUA CARTEIRINHA.")
            requests.post(WIX_URL, json={"from": phone, "convenio": msg_recebida, "status": "cadastrando_num_carteirinha"})

        elif status == "cadastrando_num_carteirinha":
            enviar_texto(phone, "Anotado! Agora, por favor, envie uma FOTO da sua CARTEIRINHA.")
            requests.post(WIX_URL, json={"from": phone, "numCarteirinha": msg_recebida, "status": "aguardando_carteirinha"})

        elif status == "aguardando_carteirinha":
            enviar_texto(phone, "Recebido! Por fim, envie uma FOTO do seu PEDIDO MÉDICO (emitido há menos de 60 dias).")
            requests.post(WIX_URL, json={"from": phone, "status": "aguardando_pedido"})

        elif status == "aguardando_pedido":
            enviar_botoes(phone, "Documentos recebidos! 🎉 Qual período você prefere para o agendamento?", ["Manhã", "Tarde"])
            requests.post(WIX_URL, json={"from": phone, "status": "agendando"})

        elif status == "agendando":
            enviar_texto(phone, "Tudo pronto! 🎉 Nossa equipe já recebeu seus dados e entrará em contato em instantes para confirmar o horário exato. Até já!")
            requests.post(WIX_URL, json={"from": phone, "status": "finalizado"})

        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Erro", 403

if __name__ == "__main__":
    app.run(port=5000)
