import os
import requests
import traceback
import re
import json
import base64
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES DE AMBIENTE
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
        print("✅ Firebase Inicializado!")
    except Exception as e:
        print(f"❌ Erro Firebase: {e}")

db = firestore.client() if firebase_admin._apps else None

# ==========================================
# FUNÇÕES DE MEMÓRIA
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
# FEEGOW: RECONHECIMENTO INVISÍVEL
# ==========================================
def buscar_veterano_feegow_celular(phone):
    """Busca silenciosa no Feegow pelo celular para ver se já é paciente antigo"""
    if not FEEGOW_TOKEN: return None
    celular = re.sub(r'\D', '', phone)
    if celular.startswith("55") and len(celular) > 11: celular = celular[2:]
    
    headers = {"Content-Type": "application/json", "x-access-token": FEEGOW_TOKEN}
    url = f"https://api.feegow.com/v1/api/patient/list?celular={celular}"
    
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            dados = res.json()
            if dados.get("success") and dados.get("content"):
                paciente = dados["content"][0]
                return {
                    "feegow_id": paciente.get("id") or paciente.get("paciente_id"),
                    "title": paciente.get("nome", "Paciente"),
                    "cpf": re.sub(r'\D', '', str(paciente.get("cpf", "")))
                }
    except: pass
    return None

def buscar_feegow_id_por_cpf(cpf):
    """Garante que temos o ID do paciente para buscar agendamentos"""
    if not FEEGOW_TOKEN or not cpf: return None
    cpf_limpo = re.sub(r'\D', '', cpf)
    headers = {"Content-Type": "application/json", "x-access-token": FEEGOW_TOKEN}
    try:
        res = requests.get(f"https://api.feegow.com/v1/api/patient/search?paciente_cpf={cpf_limpo}&photo=false", headers=headers, timeout=5)
        if res.status_code == 200 and res.json().get("success"):
            return res.json().get("content", {}).get("paciente_id") or res.json().get("content", {}).get("id")
    except: pass
    return None

# ==========================================
# MOTOR DE AGENDAMENTO E REGRAS CLÍNICAS
# ==========================================
def get_proximos_dias_uteis(quantidade=3):
    dias = []
    data_atual = datetime.now()
    while len(dias) < quantidade:
        data_atual += timedelta(days=1)
        if data_atual.weekday() < 5: dias.append(data_atual)
    return dias

def gerar_horarios_disponiveis(periodo):
    dias = get_proximos_dias_uteis(3)
    horarios = []
    slots = ["08:00", "09:30", "11:00"] if periodo.lower() == "manhã" else ["14:00", "15:30", "17:00"]
    for i in range(3):
        horarios.append(f"{dias[i].strftime('%d/%m')} às {slots[i]}")
    return horarios

def buscar_horarios_feegow(unidade_nome, servico_nome, periodo, is_veteran):
    if not FEEGOW_TOKEN: return [f"🗓️ {h}" for h in gerar_horarios_disponiveis(periodo)[:2]]
    local_id = 1 if "ipiranga" in str(unidade_nome).lower() else 0
    proc_id = 21 if "acupuntura" in str(servico_nome).lower() else 9
    dias_adicionais = 0 if is_veteran else 1
    data_alvo = datetime.now() + timedelta(days=dias_adicionais)
    if data_alvo.weekday() == 5: data_alvo += timedelta(days=2)
    elif data_alvo.weekday() == 6: data_alvo += timedelta(days=1)
    
    url = f"https://api.feegow.com.br/v1/appoints/available-schedule?local_id={local_id}&procedimento_id={proc_id}&data={data_alvo.strftime('%Y-%m-%d')}"
    headers = {"x-access-token": FEEGOW_TOKEN, "Content-Type": "application/json"}
    slots = []
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            for prof in (res.json().get("data") or res.json().get("content") or []):
                for h in prof.get("horarios", []):
                    hora_str = h.get("hora", "")
                    try:
                        hora_int = int(hora_str.split(":")[0])
                        if periodo.lower() == "manhã" and hora_int < 12: slots.append(hora_str[:5])
                        elif periodo.lower() == "tarde" and 12 <= hora_int < 18: slots.append(hora_str[:5])
                        elif periodo.lower() == "noite" and hora_int >= 18: slots.append(hora_str[:5])
                    except: pass
    except: pass
    
    slots = sorted(list(set(slots)))
    if slots: return [f"🗓️ {data_alvo.strftime('%d/%m')} às {s}" for s in slots[:2]]
    return [f"🗓️ {h}" for h in gerar_horarios_disponiveis(periodo)[:2]]

