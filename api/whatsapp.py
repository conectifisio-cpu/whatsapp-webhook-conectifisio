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
# FUNÇÕES DE MEMÓRIA E AGENDA INTELIGENTE
# ==========================================
def get_paciente(phone):
    if not db: return {}
    doc = db.collection("PatientsKanban").document(phone).get()
    return doc.to_dict() if doc.exists else {}

def update_paciente(phone, data):
    if not db: return
    data["lastInteraction"] = firestore.SERVER_TIMESTAMP
    db.collection("PatientsKanban").document(phone).set(data, merge=True)

def get_proximos_dias_uteis(quantidade=3):
    """Calcula os próximos X dias úteis, começando sempre do dia seguinte"""
    dias = []
    data_atual = datetime.now()
    while len(dias) < quantidade:
        data_atual += timedelta(days=1)
        # 0=Segunda, 1=Terça, 2=Quarta, 3=Quinta, 4=Sexta
        if data_atual.weekday() < 5: 
            dias.append(data_atual)
    return dias

def gerar_horarios_disponiveis(periodo):
    """Gera opções distribuídas nos próximos 3 dias úteis"""
    dias = get_proximos_dias_uteis(3)
    horarios = []
    
    # Horários estratégicos para Consulta Ambulatorial
    if periodo.lower() == "manhã":
        slots = ["08:00", "09:30", "11:00"]
    else:
        slots = ["14:00", "15:30", "17:00"]
        
    for i in range(3):
        dia_str = dias[i].strftime("%d/%m")
        horarios.append(f"{dia_str} às {slots[i]}")
        
    return horarios

# ==========================================
# FUNÇÕES DO FEEGOW
# ==========================================
def formatar_data_feegow(data_br):
    data_limpa = re.sub(r'\D', '', str(data_br))
    if len(data_limpa) == 8: return f"{data_limpa[4:]}-{data_limpa[2:4]}-{data_limpa[:2]}"
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

def buscar_agendamentos_futuros(feegow_id):
    if not FEEGOW_TOKEN or not feegow_id: return None
    headers = {"Content-Type": "application/json", "x-access-token": FEEGOW_TOKEN}
    base_url = "https://api.feegow.com/v1/api"
    hoje = datetime.now()
    futuro = hoje + timedelta(days=30)
    data_start = hoje.strftime("%Y-%m-%d")
    data_end = futuro.strftime("%Y-%m-%d")
    try:
        url = f"{base_url}/appoints/search?paciente_id={feegow_id}&data_start={data_start}&data_end={data_end}"
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            dados = res.json()
            if dados.get("success") and dados.get("content"):
                lista_final = []
                for a in dados["content"]:
                    if "cancelado" not in str(a.get("status_nome", "")).lower():
                        try:
                            dt_obj = datetime.strptime(a.get("data", ""), "%Y-%m-%d")
                            lista_final.append(f"🗓️ *{dt_obj.strftime('%d/%m')} às {a.get('horario', '')[:5]}* - {a.get('procedimento_nome', 'Sessão')}")
                        except: pass
                return lista_final[:3]
    except: pass
    return []

def baixar_midia_whatsapp(media_id):
    if not media_id or not WHATSAPP_TOKEN: return None
    try:
        url_info = f"https://graph.facebook.com/v18.0/{media_id}"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        res_info = requests.get(url_info, headers=headers, timeout=10)
        if res_info.status_code != 200: return None
        media_url = res_info.json().get("url")
        mime_type = res_info.json().get("mime_type", "image/jpeg")
        res_download = requests.get(media_url, headers=headers, timeout=15)
        b64_data = base64.b64encode(res_download.content).decode('utf-8')
        return f"data:{mime_type};base64,{b64_data}"
    except: return None

