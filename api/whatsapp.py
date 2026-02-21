import os
import json
import requests
import time
import re
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES v73.0 - ESCADA DE DADOS E FLUXOS COMPLETOS
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

def simular_digitacao(to):
    time.sleep(0.8)

def enviar_texto(to, texto):
    simular_digitacao(to)
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": texto}}
    requests.post(url, json=payload, headers=headers, timeout=10)

def enviar_botoes(to, texto, lista_botoes):
    simular_digitacao(to)
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    btns = [{"type": "reply", "reply": {"id": f"btn_{i}", "title": b}} for i, b in enumerate(lista_botoes[:3])]
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
        
        msg_recebida = ""
        if msg_type == "text":
            msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))

        unit = "Ipiranga" if "23629360" in value.get("metadata", {}).get("display_phone_number", "") else "SCS"

        # --- FAQ AUTOMÁTICO ---
        perguntas_frequentes = {
            "estacionamento": "Temos estacionamento conveniado logo em frente à unidade! 🚗",
            "localização": f"Nossa unidade {unit} fica numa localização privilegiada. Deseja a rota do Maps?",
            "horário": "Funcionamos de segunda a sexta, das 07h às 21h, e aos sábados das 08h às 12h. ⏰"
        }
        for chave, resposta in perguntas_frequentes.items():
            if chave in msg_recebida.lower():
                enviar_texto(phone, resposta)
                return jsonify({"status": "success"}), 200

        # --- COMANDO DE RESET PROFUNDO ---
        if msg_recebida.lower() in ["resetar tudo", "recomeçar", "sou novo"]:
            requests.post(WIX_URL, json={"from": phone, "status": "triagem", "name": "Paciente Novo", "cpf": ""})
            enviar_texto(phone, "🔄 Memória limpa com sucesso! O sistema tratará você como NOVO PACIENTE. Como podemos ajudar hoje?")
            return jsonify({"status": "success"}), 200

        # 1. CONSULTA AO WIX
        res_wix = requests.post(WIX_URL, json={"from": phone, "text": msg_recebida, "unit": unit}, timeout=15)
        info = res_wix.json()
        
        status = info.get("currentStatus", "triagem")
        p_name = info.get("patientName", "")
        is_veteran = info.get("isVeteran", False)
        p_modalidade = info.get("modalidade", "")

        # --- BOTÃO VOLTAR E CONTINUAR ---
        if msg_recebida in ["Menu Inicial", "⬅️ Voltar"]:
            requests.post(WIX_URL, json={"from": phone, "status": "triagem"})
            status = "triagem"

        elif msg_recebida == "Sim, continuar":
            prompts = {
                "aguardando_nome_novo": "Como gostaria de ser chamado(a)?",
                "escolha_especialidade": "Qual serviço você procura hoje?",
                "escolha_modalidade": "Deseja atendimento pelo CONVÊNIO ou PARTICULAR?",
                "cadastrando_data": "Qual sua DATA DE NASCIMENTO? (Ex: 15/05/1980)",
                "cadastrando_email": "Qual o seu melhor E-MAIL?",
                "cadastrando_queixa": "O que te trouxe à clínica hoje? (Sua dor ou queixa principal)",
                "cadastrando_cpf": "Digite o seu CPF (apenas números):",
                "cadastrando_convenio": "Qual o nome do seu CONVÊNIO?",
                "cadastrando_num_carteirinha": "Digite o NÚMERO DA SUA CARTEIRINHA:",
                "aguardando_carteirinha": "Por favor, envie uma FOTO da sua CARTEIRINHA:",
                "aguardando_pedido": "Por favor, envie uma FOTO do seu PEDIDO MÉDICO:",
                "pilates_caixa_nome": "Por favor, digite seu NOME COMPLETO:",
                "pilates_caixa_data": "Qual sua DATA DE NASCIMENTO? (Ex: 15/05/1980)",
                "pilates_caixa_email": "Qual o seu melhor E-MAIL?",
                "pilates_caixa_cpf": "Digite o seu CPF (apenas números):",
                "pilates_caixa_carteirinha": "Por favor, envie uma FOTO da sua CARTEIRINHA:",
                "pilates_caixa_pedido": "Por favor, envie uma FOTO do seu PEDIDO MÉDICO:",
                "pilates_aguardando_nome_particular": "Por favor, digite seu NOME COMPLETO:",
                "performance_nome": "Por favor, digite seu NOME COMPLETO:"
            }
            texto = prompts.get(status, "Por favor, continue de onde paramos.")
            enviar_texto(phone, f"Ótimo! 😊 {texto}")
            return jsonify({"status": "success"}), 200

        # --- DETECÇÃO DE SAUDAÇÃO INTELIGENTE ---
        is_greeting = False
        if msg_type == "text":
            msg_limpa = re.sub(r'[^\w\s]', '', msg_recebida.lower().strip())
            saudacoes = ["oi", "ola", "olá", "bom dia", "boa tarde", "boa noite", "oii", "oie"]
            for s in saudacoes:
                if s in msg_limpa and len(msg_limpa) <= 25:
                    is_greeting = True
                    break

        if is_greeting and status not in ["triagem", "menu_veterano", "finalizado"]:
            enviar_botoes(phone, "Olá! ✨ Notei que estávamos no meio do seu pedido. Podemos continuar?", ["Sim, continuar", "Recomeçar"])
            return jsonify({"status": "success"}), 200

        # ==========================================
        # 1. FLUXO DE NAVEGAÇÃO PRINCIPAL (INÍCIO)
        # ==========================================
        if status == "triagem":
            if is_veteran:
                txt = f"Olá, {p_name}! ✨ Que bom ter você de volta na Conectifisio unidade {unit}.\n\nComo posso facilitar seu dia hoje?"
                secoes = [{"title": "Opções", "rows": [
                    {"id": "v1", "title": "🗓️ Reagendar Sessão"}, {"id": "v2", "title": "🔄 Continuar Tratamento"},
                    {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v4", "title": "📁 Outras Solicitações"}
                ]}]
                enviar_lista(phone, txt, "Ver Opções", secoes)
                requests.post(WIX_URL, json={"from": phone, "status": "menu_veterano"})
            else:
                enviar_texto(phone, f"Olá! ✨ Seja muito bem-vindo à Conectifisio unidade {unit}.\n\nPara começarmos seu atendimento, como gostaria de ser chamado(a)?")
                requests.post(WIX_URL, json={"from": phone, "status": "aguardando_nome_novo"})

        elif status == "aguardando_nome_novo":
            nome_informado = msg_recebida.title()
            secoes = [{"title": "Serviços", "rows": [
                {"id": "s1", "title": "Fisio Ortopédica"}, {"id": "s2", "title": "Fisio Neurológica"},
                {"id": "s3", "title": "Fisio Pélvica"}, {"id": "s4", "title": "Pilates Studio"},
                {"id": "s5", "title": "Recovery"}, {"id": "s6", "title": "Liberação Miofascial"}
            ]}]
            enviar_lista(phone, f"Prazer em conhecer, {nome_informado}! 😊 Qual serviço você procura hoje?", "Ver Opções", secoes)
            requests.post(WIX_URL, json={"from": phone, "name": nome_informado, "status": "escolha_especialidade"})

        elif status == "menu_veterano":
            if "Novo Serviço" in msg_recebida:
                secoes = [{"title": "Serviços", "rows": [
                    {"id": "s1", "title": "Fisio Ortopédica"}, {"id": "s2", "title": "Fisio Neurológica"},
                    {"id": "s3", "title": "Fisio Pélvica"}, {"id": "s4", "title": "Pilates Studio"},
                    {"id": "s5", "title": "Recovery"}, {"id": "s6", "title": "Liberação Miofascial"},
                    {"id": "s0", "title": "⬅️ Voltar"}
                ]}]
                enviar_lista(phone, "Qual desses novos serviços você procura hoje?", "Ver Opções", secoes)
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_especialidade"})
            elif "Continuar Tratamento" in msg_recebida:
                enviar_botoes(phone, "As novas sessões serão pelo seu CONVÊNIO ou PARTICULAR?", ["Convênio", "Particular", "Menu Inicial"])
                requests.post(WIX_URL, json={"from": phone, "status": "veterano_escolha_modalidade"})

        # ==========================================
        # 2. ESCOLHA DE ESPECIALIDADE E TRIAGENS
        # ==========================================
        elif status == "escolha_especialidade":
            if "Pilates Studio" in msg_recebida:
                enviar_texto(phone, "Excelente escolha! 🧘‍♀️ O Pilates é fundamental para a correção postural e fortalecimento.")
                enviar_botoes(phone, "Como você pretende realizar as aulas?", ["Wellhub / Totalpass", "Saúde Caixa", "Plano Particular"])
                requests.post(WIX_URL, json={"from": phone, "status": "pilates_triagem_modalidade", "servico": "Pilates"})
            elif msg_recebida in ["Recovery", "Liberação Miofascial"]:
                enviar_texto(phone, f"O serviço de **{msg_recebida}** é focado em performance, realizado de forma **PARTICULAR**. ✨")
                if p_name and p_name != "Paciente Novo":
                    enviar_texto(phone, f"Anotado, {p_name.split()[0]}! Nossa equipe assumirá para encontrar o melhor horário. 👨‍⚕️")
                    requests.post(WIX_URL, json={"from": phone, "status": "atendimento_humano", "modalidade": "particular", "servico": msg_recebida})
                else:
                    enviar_texto(phone, "Para darmos sequência e passarmos os horários, por favor digite seu **NOME COMPLETO**:")
                    requests.post(WIX_URL, json={"from": phone, "status": "performance_nome", "modalidade": "particular", "servico": msg_recebida})
            elif "Neurológica" in msg_recebida:
                enviar_botoes(phone, "Como está a mobilidade do paciente?", ["Independente", "Semidependente", "Dependente"])
                requests.post(WIX_URL, json={"from": phone, "status": "triagem_neuro", "servico": "Neurologia"})
            else:
                enviar_botoes(phone, "Deseja atendimento pelo CONVÊNIO ou PARTICULAR?", ["Convênio", "Particular", "⬅️ Voltar"])
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade", "servico": msg_recebida})

        # ==========================================
        # 3. FLUXO GERAL (ORTOPEDIA / NEURO / PÉLVICA)
        # ==========================================
        elif status == "escolha_modalidade":
            mod_limpa = "convenio" if "Convênio" in msg_recebida or "Convénio" in msg_recebida else "particular"
            # Como ele já deu o nome no começo, vamos direto para a Data de Nascimento!
            if p_name and p_name != "Paciente Novo":
                enviar_texto(phone, f"Entendido, {p_name.split()[0]}! Para o seu cadastro, qual sua DATA DE NASCIMENTO? (Ex: 15/05/1980)")
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_data", "modalidade": mod_limpa})
            else:
                enviar_texto(phone, "Entendido! Para iniciarmos seu cadastro rápido, digite seu **NOME COMPLETO**:")
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome_completo", "modalidade": mod_limpa})

        elif status == "cadastrando_nome_completo":
            requests.post(WIX_URL, json={"from": phone, "name": msg_recebida.title(), "status": "cadastrando_data"})
            enviar_texto(phone, "Anotado! Qual sua DATA DE NASCIMENTO? (Ex: 15/05/1980)")

        elif status == "cadastrando_data":
            requests.post(WIX_URL, json={"from": phone, "birthDate": msg_recebida, "status": "cadastrando_email"})
            enviar_texto(phone, "Perfeito! Qual o seu melhor E-MAIL para enviarmos os lembretes?")

        elif status == "cadastrando_email":
            requests.post(WIX_URL, json={"from": phone, "email": msg_recebida, "status": "cadastrando_queixa"})
            enviar_texto(phone, "Obrigado! O que te trouxe à clínica hoje? (Sua dor ou queixa principal)")

        elif status == "cadastrando_queixa":
            requests.post(WIX_URL, json={"from": phone, "queixa": msg_recebida, "status": "cadastrando_cpf"})
            enviar_texto(phone, "Compreendido. Para finalizarmos a segurança do seu cadastro, digite o seu CPF (apenas números):")

        elif status == "cadastrando_cpf":
            if p_modalidade == "convenio":
                requests.post(WIX_URL, json={"from": phone, "cpf": msg_recebida, "status": "cadastrando_convenio"})
                enviar_texto(phone, "CPF recebido! Qual o nome do seu CONVÊNIO?")
            else:
                requests.post(WIX_URL, json={"from": phone, "cpf": msg_recebida, "status": "agendando"})
                enviar_botoes(phone, "Cadastro concluído! 🎉 Qual período você prefere para as sessões?", ["Manhã", "Tarde"])

        elif status == "cadastrando_convenio":
            requests.post(WIX_URL, json={"from": phone, "convenio": msg_recebida, "status": "cadastrando_num_carteirinha"})
            enviar_texto(phone, "Anotado! Agora, digite o NÚMERO DA SUA CARTEIRINHA.")

        elif status == "cadastrando_num_carteirinha":
            requests.post(WIX_URL, json={"from": phone, "numCarteirinha": msg_recebida, "status": "aguardando_carteirinha"})
            enviar_texto(phone, "Envie agora uma FOTO da sua CARTEIRINHA.")

        elif status == "aguardando_carteirinha":
            requests.post(WIX_URL, json={"from": phone, "status": "aguardando_pedido"})
            enviar_texto(phone, "Recebido! Agora, por favor, envie uma FOTO do seu PEDIDO MÉDICO.")

        elif status == "aguardando_pedido":
            requests.post(WIX_URL, json={"from": phone, "status": "agendando"})
            enviar_botoes(phone, "Documentos recebidos com sucesso! 🎉 Qual período você prefere para as sessões?", ["Manhã", "Tarde"])

        elif status == "agendando":
            requests.post(WIX_URL, json={"from": phone, "status": "finalizado", "queixa": f"[PERÍODO]: {msg_recebida}"})
            enviar_texto(phone, "Tudo pronto! Nossa equipe entrará em contato em instantes para confirmar o horário exato. Até já!")

        # ==========================================
        # 4. SUB-FLUXO EXCLUSIVO PILATES STUDIO
        # ==========================================
        elif status == "pilates_triagem_modalidade":
            if "Saúde Caixa" in msg_recebida:
                enviar_texto(phone, "Entendido! 🏦 Para o Saúde Caixa, é necessária autorização prévia e pedido médico.")
                if is_veteran:
                    enviar_texto(phone, "Como já temos seus dados, envie uma FOTO do seu PEDIDO MÉDICO atualizado para agilizarmos:")
                    requests.post(WIX_URL, json={"from": phone, "status": "pilates_caixa_pedido", "modalidade": "convenio", "convenio": "Saúde Caixa"})
                else:
                    # Aproveita o nome dado na triagem inicial!
                    if p_name and p_name != "Paciente Novo":
                        enviar_texto(phone, f"Perfeito, {p_name.split()[0]}! Para o seu cadastro, qual sua DATA DE NASCIMENTO? (Ex: 15/05/1980)")
                        requests.post(WIX_URL, json={"from": phone, "status": "pilates_caixa_data", "modalidade": "convenio", "convenio": "Saúde Caixa"})
                    else:
                        enviar_texto(phone, "Para iniciarmos seu cadastro rápido, por favor, digite seu **NOME COMPLETO**:")
                        requests.post(WIX_URL, json={"from": phone, "status": "pilates_caixa_nome", "modalidade": "convenio", "convenio": "Saúde Caixa"})
            
            elif "Particular" in msg_recebida:
                enviar_texto(phone, "No nosso estúdio você conta com fisioterapeutas especializados e equipamentos de ponta. ✨")
                if p_name and p_name != "Paciente Novo":
                    enviar_botoes(phone, f"Prazer, {p_name.split()[0]}! Gostaria de agendar uma **AULA EXPERIMENTAL**?", ["Sim, gostaria", "Não, quero começar"])
                    requests.post(WIX_URL, json={"from": phone, "status": "pilates_aula_experimental", "modalidade": "particular"})
                else:
                    enviar_texto(phone, "Para podermos passar os detalhes e agendar sua **AULA EXPERIMENTAL**, digite seu **NOME COMPLETO**:")
                    requests.post(WIX_URL, json={"from": phone, "status": "pilates_particular_nome", "modalidade": "particular"})
            
            elif "Wellhub" in msg_recebida or "Totalpass" in msg_recebida:
                enviar_texto(phone, "Ótimo! ✅ Aceitamos planos **Golden (Wellhub)** e **TP5 (Totalpass)**.")
                enviar_texto(phone, "Para sua autonomia, usamos o **App Next Fit** para agendamentos!\n📱 Android: bit.ly/app-android\n🍎 iPhone: bit.ly/app-iphone")
                if p_name and p_name != "Paciente Novo":
                    enviar_texto(phone, f"Para liberarmos seu acesso, {p_name.split()[0]}, digite seu **ID do Aplicativo**:")
                    requests.post(WIX_URL, json={"from": phone, "status": "pilates_parceria_dados"})
                else:
                    enviar_texto(phone, "Para liberarmos seu acesso, digite seu **NOME COMPLETO** e seu **ID do Aplicativo**:")
                    requests.post(WIX_URL, json={"from": phone, "status": "pilates_parceria_dados"})

        # --- A ESCADA DE DADOS: PILATES SAÚDE CAIXA (NOVO) ---
        elif status == "pilates_caixa_nome":
            requests.post(WIX_URL, json={"from": phone, "name": msg_recebida.title(), "status": "pilates_caixa_data"})
            enviar_texto(phone, f"Prazer, {msg_recebida.split()[0]}! Qual sua DATA DE NASCIMENTO? (Ex: 15/05/1980)")

        elif status == "pilates_caixa_data":
            requests.post(WIX_URL, json={"from": phone, "birthDate": msg_recebida, "status": "pilates_caixa_email"})
            enviar_texto(phone, "Anotado! Qual o seu melhor E-MAIL?")

        elif status == "pilates_caixa_email":
            requests.post(WIX_URL, json={"from": phone, "email": msg_recebida, "status": "pilates_caixa_cpf"})
            enviar_texto(phone, "Obrigado! Agora, digite o seu CPF (apenas números):")

        elif status == "pilates_caixa_cpf":
            requests.post(WIX_URL, json={"from": phone, "cpf": msg_recebida, "status": "pilates_caixa_carteirinha"})
            enviar_texto(phone, "CPF recebido! Agora, envie uma FOTO da sua CARTEIRINHA:")

        elif status == "pilates_caixa_carteirinha":
            requests.post(WIX_URL, json={"from": phone, "status": "pilates_caixa_pedido"})
            enviar_texto(phone, "Quase lá! Agora envie a FOTO do seu PEDIDO MÉDICO:")

        elif status == "pilates_caixa_pedido":
            requests.post(WIX_URL, json={"from": phone, "status": "atendimento_humano"})
            enviar_texto(phone, "Documentos e dados recebidos! 🎉 A nossa equipe assumirá agora para validar a sua autorização e agendar. Aguarde!")
            
        # --- OUTRAS FINALIZAÇÕES (Parceria / Particular / Performance) ---
        elif status == "pilates_parceria_dados":
            requests.post(WIX_URL, json={"from": phone, "status": "atendimento_humano", "queixa": f"[APP ID]: {msg_recebida}"})
            enviar_texto(phone, "ID recebido! Nossa equipe vai assumir o atendimento agora para liberar seu acesso inicial. Aguarde! 👩‍⚕️")

        elif status == "pilates_particular_nome":
            requests.post(WIX_URL, json={"from": phone, "name": msg_recebida.title(), "status": "atendimento_humano"})
            enviar_texto(phone, f"Prazer, {msg_recebida}! O Pilates vai transformar a sua postura! 😊")
            enviar_texto(phone, "A nossa equipe assumirá agora para agendar a sua **AULA EXPERIMENTAL**. Aguarde um instante!")
            
        elif status == "pilates_aula_experimental":
            requests.post(WIX_URL, json={"from": phone, "status": "atendimento_humano"})
            enviar_texto(phone, "Excelente! A nossa equipe assumirá agora para encontrar o melhor horário. Aguarde um instante! 👩‍⚕️")

        elif status == "performance_nome":
            requests.post(WIX_URL, json={"from": phone, "name": msg_recebida.title(), "status": "atendimento_humano"})
            enviar_texto(phone, "Anotado! A nossa equipe especializada assumirá o atendimento agora. 👨‍⚕️")

        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Erro", 403
