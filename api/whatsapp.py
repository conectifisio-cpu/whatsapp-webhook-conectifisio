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
# FUNÇÕES DE MEMÓRIA E BANCO
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
# MOTOR DE AGENDAMENTO (API FEEGOW REAL)
# ==========================================
def gerar_horarios_disponiveis(periodo, is_veteran):
    """MOCK DE SEGURANÇA: Só roda se a API do Feegow falhar"""
    dias_adicionais = 0 if is_veteran else 1
    data_alvo = datetime.now() + timedelta(days=dias_adicionais)
    if data_alvo.weekday() == 5: data_alvo += timedelta(days=2) # Pula Sabado
    elif data_alvo.weekday() == 6: data_alvo += timedelta(days=1) # Pula Domingo
    
    dia_str = data_alvo.strftime("%d/%m")
    if periodo.lower() == "manhã": return [f"{dia_str} às 08:00", f"{dia_str} às 09:30"]
    elif periodo.lower() == "tarde": return [f"{dia_str} às 14:00", f"{dia_str} às 15:30"]
    else: return [f"{dia_str} às 18:00", f"{dia_str} às 19:00"]

def buscar_horarios_feegow(unidade_nome, servico_nome, periodo, is_veteran):
    """BUSCA REAL NA API FEEGOW (D+1 Novo, D+0 Veterano)"""
    if not FEEGOW_TOKEN: 
        return gerar_horarios_disponiveis(periodo, is_veteran)[:2]
        
    local_id = 1 if "ipiranga" in str(unidade_nome).lower() else 0
    proc_id = 21 if "acupuntura" in str(servico_nome).lower() else 9 # Avaliação Inicial ID 9
    
    # Aplica regra de Janela de Agendamento
    dias_adicionais = 0 if is_veteran else 1
    data_alvo = datetime.now() + timedelta(days=dias_adicionais)
    
    # Pula finais de semana
    if data_alvo.weekday() == 5: data_alvo += timedelta(days=2)
    elif data_alvo.weekday() == 6: data_alvo += timedelta(days=1)
    
    data_str = data_alvo.strftime('%Y-%m-%d')
    slots = []
    
    url = f"https://api.feegow.com.br/v1/appoints/available-schedule?local_id={local_id}&procedimento_id={proc_id}&data={data_str}"
    headers = {"x-access-token": FEEGOW_TOKEN, "Content-Type": "application/json"}
    
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            dados = res.json()
            items = dados.get("data") or dados.get("content") or []
            for prof in items:
                for h in prof.get("horarios", []):
                    hora_str = h.get("hora", "")
                    try:
                        hora_int = int(hora_str.split(":")[0])
                        # Filtra pelo período escolhido
                        if periodo.lower() == "manhã" and hora_int < 12: slots.append(hora_str[:5])
                        elif periodo.lower() == "tarde" and 12 <= hora_int < 18: slots.append(hora_str[:5])
                        elif periodo.lower() == "noite" and hora_int >= 18: slots.append(hora_str[:5])
                    except: pass
    except Exception as e:
        print(f"Erro busca agenda Feegow: {e}")
    
    # Remove duplicados, ordena e aplica a REGRA DE QUANTIDADE (Apenas 2)
    slots = sorted(list(set(slots)))
    if slots:
        data_fmt = data_alvo.strftime("%d/%m")
        return [f"🗓️ {data_fmt} às {s}" for s in slots[:2]]
        
    return gerar_horarios_disponiveis(periodo, is_veteran)[:2]

