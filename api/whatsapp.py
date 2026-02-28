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
# FEEGOW: BUSCAS & SONDA MÚLTIPLA COM UNIDADE
# ==========================================
def buscar_veterano_feegow_celular(phone):
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
                p = dados["content"][0]
                return {"feegow_id": p.get("id") or p.get("paciente_id"), "title": p.get("nome", "Paciente"), "cpf": re.sub(r'\D', '', str(p.get("cpf", "")))}
    except: pass
    return None

def buscar_feegow_id_por_cpf(cpf):
    if not FEEGOW_TOKEN or not cpf: return None
    cpf_limpo = re.sub(r'\D', '', cpf)
    headers = {"Content-Type": "application/json", "x-access-token": FEEGOW_TOKEN}
    try:
        res = requests.get(f"https://api.feegow.com/v1/api/patient/search?paciente_cpf={cpf_limpo}&photo=false", headers=headers, timeout=5)
        if res.status_code == 200 and res.json().get("success"):
            return res.json().get("content", {}).get("paciente_id") or res.json().get("content", {}).get("id")
    except: pass
    return None

def processar_resultado_feegow(dados, hoje):
    """Extrai a lista de sessões de forma segura"""
    itens = dados.get("content") or dados.get("data") or []
    lista_final = []
    for a in itens:
        status = str(a.get("status_nome", "")).lower()
        if "cancelado" not in status and "falta" not in status:
            data_raw = str(a.get("data", ""))
            if "T" in data_raw: data_raw = data_raw.split("T")[0]
            try:
                dt_obj = datetime.strptime(data_raw, "%Y-%m-%d")
                if dt_obj.date() >= hoje.date():
                    proc = a.get("procedimento", {}).get("nome", a.get("procedimento_nome", "Sessão")) if isinstance(a.get("procedimento"), dict) else a.get("procedimento_nome", "Sessão")
                    hora = str(a.get('horario', ''))[:5]
                    lista_final.append(f"🗓️ *{dt_obj.strftime('%d/%m/%Y')} às {hora}* - {proc}")
            except: pass
    return lista_final[:3]

def buscar_agendamentos_futuros_com_debug(feegow_id, unidade_nome):
    """Sonda Injeta o Local ID para tentar quebrar o erro 422"""
    if not FEEGOW_TOKEN: return [], "ERRO: Token não configurado."
    if not feegow_id: return [], "ERRO: O Paciente não tem feegow_id atrelado."
    
    local_id = 1 if "ipiranga" in str(unidade_nome).lower() else 0
    hoje = datetime.now()
    futuro = hoje + timedelta(days=60)
    d_start = hoje.strftime('%Y-%m-%d')
    d_end = futuro.strftime('%Y-%m-%d')
    
    headers_old = {
        "Content-Type": "application/json", 
        "Accept": "application/json", 
        "x-access-token": FEEGOW_TOKEN
    }
    
    # Formato idêntico ao que funciona no WIX para a nova API
    headers_new = {
        "Content-Type": "application/json", 
        "Accept": "application/json", 
        "Authorization": FEEGOW_TOKEN 
    }
    
    debug_msg = f"🔍 PROBE LOCAL_ID\nID: {feegow_id} | Local: {local_id}\n\n"
    
    # Sonda 1: Old API /appoints com RANGE e LOCAL_ID (Para corrigir o 422)
    url1 = f"https://api.feegow.com/v1/api/appoints?local_id={local_id}&paciente_id={feegow_id}&data_start={d_start}&data_end={d_end}"
    try:
        r1 = requests.get(url1, headers=headers_old, timeout=5)
        debug_msg += f"S1 (Old c/ Range): {r1.status_code} | {r1.text[:50]}\n"
        if r1.status_code == 200 and r1.json().get("success") != False:
            res = processar_resultado_feegow(r1.json(), hoje)
            if res: return res, ""
    except Exception as e: debug_msg += f"S1 Erro: {e}\n"

    # Sonda 2: Old API /appoints APENAS PARA HOJE (Para ver se devolve 200)
    url2 = f"https://api.feegow.com/v1/api/appoints?local_id={local_id}&paciente_id={feegow_id}&data={d_start}"
    try:
        r2 = requests.get(url2, headers=headers_old, timeout=5)
        debug_msg += f"S2 (Old Dia Único): {r2.status_code} | {r2.text[:50]}\n"
        if r2.status_code == 200 and r2.json().get("success") != False:
            res = processar_resultado_feegow(r2.json(), hoje)
            if res: return res, ""
    except Exception as e: debug_msg += f"S2 Erro: {e}\n"

    # Sonda 3: Nova API com Header idêntico ao do WIX
    url3 = f"https://api.feegow.com.br/v1/agendamentos?paciente_id={feegow_id}"
    try:
        r3 = requests.get(url3, headers=headers_new, timeout=5)
        debug_msg += f"S3 (New Agendamentos): {r3.status_code} | {r3.text[:50]}\n"
        if r3.status_code == 200 and r3.json().get("success") != False:
            res = processar_resultado_feegow(r3.json(), hoje)
            if res: return res, ""
    except Exception as e: debug_msg += f"S3 Erro: {e}\n"

    # Sonda 4: Rota relacional de Paciente na Nova API
    url4 = f"https://api.feegow.com.br/v1/pacientes/{feegow_id}/agendamentos"
    try:
        r4 = requests.get(url4, headers=headers_new, timeout=5)
        debug_msg += f"S4 (New Pacientes/Agend): {r4.status_code} | {r4.text[:50]}\n"
        if r4.status_code == 200 and r4.json().get("success") != False:
            res = processar_resultado_feegow(r4.json(), hoje)
            if res: return res, ""
    except Exception as e: debug_msg += f"S4 Erro: {e}\n"

    return [], debug_msg