def integrar_feegow(phone, info):
    if not FEEGOW_TOKEN: return {"feegow_status": "Token Ausente"}
    cpf = re.sub(r'\D', '', info.get("cpf", ""))
    if len(cpf) != 11: return {"feegow_status": "CPF Inválido"}
    celular = re.sub(r'\D', '', phone)
    if celular.startswith("55") and len(celular) > 11: celular = celular[2:]

    headers = {"Content-Type": "application/json", "x-access-token": FEEGOW_TOKEN}
    base_url = "https://api.feegow.com/v1/api"
    paciente_id = None
    
    try:
        res_search = requests.get(f"{base_url}/patient/search?paciente_cpf={cpf}&photo=false", headers=headers, timeout=10)
        if res_search.status_code == 200:
            dados = res_search.json()
            if dados.get("success") != False and dados.get("content"):
                paciente_id = dados.get("content", {}).get("paciente_id") or dados.get("content", {}).get("id")
    except: pass

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
            payload_create["convenio_id"] = convenio_id
            payload_create["plano_id"] = 0
            payload_create["matricula"] = info.get("numCarteirinha", "")

        try:
            res_create = requests.post(f"{base_url}/patient/create", json=payload_create, headers=headers, timeout=10)
            if res_create.status_code == 200:
                d = res_create.json()
                if d.get("success") != False: paciente_id = d.get("content", {}).get("paciente_id") or d.get("paciente_id")
        except: pass

    if paciente_id:
        paciente_id_int = int(paciente_id)
        cart_id = info.get("carteirinha_media_id")
        ped_id = info.get("pedido_media_id")
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

def responder_texto(to, texto):
    enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})

