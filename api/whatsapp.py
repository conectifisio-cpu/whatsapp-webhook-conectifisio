import os
import requests
import traceback
import re
import json
import base64
from flask import Flask, request, jsonify
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES DE AMBIENTE (Vercel)
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
API_KEY = os.environ.get("GEMINI_API_KEY", "")
FEEGOW_TOKEN = os.environ.get("FEEGOW_TOKEN", "")

# ==========================================
# INICIALIZAÇÃO DO FIREBASE
# ==========================================
firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
if firebase_creds_json and not firebase_admin._apps:
    try:
        cred_dict = json.loads(firebase_creds_json, strict=False)
        if 'private_key' in cred_dict:
            cred_dict['private_key'] = cred_dict['private_key'].replace('\\n', '\n')
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Inicializado com Sucesso!")
    except Exception as e:
        print(f"❌ Erro Crítico ao carregar Firebase: {e}")

db = firestore.client() if firebase_admin._apps else None

# ==========================================
# FUNÇÕES DE MEMÓRIA (FIREBASE)
# ==========================================
def get_paciente(phone):
    if not db: return {}
    doc = db.collection("PatientsKanban").document(phone).get()
    return doc.to_dict() if doc.exists else {}

def update_paciente(phone, data):
    if not db: return
    data["lastInteraction"] = firestore.SERVER_TIMESTAMP
    db.collection("PatientsKanban").document(phone).set(data, merge=True)

# ==========================================
# FUNÇÕES DO FEEGOW (DADOS + CONVÊNIO + FOTOS)
# ==========================================
def formatar_data_feegow(data_br):
    """Garante o formato YYYY-MM-DD mesmo se o paciente digitar sem barras"""
    data_limpa = re.sub(r'\D', '', str(data_br))
    if len(data_limpa) == 8:
        return f"{data_limpa[4:]}-{data_limpa[2:4]}-{data_limpa[:2]}"
    return data_br

def mapear_convenio(nome):
    nome_upper = str(nome).upper()
    if "BRADESCO" in nome_upper and "OPERADORA" in nome_upper: return 5
    if "BRADESCO" in nome_upper: return 2
    if "AMIL" in nome_upper: return 3
    if "PORTO SEGURO" in nome_upper: return 4
    if "GEAP" in nome_upper: return 6
    if "PREVENT" in nome_upper: return 7
    if "CASSI" in nome_upper: return 8
    if "PETROBRAS" in nome_upper: return 11
    if "MEDISERVICE" in nome_upper: return 9968
    if "CAIXA" in nome_upper: return 10154
    return 0

def baixar_midia_whatsapp(media_id):
    """Baixa a foto do WhatsApp da Meta e converte para Base64 para o Feegow"""
    if not media_id or not WHATSAPP_TOKEN: return None
    try:
        url_info = f"https://graph.facebook.com/v18.0/{media_id}"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        res_info = requests.get(url_info, headers=headers, timeout=10)
        
        if res_info.status_code != 200: 
            print(f"Erro ao buscar media info: {res_info.text}")
            return None
        
        media_url = res_info.json().get("url")
        mime_type = res_info.json().get("mime_type", "image/jpeg")
        
        res_download = requests.get(media_url, headers=headers, timeout=15)
        if res_download.status_code != 200: 
            print("Erro no download da imagem da Meta")
            return None
        
        b64_data = base64.b64encode(res_download.content).decode('utf-8')
        return f"data:{mime_type};base64,{b64_data}"
    except Exception as e:
        print(f"Erro ao baixar foto da Meta: {e}")
        return None

