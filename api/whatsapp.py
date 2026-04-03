import os, requests, traceback, re, json, base64
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)

@app.after_request
def add_cors_headers(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

# ==========================================
# CONFIGURAÇÕES DE AMBIENTE
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
API_KEY = os.environ.get("GEMINI_API_KEY", "")
FEEGOW_TOKEN = os.environ.get("FEEGOW_TOKEN", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "conectifisio_webhook_2026")

print(f"🔧 [DEBUG] FEEGOW_TOKEN configurado: {'✅ SIM' if FEEGOW_TOKEN else '❌ NÃO'}")
print(f"🔧 [DEBUG] WHATSAPP_TOKEN configurado: {'✅ SIM' if WHATSAPP_TOKEN else '❌ NÃO'}")

# ==========================================
# INICIALIZAÇÃO DO FIREBASE
# ==========================================
firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
db = None
if firebase_creds_json:
    try:
        if not firebase_admin._apps:
            cred_dict = json.loads(firebase_creds_json)
            if 'private_key' in cred_dict:
                cred_dict['private_key'] = cred_dict['private_key'].replace('\\n', '\n')
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("✅ [DEBUG] Firebase inicializado com sucesso")
    except Exception as e:
        print(f"❌ [DEBUG] Erro ao inicializar Firebase: {e}")

# ==========================================
# CACHE EM MEMÓRIA PARA EVITAR EXCEDER COTA DO FIREBASE
# ==========================================
_patients_cache = {"data": None, "ts": 0}
_CACHE_TTL = 12

# ==========================================
# UNIDADES — ENDEREÇOS E RECOMENDAÇÕES
# ==========================================
UNIDADES = {
    "Ipiranga": {
        "endereco": "R. Visc. de Pirajá, 525 - Vila Dom Pedro I, São Paulo - SP, 04277-020",
        "maps": "https://maps.app.goo.gl/MCoghy1k9LGfSaGx9",
        "recomendacao": "Traga o pedido médico original e chegue com 15 minutos de antecedência."
    },
    "São Caetano": {
        "endereco": "Av. Goiás, 1100 - Centro, São Caetano do Sul - SP, 09520-000",
        "maps": "https://maps.app.goo.gl/mhct13HEmChxmfJF8",
        "recomendacao": "Traga o pedido médico original e chegue com 15 minutos de antecedência."
    }
}

# ==========================================
# VALIDAÇÕES DE SEGURANÇA
# ==========================================
def validar_cpf(cpf_str):
    cpf = re.sub(r'\D', '', str(cpf_str))
    if len(cpf) != 11 or len(set(cpf)) == 1:
        return False
    for i in range(9, 11):
        valor = sum((int(cpf[num]) * ((i + 1) - num) for num in range(0, i)))
        digito = ((valor * 10) % 11) % 10
        if digito != int(cpf[i]):
            return False
    return True

def validar_data_nascimento(data_str):
    if not re.match(r'^\d{2}/\d{2}/\d{4}$', data_str):
        return False
    try:
        data_obj = datetime.strptime(data_str, "%d/%m/%Y")
        hoje = datetime.now()
        if data_obj > hoje or data_obj.year < (hoje.year - 120):
            return False
        return True
    except ValueError:
        return False

# ==========================================
# FUNÇÕES DE MEMÓRIA E HISTÓRICO (FIREBASE)
# ==========================================
def get_paciente(phone):
    if not db: return {}
    doc = db.collection("PatientsKanban").document(phone).get()
    return doc.to_dict() if doc.exists else {}

def update_paciente(phone, data):
    if not db: return
    data["lastInteraction"] = firestore.SERVER_TIMESTAMP
    db.collection("PatientsKanban").document(phone).set(data, merge=True)

def registrar_historico(phone, remetente, tipo, conteudo):
    if not db: return
    nova_msg = {
        "de": remetente,
        "tipo": tipo,
        "conteudo": conteudo,
        "data": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S+00:00')
    }
    db.collection("PatientsKanban").document(phone).set({
        "historico": firestore.ArrayUnion([nova_msg]),
        "lastInteraction": firestore.SERVER_TIMESTAMP
    }, merge=True)

# ==========================================
# INTEGRAÇÃO FEEGOW (COM DEBUG DETALHADO)
# ==========================================
def get_feegow_headers():
    headers = {
        "Content-Type": "application/json", 
        "Authorization": f"Bearer {FEEGOW_TOKEN}",
        "User-Agent": "Integracao-Conectifisio/1.0"
    }
    print(f"🔧 [DEBUG] Headers Feegow: {json.dumps({k: v[:10]+'...' if len(v) > 10 else v for k,v in headers.items()})}")
    return headers

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

def verificar_cobertura(convenio, servico):
    conv = str(convenio).lower()
    serv = str(servico).lower()
    if "pélvica" in serv:
        if any(x in conv for x in ["amil", "bradesco", "porto", "mediservice"]): return False
    if "acupuntura" in serv:
        if not any(x in conv for x in ["prevent", "caixa", "geap", "blue"]): return False
    if "pilates" in serv:
        if "caixa" not in conv: return False
    return True

def buscar_feegow_por_cpf(cpf):
    if not FEEGOW_TOKEN: return None
    cpf_limpo = re.sub(r'\D', '', str(cpf))
    url = f"https://api.feegow.com/v1/api/patient/search?paciente_cpf={cpf_limpo}&photo=false"
    try:
        res = requests.get(url, headers=get_feegow_headers(), timeout=10)
        if res.status_code == 200:
            dados = res.json()
            if dados.get("success") != False and dados.get("content"):
                p = dados["content"][0] if isinstance(dados["content"], list) else dados["content"]
                print(f"✅ [DEBUG] Paciente encontrado em Feegow: {p.get('nome_completo')}")
                return {"id": p.get("paciente_id") or p.get("id"), "nome": p.get("nome_completo") or p.get("nome")}
    except Exception as e:
        print(f"❌ [DEBUG] Erro ao buscar paciente Feegow: {e}")
    return None

def buscar_horarios_disponiveis(paciente_id):
    """
    🆕 NOVA FUNÇÃO: Busca horários disponíveis com DEBUG detalhado
    """
    print(f"\n🔍 [DEBUG] ===== INICIANDO buscar_horarios_disponiveis =====")
    print(f"🔍 [DEBUG] Paciente ID: {paciente_id}")
    print(f"🔍 [DEBUG] Token Feegow presente: {'✅ SIM' if FEEGOW_TOKEN else '❌ NÃO'}")
    
    if not FEEGOW_TOKEN or not paciente_id:
        print(f"❌ [DEBUG] Dados inválidos - retornando None")
        return None
    
    hoje = datetime.now()
    futuro = hoje + timedelta(days=30)
    
    url = f"https://agenda-api.feegow.com.br/v1/appoints/available-schedule"
    params = {
        "patient_id": paciente_id,
        "start_date": hoje.strftime('%Y-%m-%d'),
        "end_date": futuro.strftime('%Y-%m-%d')
    }
    
    print(f"🔍 [DEBUG] URL: {url}")
    print(f"🔍 [DEBUG] Params: {params}")
    
    try:
        headers = get_feegow_headers()
        print(f"🔍 [DEBUG] Headers preparados, fazendo requisição...")
        
        res = requests.get(url, params=params, headers=headers, timeout=10)
        
        print(f"🔍 [DEBUG] Status Code: {res.status_code}")
        print(f"🔍 [DEBUG] Response: {res.text[:200]}...")
        
        if res.status_code == 200:
            dados = res.json()
            print(f"🔍 [DEBUG] JSON recebido: {json.dumps(dados)[:300]}...")
            
            if dados.get("data"):
                print(f"✅ [DEBUG] {len(dados['data'])} horários encontrados!")
                return dados["data"]
            else:
                print(f"⚠️ [DEBUG] Nenhum horário encontrado no response")
                return None
        else:
            print(f"❌ [DEBUG] Status {res.status_code} - {res.text[:100]}")
            return None
            
    except Exception as e:
        print(f"❌ [DEBUG] EXCEÇÃO em buscar_horarios_disponiveis: {str(e)}")
        print(f"❌ [DEBUG] Traceback: {traceback.format_exc()}")
        return None

def integrar_feegow(phone, info):
    if not FEEGOW_TOKEN: return {"feegow_status": "Token Ausente"}
    cpf = re.sub(r'\D', '', info.get("cpf", ""))
    feegow_id = info.get("feegow_id")
    if not feegow_id and cpf:
        busca = buscar_feegow_por_cpf(cpf)
        if busca: feegow_id = busca['id']

    base_url = "https://api.feegow.com/v1/api"
    celular = re.sub(r'\D', '', phone)
    if celular.startswith("55") and len(celular) > 11: celular = celular[2:]
    
    convenio_id = mapear_convenio(info.get("convenio", ""))
    matricula = info.get("numCarteirinha", "")

    if not feegow_id:
        payload_create = {
            "nome_completo": info.get("title", "Paciente Sem Nome"), "cpf": cpf,
            "data_nascimento": formatar_data_feegow(info.get("birthDate", "")),
            "celular1": celular, "email1": info.get("email", "")
        }
        if convenio_id > 0: payload_create.update({"convenio_id": convenio_id, "plano_id": 0, "matricula": matricula})
        try:
            res_create = requests.post(f"{base_url}/patient/create", json=payload_create, headers=get_feegow_headers(), timeout=10)
            if res_create.status_code == 200 and res_create.json().get("success") != False:
                feegow_id = res_create.json().get("content", {}).get("paciente_id") or res_create.json().get("paciente_id")
                print(f"✅ [DEBUG] Novo paciente criado em Feegow: {feegow_id}")
        except Exception as e:
            print(f"❌ [DEBUG] Erro ao criar paciente Feegow: {e}")

    if feegow_id:
        return {"feegow_id": int(feegow_id), "feegow_status": f"ID: {feegow_id}"}
        
    return {"feegow_status": "Erro Integração"}

# ==========================================
# MENSAGERIA E IA
# ==========================================
def chamar_gemini(query):
    if not API_KEY: return None
    system_prompt = "Atue como o Assistente Virtual da clínica Conectifisio. Seu tom de voz deve ser brasileiro (PT-BR), acolhedor."
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={API_KEY}"
    payload = {"contents": [{"parts": [{"text": query[:300]}]}], "systemInstruction": {"parts": [{"text": system_prompt}]}}
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200: return res.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
    except: pass
    return None

def enviar_whatsapp(to, payload_msg):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, **payload_msg}
    try: return requests.post(url, json=payload, headers=headers, timeout=10)
    except: return None