def enviar_botoes(to, texto, botoes):
    payload = {"type": "interactive", "interactive": {"type": "button", "body": {"text": texto}, "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}} for b in botoes]}}}
    enviar_whatsapp(to, payload)

def enviar_lista(to, texto, titulo_botao, secoes):
    payload = {"type": "interactive", "interactive": {"type": "list", "body": {"text": texto}, "action": {"button": titulo_botao[:20], "sections": secoes}}}
    enviar_whatsapp(to, payload)

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

        if msg_type == "text": msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            msg_recebida = message["interactive"].get("button_reply", {}).get("title", message["interactive"].get("list_reply", {}).get("title", ""))
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
        if len(msg_limpa) <= 25 and (any(msg_limpa.startswith(w) for w in ["obrigad", "obg", "ok", "valeu", "certo", "tá bom", "perfeito", "beleza"]) or any(char in msg_limpa for char in ["👍", "🙏", "❤️", "👏"])):
            is_courtesy = True

        if status == "finalizado":
            if is_courtesy:
                responder_texto(phone, "Por nada! 😊")
                return jsonify({"status": "courtesy_ignored"}), 200
            if is_veteran:
                update_paciente(phone, {"status": "menu_veterano"})
                botoes = [{"id": "v1", "title": "🗓️ Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, f"Olá, {info.get('title', 'paciente')}! ✨ Que bom ter você de volta. Como posso te ajudar hoje?", botoes)
                return jsonify({"status": "restart_veteran"}), 200
            else: status = "triagem"
        
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
                botoes = [{"id": "v1", "title": "🗓️ Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, f"Olá, {msg_recebida}! ✨ Que bom ter você de volta. Como posso te ajudar hoje?", botoes)
            else:
                update_paciente(phone, {"title": msg_recebida, "status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}]}]
                enviar_lista(phone, f"Prazer, {msg_recebida}! 😊\n\nQual serviço você procura hoje?", "Ver Serviços", secoes)

        elif status == "menu_veterano":
            if "Novo Serviço" in msg_recebida:
                update_paciente(phone, {"status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}, {"id": "e8", "title": "⬅️ Voltar ao Menu"}]}]
                enviar_lista(phone, "Perfeito! Qual novo serviço você deseja agendar?", "Ver Serviços", secoes)
            elif "Agendamentos" in msg_recebida:
                feegow_id = info.get("feegow_id")
                lista_sessoes = buscar_agendamentos_futuros(feegow_id)
                if lista_sessoes:
                    msg_agenda = "Localizei as suas próximas sessões: 👇\n\n" + "\n".join(lista_sessoes) + "\n\nO que deseja fazer?"
                    botoes = [{"id": "ag_ok", "title": "👍 Apenas Consultar"}, {"id": "ag_mudar", "title": "🔄 Reagendar/Cancelar"}, {"id": "ag_voltar", "title": "⬅️ Voltar"}]
                    update_paciente(phone, {"status": "vendo_agendamentos"})
                    enviar_botoes(phone, msg_agenda, botoes)
                else:
                    update_paciente(phone, {"status": "agendando"})
                    botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                    enviar_botoes(phone, "Não encontrei agendamentos futuros para si. 🤔\n\nPosso agendar um agora! Qual o melhor período? ☀️ ⛅", botoes)
            elif "Nova Guia" in msg_recebida or "Retomar" in msg_recebida:
                update_paciente(phone, {"status": "modalidade"})
                botoes = [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}]
                enviar_botoes(phone, "As novas sessões serão pelo seu CONVÊNIO ou de forma PARTICULAR?", botoes)

        elif status == "vendo_agendamentos":
            if "Reagendar" in msg_recebida:
                update_paciente(phone, {"status": "atendimento_humano"})
                responder_texto(phone, "Entendido! A nossa equipa de receção foi notificada para lhe dar prioridade e realizar a alteração do horário. Aguarde um instante! 👩‍⚕️")
            elif "Voltar" in msg_recebida:
                update_paciente(phone, {"status": "menu_veterano"})
                botoes = [{"id": "v1", "title": "🗓️ Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, "Voltando ao menu principal. 👇", botoes)
            else:
                update_paciente(phone, {"status": "finalizado"})
                responder_texto(phone, "Perfeito! Qualquer outra dúvida, estou por aqui. Tenha uma ótima sessão! ✨")

        elif status == "escolhendo_especialidade":
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
            update_paciente(phone, {"queixa": msg_recebida, "status": "modalidade" if servico not in ["Recovery", "Liberação Miofascial"] else "cadastrando_nome_completo"})
            if servico in ["Recovery", "Liberação Miofascial"]: responder_texto(phone, "Para cadastro, digite seu NOME COMPLETO:")
            else: 
                botoes = [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}]
                enviar_botoes(phone, "Deseja atendimento pelo seu CONVÊNIO ou PARTICULAR?", botoes)
                
        elif status == "modalidade":
            if "Convênio" in msg_recebida:
                update_paciente(phone, {"modalidade": "Convênio", "status": "nome_convenio"})
                secoes = [{"title": "Convênios", "rows": [{"id": "c1", "title": "Saúde Petrobras"}, {"id": "c5", "title": "Amil"}, {"id": "c6", "title": "Bradesco Saúde"}, {"id": "c7", "title": "Prevent Senior"}, {"id": "c8", "title": "Saúde Caixa"}]}]
                enviar_lista(phone, "Selecione seu plano:", "Ver Convênios", secoes)
            else:
                update_paciente(phone, {"modalidade": "Particular", "status": "cadastrando_nome_completo"})
                responder_texto(phone, "Digite seu NOME COMPLETO:")
                
        elif status == "nome_convenio":
            update_paciente(phone, {"convenio": msg_recebida, "status": "cadastrando_nome_completo"})
            responder_texto(phone, "Digite seu NOME COMPLETO:")
            
        elif status == "cadastrando_nome_completo":
            update_paciente(phone, {"title": msg_recebida, "status": "cpf"})
            responder_texto(phone, "Digite seu CPF (apenas números):")
            
        elif status == "cpf":
            update_paciente(phone, {"cpf": re.sub(r'\D','',msg_recebida), "status": "data_nascimento"})
            responder_texto(phone, "Qual sua data de nascimento? (DD/MM/AAAA)")
            
        elif status == "data_nascimento":
            update_paciente(phone, {"birthDate": msg_recebida, "status": "coletando_email"})
            responder_texto(phone, "Qual seu melhor E-MAIL?")
            
        elif status == "coletando_email":
            update_paciente(phone, {"email": msg_recebida, "status": "num_carteirinha" if modalidade=="Convênio" else "agendando"})
            if modalidade=="Convênio": responder_texto(phone, "Número da Carteirinha:")
            else: 
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                enviar_botoes(phone, "Qual o melhor período?", botoes)
                
        elif status == "num_carteirinha":
            update_paciente(phone, {"numCarteirinha": msg_recebida, "status": "foto_carteirinha"})
            responder_texto(phone, "Envie FOTO da Carteirinha:")
            
        elif status == "foto_carteirinha":
            if tem_anexo:
                update_paciente(phone, {"status": "foto_pedido_medico", "carteirinha_media_id": media_id})
                responder_texto(phone, "Foto recebida! Agora envie a FOTO DO PEDIDO MÉDICO:")
            else: responder_texto(phone, "Por favor, envie a foto.")
            
        elif status == "foto_pedido_medico":
            if tem_anexo:
                update_paciente(phone, {"status": "agendando", "pedido_media_id": media_id})
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                enviar_botoes(phone, "Tudo recebido! Qual o melhor período para a sua Consulta Ambulatorial?", botoes)
            else: responder_texto(phone, "Por favor, envie o pedido.")

        # 🚀 O PULO DO GATO: AGENDA VIRTUAL COM REGRA DOS 3 DIAS ÚTEIS
        elif status == "agendando":
            if msg_recebida in ["Manhã", "Tarde"]:
                info["periodo"] = msg_recebida
                
                # Regra exclusiva para Pacientes Novos de Convênio
                if not is_veteran and modalidade == "Convênio":
                    horarios_gerados = gerar_horarios_disponiveis(msg_recebida)
                    
                    rows = [{"id": f"h_{i}", "title": f"🗓️ {h}"} for i, h in enumerate(horarios_gerados)]
                    rows.append({"id": "h_outros", "title": "📅 Mais de 3 dias"}) # A Válvula de Escape
                    
                    secoes = [{"title": "Opções de Avaliação", "rows": rows}]
                    
                    update_paciente(phone, {"periodo": msg_recebida, "status": "escolhendo_horario_novo"})
                    
                    # Mensagem humanizada explicando a regra clínica
                    msg_regra = "Como precisamos de um prazo para solicitar a autorização junto ao seu convênio, nunca agendamos a avaliação inicial para o mesmo dia. 📄⏳\n\nPara agilizar, já busquei na agenda e separei as vagas mais próximas a partir do próximo dia útil. Qual fica melhor para você?"
                    
                    enviar_lista(phone, msg_regra, "Ver Datas", secoes)
                else:
                    # Pacientes Particulares ou Veteranos
                    update_data = {"periodo": msg_recebida, "status": "finalizado"}
                    if servico and "Pilates" not in servico:
                        res_feegow = integrar_feegow(phone, info)
                        if res_feegow: update_data.update(res_feegow)
                    
                    update_paciente(phone, update_data)
                    responder_texto(phone, f"Horário de {msg_recebida} pré-agendado! ✅ Nossa equipe vai finalizar o processo e confirmar em instantes.")
            else:
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                enviar_botoes(phone, "Escolha o período:", botoes)

        # 🚀 A ENCRUZILHADA FINAL DA AGENDA VIRTUAL
        elif status == "escolhendo_horario_novo":
            if "Mais de 3 dias" in msg_recebida:
                # Transbordo para Humano
                update_data = {"periodo": info.get("periodo", "Não especificado"), "status": "finalizado"}
                if servico and "Pilates" not in servico:
                    res_feegow = integrar_feegow(phone, info)
                    if res_feegow: update_data.update(res_feegow)
                    
                update_paciente(phone, update_data)
                responder_texto(phone, "Entendido! Como você precisa de uma data mais para a frente, a nossa equipa da receção vai assumir o atendimento agora para encontrar o dia perfeito na agenda. Aguarde um momento! 👩‍⚕️")
            else:
                # Agendou com a IA num dos 3 dias
                update_data = {"horario_escolhido": msg_recebida, "status": "finalizado"}
                if servico and "Pilates" not in servico:
                    res_feegow = integrar_feegow(phone, info)
                    if res_feegow: update_data.update(res_feegow)
                    
                update_paciente(phone, update_data)
                responder_texto(phone, f"Consulta Ambulatorial pré-agendada para {msg_recebida}! ✅\n\nNossa equipe já recebeu seus documentos para solicitar a autorização ao convênio e confirmará tudo consigo em instantes. Aguarde um momento! 👩‍⚕️")

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"❌ Erro Crítico: {traceback.format_exc()}")
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