def integrar_feegow(phone, info):
    if not FEEGOW_TOKEN: return {"feegow_status": "Token Ausente"}
    
    cpf = re.sub(r'\D', '', info.get("cpf", ""))
    if len(cpf) != 11: return {"feegow_status": "CPF Inválido"}

    celular = re.sub(r'\D', '', phone)
    if celular.startswith("55") and len(celular) > 11:
        celular = celular[2:]

    headers = {"Content-Type": "application/json", "x-access-token": FEEGOW_TOKEN}
    base_url = "https://api.feegow.com/v1/api"
    paciente_id = None
    
    # 1. BUSCAR PACIENTE
    try:
        res_search = requests.get(f"{base_url}/patient/search?paciente_cpf={cpf}&photo=false", headers=headers, timeout=10)
        if res_search.status_code == 200:
            dados = res_search.json()
            if dados.get("success") != False and dados.get("content"):
                paciente_id = dados.get("content", {}).get("paciente_id") or dados.get("content", {}).get("id")
    except Exception as e:
        print(f"Erro na busca Feegow: {e}")

    # 2. CRIAR PACIENTE SE NÃO EXISTIR
    if not paciente_id:
        # Só envia os dados que realmente existem (evita erros de "Email inválido" por mandar vazio)
        payload_create = {
            "nome_completo": info.get("title", "Paciente Sem Nome"),
            "cpf": cpf,
            "data_nascimento": formatar_data_feegow(info.get("birthDate", "")),
            "celular1": celular
        }
        
        email = info.get("email", "").strip()
        if email and "@" in email:
            payload_create["email1"] = email

        try:
            res_create = requests.post(f"{base_url}/patient/create", json=payload_create, headers=headers, timeout=10)
            dados = res_create.json()
            if res_create.status_code == 200 and dados.get("success") != False:
                paciente_id = dados.get("content", {}).get("paciente_id") or dados.get("paciente_id")
            else:
                erro_msg = dados.get("message", str(dados))
                print(f"Erro Create Feegow: {erro_msg}")
                return {"feegow_status": f"Falha Cadastro: {erro_msg}"}
        except Exception as e:
            print(f"Erro fatal criação Feegow: {e}")
            return {"feegow_status": "Falha de Conexão Feegow"}

    # 3. SE O PACIENTE FOI ENCONTRADO/CRIADO -> SALVAR CONVÊNIO E FOTOS
    if paciente_id:
        paciente_id_int = int(paciente_id) # Garante que é número
        convenio_id = mapear_convenio(info.get("convenio", ""))
        matricula = info.get("numCarteirinha", "")
        
        # A. Atualizar Convênio e Carteirinha
        if convenio_id > 0:
            try:
                payload_edit = {
                    "paciente_id": paciente_id_int,
                    "convenio_id": convenio_id,
                    "plano_id": 0,
                    "matricula": matricula
                }
                requests.post(f"{base_url}/patient/edit", json=payload_edit, headers=headers, timeout=10)
            except Exception as e: pass

        # B. Fazer Upload das Fotos
        carteirinha_id = info.get("carteirinha_media_id")
        pedido_id = info.get("pedido_media_id")
        fotos_enviadas = []

        if carteirinha_id:
            try:
                b64_cart = baixar_midia_whatsapp(carteirinha_id)
                if b64_cart:
                    res_cart = requests.post(f"{base_url}/patient/upload-base64", json={"paciente_id": paciente_id_int, "arquivo_descricao": "Carteirinha (Robô)", "base64_file": b64_cart}, headers=headers, timeout=15)
                    if res_cart.status_code == 200 and res_cart.json().get("success") != False:
                        fotos_enviadas.append("Carteirinha")
                    else:
                        print(f"Erro Feegow ao salvar carteirinha: {res_cart.text}")
            except Exception as e: print(f"Erro upload carteirinha: {e}")

        if pedido_id:
            try:
                b64_pedido = baixar_midia_whatsapp(pedido_id)
                if b64_pedido:
                    res_ped = requests.post(f"{base_url}/patient/upload-base64", json={"paciente_id": paciente_id_int, "arquivo_descricao": "Pedido Médico (Robô)", "base64_file": b64_pedido}, headers=headers, timeout=15)
                    if res_ped.status_code == 200 and res_ped.json().get("success") != False:
                        fotos_enviadas.append("Pedido")
                    else:
                        print(f"Erro Feegow ao salvar pedido: {res_ped.text}")
            except Exception as e: print(f"Erro upload pedido: {e}")

        status_final = f"ID: {paciente_id_int}"
        if fotos_enviadas: status_final += f" | Anexos: {', '.join(fotos_enviadas)}"
        
        return {"feegow_id": paciente_id_int, "feegow_status": status_final}
        
    return {"feegow_status": "Erro na Integração"}

# ==========================================
# FUNÇÕES DE MENSAGERIA E IA
# ==========================================
def chamar_gemini(query, system_prompt):
    if not API_KEY: return None
    query_segura = query[:300]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={API_KEY}"
    payload = {"contents": [{"parts": [{"text": query_segura}]}], "systemInstruction": {"parts": [{"text": system_prompt}]}}
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            return res.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
    except: pass
    return None

def enviar_whatsapp(to, payload_msg):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, **payload_msg}
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        return res
    except Exception: 
        return None

def responder_texto(to, texto):
    return enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})

def enviar_botoes(to, texto, botoes):
    payload = {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texto},
            "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}} for b in botoes]}
        }
    }
    return enviar_whatsapp(to, payload)

def enviar_lista(to, texto, titulo_botao, secoes):
    payload = {
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": texto},
            "action": {"button": titulo_botao[:20], "sections": secoes}
        }
    }
    return enviar_whatsapp(to, payload)