def responder_texto(to, texto, remetente="robo"):
    registrar_historico(to, remetente, "texto", texto)
    return enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})

def enviar_botoes(to, texto, botoes):
    registrar_historico(to, "robo", "texto", texto)
    return enviar_whatsapp(to, {
        "type": "interactive",
        "interactive": {"type": "button", "body": {"text": texto}, "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}} for b in botoes]}}
    })

def enviar_lista(to, texto, titulo_botao, secoes):
    registrar_historico(to, "robo", "texto", texto)
    return enviar_whatsapp(to, {
        "type": "interactive",
        "interactive": {"type": "list", "body": {"text": texto}, "action": {"button": titulo_botao[:20], "sections": secoes}}
    })

# ==========================================
# WEBHOOK PRINCIPAL (REDUZIDO PARA AGENDANDO)
# ==========================================
@app.route("/api/whatsapp", methods=["GET", "POST", "OPTIONS"])
def webhook():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200
        
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN: 
            return request.args.get("hub.challenge"), 200
        return "Acesso Negado", 403

    # --- POST: RECEBER MENSAGENS WHATSAPP ---
    data = request.get_json()
    if not data or "entry" not in data: 
        return jsonify({"status": "ok"}), 200

    try:
        val = data["entry"][0]["changes"][0]["value"]
        if "messages" not in val: 
            return jsonify({"status": "not_a_message"}), 200

        message = val["messages"][0]
        phone = message["from"]
        msg_type = message.get("type")
        
        info = get_paciente(phone)
        status_atual = info.get("status", "triagem") if info else "triagem"

        if msg_type not in ["text", "interactive", "image", "document"]:
            return jsonify({"status": "tipo_ignorado"}), 200

        msg_recebida = "Anexo Recebido"
        tem_anexo = False
        media_id = None 

        if msg_type == "text": 
            msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive": 
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))
        elif msg_type in ["image", "document"]:
            tem_anexo = True
            media_id = message.get(msg_type, {}).get("id")

        registrar_historico(phone, "paciente", "texto" if not tem_anexo else "anexo", msg_recebida)

        print(f"\n📱 [DEBUG] Mensagem recebida de {phone}")
        print(f"📱 [DEBUG] Status atual: {status_atual}")
        print(f"📱 [DEBUG] Mensagem: {msg_recebida}")
        print(f"📱 [DEBUG] Feegow ID no arquivo: {info.get('feegow_id')}")

        if not info:
            info = {"cellphone": phone, "status": "triagem"}
            update_paciente(phone, info)

        # ==========================================
        # 🎯 FLUXO AGENDANDO COM DEBUG
        # ==========================================
        if status_atual == "agendando":
            print(f"\n🎯 [DEBUG] ===== ESTADO: AGENDANDO =====")
            
            if msg_recebida in ["Manhã", "Tarde", "Noite"]:
                print(f"🎯 [DEBUG] Período selecionado: {msg_recebida}")
                
                info["periodo"] = msg_recebida
                update_data = {"periodo": msg_recebida, "status": "finalizado"}
                
                # ✅ NOVO: Tentar buscar horários (com DEBUG)
                print(f"🎯 [DEBUG] Feegow ID para buscar horários: {info.get('feegow_id')}")
                
                if info.get("feegow_id"):
                    print(f"✅ [DEBUG] Feegow ID presente, chamando buscar_horarios_disponiveis()...")
                    try:
                        horarios = buscar_horarios_disponiveis(info.get("feegow_id"))
                        if horarios:
                            print(f"✅ [DEBUG] {len(horarios)} horários encontrados para {phone}")
                        else:
                            print(f"⚠️ [DEBUG] Nenhum horário disponível para {phone}")
                    except Exception as e:
                        print(f"❌ [DEBUG] ERRO ao buscar horários: {str(e)}")
                        print(f"❌ [DEBUG] Traceback completo: {traceback.format_exc()}")
                else:
                    print(f"⚠️ [DEBUG] Feegow ID VAZIO - não buscando horários")
                
                # Integração Feegow normal
                servico = info.get("servico", "")
                if servico and "Pilates" not in servico:
                    print(f"🎯 [DEBUG] Integrando com Feegow (serviço: {servico})...")
                    resultado_feegow = integrar_feegow(phone, info)
                    if resultado_feegow: 
                        update_data.update(resultado_feegow)
                        print(f"✅ [DEBUG] Integração Feegow sucesso: {resultado_feegow}")
                
                update_paciente(phone, update_data)
                
                modalidade = info.get("modalidade", "")
                if modalidade == "Convênio":
                    texto_final = (f"Período selecionado com sucesso! ✅ Nossa recepção já recebeu as suas fotos e está realizando a validação de cobertura junto ao seu plano de saúde.\n\n"
                                   f"Assim que a elegibilidade for confirmada, enviaremos as opções de horários disponíveis. Fique de olho por aqui! 😊")
                else:
                    texto_final = (f"Período selecionado com sucesso! ✅ Tudo pronto! A nossa equipe já está verificando a disponibilidade dos nossos especialistas para o período da {msg_recebida}.\n\n"
                                   f"Em instantes voltaremos com as opções exatas para confirmarmos o seu horário. Fique de olho por aqui! ✨")
                
                responder_texto(phone, texto_final)
                print(f"✅ [DEBUG] Mensagem final enviada")
            else:
                print(f"⚠️ [DEBUG] Período inválido: {msg_recebida}")
                enviar_botoes(phone, "Por favor, escolha o período:", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])

        return jsonify({"status": "success"}), 200
        
    except Exception as e:
        print(f"❌ [DEBUG] ERRO CRÍTICO: {str(e)}")
        print(f"❌ [DEBUG] Traceback: {traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 200

if __name__ == "__main__":
    print("🚀 [DEBUG] Iniciando aplicação...")
    app.run(port=5000)