def buscar_agendamentos_futuros(feegow_id):
    """Busca as sessões ativas do paciente nos próximos 30 dias"""
    if not FEEGOW_TOKEN or not feegow_id: return None
    headers = {"Content-Type": "application/json", "x-access-token": FEEGOW_TOKEN}
    hoje = datetime.now()
    futuro = hoje + timedelta(days=30)
    url = f"https://api.feegow.com/v1/api/appoints/search?paciente_id={feegow_id}&data_start={hoje.strftime('%Y-%m-%d')}&data_end={futuro.strftime('%Y-%m-%d')}"
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200 and res.json().get("success"):
            lista_final = []
            for a in res.json().get("content", []):
                if "cancelado" not in str(a.get("status_nome", "")).lower():
                    try:
                        dt_obj = datetime.strptime(a.get("data", ""), "%Y-%m-%d")
                        lista_final.append(f"🗓️ *{dt_obj.strftime('%d/%m')} às {a.get('horario', '')[:5]}* - {a.get('procedimento_nome', 'Sessão')}")
                    except: pass
            return lista_final[:3] # Retorna as 3 próximas
    except: pass
    return []

# ==========================================
# INTEGRAÇÃO DE CADASTRO E UPLOAD (FEEGOW)
# ==========================================
def mapear_convenio(nome):
    nome_upper = str(nome).upper()
    if "BRADESCO" in nome_upper and "OPERADORA" in nome_upper: return 5
    if "BRADESCO" in nome_upper: return 2
    if "AMIL" in nome_upper: return 3
    if "PORTO" in nome_upper: return 4
    if "GEAP" in nome_upper: return 6
    if "PREVENT" in nome_upper: return 7
    if "CASSI" in nome_upper: return 8
    if "PETROBRAS" in nome_upper: return 11
    if "MEDISERVICE" in nome_upper: return 9968
    if "CAIXA" in nome_upper: return 10154
    return 0

def formatar_data_feegow(data_br):
    data_limpa = re.sub(r'\D', '', str(data_br))
    if len(data_limpa) == 8: return f"{data_limpa[4:]}-{data_limpa[2:4]}-{data_limpa[:2]}"
    return data_br

def baixar_midia_whatsapp(media_id):
    if not media_id or not WHATSAPP_TOKEN: return None
    try:
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        res_info = requests.get(f"https://graph.facebook.com/v18.0/{media_id}", headers=headers, timeout=5)
        if res_info.status_code != 200: return None
        res_download = requests.get(res_info.json().get("url"), headers=headers, timeout=15)
        return f"data:{res_info.json().get('mime_type', 'image/jpeg')};base64,{base64.b64encode(res_download.content).decode('utf-8')}"
    except: return None

def integrar_feegow(phone, info):
    if not FEEGOW_TOKEN: return {"feegow_status": "Token Ausente"}
    cpf = re.sub(r'\D', '', info.get("cpf", ""))
    celular = re.sub(r'\D', '', phone)
    if celular.startswith("55") and len(celular) > 11: celular = celular[2:]

    headers = {"Content-Type": "application/json", "x-access-token": FEEGOW_TOKEN}
    base_url = "https://api.feegow.com/v1/api"
    paciente_id = info.get("feegow_id")
    
    if not paciente_id and len(cpf) == 11:
        paciente_id = buscar_feegow_id_por_cpf(cpf)

    if not paciente_id:
        payload_create = {
            "nome_completo": info.get("title", "Paciente Sem Nome"),
            "cpf": cpf,
            "data_nascimento": formatar_data_feegow(info.get("birthDate", "")),
            "celular1": celular
        }
        email = info.get("email", "").strip()
        if email and "@" in email: payload_create["email1"] = email
        convenio_id = mapear_convenio(info.get("convenio", ""))
        if convenio_id > 0:
            payload_create["convenio_id"], payload_create["plano_id"], payload_create["matricula"] = convenio_id, 0, info.get("numCarteirinha", "")

        try:
            res_create = requests.post(f"{base_url}/patient/create", json=payload_create, headers=headers, timeout=10)
            if res_create.status_code == 200 and res_create.json().get("success"):
                paciente_id = res_create.json().get("content", {}).get("paciente_id") or res_create.json().get("paciente_id")
        except: pass

    if paciente_id:
        paciente_id_int = int(paciente_id)
        cart_id, ped_id = info.get("carteirinha_media_id"), info.get("pedido_media_id")
        if cart_id:
            b64 = baixar_midia_whatsapp(cart_id)
            if b64: requests.post(f"{base_url}/patient/upload-base64", json={"paciente_id": paciente_id_int, "arquivo_descricao": "Carteirinha (Robô)", "base64_file": b64}, headers=headers, timeout=15)
        if ped_id:
            b64 = baixar_midia_whatsapp(ped_id)
            if b64: requests.post(f"{base_url}/patient/upload-base64", json={"paciente_id": paciente_id_int, "arquivo_descricao": "Pedido Médico (Robô)", "base64_file": b64}, headers=headers, timeout=15)
        return {"feegow_id": paciente_id_int, "feegow_status": f"ID: {paciente_id_int}"}
    return {"feegow_status": "Erro Integração"}