# ==========================================
# WEBHOOK POST
# ==========================================
@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "entry" not in data: return jsonify({"status": "ok"}), 200

    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" not in value: return jsonify({"status": "not_a_message"}), 200

        message = value["messages"][0]
        phone = message["from"]
        msg_type = message.get("type")
        
        msg_recebida = ""
        tem_anexo = False
        media_id = None 

        if msg_type == "text": 
            msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))
        elif msg_type in ["image", "document"]:
            tem_anexo = True
            msg_recebida = "Anexo Recebido"
            media_id = message.get(msg_type, {}).get("id")

        if msg_recebida.lower() in ["recomeçar", "reset", "menu inicial", "⬅️ voltar ao menu"]:
            update_paciente(phone, {"status": "triagem", "cellphone": phone, "servico": "", "modalidade": ""})
            botoes = [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}]
            enviar_botoes(phone, "Atendimento reiniciado. 🔄\n\nEm qual unidade deseja ser atendido?", botoes)
            return jsonify({"status": "reset"}), 200

        info = get_paciente(phone)
        if not info:
            info = {"cellphone": phone, "status": "triagem"}
            update_paciente(phone, info)

        status = info.get("status", "triagem")
        servico = info.get("servico", "")
        cpf_salvo = info.get("cpf", "")
        
        is_veteran = True if len(re.sub(r'\D', '', cpf_salvo or "")) >= 11 else False
        modalidade = info.get("modalidade", "")
        convenio = info.get("convenio", "")
        if not modalidade and convenio: modalidade = "Convênio"
        elif not modalidade and servico in ["Recovery", "Liberação Miofascial"]: modalidade = "Particular"

        msg_limpa = msg_recebida.lower().strip()
        is_courtesy = False
        if len(msg_limpa) <= 25:
            if any(msg_limpa.startswith(w) for w in ["obrigad", "obg", "ok", "valeu", "certo", "tá bom", "ta bom", "perfeito", "beleza", "joia", "amém", "show"]):
                is_courtesy = True
            elif any(char in msg_limpa for char in ["👍", "🙏", "❤️", "👏", "🙌"]):
                is_courtesy = True

        if status == "finalizado":
            if is_courtesy:
                responder_texto(phone, "Por nada! 😊 Nossa equipe já recebeu seus dados e confirmará tudo em instantes.")
                return jsonify({"status": "courtesy_ignored"}), 200

            if is_veteran:
                update_paciente(phone, {"status": "menu_veterano"})
                botoes = [{"id": "v1", "title": "🗓️ Reagendar"}, {"id": "v2", "title": "🔄 Nova Guia"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, f"Olá, {info.get('title', 'paciente')}! ✨ Que bom ter você de volta. Como posso te ajudar hoje?", botoes)
                return jsonify({"status": "restart_veteran"}), 200
            else:
                status = "triagem"
        
        if status == "triagem":
            update_paciente(phone, {"status": "escolhendo_unidade"})
            botoes = [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}]
            enviar_botoes(phone, "Olá! ✨ Seja muito bem-vindo à Conectifisio.\n\nPara iniciarmos, em qual unidade você deseja ser atendido?", botoes)

        elif status == "escolhendo_unidade":
            update_paciente(phone, {"unit": msg_recebida, "status": "cadastrando_nome"})
            responder_texto(phone, f"Unidade {msg_recebida} selecionada! ✅\n\nComo você gostaria de ser chamado(a)?")

        elif status == "cadastrando_nome":
            if is_veteran:
                update_paciente(phone, {"title": msg_recebida, "status": "menu_veterano"})
                botoes = [{"id": "v1", "title": "🗓️ Reagendar"}, {"id": "v2", "title": "🔄 Nova Guia"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, f"Olá, {msg_recebida}! ✨ Que bom ter você de volta. Como posso te ajudar hoje?", botoes)
            else:
                update_paciente(phone, {"title": msg_recebida, "status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [
                    {"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"},
                    {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"},
                    {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"},
                    {"id": "e7", "title": "Liberação Miofascial"}
                ]}]
                enviar_lista(phone, f"Prazer, {msg_recebida}! 😊\n\nQual serviço você procura hoje?", "Ver Serviços", secoes)

        elif status == "menu_veterano":
            if "Novo Serviço" in msg_recebida:
                update_paciente(phone, {"status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [
                    {"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"},
                    {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"},
                    {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"},
                    {"id": "e7", "title": "Liberação Miofascial"}, {"id": "e8", "title": "⬅️ Voltar ao Menu"}
                ]}]
                enviar_lista(phone, "Perfeito! Qual novo serviço você deseja agendar?", "Ver Serviços", secoes)
            elif "Nova Guia" in msg_recebida or "Retomar" in msg_recebida:
                update_paciente(phone, {"status": "modalidade"})
                botoes = [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}]
                enviar_botoes(phone, "As novas sessões serão pelo seu CONVÊNIO ou de forma PARTICULAR?", botoes)
            elif "Reagendar" in msg_recebida:
                update_paciente(phone, {"status": "agendando"})
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                enviar_botoes(phone, "Certo! Vamos organizar isso. Qual o melhor período para você? ☀️ ⛅", botoes)

        elif status == "escolhendo_especialidade":
            if "Voltar" in msg_recebida:
                update_paciente(phone, {"status": "menu_veterano"})
                botoes = [{"id": "v1", "title": "🗓️ Reagendar"}, {"id": "v2", "title": "🔄 Nova Guia"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, "Voltando ao menu principal. Como posso ajudar?", botoes)
            elif msg_recebida in ["Recovery", "Liberação Miofascial"]:
                update_paciente(phone, {"servico": msg_recebida, "modalidade": "Particular", "status": "cadastrando_queixa"})
                responder_texto(phone, f"Ótima escolha para performance em {msg_recebida}! 🚀\n\nMe conte brevemente: o que te trouxe aqui hoje?")
            elif msg_recebida == "Fisio Neurológica":
                update_paciente(phone, {"servico": msg_recebida, "status": "triagem_neuro"})
                botoes = [{"id": "n1", "title": "🔹 Independente"}, {"id": "n2", "title": "🤝 Semidependente"}, {"id": "n3", "title": "👨‍🦽 Dependente"}]
                enviar_botoes(phone, "Para agendarmos com o especialista ideal, como está a mobilidade do paciente?\n\n🔹 *Independente:* Faz tudo sozinho.\n🤝 *Semidependente:* Precisa de apoio.\n👨‍🦽 *Dependente:* Auxílio constante.", botoes)
            elif msg_recebida == "Pilates Studio":
                if info.get("unit") == "Ipiranga":
                    update_paciente(phone, {"servico": msg_recebida, "status": "transferencia_pilates"})
                    botoes = [{"id": "tp_sim", "title": "Sim, mudar p/ SCS"}, {"id": "tp_nao", "title": "Não, escolher outro"}]
                    enviar_botoes(phone, "O Pilates Studio é uma modalidade exclusiva da nossa unidade de **São Caetano do Sul (SCS)**. 🧘‍♀️\n\nDeseja transferir o seu atendimento para lá para realizar o Pilates?", botoes)
                else:
                    update_paciente(phone, {"servico": msg_recebida, "status": "pilates_modalidade"})
                    secoes = [{"title": "Modalidade Pilates", "rows": [
                        {"id": "p_part", "title": "💎 Plano Particular"},
                        {"id": "p_caixa", "title": "🏦 Saúde Caixa"},
                        {"id": "p_app", "title": "💪 Wellhub/Totalpass"},
                        {"id": "p_vol", "title": "⬅️ Voltar"}
                    ]}]
                    enviar_lista(phone, "Excelente escolha! 🧘‍♀️ O Pilates é fundamental para a correção postural e fortalecimento.\n\nPara passarmos as informações corretas de horários e valores, como você pretende realizar as aulas?", "Ver Opções", secoes)
            else:
                update_paciente(phone, {"servico": msg_recebida, "status": "cadastrando_queixa"})
                responder_texto(phone, f"Entendido! {msg_recebida} selecionada.\n\nMe conte brevemente: o que te trouxe à clínica hoje?")

        elif status == "transferencia_pilates":
            if "Sim" in msg_recebida or "mudar" in msg_recebida.lower():
                update_paciente(phone, {"unit": "SCS", "status": "pilates_modalidade"})
                secoes = [{"title": "Modalidade Pilates", "rows": [
                    {"id": "p_part", "title": "💎 Plano Particular"}, {"id": "p_caixa", "title": "🏦 Saúde Caixa"},
                    {"id": "p_app", "title": "💪 Wellhub/Totalpass"}, {"id": "p_vol", "title": "⬅️ Voltar"}
                ]}]
                enviar_lista(phone, "Perfeito! A sua unidade foi alterada para **SCS** com sucesso. ✅\n\nAgora, como você pretende realizar as aulas de Pilates?", "Ver Opções", secoes)
            else:
                update_paciente(phone, {"servico": "", "status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [
                    {"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"},
                    {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"},
                    {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}
                ]}]
                enviar_lista(phone, "Sem problemas! Mantemos o seu atendimento na unidade **Ipiranga**. Qual outro serviço você procura hoje?", "Ver Serviços", secoes)

        # -----------------------------------------------------
        # RAMIFICAÇÃO PILATES STUDIO
        # -----------------------------------------------------
        elif status.startswith("pilates_"):
            if status == "pilates_modalidade":
                if "Voltar" in msg_recebida:
                    update_paciente(phone, {"status": "escolhendo_especialidade"})
                    secoes = [{"title": "Nossos Serviços", "rows": [
                        {"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"},
                        {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"},
                        {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, 
                        {"id": "e7", "title": "Liberação Miofascial"}
                    ]}]
                    enviar_lista(phone, "Voltando ao menu de especialidades. Qual serviço você procura hoje?", "Ver Serviços", secoes)
                
                elif "Wellhub" in msg_recebida or "Totalpass" in msg_recebida:
                    update_paciente(phone, {"modalidade": "Parceria App"})
                    if is_veteran:
                        update_paciente(phone, {"status": "pilates_app"})
                        botoes = [{"id": "w1", "title": "Wellhub"}, {"id": "t1", "title": "Totalpass"}]
                        enviar_botoes(phone, f"Prazer ter você aqui novamente, {info.get('title', 'paciente')}! ✨ Qual desses aplicativos você utiliza?", botoes)
                    else:
                        update_paciente(phone, {"status": "pilates_app_nome_completo"})
                        responder_texto(phone, "Perfeito! ✅ Aceitamos os planos Golden (Wellhub) e TP5 (Totalpass).\n\nPara iniciarmos seu cadastro obrigatório, digite o seu NOME COMPLETO:")
                
                elif "Saúde Caixa" in msg_recebida:
                    update_paciente(phone, {"modalidade": "Convênio", "convenio": "Saúde Caixa"})
                    if is_veteran:
                        update_paciente(phone, {"status": "pilates_caixa_foto_pedido"})
                        responder_texto(phone, f"Olá, {info.get('title', 'paciente')}! Para seguirmos, envie uma FOTO ou PDF do seu PEDIDO MÉDICO atualizado.")
                    else:
                        update_paciente(phone, {"status": "pilates_caixa_nome"})
                        responder_texto(phone, "Entendido! 🏦 Para o plano Saúde Caixa, é obrigatório apresentar o pedido médico.\n\nPara começarmos seu cadastro, digite o seu NOME COMPLETO:")
                
                elif "Particular" in msg_recebida:
                    update_paciente(phone, {"modalidade": "Particular"})
                    update_paciente(phone, {"status": "pilates_part_exp"})
                    botoes = [{"id": "pe_sim", "title": "Sim, gostaria"}, {"id": "pe_nao", "title": "Não, já quero começar"}]
                    enviar_botoes(phone, "Ótima escolha! ✨ O Pilates vai ajudar a aliviar dores e fortalecer o corpo todo. Gostaria de agendar uma aula experimental gratuita para conhecer o nosso estúdio?", botoes)

            elif status == "pilates_part_exp":
                update_paciente(phone, {"interesse_experimental": msg_recebida, "status": "pilates_part_periodo"})
                botoes = [{"id": "pe_m", "title": "☀️ Manhã"}, {"id": "pe_t", "title": "⛅ Tarde"}, {"id": "pe_n", "title": "🌙 Noite"}]
                if "Sim" in msg_recebida:
                    enviar_botoes(phone, "Agradecemos a escolha! Para a sua aula experimental, qual o melhor período?", botoes)
                else:
                    enviar_botoes(phone, "Excelente escolha! Vamos direto para a agenda. Qual o melhor período para você?", botoes)

            elif status == "pilates_part_periodo":
                update_paciente(phone, {"periodo": msg_recebida})
                if is_veteran:
                    update_paciente(phone, {"status": "atendimento_humano"})
                    responder_texto(phone, "Tudo pronto! Nossa equipe vai assumir o atendimento agora mesmo para encontrar o melhor horário. Aguarde um instante! 👩‍⚕️")
                else:
                    update_paciente(phone, {"status": "pilates_part_nome"})
                    responder_texto(phone, "Para finalizarmos seu cadastro, por favor, digite seu NOME COMPLETO:")
            
            elif status == "pilates_part_nome":
                update_paciente(phone, {"title": msg_recebida, "status": "pilates_part_cpf"})
                responder_texto(phone, "Nome registrado! ✅ Agora, digite seu CPF (apenas os 11 números):")
            elif status == "pilates_part_cpf":
                cpf_limpo = re.sub(r'\D', '', msg_recebida)
                if len(cpf_limpo) != 11: responder_texto(phone, "❌ CPF inválido. Digite apenas os 11 números.")
                else:
                    update_paciente(phone, {"cpf": cpf_limpo, "status": "pilates_part_nasc"})
                    responder_texto(phone, "Recebido! ✅ Qual sua data de nascimento? (Ex: 15/05/1980)")
            elif status == "pilates_part_nasc":
                update_paciente(phone, {"birthDate": msg_recebida, "status": "pilates_part_email"})
                responder_texto(phone, "Para completarmos, qual seu melhor E-MAIL?")
            elif status == "pilates_part_email":
                update_paciente(phone, {"email": msg_recebida, "status": "atendimento_humano"})
                responder_texto(phone, "Tudo pronto! Nossa equipe vai assumir o atendimento agora mesmo para confirmar o seu horário. Aguarde um instante! 👩‍⚕️")
                
            elif status == "pilates_app_nome_completo":
                update_paciente(phone, {"title": msg_recebida, "status": "pilates_app_cpf"})
                responder_texto(phone, "Nome registrado! ✅ Agora, digite seu CPF (apenas os 11 números):")
            elif status == "pilates_app_cpf":
                cpf_limpo = re.sub(r'\D', '', msg_recebida)
                if len(cpf_limpo) != 11: responder_texto(phone, "❌ CPF inválido. Digite apenas os 11 números.")
                else:
                    update_paciente(phone, {"cpf": cpf_limpo, "status": "pilates_app_nasc"})
                    responder_texto(phone, "Recebido! ✅ Qual sua data de nascimento? (Ex: 15/05/1980)")
            elif status == "pilates_app_nasc":
                update_paciente(phone, {"birthDate": msg_recebida, "status": "pilates_app_email"})
                responder_texto(phone, "Para completarmos o registro, qual seu melhor E-MAIL?")
            elif status == "pilates_app_email":
                update_paciente(phone, {"email": msg_recebida, "status": "pilates_app"})
                botoes = [{"id": "w1", "title": "Wellhub"}, {"id": "t1", "title": "Totalpass"}]
                enviar_botoes(phone, "Cadastro concluído! 🎉 Qual desses aplicativos você utiliza para o seu plano?", botoes)
            elif status == "pilates_app":
                update_paciente(phone, {"convenio": msg_recebida})
                if msg_recebida == "Wellhub":
                    update_paciente(phone, {"status": "pilates_wellhub_id"})
                    responder_texto(phone, "Por favor, informe o seu Wellhub ID.")
                else:
                    update_paciente(phone, {"status": "pilates_app_pref"})
                    botoes = [{"id": "pa_app", "title": "📱 App da Clínica"}, {"id": "pa_parceiro", "title": "🎫 App Parceiro"}]
                    enviar_botoes(phone, "Como prefere agendar as suas aulas? Pelo nosso App Exclusivo ou App do parceiro?", botoes)
            elif status == "pilates_wellhub_id":
                update_paciente(phone, {"numCarteirinha": msg_recebida, "status": "pilates_app_pref"})
                botoes = [{"id": "pa_app", "title": "📱 App da Clínica"}, {"id": "pa_parceiro", "title": "🎫 App Parceiro"}]
                enviar_botoes(phone, "ID recebido! Como prefere agendar as suas aulas? Pelo nosso App Exclusivo ou App do parceiro?", botoes)
            elif status == "pilates_app_pref":
                if "Clínica" in msg_recebida:
                    update_paciente(phone, {"status": "pilates_app_os"})
                    botoes = [{"id": "os_android", "title": "🤖 Android"}, {"id": "os_ios", "title": "🍏 iPhone (iOS)"}]
                    enviar_botoes(phone, "Ótima escolha! Para eu te enviar o link de download correto, qual é o sistema do seu celular?", botoes)
                else:
                    update_paciente(phone, {"status": "atendimento_humano"})
                    responder_texto(phone, "Perfeito! Você pode agendar as suas aulas diretamente pelo aplicativo parceiro.\n\nNossa equipe vai assumir o atendimento para alinhar detalhes. Aguarde! 👩‍⚕️")
            elif status == "pilates_app_os":
                update_paciente(phone, {"status": "atendimento_humano"})
                link = "https://play.google.com/store/apps/details?id=br.com.nextfit.app" if "Android" in msg_recebida else "https://apps.apple.com/app/next-fit/id1451167440"
                responder_texto(phone, f"Aqui está o seu link: {link}\n\n1️⃣ Baixe e abra o app\n2️⃣ Busque por: Conectifisio - Ictus Fisioterapia SCS\n\nNossa equipe vai liberar o seu acesso inicial em instantes! 👩‍⚕️")

            elif status == "pilates_caixa_nome":
                update_paciente(phone, {"title": msg_recebida, "status": "pilates_caixa_cpf"})
                responder_texto(phone, "Nome registrado! ✅ Agora, digite seu CPF (apenas os 11 números):")
            elif status == "pilates_caixa_cpf":
                cpf_limpo = re.sub(r'\D', '', msg_recebida)
                if len(cpf_limpo) != 11: responder_texto(phone, "❌ CPF inválido. Digite apenas os 11 números.")
                else:
                    update_paciente(phone, {"cpf": cpf_limpo, "status": "pilates_caixa_nasc"})
                    responder_texto(phone, "Recebido! ✅ Qual sua data de nascimento? (Ex: 15/05/1980)")
            elif status == "pilates_caixa_nasc":
                update_paciente(phone, {"birthDate": msg_recebida, "status": "pilates_caixa_email"})
                responder_texto(phone, "Ótimo! Qual seu melhor E-MAIL?")
            elif status == "pilates_caixa_email":
                update_paciente(phone, {"email": msg_recebida, "status": "pilates_caixa_foto_cart"})
                responder_texto(phone, "Anotado! ✅ Agora envie uma FOTO NÍTIDA da sua carteirinha Saúde Caixa.")
            elif status == "pilates_caixa_foto_cart":
                if not tem_anexo: responder_texto(phone, "❌ Por favor, envie a foto da sua carteirinha.")
                else:
                    update_paciente(phone, {"status": "pilates_caixa_foto_pedido", "tem_foto_carteirinha": True, "carteirinha_media_id": media_id})
                    responder_texto(phone, "Foto recebida! ✅\n\nAgora, envie a FOTO ou PDF DO SEU PEDIDO MÉDICO.")
            elif status == "pilates_caixa_foto_pedido":
                if not tem_anexo: responder_texto(phone, "❌ Por favor, envie o Pedido Médico.")
                else:
                    update_paciente(phone, {"status": "atendimento_humano", "tem_foto_pedido": True, "pedido_media_id": media_id})
                    responder_texto(phone, "Dados recebidos! Nossa equipe vai assumir o atendimento para dar andamento. Aguarde! 👩‍⚕️")

        # -----------------------------------------------------
        # RETORNO AO FLUXO CLÍNICO
        # -----------------------------------------------------
        elif status == "triagem_neuro":
            if "Dependente" in msg_recebida and "Semi" not in msg_recebida:
                update_paciente(phone, {"mobilidade": msg_recebida, "status": "atendimento_humano"})
                responder_texto(phone, "Devido à complexidade do caso, nosso fisioterapeuta responsável entrará em contato agora para te dar atenção total. Aguarde um instante! 👨‍⚕️")
            else:
                update_paciente(phone, {"mobilidade": msg_recebida, "status": "cadastrando_queixa"})
                responder_texto(phone, "Anotado! ✅\n\nMe conte brevemente: o que te trouxe à clínica hoje?")

        elif status == "cadastrando_queixa":
            prompt_ia = f"""Você é um Fisioterapeuta experiente e humano da clínica Conectifisio. 
O paciente relatou: "{msg_recebida[:300]}".
Responda com UMA ÚNICA frase curta dizendo que sente muito pela dor/problema e que a clínica vai cuidar dele.
Não prescreva tratamentos e não faça perguntas."""
            
            acolhimento = chamar_gemini(msg_recebida, prompt_ia) or "Compreendo perfeitamente, e saiba que estamos aqui para cuidar de você da melhor forma."
            
            if servico in ["Recovery", "Liberação Miofascial"]:
                if is_veteran:
                    update_paciente(phone, {"queixa": msg_recebida, "queixa_ia": acolhimento, "status": "agendando"})
                    botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                    enviar_botoes(phone, f"{acolhimento}\n\nComo você já é nosso paciente, vamos direto para a agenda. Qual o melhor período para você? ☀️ ⛅", botoes)
                else:
                    update_paciente(phone, {"queixa": msg_recebida, "queixa_ia": acolhimento, "status": "cadastrando_nome_completo"})
                    responder_texto(phone, f"{acolhimento}\n\nPara iniciarmos seu cadastro, por favor digite seu NOME COMPLETO (conforme documento):")
            else:
                update_paciente(phone, {"queixa": msg_recebida, "queixa_ia": acolhimento, "status": "modalidade"})
                botoes = [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}]
                enviar_botoes(phone, f"{acolhimento}\n\nDeseja atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", botoes)

        elif status == "modalidade":
            if "Convênio" in msg_recebida:
                update_paciente(phone, {"modalidade": "Convênio", "status": "nome_convenio"})
                secoes = [{"title": "Convênios Aceitos", "rows": [
                    {"id": "c1", "title": "Saúde Petrobras"}, {"id": "c2", "title": "Mediservice"},
                    {"id": "c3", "title": "Cassi"}, {"id": "c4", "title": "Geap Saúde"},
                    {"id": "c5", "title": "Amil"}, {"id": "c6", "title": "Bradesco Saúde"},
                    {"id": "c7", "title": "Porto Seguro Saúde"}, {"id": "c8", "title": "Prevent Senior"}, 
                    {"id": "c9", "title": "Saúde Caixa"}
                ]}]
                enviar_lista(phone, "Selecione o seu plano de saúde para validarmos a cobertura:", "Ver Convênios", secoes)
            else:
                if is_veteran:
                    update_paciente(phone, {"modalidade": "Particular", "status": "agendando"})
                    botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                    enviar_botoes(phone, "Perfeito! Como você já é nosso paciente, vamos direto para a agenda. Qual o melhor período para você? ☀️ ⛅", botoes)
                else:
                    update_paciente(phone, {"modalidade": "Particular", "status": "cadastrando_nome_completo"})
                    responder_texto(phone, "Perfeito! Para seu cadastro particular, digite seu NOME COMPLETO (conforme documento):")

        elif status == "nome_convenio":
            if is_veteran:
                update_paciente(phone, {"convenio": msg_recebida, "status": "foto_carteirinha"})
                responder_texto(phone, f"Anotado: {msg_recebida}! ✅\n\nComo você já é nosso paciente, pulei o preenchimento de CPF e E-mail! Por favor, envie uma FOTO NÍTIDA da sua carteirinha.")
            else:
                update_paciente(phone, {"convenio": msg_recebida, "status": "cadastrando_nome_completo"})
                responder_texto(phone, f"Anotado: {msg_recebida}! ✅\n\nAgora, digite seu NOME COMPLETO (conforme documento):")

        elif status == "cadastrando_nome_completo":
            update_paciente(phone, {"title": msg_recebida, "status": "cpf"})
            responder_texto(phone, "Nome registrado! ✅ Agora, digite seu CPF (apenas os 11 números):")

        elif status == "cpf":
            cpf_limpo = re.sub(r'\D', '', msg_recebida)
            if len(cpf_limpo) != 11:
                responder_texto(phone, "❌ CPF inválido. Digite apenas os 11 números, sem pontos ou traços.")
            else:
                update_paciente(phone, {"cpf": cpf_limpo, "status": "data_nascimento"})
                responder_texto(phone, "Recebido! ✅ Qual sua data de nascimento? (Ex: 15/05/1980)")

        elif status == "data_nascimento":
            update_paciente(phone, {"birthDate": msg_recebida, "status": "coletando_email"})
            responder_texto(phone, "Ótimo! Para finalizar seu cadastro, qual seu melhor E-MAIL?")

        elif status == "coletando_email":
            if modalidade == "Particular":
                update_paciente(phone, {"email": msg_recebida, "status": "agendando"})
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                enviar_botoes(phone, "Cadastro concluído! 🎉\n\nQual o melhor período para verificarmos a agenda particular?", botoes)
            else:
                update_paciente(phone, {"email": msg_recebida, "status": "num_carteirinha"})
                responder_texto(phone, "Certo! E qual o NÚMERO DA CARTEIRINHA do seu plano? (apenas números)")

        elif status == "num_carteirinha":
            num_limpo = re.sub(r'\D', '', msg_recebida)
            update_paciente(phone, {"numCarteirinha": num_limpo, "status": "foto_carteirinha"})
            responder_texto(phone, "Anotado! ✅ Agora a parte documental:\n\nEnvie uma FOTO NÍTIDA da sua carteirinha (use o ícone de clipe ou câmera do WhatsApp).")

        elif status == "foto_carteirinha":
            if not tem_anexo: 
                responder_texto(phone, "❌ Não recebi a imagem. Por favor, envie a foto da sua carteirinha.")
            else:
                update_paciente(phone, {"status": "foto_pedido_medico", "tem_foto_carteirinha": True, "carteirinha_media_id": media_id})
                responder_texto(phone, "Foto recebida! ✅\n\nAgora, envie a FOTO DO SEU PEDIDO MÉDICO.")

        elif status == "foto_pedido_medico":
            if not tem_anexo: 
                responder_texto(phone, "❌ Por favor, envie a foto do seu Pedido Médico.")
            else:
                update_paciente(phone, {"status": "agendando", "tem_foto_pedido": True, "pedido_media_id": media_id})
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                enviar_botoes(phone, "Documentação completa! 🎉\n\nQual o melhor período para verificarmos a sua vaga?", botoes)

        elif status == "agendando":
            if msg_recebida in ["Manhã", "Tarde"]:
                info["periodo"] = msg_recebida
                update_data = {"periodo": msg_recebida, "status": "finalizado"}
                
                # FEEGOW INTEGRATION CALL
                if servico and "Pilates" not in servico:
                    resultado_feegow = integrar_feegow(phone, info)
                    if resultado_feegow:
                        update_data.update(resultado_feegow)
                
                update_paciente(phone, update_data)
                responder_texto(phone, f"Horário de {msg_recebida} pré-agendado com sucesso! ✅ Nossa equipe de recepção vai finalizar a autorização no sistema e confirmar tudo com você em instantes.")
            else:
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                enviar_botoes(phone, "Por favor, utilize os botões abaixo para escolher o período de agendamento: ☀️ ⛅", botoes)

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"❌ Erro Crítico POST: {traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 200

# ==========================================
# WEBHOOK GET (Meta & Dashboard)
# ==========================================
@app.route("/api/whatsapp", methods=["GET"])
def verify_or_data():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
        
    if request.args.get("action") == "get_patients":
        try:
            if not db: return jsonify({"items": []}), 200
            docs = db.collection("PatientsKanban").stream()
            patients = []
            for doc in docs:
                data = doc.to_dict()
                data["id"] = doc.id
                if "lastInteraction" in data and data["lastInteraction"]:
                    try: data["lastInteraction"] = data["lastInteraction"].isoformat()
                    except: data["lastInteraction"] = str(data["lastInteraction"])
                patients.append(data)
            return jsonify({"items": patients}), 200
        except Exception as e:
            return jsonify({"error": str(e), "items": []}), 500
            
    return "Acesso Negado ou Rota Incorreta", 403

if __name__ == "__main__":
    app.run(port=5000)