# ==========================================
# MOTOR DE AGENDAMENTO (CRIAR NOVOS)
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
    slots = ["08:00", "09:30", "11:00"] if periodo.lower() == "manhã" else ["14:00", "15:30", "17:00"]
    return [f"{dias[i].strftime('%d/%m')} às {slots[i]}" for i in range(3)]

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

# ==========================================
# MENSAGERIA
# ==========================================
def enviar_whatsapp(to, payload_msg):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, **payload_msg}
    try: requests.post(url, json=payload, headers=headers, timeout=10)
    except: pass

def responder_texto(to, texto): enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})
def enviar_botoes(to, texto, botoes): enviar_whatsapp(to, {"type": "interactive", "interactive": {"type": "button", "body": {"text": texto}, "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}} for b in botoes]}}})

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
        if msg_type == "text": 
            msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            msg_recebida = message["interactive"].get("button_reply", {}).get("title", message["interactive"].get("list_reply", {}).get("title", ""))

        msg_limpa = msg_recebida.lower().strip()

        if msg_limpa in ["recomeçar", "reset", "menu inicial"]:
            update_paciente(phone, {"status": "menu_veterano"})
            botoes = [{"id": "v1", "title": "🗓️ Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia"}]
            enviar_botoes(phone, "Atendimento reiniciado para o teste de Agendamentos! 👇", botoes)
            return jsonify({"status": "reset"}), 200

        info = get_paciente(phone)
        if not info:
            info = {"cellphone": phone, "status": "menu_veterano"}
            update_paciente(phone, info)
            
        status = info.get("status", "menu_veterano")

        if status == "menu_veterano" or status == "vendo_agendamentos" or status == "agendando":
            
            if "Agendamentos" in msg_recebida or "Reagendar" in msg_recebida:
                feegow_id = info.get("feegow_id")
                
                if not feegow_id:
                    vet = buscar_veterano_feegow_celular(phone)
                    if vet and vet.get("feegow_id"):
                        feegow_id = vet["feegow_id"]
                    elif info.get("cpf"):
                        feegow_id = buscar_feegow_id_por_cpf(info.get("cpf"))
                    
                    if feegow_id:
                        update_paciente(phone, {"feegow_id": feegow_id})
                
                unidade_nome = info.get("unit", "SCS") # Pega a unidade salva
                lista_sessoes, log_debug = buscar_agendamentos_futuros_com_debug(feegow_id, unidade_nome)
                
                if lista_sessoes:
                    msg_agenda = "Localizei suas próximas sessões: 👇\n\n" + "\n".join(lista_sessoes) + "\n\nO que deseja fazer?"
                    botoes = [{"id": "ag_ok", "title": "👍 Apenas Consultar"}, {"id": "ag_mudar", "title": "🔄 Reagendar/Cancelar"}]
                    update_paciente(phone, {"status": "vendo_agendamentos"})
                    enviar_botoes(phone, msg_agenda, botoes)
                else:
                    update_paciente(phone, {"status": "agendando"})
                    
                    msg_erro = "Não encontrei agendamentos futuros para você no sistema. 🤔\n\n"
                    if log_debug:
                        msg_erro += f"*{log_debug}*\n\n"
                        
                    msg_erro += "Posso agendar um agora! Qual o melhor período? ☀️ ⛅"
                    
                    botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}, {"id": "t3", "title": "Noite"}]
                    enviar_botoes(phone, msg_erro, botoes)
            
            elif "Nova Guia" in msg_recebida or "Novo Serviço" in msg_recebida:
                responder_texto(phone, "Para focarmos no teste da agenda, por favor clique em '🗓️ Agendamentos'. Se quiser recomeçar, digite 'Reset'.")
                
            elif status == "vendo_agendamentos":
                if "Reagendar" in msg_recebida:
                    responder_texto(phone, "Entendido! A nossa equipe da recepção foi notificada para realizar a alteração do horário com prioridade. Aguarde um instante! 👩‍⚕️")
                else:
                    responder_texto(phone, "Perfeito! Qualquer outra dúvida, estou por aqui. Tenha uma ótima sessão! ✨")
                    
            elif status == "agendando" and msg_recebida in ["Manhã", "Tarde", "Noite"]:
                responder_texto(phone, f"Horário de {msg_recebida} recebido (Fim do fluxo de teste). Digite 'Reset' para testar novamente.")
            else:
                botoes = [{"id": "v1", "title": "🗓️ Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia"}]
                enviar_botoes(phone, "Estamos no modo de Teste de Agenda. Por favor, clique abaixo:", botoes)

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"❌ Erro Crítico POST: {traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify_or_data():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro": return request.args.get("hub.challenge"), 200
    return "Acesso Negado", 403

if __name__ == "__main__":
    app.run(port=5000)