# ==========================================
# MENSAGERIA E IA
# ==========================================
def chamar_gemini(query, system_prompt):
    if not API_KEY: return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={API_KEY}"
    payload = {"contents": [{"parts": [{"text": query[:300]}]}], "systemInstruction": {"parts": [{"text": system_prompt}]}}
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200: return res.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
    except: pass
    return None

def enviar_whatsapp(to, payload_msg):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, **payload_msg}
    try: requests.post(url, json=payload, headers=headers, timeout=10)
    except: pass

def responder_texto(to, texto): enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})
def enviar_botoes(to, texto, botoes): enviar_whatsapp(to, {"type": "interactive", "interactive": {"type": "button", "body": {"text": texto}, "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}} for b in botoes]}}})
def enviar_lista(to, texto, titulo_botao, secoes): enviar_whatsapp(to, {"type": "interactive", "interactive": {"type": "list", "body": {"text": texto}, "action": {"button": titulo_botao[:20], "sections": secoes}}})

# ==========================================
# WEBHOOK POST PRINCIPAL
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
        is_button_reply = False

        if msg_type == "text": 
            msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            is_button_reply = True
            msg_recebida = message["interactive"].get("button_reply", {}).get("title", message["interactive"].get("list_reply", {}).get("title", ""))
        elif msg_type in ["image", "document"]:
            tem_anexo = True
            msg_recebida = "Anexo Recebido"
            media_id = message.get(msg_type, {}).get("id")

        msg_limpa = msg_recebida.lower().strip()

        # 1. COMANDO DE RESET GLOBAL
        if msg_limpa in ["recomeçar", "reset", "menu inicial", "⬅️ voltar ao menu"]:
            update_paciente(phone, {"status": "triagem", "cellphone": phone, "servico": "", "modalidade": ""})
            botoes = [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}]
            enviar_botoes(phone, "Atendimento reiniciado. 🔄\n\nEm qual unidade deseja ser atendido?", botoes)
            return jsonify({"status": "reset"}), 200

        # 2. CARREGAMENTO DE MEMÓRIA E RECONHECIMENTO INVISÍVEL
        info = get_paciente(phone)
        if not info:
            veterano_feegow = buscar_veterano_feegow_celular(phone)
            if veterano_feegow:
                info = {"cellphone": phone, "status": "menu_veterano", **veterano_feegow}
                update_paciente(phone, info)
                botoes = [{"id": "v1", "title": "🗓️ Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, f"Olá, {info['title']}! ✨ Que bom ter você de volta. Como posso te ajudar hoje?", botoes)
                return jsonify({"status": "veteran_found"}), 200
            else:
                info = {"cellphone": phone, "status": "triagem"}
                update_paciente(phone, info)

        status = info.get("status", "triagem")
        servico = info.get("servico", "")
        cpf_salvo = info.get("cpf", "")
        is_veteran = True if len(re.sub(r'\D', '', cpf_salvo or "")) >= 11 or info.get("feegow_id") else False
        modalidade = info.get("modalidade", "")
        
        # 3. ESCUDO ANTI-LIXO E SAUDAÇÕES
        is_saudacao = len(msg_limpa) <= 20 and any(w in msg_limpa for w in ["oi", "olá", "ola", "bom dia", "boa tarde", "boa noite", "tudo bem"])
        if is_saudacao and status not in ["triagem", "finalizado", "menu_veterano"]:
            botoes = [{"id": "btn_cont", "title": "Sim, continuar"}, {"id": "btn_rec", "title": "Recomeçar"}]
            enviar_botoes(phone, "Olá! ✨ Notei que estávamos no meio do seu atendimento. Podemos continuar de onde paramos?", botoes)
            return jsonify({"status": "anti_lixo"}), 200

        # 4. TRATAMENTO DO "SIM, CONTINUAR"
        if msg_recebida == "Sim, continuar":
            prompts = {
                "escolhendo_unidade": ("Por favor, escolha a unidade:", [{"id":"u1","title":"SCS"},{"id":"u2","title":"Ipiranga"}], "botoes"),
                "escolhendo_especialidade": ("Qual serviço você procura?", [{"title":"Opções","rows":[{"id":"e1","title":"Fisio Ortopédica"},{"id":"e2","title":"Pilates Studio"}]}], "lista"),
                "cpf": ("Por favor, digite o seu CPF (apenas números):", None, "texto"),
                "data_nascimento": ("Qual a sua data de nascimento? (DD/MM/AAAA)", None, "texto"),
                "coletando_email": ("Qual o seu melhor e-mail?", None, "texto"),
                "foto_carteirinha": ("Por favor, envie a foto da sua carteirinha.", None, "texto")
            }
            if status in prompts:
                p = prompts[status]
                if p[2] == "botoes": enviar_botoes(phone, p[0], p[1])
                elif p[2] == "lista": enviar_lista(phone, p[0], "Ver", p[1])
                else: responder_texto(phone, p[0])
            else:
                responder_texto(phone, "Por favor, responda à etapa anterior para continuarmos.")
            return jsonify({"status": "continuando"}), 200

        # 5. FILTRO DE CORTESIA FINAL
        if status == "finalizado":
            if len(msg_limpa) <= 25 and (any(msg_limpa.startswith(w) for w in ["obrigad", "obg", "ok", "valeu", "certo"]) or any(char in msg_limpa for char in ["👍", "🙏", "❤️"])):
                responder_texto(phone, "Por nada! 😊 Nossa equipe vai cuidar de você.")
                return jsonify({"status": "courtesy"}), 200
            if is_veteran:
                update_paciente(phone, {"status": "menu_veterano"})
                botoes = [{"id": "v1", "title": "🗓️ Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, f"Olá, {info.get('title', 'paciente')}! ✨ Como posso te ajudar hoje?", botoes)
                return jsonify({"status": "restart"}), 200
            else: status = "triagem"
        
        # ==========================================
        # MÁQUINA DE ESTADOS E VALIDAÇÕES ESTRITAS
        # ==========================================
        if status == "triagem":
            update_paciente(phone, {"status": "escolhendo_unidade"})
            botoes = [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}]
            enviar_botoes(phone, "Olá! ✨ Seja muito bem-vindo à Conectifisio.\n\nPara iniciarmos, em qual unidade você deseja ser atendido?", botoes)

        elif status == "escolhendo_unidade":
            if msg_recebida not in ["SCS", "Ipiranga"]:
                botoes = [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}]
                enviar_botoes(phone, "⚠️ Por favor, utilize os botões abaixo para escolher a unidade:", botoes)
                return jsonify({"status": "invalid_button"}), 200
                
            update_paciente(phone, {"unit": msg_recebida, "status": "cadastrando_nome"})
            responder_texto(phone, f"Unidade {msg_recebida} selecionada! ✅\n\nComo você gostaria de ser chamado(a)?")

        elif status == "cadastrando_nome":
            if is_veteran:
                update_paciente(phone, {"title": msg_recebida, "status": "menu_veterano"})
                botoes = [{"id": "v1", "title": "🗓️ Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, f"Olá, {msg_recebida}! ✨ Que bom ter você de volta. Como posso te ajudar hoje?", botoes)
            else:
                update_paciente(phone, {"title": msg_recebida, "status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}]}]
                enviar_lista(phone, f"Prazer, {msg_recebida}! 😊\n\nQual serviço você procura hoje?", "Ver Serviços", secoes)

        # ---------------------------------------------
        # 🚀 O MENU DO VETERANO (PESQUISA FEEGOW AQUI)
        # ---------------------------------------------
        elif status == "menu_veterano":
            if "Novo Serviço" in msg_recebida:
                update_paciente(phone, {"status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}]}]
                enviar_lista(phone, "Perfeito! Qual novo serviço você deseja agendar?", "Ver Serviços", secoes)
            
            elif "Agendamentos" in msg_recebida or "Reagendar" in msg_recebida:
                # O BOTÃO "🗓️ Agendamentos" CAI AQUI E ACIONA A PESQUISA!
                feegow_id = info.get("feegow_id")
                if not feegow_id and info.get("cpf"):
                    feegow_id = buscar_feegow_id_por_cpf(info.get("cpf"))
                    if feegow_id: update_paciente(phone, {"feegow_id": feegow_id})
                
                lista_sessoes = buscar_agendamentos_futuros(feegow_id)
                if lista_sessoes:
                    msg_agenda = "Localizei as suas próximas sessões: 👇\n\n" + "\n".join(lista_sessoes) + "\n\nO que deseja fazer?"
                    botoes = [{"id": "ag_ok", "title": "👍 Apenas Consultar"}, {"id": "ag_mudar", "title": "🔄 Reagendar/Cancelar"}, {"id": "ag_voltar", "title": "⬅️ Voltar"}]
                    update_paciente(phone, {"status": "vendo_agendamentos"})
                    enviar_botoes(phone, msg_agenda, botoes)
                else:
                    update_paciente(phone, {"status": "agendando"})
                    botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}, {"id": "t3", "title": "Noite"}]
                    enviar_botoes(phone, "Não encontrei agendamentos futuros para si. 🤔\n\nPosso agendar um agora! Qual o melhor período? ☀️ ⛅", botoes)
            
            elif "Nova Guia" in msg_recebida or "Retomar" in msg_recebida:
                update_paciente(phone, {"status": "modalidade"})
                botoes = [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}]
                enviar_botoes(phone, "As novas sessões serão pelo seu CONVÊNIO ou de forma PARTICULAR?", botoes)
            else:
                botoes = [{"id": "v1", "title": "🗓️ Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, "⚠️ Por favor, utilize os botões acima para escolher uma opção.", botoes)

        elif status == "vendo_agendamentos":
            if "Reagendar" in msg_recebida:
                update_paciente(phone, {"status": "atendimento_humano"})
                responder_texto(phone, "Entendido! A nossa equipe da recepção foi notificada para realizar a alteração do horário com prioridade. Aguarde um instante! 👩‍⚕️")
            elif "Voltar" in msg_recebida:
                update_paciente(phone, {"status": "menu_veterano"})
                botoes = [{"id": "v1", "title": "🗓️ Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, "Voltando ao menu principal. 👇", botoes)
            else:
                update_paciente(phone, {"status": "finalizado"})
                responder_texto(phone, "Perfeito! Qualquer outra dúvida, estou por aqui. Tenha uma ótima sessão! ✨")

        elif status == "escolhendo_especialidade":
            if not is_button_reply:
                responder_texto(phone, "⚠️ Por favor, clique no botão 'Ver Serviços' acima para escolher uma opção válida da lista.")
                return jsonify({"status": "invalid_list"}), 200

            if "Voltar" in msg_recebida:
                update_paciente(phone, {"status": "menu_veterano"})
                botoes = [{"id": "v1", "title": "🗓️ Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, "Voltando ao menu principal.", botoes)
            elif msg_recebida == "Pilates Studio":
                if info.get("unit") == "Ipiranga":
                    update_paciente(phone, {"servico": msg_recebida, "status": "transferencia_pilates"})
                    botoes = [{"id": "tp_sim", "title": "Sim, mudar p/ SCS"}, {"id": "tp_nao", "title": "Não, escolher outro"}]
                    enviar_botoes(phone, "O Pilates é exclusivo da unidade **SCS**. Deseja transferir o atendimento?", botoes)
                else:
                    update_paciente(phone, {"servico": msg_recebida, "status": "pilates_modalidade"})
                    secoes = [{"title": "Modalidade", "rows": [{"id": "p_part", "title": "💎 Particular"}, {"id": "p_caixa", "title": "🏦 Saúde Caixa"}, {"id": "p_app", "title": "💪 Wellhub/Totalpass"}, {"id": "p_vol", "title": "⬅️ Voltar"}]}]
                    enviar_lista(phone, "Excelente! Como pretende realizar as aulas?", "Ver Opções", secoes)
            elif msg_recebida in ["Recovery", "Liberação Miofascial"]:
                update_paciente(phone, {"servico": msg_recebida, "modalidade": "Particular", "status": "cadastrando_queixa"})
                responder_texto(phone, f"Ótima escolha para {msg_recebida}! 🚀\n\nMe conte brevemente: o que te trouxe aqui hoje?")
            else:
                update_paciente(phone, {"servico": msg_recebida, "status": "cadastrando_queixa"})
                responder_texto(phone, f"{msg_recebida} selecionada.\n\nMe conte brevemente: o que te trouxe à clínica hoje?")

        elif status == "cadastrando_queixa":
            prompt_ia = f"Paciente relatou: '{msg_recebida[:300]}'. Responda com UMA ÚNICA frase empática."
            acolhimento = chamar_gemini(msg_recebida, prompt_ia) or "Compreendo perfeitamente, e saiba que estamos aqui para cuidar de você da melhor forma."
            
            update_paciente(phone, {"queixa": msg_recebida, "queixa_ia": acolhimento, "status": "modalidade" if servico not in ["Recovery", "Liberação Miofascial"] else "cadastrando_nome_completo"})
            if servico in ["Recovery", "Liberação Miofascial"]: responder_texto(phone, f"{acolhimento}\n\nPara o seu cadastro, digite o seu NOME COMPLETO:")
            else: 
                botoes = [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}]
                enviar_botoes(phone, f"{acolhimento}\n\nDeseja atendimento pelo seu CONVÊNIO ou PARTICULAR?", botoes)
                
        elif status == "modalidade":
            if msg_recebida not in ["Convênio", "Particular"]:
                botoes = [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}]
                enviar_botoes(phone, "⚠️ Por favor, utilize os botões para responder:", botoes)
                return jsonify({"status": "invalid_button"}), 200

            if "Convênio" in msg_recebida:
                update_paciente(phone, {"modalidade": "Convênio", "status": "nome_convenio"})
                secoes = [{"title": "Convênios", "rows": [
                    {"id": "c1", "title": "Bradesco Saúde"}, {"id": "c2", "title": "Bradesco Operadora"},
                    {"id": "c3", "title": "Amil"}, {"id": "c4", "title": "Porto Seguro Saúde"},
                    {"id": "c5", "title": "GEAP"}, {"id": "c6", "title": "Prevent Senior"},
                    {"id": "c7", "title": "Cassi"}, {"id": "c8", "title": "Saúde Petrobras"},
                    {"id": "c9", "title": "Mediservice"}, {"id": "c10", "title": "Saúde Caixa"}
                ]}]
                enviar_lista(phone, "Selecione o seu plano na lista abaixo:", "Ver Convênios", secoes)
            else:
                update_paciente(phone, {"modalidade": "Particular", "status": "cadastrando_nome_completo"})
                responder_texto(phone, "Digite seu NOME COMPLETO (conforme documento):")
                
        elif status == "nome_convenio":
            if not is_button_reply:
                responder_texto(phone, "⚠️ Por favor, clique em 'Ver Convênios' e escolha uma opção da lista.")
                return jsonify({"status": "invalid_list"}), 200
            update_paciente(phone, {"convenio": msg_recebida, "status": "cadastrando_nome_completo"})
            responder_texto(phone, f"Anotado: {msg_recebida}! ✅\n\nAgora, digite seu NOME COMPLETO:")
            
        elif status == "cadastrando_nome_completo":
            update_paciente(phone, {"title": msg_recebida, "status": "cpf"})
            responder_texto(phone, "Nome registrado! ✅ Agora, digite seu CPF (apenas os 11 números):")
            
        elif status == "cpf":
            cpf_limpo = re.sub(r'\D','',msg_recebida)
            if len(cpf_limpo) != 11:
                responder_texto(phone, "❌ O CPF deve conter exatamente 11 números, sem pontos ou traços. Tente novamente:")
            else:
                update_paciente(phone, {"cpf": cpf_limpo, "status": "data_nascimento"})
                responder_texto(phone, "Recebido! ✅ Qual sua data de nascimento? (DD/MM/AAAA)")
            
        elif status == "data_nascimento":
            update_paciente(phone, {"birthDate": msg_recebida, "status": "coletando_email"})
            responder_texto(phone, "Qual seu melhor E-MAIL?")
            
        elif status == "coletando_email":
            update_paciente(phone, {"email": msg_recebida, "status": "num_carteirinha" if modalidade=="Convênio" else "agendando"})
            if modalidade=="Convênio": responder_texto(phone, "Certo! Qual o NÚMERO DA CARTEIRINHA do seu plano?")
            else: 
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}, {"id": "t3", "title": "Noite"}]
                enviar_botoes(phone, "Qual o melhor período para verificarmos a sua vaga?", botoes)
                
        elif status == "num_carteirinha":
            update_paciente(phone, {"numCarteirinha": msg_recebida, "status": "foto_carteirinha"})
            responder_texto(phone, "Anotado! ✅ Agora a parte documental:\n\nEnvie uma FOTO NÍTIDA da sua carteirinha.")
            
        elif status == "foto_carteirinha":
            if tem_anexo:
                update_paciente(phone, {"status": "foto_pedido_medico", "carteirinha_media_id": media_id})
                responder_texto(phone, "Foto recebida! ✅\n\nAgora, envie a FOTO DO SEU PEDIDO MÉDICO:")
            else: responder_texto(phone, "❌ Não recebi a imagem. Por favor, envie a foto da sua carteirinha.")
            
        elif status == "foto_pedido_medico":
            if tem_anexo:
                update_paciente(phone, {"status": "agendando", "pedido_media_id": media_id})
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}, {"id": "t3", "title": "Noite"}]
                enviar_botoes(phone, "Documentação completa! 🎉\n\nQual o melhor período para a sua Consulta Ambulatorial?", botoes)
            else: responder_texto(phone, "❌ Por favor, envie a foto do seu Pedido Médico.")

        # ==========================================
        # MOTOR DE AGENDAMENTO INTELIGENTE
        # ==========================================
        elif status == "agendando":
            if msg_recebida not in ["Manhã", "Tarde", "Noite"]:
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}, {"id": "t3", "title": "Noite"}]
                enviar_botoes(phone, "⚠️ Por favor, utilize os botões para escolher o período:", botoes)
                return jsonify({"status": "invalid_button"}), 200

            info["periodo"] = msg_recebida
            slots = buscar_horarios_feegow(info.get("unit", "SCS"), servico, msg_recebida, is_veteran)
            update_paciente(phone, {"periodo": msg_recebida, "status": "escolhendo_horario_novo"})
            
            botoes = [{"id": f"h_{i}", "title": s[:20]} for i, s in enumerate(slots)]
            botoes.append({"id": "h_outros", "title": "📅 Outras opções"})
            
            msg_regra = "Encontrei estas vagas exatas na nossa agenda. Qual delas fica melhor para você?"
            if not is_veteran and modalidade == "Convênio":
                msg_regra = "Como precisamos de um prazo para solicitar a autorização junto ao seu convênio, busquei as vagas a partir do próximo dia útil. Qual fica melhor?"
            
            enviar_botoes(phone, msg_regra, botoes)

        elif status == "escolhendo_horario_novo":
            if not is_button_reply:
                responder_texto(phone, "⚠️ Por favor, clique num dos botões de horário acima.")
                return jsonify({"status": "invalid_button"}), 200

            if "Outras" in msg_recebida:
                update_data = {"status": "atendimento_humano"}
                if servico and "Pilates" not in servico:
                    res_feegow = integrar_feegow(phone, info)
                    if res_feegow: update_data.update(res_feegow)
                update_paciente(phone, update_data)
                responder_texto(phone, "Entendido! Como você precisa de uma data mais para a frente, a nossa equipe da recepção vai assumir o atendimento para encontrar o dia perfeito. Aguarde um instante! 👩‍⚕️")
            else:
                update_data = {"horario_escolhido": msg_recebida, "status": "finalizado"}
                if servico and "Pilates" not in servico:
                    res_feegow = integrar_feegow(phone, info)
                    if res_feegow: update_data.update(res_feegow)
                update_paciente(phone, update_data)
                horario_limpo = msg_recebida.replace('🗓️ ', '')
                responder_texto(phone, f"Consulta pré-agendada para {horario_limpo}! ✅\n\nNossa equipe já recebeu seus dados e confirmará tudo com você em instantes. Aguarde um momento! 👩‍⚕️")

        # ==========================================
        # FLUXOS ISOLADOS DE PILATES (INSTRUÇÕES CLARAS)
        # ==========================================
        elif status.startswith("pilates_"):
            if status == "pilates_modalidade":
                if "Voltar" in msg_recebida:
                    update_paciente(phone, {"status": "escolhendo_especialidade"})
                    secoes = [{"title": "Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e5", "title": "Pilates Studio"}]}]
                    enviar_lista(phone, "Voltando ao menu principal.", "Ver Serviços", secoes)
                elif "Wellhub" in msg_recebida or "Totalpass" in msg_recebida:
                    update_paciente(phone, {"modalidade": "Parceria App"})
                    if is_veteran:
                        update_paciente(phone, {"status": "pilates_app"})
                        botoes = [{"id": "w1", "title": "Wellhub"}, {"id": "t1", "title": "Totalpass"}]
                        enviar_botoes(phone, f"Prazer ter você aqui novamente! ✨ Qual aplicativo você utiliza?", botoes)
                    else:
                        update_paciente(phone, {"status": "pilates_app_nome_completo"})
                        responder_texto(phone, "Perfeito! ✅ Aceitamos os planos Golden (Wellhub) e TP5 (Totalpass).\n\nPara iniciarmos, digite o seu NOME COMPLETO:")
                elif "Particular" in msg_recebida:
                    update_paciente(phone, {"modalidade": "Particular", "status": "pilates_part_exp"})
                    botoes = [{"id": "pe_sim", "title": "Sim, gostaria"}, {"id": "pe_nao", "title": "Não, já quero começar"}]
                    enviar_botoes(phone, "Ótima escolha! Gostaria de agendar uma aula experimental gratuita?", botoes)

            elif status == "pilates_app_nome_completo":
                update_paciente(phone, {"title": msg_recebida, "status": "pilates_app_cpf"})
                responder_texto(phone, "Digite seu CPF (apenas números):")
            elif status == "pilates_app_cpf":
                update_paciente(phone, {"cpf": re.sub(r'\D','',msg_recebida), "status": "pilates_app_nasc"})
                responder_texto(phone, "Qual sua data de nascimento?")
            elif status == "pilates_app_nasc":
                update_paciente(phone, {"birthDate": msg_recebida, "status": "pilates_app_email"})
                responder_texto(phone, "Qual seu melhor E-MAIL?")
            elif status == "pilates_app_email":
                update_paciente(phone, {"email": msg_recebida, "status": "pilates_app"})
                botoes = [{"id": "w1", "title": "Wellhub"}, {"id": "t1", "title": "Totalpass"}]
                enviar_botoes(phone, "Cadastro concluído! Qual aplicativo utiliza?", botoes)
            elif status == "pilates_app":
                if msg_recebida == "Wellhub":
                    update_paciente(phone, {"convenio": "Wellhub", "status": "pilates_wellhub_id"})
                    responder_texto(phone, "Por favor, informe o seu *Wellhub ID* (Você pode encontrá-lo no seu aplicativo Wellhub, logo abaixo do seu nome na aba de Perfil).")
                else:
                    update_paciente(phone, {"convenio": "Totalpass", "status": "pilates_app_pref"})
                    botoes = [{"id": "pa_app", "title": "📱 App da Clínica"}, {"id": "pa_parceiro", "title": "🎫 App Parceiro"}]
                    enviar_botoes(phone, "Como prefere agendar as suas aulas?\n\n📱 *App da Clínica:* Baixe o nosso app (NextFit) para ter autonomia total.\n🎫 *App Parceiro:* Agende diretamente pelo app do Gympass/Totalpass.", botoes)
            elif status == "pilates_wellhub_id":
                update_paciente(phone, {"numCarteirinha": msg_recebida, "status": "pilates_app_pref"})
                botoes = [{"id": "pa_app", "title": "📱 App da Clínica"}, {"id": "pa_parceiro", "title": "🎫 App Parceiro"}]
                enviar_botoes(phone, "ID recebido! Como prefere agendar as suas aulas?\n\n📱 *App da Clínica:* Autonomia total de horários via NextFit.\n🎫 *App Parceiro:* Agende direto pelo app parceiro.", botoes)
            elif status == "pilates_app_pref":
                if "Clínica" in msg_recebida:
                    update_paciente(phone, {"status": "pilates_app_os"})
                    botoes = [{"id": "os_android", "title": "🤖 Android"}, {"id": "os_ios", "title": "🍏 iPhone"}]
                    enviar_botoes(phone, "Ótima escolha! Qual é o sistema do seu celular?", botoes)
                else:
                    update_paciente(phone, {"status": "atendimento_humano"})
                    responder_texto(phone, "Perfeito! Você pode agendar as aulas pelo parceiro. Nossa equipe vai confirmar o seu plano. Aguarde! 👩‍⚕️")
            elif status == "pilates_app_os":
                update_paciente(phone, {"status": "atendimento_humano"})
                link = "https://play.google.com/store/apps/details?id=br.com.nextfit.app" if "Android" in msg_recebida else "https://apps.apple.com/app/next-fit/id1451167440"
                responder_texto(phone, f"Aqui está o seu link: {link}\n\n1️⃣ Baixe e abra o app\n2️⃣ Busque por: Conectifisio - Ictus Fisioterapia SCS\n\nAguarde a nossa recepção liberar o acesso! 👩‍⚕️")

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"❌ Erro Crítico POST: {traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify_or_data():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro": return request.args.get("hub.challenge"), 200
    if request.args.get("action") == "get_patients":
        if not db: return jsonify({"items": []}), 200
        docs = db.collection("PatientsKanban").stream()
        patients = [{"id": d.id, **d.to_dict()} for d in docs]
        return jsonify({"items": patients}), 200
    return "Acesso Negado", 403

if __name__ == "__main__":
    app.run(port=5000)