# ==========================================
# INTEGRAÇÃO FEEGOW CADASTRO
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
    if "PORTO" in nome_upper: return 4
    if "GEAP" in nome_upper: return 6
    if "PREVENT" in nome_upper: return 7
    if "CASSI" in nome_upper: return 8
    if "PETROBRAS" in nome_upper: return 11
    if "MEDISERVICE" in nome_upper: return 9968
    if "CAIXA" in nome_upper: return 10154
    return 0

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

        msg_limpa = msg_recebida.lower().strip()

        # COMANDO DE RESET GLOBAL (TRAVA DE EMERGÊNCIA)
        if msg_limpa in ["recomeçar", "reset", "menu inicial", "⬅️ voltar ao menu"]:
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

        # ESCUDO ANTI-SAUDAÇÃO (Anti-Lixo)
        is_saudacao_lixo = False
        if status not in ["triagem", "finalizado", "menu_veterano", "escolhendo_especialidade", "agendando", "escolhendo_horario_novo"]:
            if len(msg_limpa) <= 20 and any(w in msg_limpa for w in ["oi", "olá", "ola", "bom dia", "boa tarde", "boa noite"]):
                is_saudacao_lixo = True
                
        if is_saudacao_lixo:
            botoes = [{"id": "btn_continuar", "title": "Sim, continuar"}, {"id": "btn_recomecar", "title": "Recomeçar"}]
            enviar_botoes(phone, "Olá! ✨ Notei que estávamos no meio do seu atendimento. Podemos continuar de onde paramos?", botoes)
            return jsonify({"status": "anti_lixo_ativado"}), 200
            
        if msg_recebida == "Sim, continuar":
            responder_texto(phone, "Ótimo! Por favor, responda a pergunta anterior para seguirmos.")
            return jsonify({"status": "continuando"}), 200

        # FILTRO DE CORTESIA NO FINAL
        if status == "finalizado":
            if len(msg_limpa) <= 25 and (any(msg_limpa.startswith(w) for w in ["obrigad", "obg", "ok", "valeu", "certo", "tá bom", "perfeito", "beleza"]) or any(char in msg_limpa for char in ["👍", "🙏", "❤️", "👏"])):
                responder_texto(phone, "Por nada! 😊 Nossa equipe vai cuidar de você.")
                return jsonify({"status": "courtesy_ignored"}), 200
            if is_veteran:
                update_paciente(phone, {"status": "menu_veterano"})
                botoes = [{"id": "v1", "title": "🗓️ Reagendar Sessão"}, {"id": "v2", "title": "🔄 Nova Guia"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, f"Olá, {info.get('title', 'paciente')}! ✨ Que bom ter você de volta. Como posso te ajudar hoje?", botoes)
                return jsonify({"status": "restart_veteran"}), 200
            else: status = "triagem"
        
        # --- MÁQUINA DE ESTADOS (FLUXO OFICIAL) ---
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
                botoes = [{"id": "v1", "title": "🗓️ Reagendar Sessão"}, {"id": "v2", "title": "🔄 Nova Guia"}, {"id": "v3", "title": "➕ Novo Serviço"}]
                enviar_botoes(phone, f"Olá, {msg_recebida}! ✨ Que bom ter você de volta. Como posso te ajudar hoje?", botoes)
            else:
                update_paciente(phone, {"title": msg_recebida, "status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}]}]
                enviar_lista(phone, f"Prazer, {msg_recebida}! 😊\n\nQual serviço você procura hoje?", "Ver Serviços", secoes)

        elif status == "menu_veterano":
            if "Novo Serviço" in msg_recebida:
                update_paciente(phone, {"status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}]}]
                enviar_lista(phone, "Perfeito! Qual novo serviço você deseja agendar?", "Ver Serviços", secoes)
            elif "Nova Guia" in msg_recebida or "Retomar" in msg_recebida:
                update_paciente(phone, {"status": "modalidade"})
                botoes = [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}]
                enviar_botoes(phone, "As novas sessões serão pelo seu CONVÊNIO ou de forma PARTICULAR?", botoes)
            elif "Reagendar" in msg_recebida:
                # Veterano Reagendando cai na mesma regra de oferecer D+0
                update_paciente(phone, {"status": "agendando"})
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}, {"id": "t3", "title": "Noite"}]
                enviar_botoes(phone, "Qual o melhor período para buscarmos as vagas da sua sessão? ☀️ ⛅", botoes)

        elif status == "escolhendo_especialidade":
            if msg_recebida == "Pilates Studio":
                if info.get("unit") == "Ipiranga":
                    update_paciente(phone, {"servico": msg_recebida, "status": "transferencia_pilates"})
                    botoes = [{"id": "tp_sim", "title": "Sim, mudar p/ SCS"}, {"id": "tp_nao", "title": "Não, escolher outro"}]
                    enviar_botoes(phone, "O Pilates Studio é uma modalidade exclusiva da unidade **SCS**. Deseja transferir o atendimento?", botoes)
                else:
                    update_paciente(phone, {"servico": msg_recebida, "status": "pilates_modalidade"})
                    secoes = [{"title": "Modalidade Pilates", "rows": [{"id": "p_part", "title": "💎 Plano Particular"}, {"id": "p_caixa", "title": "🏦 Saúde Caixa"}, {"id": "p_app", "title": "💪 Wellhub/Totalpass"}, {"id": "p_vol", "title": "⬅️ Voltar"}]}]
                    enviar_lista(phone, "Excelente! Como pretende realizar as aulas?", "Ver Opções", secoes)
            elif msg_recebida in ["Recovery", "Liberação Miofascial"]:
                update_paciente(phone, {"servico": msg_recebida, "modalidade": "Particular", "status": "cadastrando_queixa"})
                responder_texto(phone, f"Ótima escolha para performance em {msg_recebida}! 🚀\n\nMe conte brevemente: o que te trouxe aqui hoje?")
            elif msg_recebida == "Fisio Neurológica":
                update_paciente(phone, {"servico": msg_recebida, "status": "triagem_neuro"})
                botoes = [{"id": "n1", "title": "🔹 Independente"}, {"id": "n2", "title": "🤝 Semidependente"}, {"id": "n3", "title": "👨‍🦽 Dependente"}]
                enviar_botoes(phone, "Para agendarmos com o especialista ideal, como está a mobilidade do paciente?\n\n🔹 *Independente:* Faz tudo sozinho.\n🤝 *Semidependente:* Precisa de apoio.\n👨‍🦽 *Dependente:* Auxílio constante.", botoes)
            else:
                update_paciente(phone, {"servico": msg_recebida, "status": "cadastrando_queixa"})
                responder_texto(phone, f"Entendido! {msg_recebida} selecionada.\n\nMe conte brevemente: o que te trouxe à clínica hoje?")

        elif status == "transferencia_pilates":
            if "Sim" in msg_recebida or "mudar" in msg_recebida.lower():
                update_paciente(phone, {"unit": "SCS", "status": "pilates_modalidade"})
                secoes = [{"title": "Modalidade Pilates", "rows": [{"id": "p_part", "title": "💎 Plano Particular"}, {"id": "p_caixa", "title": "🏦 Saúde Caixa"}, {"id": "p_app", "title": "💪 Wellhub/Totalpass"}, {"id": "p_vol", "title": "⬅️ Voltar"}]}]
                enviar_lista(phone, "Perfeito! A sua unidade foi alterada para **SCS** com sucesso. ✅\n\nAgora, como você pretende realizar as aulas de Pilates?", "Ver Opções", secoes)
            else:
                update_paciente(phone, {"servico": "", "status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}]}]
                enviar_lista(phone, "Sem problemas! Mantemos o seu atendimento em Ipiranga. Qual outro serviço procura?", "Ver Serviços", secoes)

        # --- IA DE ESCUTA ATIVA ---
        elif status == "cadastrando_queixa":
            update_paciente(phone, {"queixa": msg_recebida, "status": "modalidade" if servico not in ["Recovery", "Liberação Miofascial"] else "cadastrando_nome_completo"})
            if servico in ["Recovery", "Liberação Miofascial"]: responder_texto(phone, "Para cadastro, digite seu NOME COMPLETO:")
            else: 
                botoes = [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}]
                enviar_botoes(phone, "Deseja atendimento pelo seu CONVÊNIO ou PARTICULAR?", botoes)
                
        elif status == "modalidade":
            if "Convênio" in msg_recebida:
                update_paciente(phone, {"modalidade": "Convênio", "status": "nome_convenio"})
                secoes = [{"title": "Convênios", "rows": [
                    {"id": "c1", "title": "Bradesco Saúde"},
                    {"id": "c2", "title": "Bradesco Operadora"},
                    {"id": "c3", "title": "Amil"},
                    {"id": "c4", "title": "Porto Seguro"},
                    {"id": "c5", "title": "GEAP"},
                    {"id": "c6", "title": "Prevent Senior"},
                    {"id": "c7", "title": "Cassi"},
                    {"id": "c8", "title": "Saúde Petrobras"},
                    {"id": "c9", "title": "Mediservice"},
                    {"id": "c10", "title": "Saúde Caixa"}
                ]}]
                enviar_lista(phone, "Selecione seu plano:", "Ver Convênios", secoes)
            else:
                update_paciente(phone, {"modalidade": "Particular", "status": "cadastrando_nome_completo"})
                responder_texto(phone, "Digite seu NOME COMPLETO:")
                
        elif status == "nome_convenio":
            if is_veteran:
                update_paciente(phone, {"convenio": msg_recebida, "status": "foto_carteirinha"})
                responder_texto(phone, f"Anotado: {msg_recebida}! ✅\n\nComo você já é paciente, pulei o preenchimento de CPF e E-mail! Por favor, envie uma FOTO NÍTIDA da sua carteirinha atualizada.")
            else:
                update_paciente(phone, {"convenio": msg_recebida, "status": "cadastrando_nome_completo"})
                responder_texto(phone, f"Anotado: {msg_recebida}! ✅\n\nAgora, digite seu NOME COMPLETO (conforme documento):")

        elif status == "cadastrando_nome_completo":
            update_paciente(phone, {"title": msg_recebida, "status": "cpf"})
            responder_texto(phone, "Nome registrado! ✅ Agora, digite seu CPF (apenas os 11 números):")

        elif status == "cpf":
            cpf_limpo = re.sub(r'\D', '', msg_recebida)
            if len(cpf_limpo) != 11:
                responder_texto(phone, "❌ O CPF informado parece incorreto. Por favor, envie os 11 números sem pontos ou traços.")
            else:
                update_paciente(phone, {"cpf": cpf_limpo, "status": "data_nascimento"})
                responder_texto(phone, "Recebido! ✅ Qual sua data de nascimento? (Ex: 15/05/1980)")

        elif status == "data_nascimento":
            update_paciente(phone, {"birthDate": msg_recebida, "status": "coletando_email"})
            responder_texto(phone, "Ótimo! Para finalizar seu cadastro, qual seu melhor E-MAIL?")

        elif status == "coletando_email":
            if modalidade == "Particular":
                update_paciente(phone, {"email": msg_recebida, "status": "agendando"})
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}, {"id": "t3", "title": "Noite"}]
                enviar_botoes(phone, "Cadastro concluído! 🎉\n\nQual o melhor período para verificarmos a sua vaga na agenda?", botoes)
            else:
                update_paciente(phone, {"email": msg_recebida, "status": "num_carteirinha"})
                responder_texto(phone, "Certo! E qual o NÚMERO DA CARTEIRINHA do seu plano? (Apenas números)")

        elif status == "num_carteirinha":
            num_limpo = re.sub(r'\D', '', msg_recebida)
            update_paciente(phone, {"numCarteirinha": num_limpo, "status": "foto_carteirinha"})
            responder_texto(phone, "Anotado! ✅ Agora a parte documental (Obrigatório para Convênio):\n\nEnvie uma FOTO NÍTIDA da sua carteirinha.")

        elif status == "foto_carteirinha":
            if not tem_anexo: responder_texto(phone, "❌ Não recebi a imagem. Por favor, envie a foto da sua carteirinha.")
            else:
                update_paciente(phone, {"status": "foto_pedido_medico", "carteirinha_media_id": media_id})
                responder_texto(phone, "Foto recebida! ✅\n\nAgora, envie a FOTO DO SEU PEDIDO MÉDICO.")

        elif status == "foto_pedido_medico":
            if not tem_anexo: responder_texto(phone, "❌ Por favor, envie a foto do seu Pedido Médico.")
            else:
                update_paciente(phone, {"status": "agendando", "pedido_media_id": media_id})
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}, {"id": "t3", "title": "Noite"}]
                enviar_botoes(phone, "Documentação completa! 🎉\n\nQual o melhor período para verificarmos a sua vaga de Avaliação?", botoes)

        # =========================================================
        # 🚀 O MOTOR DE AGENDAMENTO (O PULO DO GATO RESTABELECIDO)
        # =========================================================
        elif status == "agendando":
            if msg_recebida in ["Manhã", "Tarde", "Noite"]:
                info["periodo"] = msg_recebida
                
                # O Robô vai ao Feegow e varre a clínica atrás dos 2 próximos horários!
                slots = buscar_horarios_feegow(info.get("unit", "SCS"), servico, msg_recebida, is_veteran)
                
                update_paciente(phone, {"periodo": msg_recebida, "status": "escolhendo_horario_novo"})
                
                # Regra de Ouro: Exibir 2 opções exatas (A Falsa Escolha) + Botão de Fuga
                botoes = [{"id": f"h_{i}", "title": s[:20]} for i, s in enumerate(slots)]
                botoes.append({"id": "h_outros", "title": "📅 Outras opções"})
                
                msg_regra = "Encontrei estas vagas exatas na nossa agenda. Qual delas fica melhor para você?"
                if not is_veteran and modalidade == "Convênio":
                    msg_regra = "Como precisamos de um prazo para solicitar a autorização junto ao seu convênio, busquei as vagas a partir do próximo dia útil. Qual fica melhor?"
                
                enviar_botoes(phone, msg_regra, botoes)
            else:
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}, {"id": "t3", "title": "Noite"}]
                enviar_botoes(phone, "Por favor, utilize os botões para escolher o período:", botoes)

        elif status == "escolhendo_horario_novo":
            if "Outras opções" in msg_recebida or "Outras" in msg_recebida:
                update_data = {"status": "atendimento_humano"}
                # Dispara pro Feegow mesmo assim (sem horário final)
                if servico and "Pilates" not in servico:
                    res_feegow = integrar_feegow(phone, info)
                    if res_feegow: update_data.update(res_feegow)
                    
                update_paciente(phone, update_data)
                responder_texto(phone, "Entendido! Como você precisa de uma data mais para a frente, a nossa equipe da recepção vai assumir o atendimento agora para encontrar o dia perfeito. Aguarde um instante! 👩‍⚕️")
            else:
                # Fechamento com Horário Escolhido
                update_data = {"horario_escolhido": msg_recebida, "status": "finalizado"}
                
                # CRIA O PACIENTE E A AUTORIZAÇÃO (ID 9) NO FEEGOW
                if servico and "Pilates" not in servico:
                    res_feegow = integrar_feegow(phone, info)
                    if res_feegow: update_data.update(res_feegow)
                    
                update_paciente(phone, update_data)
                horario_limpo = msg_recebida.replace('🗓️ ', '')
                responder_texto(phone, f"Consulta pré-agendada para {horario_limpo}! ✅\n\nNossa equipe já recebeu seus dados e confirmará tudo com você em instantes. Aguarde um momento! 👩‍⚕️")


        # --- FLUXOS ISOLADOS DE PILATES MANTIDOS (Resumo Seguro) ---
        elif status.startswith("pilates_"):
            if status == "pilates_modalidade":
                if "Voltar" in msg_recebida:
                    update_paciente(phone, {"status": "escolhendo_especialidade"})
                    secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}]}]
                    enviar_lista(phone, "Voltando ao menu principal.", "Ver Serviços", secoes)
                elif "Wellhub" in msg_recebida or "Totalpass" in msg_recebida:
                    update_paciente(phone, {"modalidade": "Parceria App"})
                    if is_veteran:
                        update_paciente(phone, {"status": "pilates_app"})
                        botoes = [{"id": "w1", "title": "Wellhub"}, {"id": "t1", "title": "Totalpass"}]
                        enviar_botoes(phone, f"Prazer ter você aqui novamente! ✨ Qual desses aplicativos você utiliza?", botoes)
                    else:
                        update_paciente(phone, {"status": "pilates_app_nome_completo"})
                        responder_texto(phone, "Perfeito! ✅ Aceitamos os planos Golden (Wellhub) e TP5 (Totalpass).\n\nPara iniciarmos seu cadastro obrigatório, digite o seu NOME COMPLETO:")
                elif "Saúde Caixa" in msg_recebida:
                    update_paciente(phone, {"modalidade": "Convênio", "convenio": "Saúde Caixa"})
                    if is_veteran:
                        update_paciente(phone, {"status": "pilates_caixa_foto_pedido"})
                        responder_texto(phone, "Para seguirmos, envie uma FOTO do seu PEDIDO MÉDICO com indicação de Pilates.")
                    else:
                        update_paciente(phone, {"status": "pilates_caixa_nome"})
                        responder_texto(phone, "Entendido! 🏦 Para o plano Saúde Caixa, é obrigatório apresentar o pedido médico.\n\nDigite o seu NOME COMPLETO:")
                elif "Particular" in msg_recebida:
                    update_paciente(phone, {"modalidade": "Particular"})
                    update_paciente(phone, {"status": "pilates_part_exp"})
                    botoes = [{"id": "pe_sim", "title": "Sim, gostaria"}, {"id": "pe_nao", "title": "Não, já quero começar"}]
                    enviar_botoes(phone, "Ótima escolha! ✨ O Pilates vai ajudar a aliviar dores e fortalecer o corpo todo. Gostaria de agendar uma aula experimental gratuita?", botoes)
            # Fluxo Simplificado de Pilates
            elif status == "pilates_part_exp":
                update_paciente(phone, {"interesse_experimental": msg_recebida, "status": "pilates_part_periodo"})
                botoes = [{"id": "pe_m", "title": "☀️ Manhã"}, {"id": "pe_t", "title": "⛅ Tarde"}, {"id": "pe_n", "title": "🌙 Noite"}]
                enviar_botoes(phone, "Qual o melhor período?", botoes)
            elif status == "pilates_part_periodo":
                update_paciente(phone, {"periodo": msg_recebida})
                if is_veteran:
                    update_paciente(phone, {"status": "atendimento_humano"})
                    responder_texto(phone, "Tudo pronto! Nossa equipe vai assumir o atendimento para agendar. Aguarde! 👩‍⚕️")
                else:
                    update_paciente(phone, {"status": "pilates_part_nome"})
                    responder_texto(phone, "Para finalizarmos seu cadastro, digite seu NOME COMPLETO:")
            elif status == "pilates_part_nome":
                update_paciente(phone, {"title": msg_recebida, "status": "pilates_part_cpf"})
                responder_texto(phone, "Nome registrado! ✅ Agora, digite seu CPF (apenas números):")
            elif status == "pilates_part_cpf":
                update_paciente(phone, {"cpf": re.sub(r'\D','',msg_recebida), "status": "pilates_part_nasc"})
                responder_texto(phone, "Recebido! ✅ Qual sua data de nascimento? (Ex: 15/05/1980)")
            elif status == "pilates_part_nasc":
                update_paciente(phone, {"birthDate": msg_recebida, "status": "pilates_part_email"})
                responder_texto(phone, "Para completarmos, qual seu melhor E-MAIL?")
            elif status == "pilates_part_email":
                update_paciente(phone, {"email": msg_recebida, "status": "atendimento_humano"})
                responder_texto(phone, "Tudo pronto! Nossa equipe vai assumir o atendimento agora mesmo. Aguarde um instante! 👩‍⚕️")
            # Fluxo APP simplificado
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
                    responder_texto(phone, "Por favor, informe o seu Wellhub ID.")
                else:
                    update_paciente(phone, {"convenio": "Totalpass", "status": "pilates_app_pref"})
                    botoes = [{"id": "pa_app", "title": "📱 App da Clínica"}, {"id": "pa_parceiro", "title": "🎫 App Parceiro"}]
                    enviar_botoes(phone, "Como prefere agendar as suas aulas? Pelo nosso App ou App Parceiro?", botoes)
            elif status == "pilates_wellhub_id":
                update_paciente(phone, {"numCarteirinha": msg_recebida, "status": "pilates_app_pref"})
                botoes = [{"id": "pa_app", "title": "📱 App da Clínica"}, {"id": "pa_parceiro", "title": "🎫 App Parceiro"}]
                enviar_botoes(phone, "ID recebido! Como prefere agendar as suas aulas?", botoes)
            elif status == "pilates_app_pref":
                if "Clínica" in msg_recebida:
                    update_paciente(phone, {"status": "pilates_app_os"})
                    botoes = [{"id": "os_android", "title": "🤖 Android"}, {"id": "os_ios", "title": "🍏 iPhone"}]
                    enviar_botoes(phone, "Ótima escolha! Qual é o sistema do seu celular?", botoes)
                else:
                    update_paciente(phone, {"status": "atendimento_humano"})
                    responder_texto(phone, "Perfeito! Você pode agendar as aulas pelo parceiro. Nossa equipe vai confirmar o check-in. Aguarde! 👩‍⚕️")
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
