import os
import json
import re
import traceback
import io
import requests
import base64
from datetime import datetime, timedelta, timezone
import threading
_thread_local = threading.local()
try:
    from PIL import Image as PILImage
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False
from flask import Flask, request, jsonify, send_from_directory, render_template
import firebase_admin
from firebase_admin import credentials, firestore, storage as fb_storage
from totem import totem_bp
# Configura o Flask para encontrar templates e static a partir da raiz do projeto
_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
app = Flask(__name__, template_folder=os.path.join(_ROOT_DIR, 'templates'), static_folder=os.path.join(_ROOT_DIR, 'static'))
app.register_blueprint(totem_bp)
@app.after_request
def add_cors_headers(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

@app.route("/")
def serve_dashboard():
    return render_template('index.html')

# ==========================================
# CONFIGURAÇÕES DE AMBIENTE E UNIDADES
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
API_KEY = os.environ.get("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "ft:gpt-4o-mini-2024-07-18:conectifisio:conectifisio-v7:DahD76G7")
OPENAI_FAQ_MODEL = os.environ.get("OPENAI_FAQ_MODEL", OPENAI_MODEL) # Modelo FAQ ativo (v2 quando disponível)
FEEGOW_TOKEN = os.environ.get("FEEGOW_TOKEN", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "conectifisio_webhook_2026")
PORTO_SEGURO_CPF = os.environ.get("PORTO_SEGURO_CPF", "25052258852")
PORTO_SEGURO_SENHA = os.environ.get("PORTO_SEGURO_SENHA", "")
PORTO_SEGURO_CODIGO_PRESTADOR = "512560"
PORTO_SEGURO_TUSS_FISIO = "25090089"

# ==========================================
# INICIALIZAÇÃO DO FIREBASE
# ==========================================
firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
FIREBASE_STORAGE_BUCKET = os.environ.get("FIREBASE_STORAGE_BUCKET", "conectifisio-bot.firebasestorage.app")
db = None
storage_bucket = None
if firebase_creds_json:
    try:
        if not firebase_admin._apps:
            cred_dict = json.loads(firebase_creds_json)
            if 'private_key' in cred_dict:
                cred_dict['private_key'] = cred_dict['private_key'].replace('\\n', '\n')
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred, {'storageBucket': FIREBASE_STORAGE_BUCKET})
        db = firestore.client()
        try:
            storage_bucket = fb_storage.bucket()
            import sys; print(f"[STORAGE] Bucket inicializado: {storage_bucket.name}", file=sys.stderr)
        except Exception as e_storage:
            import sys; print(f"[STORAGE] Falha ao inicializar bucket: {e_storage}", file=sys.stderr)
            storage_bucket = None
    except: pass

# ==========================================
# CACHE EM MEMÓRIA PARA EVITAR EXCEDER COTA DO FIREBASE
# ==========================================
_patients_cache = {"data": None, "ts": 0}
_CACHE_TTL = 25  # segundos — Dashboard atualiza a cada 20s, cache de 25s evita leituras duplas
# Nota: Firestore Free Tier = 50.000 leituras/dia. Com TTL=25s: máx ~3.456 leituras/dia (seguro)

# ==========================================
# UNIDADES — ENDEREÇOS E RECOMENDAÇÕES
# ==========================================
UNIDADES = {
    "Ipiranga": {
        "endereco": "Rua Visconde de Pirajá, 525 - Vila Dom Pedro I, São Paulo - SP (Próximo ao Metrô Alto do Ipiranga)",
        "maps": "https://maps.app.goo.gl/MCoghy1k9LGfSaGx9",
        "recomendacao": "Traga o pedido médico original, documento com foto e carteirinha. Chegue com 15 minutos de antecedência. Unidade próxima ao metrô Alto do Ipiranga (30 metros)."
    },
    "São Caetano": {
        "endereco": "Rua Alegre, 667 - Santa Paula, São Caetano do Sul - SP (Próximo ao Hotel Mercure)",
        "maps": "https://maps.app.goo.gl/mhct13HEmChxmfJF8",
        "recomendacao": "Traga o pedido médico original, documento com foto e carteirinha. Chegue com 15 minutos de antecedência. Temos vaga de embarque/desembarque na porta e convênio com Valet."
    }
}

# ==========================================
# VALIDAÇÕES DE SEGURANÇA (MATEMÁTICA E CALENDÁRIO)
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
    # Aceitar ano com 2 dígitos (ex: 15/04/60 → 15/04/1960)
    if re.match(r'^\d{2}/\d{2}/\d{2}$', data_str):
        partes = data_str.split('/')
        ano = int(partes[2])
        ano_completo = 1900 + ano if ano >= 20 else 2000 + ano
        data_str = f"{partes[0]}/{partes[1]}/{ano_completo}"
    if not re.match(r'^\d{2}/\d{2}/\d{4}$', data_str):
        return False
    try:
        data_obj = datetime.strptime(data_str, "%d/%m/%Y")
        hoje = datetime.now()
        if data_obj > hoje or data_obj.year < (hoje.year - 120):
            return False
        
        # Funcionalidade 1: Menor de 12 anos (Encaminhar para Humano)
        idade = hoje.year - data_obj.year - ((hoje.month, hoje.day) < (data_obj.month, data_obj.day))
        if idade < 12:
            return "menor_12"
            
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

# ==========================================
# FAQ COM IA — Cache + Gemini semântico
# ==========================================
_faq_cache = {"data": None, "ts": 0}
_FAQ_CACHE_TTL = 300  # 5 minutos — evita leituras repetidas do Firestore

def _carregar_faq():
    """Carrega o FAQ do Firestore com cache de 5 minutos."""
    import sys, time
    now = time.time()
    if _faq_cache["data"] is not None and (now - _faq_cache["ts"]) < _FAQ_CACHE_TTL:
        return _faq_cache["data"]
    if not db:
        return []
    try:
        docs = db.collection("FAQ").stream()
        faq = [doc.to_dict() for doc in docs]
        _faq_cache["data"] = faq
        _faq_cache["ts"] = now
        print(f"[FAQ] Cache atualizado: {len(faq)} categorias carregadas", file=sys.stderr)
        return faq
    except Exception as e:
        print(f"[FAQ] Erro ao carregar Firestore: {e}", file=sys.stderr)
        return _faq_cache["data"] or []

def _busca_por_keywords(msg_limpa, faq_data):
    """Busca rápida por palavras-chave — sem custo de API."""
    import sys
    for cat in faq_data:
        for pq in cat.get("perguntas_frequentes", []):
            todas = [pq.get("pergunta", "")] + pq.get("variacoes", [])
            for v in todas:
                if not v: continue
                if v.lower() in msg_limpa or msg_limpa in v.lower():
                    print(f"[FAQ-KW] Match: '{v[:40]}'", file=sys.stderr)
                    return pq.get("resposta_ideal")
    return None

def _busca_por_ia(mensagem, faq_data):
    """Usa o modelo treinado OpenAI para responder duvidas da clinica.
    Funciona com ou sem FAQ no Firestore:
    - COM FAQ: fornece as respostas como contexto para o modelo
    - SEM FAQ: o modelo responde com o conhecimento do treinamento direto"""
    import sys
    if not OPENAI_API_KEY or len(mensagem.strip()) < 5:
        if not OPENAI_API_KEY:
            print("[FAQ-IA] OPENAI_API_KEY ausente", file=sys.stderr)
        return None

    # Monta contexto do FAQ se houver conteudo no Firestore
    linhas_faq = []
    for cat in (faq_data or []):
        for pq in cat.get("perguntas_frequentes", []):
            pergunta = pq.get("pergunta", "")
            resposta = pq.get("resposta_ideal", "")
            variacoes = ", ".join(pq.get("variacoes", []))
            if pergunta and resposta:
                entrada = "PERGUNTA: " + pergunta + chr(10) + "VARIACOES: " + variacoes + chr(10) + "RESPOSTA: " + resposta
                linhas_faq.append(entrada)

    # Prompt adaptado: com ou sem FAQ no Firestore
    if linhas_faq:
        faq_formatado = (chr(10) + chr(10) + "---" + chr(10) + chr(10)).join(linhas_faq)
        linhas_prompt = [
            "Voce e o assistente virtual da clinica Conectifisio (fisioterapia, Sao Paulo).",
            "",
            "Um paciente enviou esta mensagem:",
            '"' + mensagem + '"',
            "",
            "Abaixo estao as duvidas frequentes da clinica com as respostas corretas:",
            "",
            faq_formatado,
            "",
            "INSTRUCAO: Analise se a mensagem e uma duvida respondida pelo FAQ acima.",
            "- Se SIM: responda SOMENTE com o texto exato da RESPOSTA correspondente.",
            "- Se NAO: responda SOMENTE com a palavra NENHUMA.",
            "",
            "Resposta:"
        ]
    else:
        # Sem FAQ no Firestore — modelo responde com conhecimento do treinamento
        linhas_prompt = [
            mensagem
        ]

    prompt = chr(10).join(linhas_prompt)

    url = "https://api.openai.com/v1/chat/completions"
    headers_oai = {
        "Authorization": "Bearer " + OPENAI_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "model": OPENAI_FAQ_MODEL,  # Usa o modelo FAQ específico (conectifisio-v2)
        "messages": [
            {"role": "system", "content": "Você é o assistente virtual da ConectiFisio, uma clínica de fisioterapia e pilates com unidades em São Caetano e Ipiranga. Seu tom é profissional, acolhedor e eficiente. Use as informações do manual para responder dúvidas de pacientes de forma natural. Se a mensagem nao for uma duvida sobre a clinica (saudacao, agradecimento ou assunto fora do escopo), responda SOMENTE com a palavra NENHUMA."},
            {"role": "user", "content": prompt[:3000]}
        ],
        "max_tokens": 800,
        "temperature": 0.2
    }

    try:
        res = requests.post(url, json=payload, headers=headers_oai, timeout=15)
        if res.status_code == 200:
            resposta_ia = res.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            print("[FAQ-IA] OpenAI: " + resposta_ia[:80], file=sys.stderr)
            # Rejeita respostas truncadas (menos de 15 chars ou sem pontuação final)
            if resposta_ia and resposta_ia.upper() != "NENHUMA" and len(resposta_ia) > 15:
                return resposta_ia
        else:
            print("[FAQ-IA] OpenAI HTTP " + str(res.status_code) + ": " + res.text[:200], file=sys.stderr)
    except Exception as e:
        print("[FAQ-IA] Erro: " + str(e), file=sys.stderr)

    return None

def consultar_faq(mensagem):
    """FAQ com IA — dois estágios:
    1. Busca por palavras-chave no Firestore (se tiver conteúdo)
    2. OpenAI ft:gpt-4o-mini:conectifisio-v1 sempre como fallback
    O OpenAI funciona mesmo com Firestore vazio — usa o conhecimento do treinamento."""
    import sys
    msg_limpa = mensagem.lower().strip()
    faq_data = _carregar_faq()

    # Estágio único: modelo fine-tuned v5 — ignora keywords do Firestore
    # O Firestore tem respostas antigas incompatíveis com o fluxo atual
    match_ia = _busca_por_ia(mensagem, [])  # passa lista vazia — modelo responde pelo treinamento
    if match_ia:
        print("[FAQ-IA] Respondendo via modelo v5", file=sys.stderr)
    return match_ia

def update_paciente(phone, data):
    if not db: return
    # lastInteraction é o timer GERAL (usado pelo Kanban)
    data["lastInteraction"] = firestore.SERVER_TIMESTAMP
    db.collection("PatientsKanban").document(phone).set(data, merge=True)

def registrar_historico(phone, remetente, tipo, conteudo, media_id=None):
    if not db: return
    now_iso = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S+00:00')
    nova_msg = {
        "de": remetente, # 'paciente', 'clinica' ou 'robo'
        "tipo": tipo,
        "conteudo": conteudo,
        "data": now_iso
    }
    if media_id:
        nova_msg["media_id"] = media_id  # Salva para exibir miniatura no Dashboard
    
    update_data = {
        "historico": firestore.ArrayUnion([nova_msg]),
        "lastInteraction": firestore.SERVER_TIMESTAMP
    }
    
    # Bug 3: lastPatientInteraction é o timer de FOLLOW-UP (só reseta quando o PACIENTE fala)
    if remetente == "paciente":
        update_data["lastPatientInteraction"] = firestore.SERVER_TIMESTAMP
        
    db.collection("PatientsKanban").document(phone).set(update_data, merge=True)

# ==========================================
# INTEGRAÇÃO FEEGOW (BLINDADA CONTRA ERRO 403)
# ==========================================
def get_feegow_headers():
    return {
        "Content-Type": "application/json", 
        "x-access-token": FEEGOW_TOKEN,
        "User-Agent": "Conectifisio-Integration/1.0"
    }

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

def baixar_midia_whatsapp_raw(media_id):
    """Baixa mídia do WhatsApp e retorna (bytes, mime_type).
    Imagens são convertidas para JPEG para compatibilidade com Feegow.
    Retorna (None, None) em caso de falha."""
    if not media_id or not WHATSAPP_TOKEN: return None, None
    try:
        url_info = f"https://graph.facebook.com/v19.0/{media_id}"
        headers_wa = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        res_info = requests.get(url_info, headers=headers_wa, timeout=10)
        if res_info.status_code != 200: return None, None
        info_json = res_info.json()
        media_url = info_json.get("url")
        mime_type = info_json.get("mime_type", "image/jpeg")
        res_download = requests.get(media_url, headers=headers_wa, timeout=20)
        if res_download.status_code != 200: return None, None
        conteudo = res_download.content
        
        import sys
        if "image" in mime_type:
            if mime_type == "image/jpg": mime_type = "image/jpeg"
            print(f"[MEDIA] Imagem recebida sem processamento Pillow: {mime_type}, {len(conteudo)/1024:.1f} KB", file=sys.stderr)
        
        if mime_type == "image/jpg": mime_type = "image/jpeg"
        return conteudo, mime_type
    except Exception as e:
        import sys; print(f"[ERRO] baixar_midia_whatsapp_raw({media_id}): {e}", file=sys.stderr)
        return None, None

def baixar_midia_whatsapp(media_id):
    """Baixa mídia do WhatsApp e retorna como data URI (data:mime/type;base64,...).
    Wrapper de compatibilidade — usado pelo integrar_feegow como fallback."""
    conteudo, mime_type = baixar_midia_whatsapp_raw(media_id)
    if not conteudo: return None
    b64_data = base64.b64encode(conteudo).decode('utf-8')
    return f"data:{mime_type};base64,{b64_data}"

# ==========================================
# FIREBASE STORAGE — Armazenamento de mídia sem limite de 1MB
# ==========================================
def salvar_midia_storage(phone, tipo_doc, conteudo_bytes, mime_type):
    """Salva mídia no Firebase Storage e retorna a URL pública de download."""
    import sys
    if not storage_bucket or not conteudo_bytes:
        print(f"[STORAGE] Bucket indisponível ou conteúdo vazio para {phone}/{tipo_doc}", file=sys.stderr)
        return None
    try:
        ext = 'jpg' if 'jpeg' in mime_type else ('pdf' if 'pdf' in mime_type else 'bin')
        blob_path = f"pacientes/{phone}/{tipo_doc}.{ext}"
        blob = storage_bucket.blob(blob_path)
        blob.upload_from_string(conteudo_bytes, content_type=mime_type)
        blob.make_public()
        url = blob.public_url
        tamanho_kb = len(conteudo_bytes) / 1024
        print(f"[STORAGE] Salvo {blob_path} ({tamanho_kb:.1f} KB) → {url}", file=sys.stderr)
        return url
    except Exception as e:
        print(f"[STORAGE] Erro ao salvar {phone}/{tipo_doc}: {e}", file=sys.stderr)
        return None

def baixar_do_storage_como_data_uri(url, mime_type_hint="image/jpeg"):
    """Baixa arquivo do Firebase Storage e retorna como data URI para o Feegow."""
    import sys
    if not url: return None
    try:
        res = requests.get(url, timeout=20)
        if res.status_code != 200:
            print(f"[STORAGE] Falha ao baixar {url}: status={res.status_code}", file=sys.stderr)
            return None
        conteudo = res.content
        mime_type = res.headers.get('Content-Type', mime_type_hint)
        if 'jpeg' in mime_type or 'jpg' in mime_type: mime_type = 'image/jpeg'
        elif 'pdf' in mime_type: mime_type = 'application/pdf'
        b64_data = base64.b64encode(conteudo).decode('utf-8')
        tamanho_kb = len(conteudo) / 1024
        print(f"[STORAGE] Baixado {url} ({tamanho_kb:.1f} KB) como data URI", file=sys.stderr)
        return f"data:{mime_type};base64,{b64_data}"
    except Exception as e:
        print(f"[STORAGE] Erro ao baixar {url}: {e}", file=sys.stderr)
        return None

def salvar_midia_imediata(phone, tipo_doc, media_id):
    """Fluxo completo: baixa do WhatsApp → salva no Storage → retorna dados para o Firestore."""
    import sys
    conteudo, mime_type = baixar_midia_whatsapp_raw(media_id)
    result = {f"{tipo_doc}_media_id": media_id}
    
    if not conteudo:
        print(f"[MEDIA] Falha ao baixar {tipo_doc} do WhatsApp (media_id={media_id})", file=sys.stderr)
        result[f"{tipo_doc}_b64"] = None
        result[f"{tipo_doc}_storage_url"] = None
        return result
    
    tamanho_b64 = len(base64.b64encode(conteudo))
    tamanho_kb = tamanho_b64 / 1024
    print(f"[MEDIA] {tipo_doc} baixado: {len(conteudo)/1024:.1f} KB raw, {tamanho_kb:.1f} KB Base64", file=sys.stderr)
    
    storage_url = salvar_midia_storage(phone, tipo_doc, conteudo, mime_type)
    result[f"{tipo_doc}_storage_url"] = storage_url
    
    if tamanho_b64 < 900_000:
        b64_data = base64.b64encode(conteudo).decode('utf-8')
        result[f"{tipo_doc}_b64"] = f"data:{mime_type};base64,{b64_data}"
        print(f"[MEDIA] {tipo_doc} salvo no Firestore (< 900KB)", file=sys.stderr)
    else:
        result[f"{tipo_doc}_b64"] = None
        print(f"[MEDIA] {tipo_doc} NÃO salvo no Firestore ({tamanho_kb:.1f} KB > 900KB) — usando Storage", file=sys.stderr)
    
    return result

def buscar_feegow_por_telefone(phone):
    if not FEEGOW_TOKEN: return None
    celular = re.sub(r'\D', '', phone)
    if celular.startswith("55") and len(celular) > 11: celular = celular[2:]
    # Endpoint correto: /patient/list com parâmetro telefone
    url = f"https://api.feegow.com/v1/api/patient/list?telefone={celular}"
    try:
        import sys
        print(f"[FEEGOW-BUSCA] Buscando paciente por telefone: {celular}", file=sys.stderr)
        res = requests.get(url, headers=get_feegow_headers(), timeout=10)
        print(f"[FEEGOW-BUSCA] HTTP {res.status_code} | resp={res.text[:300]}", file=sys.stderr)
        if res.status_code == 200:
            dados = res.json()
            if dados.get("success") != False and dados.get("content"):
                p = dados["content"][0] if isinstance(dados["content"], list) else dados["content"]
                pid = p.get("patient_id") or p.get("paciente_id") or p.get("id")
                nome = p.get("nome_completo") or p.get("nome") or ""
                cpf = p.get("cpf", "")
                print(f"[FEEGOW-BUSCA] Encontrado: id={pid} nome={nome}", file=sys.stderr)
                return {"id": pid, "nome": nome, "cpf": cpf}
    except Exception as e:
        import sys
        print(f"[FEEGOW-BUSCA] Erro: {e}", file=sys.stderr)
    return None

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
                return {"id": p.get("paciente_id") or p.get("id"), "nome": p.get("nome_completo") or p.get("nome")}
    except: pass
    return None

# Mapa de equipamentos Feegow → unidade e serviço
# local_id confirmado via URL ?P=Equipamentos&I=X no Feegow
_LOCAL_ID_MAP = {
    # Confirmado via agendamentos reais em 16/05/2026 (paciente_id=2256)
    # 42440 local_id=2 Cinesioterapia SCS
    # 42439 local_id=3 Acupuntura SCS
    # 42441 local_id=5 Cinesioterapia Ipiranga
    # 42442 local_id=8 Acupuntura Ipiranga
    2: {"unidade": "São Caetano", "servico": "Fisioterapia"},  # Cinesioterapia SCS
    3: {"unidade": "São Caetano", "servico": "Acupuntura"},    # Acupuntura SCS
    5: {"unidade": "Ipiranga",    "servico": "Fisioterapia"},  # Cinesioterapia Ipiranga
    8: {"unidade": "Ipiranga",    "servico": "Acupuntura"},    # Acupuntura Ipiranga
}

_LOCAL_ID_SLOTS = {
    # local_id=4 agenda Fisioterapia, local_id=6 agenda Acupuntura
    2: [4],   # Fisioterapia SCS
    3: [6],   # Acupuntura SCS
    5: [4],   # Fisioterapia Ipiranga
    8: [6],   # Acupuntura Ipiranga
}

# Mapa local_id → unidade_id Feegow (confirmado via agendamentos 16/05/2026)
# SCS = unidade principal = unidade_id=0
# SP (Ipiranga) = unidade_id=1
_LOCAL_ID_UNIDADE = {
    2: 0,  # Cinesioterapia SCS
    3: 0,  # Acupuntura SCS
    5: 1,  # Cinesioterapia SP
    8: 1,  # Acupuntura SP
}

# Mapa local_id equipamento → local_id agenda retornado pela API
# Confirmado via testes 17/05/2026
_LOCAL_ID_AGENDA = {
    2: 4,  # Fisioterapia SCS  → agenda local_id=4
    3: 4,  # Acupuntura SCS    → agenda local_id=4
    5: 6,  # Fisioterapia SP   → agenda local_id=6
    8: 6,  # Acupuntura SP     → agenda local_id=6
}

_PROC_ID_SERVICO = {
    21: "Acupuntura", 39: "Avaliação", 42: "Fisioterapia",
    9: "Avaliação", 32: "Avaliação",
    12: "Fisioterapia", 10: "Fisioterapia", 11: "Fisioterapia",
    14: "Fisioterapia", 15: "Fisioterapia", 16: "Fisioterapia",
    17: "Fisioterapia", 18: "Fisioterapia", 22: "Fisioterapia",
    34: "Fisioterapia", 35: "Fisioterapia", 43: "Fisioterapia",
    44: "Fisioterapia", 45: "Fisioterapia", 46: "Fisioterapia", 49: "Fisioterapia",
    20: "Pélvica", 25: "Pélvica", 31: "Pélvica", 38: "Pélvica", 40: "Pélvica",
    23: "Pilates", 27: "Terapia Manual", 30: "Terapia Manual",
    28: "Drenagem", 36: "Respiratória", 37: "Respiratória", 26: "RPG",
}

def _nome_para_servico(nome_equipamento):
    """Converte nome do equipamento Feegow para serviço legível."""
    nome = str(nome_equipamento).lower()
    if "acupuntura" in nome: return "Acupuntura"
    if "cinesio" in nome or "fisio" in nome: return "Fisioterapia"
    if "pilates" in nome: return "Pilates"
    return None

def _nome_para_unidade(nome_equipamento):
    """Extrai unidade do nome do equipamento Feegow."""
    nome = str(nome_equipamento).lower()
    if "ipiranga" in nome: return "Ipiranga"
    if "scs" in nome or "são caetano" in nome or "santa paula" in nome: return "São Caetano"
    return None

def consultar_agenda_feegow(paciente_id, retornar_raw=False, historico=False):
    """Consulta agenda do paciente no Feegow.
    retornar_raw=True retorna dict com sessoes (labels) e agendamentos (dados brutos).
    historico=True busca os últimos 90 dias (para Nova Guia com sessões esgotadas).
    Usa local_id para determinar unidade e serviço reais de cada sessão.
    """
    import sys
    if not FEEGOW_TOKEN or not paciente_id:
        return None
    hoje = datetime.now()
    if historico:
        ini = hoje - timedelta(days=90)
        url = f"https://api.feegow.com/v1/api/appoints/search?paciente_id={paciente_id}&data_start={ini.strftime('%d-%m-%Y')}&data_end={hoje.strftime('%d-%m-%Y')}"
    else:
        futuro = hoje + timedelta(days=90)
        url = f"https://api.feegow.com/v1/api/appoints/search?paciente_id={paciente_id}&data_start={hoje.strftime('%d-%m-%Y')}&data_end={futuro.strftime('%d-%m-%Y')}"
    print(f"[FEEGOW-AGENDA] Consultando: paciente_id={paciente_id} historico={historico}", file=sys.stderr)
    try:
        res = requests.get(url, headers=get_feegow_headers(), timeout=10)
        print(f"[FEEGOW-AGENDA] HTTP {res.status_code} | resp={res.text[:4000]}", file=sys.stderr)
        if res.status_code == 200:
            dados = res.json()
            if dados.get("success") != False and dados.get("content"):
                sessoes = []
                agendamentos = []
                VALID_STATUS_IDS = {1, 2, 4, 15}  # Marcado, Confirmado, Aguardando, Remarcado
                for a in dados["content"]:
                    status_id_ag = a.get("status_id", 1)
                    if status_id_ag not in VALID_STATUS_IDS:
                        continue  # ignora cancelados, desmarcados, faltas
                    data_raw = str(a.get("data", "")).split("T")[0]
                    # Converte DD-MM-YYYY para YYYY-MM-DD para comparação
                    if re.match(r"^\d{2}-\d{2}-\d{4}$", data_raw):
                        partes = data_raw.split("-")
                        data_iso = f"{partes[2]}-{partes[1]}-{partes[0]}"
                    else:
                        data_iso = data_raw
                    if (not historico and data_iso >= hoje.strftime('%Y-%m-%d')) or \
                       (historico and data_iso < hoje.strftime('%Y-%m-%d')):
                        hora = str(a.get("horario") or a.get("hora", ""))[:5]
                        local_id = a.get("local_id")
                        local_info = _LOCAL_ID_MAP.get(local_id, {})
                        servico_nome = local_info.get("servico") or _PROC_ID_SERVICO.get(a.get("procedimento_id")) or a.get("procedimento_nome") or "Sessão"
                        unidade_ag = local_info.get("unidade", "")
                        parts = data_iso.split("-")
                        if len(parts) == 3:
                            label = f"🗓️ *{parts[2]}/{parts[1]}/{parts[0]} às {hora}* - {servico_nome}"
                            sessoes.append(label)
                            agendamentos.append({
                                "agendamento_id": a.get("agendamento_id"),
                                "data": data_iso,
                                "data_br": f"{parts[2]}/{parts[1]}/{parts[0]}",
                                "hora": hora,
                                "local_id": local_id,
                                "procedimento_id": a.get("procedimento_id"),
                                "profissional_id": a.get("profissional_id"),
                                "unidade": unidade_ag,
                                "servico": servico_nome,
                                "label": label
                            })
                print(f"[FEEGOW-AGENDA] {len(sessoes)} sessão(ões) encontrada(s)", file=sys.stderr)
                if historico:
                    agendamentos.sort(key=lambda x: x["data"], reverse=True)
                if retornar_raw:
                    return {"sessoes": sessoes, "agendamentos": agendamentos}
                return sessoes
    except Exception as e:
        print(f"[FEEGOW-AGENDA] Exceção: {e}", file=sys.stderr)
    return None

def _buscar_servico_atual_feegow(paciente_id):
    """Busca serviço mais recente: tenta futuro → histórico 90 dias.
    Retorna dict com servico, unidade, local_id, procedimento_id ou None.
    """
    import sys
    if not paciente_id:
        return None
    for modo_hist in [False, True]:
        resultado = consultar_agenda_feegow(paciente_id, retornar_raw=True, historico=modo_hist)
        if resultado and resultado.get("agendamentos"):
            ag = resultado["agendamentos"][0]
            if ag.get("servico") and ag["servico"] != "Sessão":
                print(f"[SERVICO-ATUAL] {ag['servico']} ({ag['unidade']}) historico={modo_hist}", file=sys.stderr)
                return {
                    "servico": ag["servico"],
                    "unidade": ag.get("unidade", ""),
                    "local_id": ag.get("local_id"),
                    "procedimento_id": ag.get("procedimento_id"),
                    "fonte": "historico" if modo_hist else "futuro"
                }
    return None

def extrair_preferencia_data(texto):
    """Usa gpt-4o-mini para extrair data/hora. Fallback Python para dias da semana."""
    import sys
    hoje = datetime.now()
    dias_map = {0: "segunda", 1: "terça", 2: "quarta", 3: "quinta", 4: "sexta", 5: "sábado", 6: "domingo"}
    hoje_nome = dias_map.get(hoje.weekday(), "")

    # Fallback Python — extrai dia da semana e hora do texto sem IA
    def _fallback_parse(txt):
        txt_lower = txt.lower()
        dias_semana = {"segunda": 0, "terca": 1, "terça": 1, "quarta": 2, "quinta": 3, "sexta": 4}
        data_result = None
        hora_result = None
        periodo_result = None
        for nome, wd in dias_semana.items():
            if nome in txt_lower:
                for i in range(1, 8):
                    cand = hoje + timedelta(days=i)
                    if cand.weekday() == wd:
                        data_result = cand.strftime('%Y-%m-%d')
                        break
        import re as _re
        hora_match = _re.search(r'(\d{1,2})[h:](\d{2})?', txt_lower)
        if hora_match:
            h = int(hora_match.group(1))
            m = int(hora_match.group(2)) if hora_match.group(2) else 0
            if 0 <= h <= 23:
                hora_result = f"{h:02d}:{m:02d}"
        if "manhã" in txt_lower or "manha" in txt_lower: periodo_result = "manha"
        elif "tarde" in txt_lower: periodo_result = "tarde"
        return {"data": data_result, "hora": hora_result, "periodo": periodo_result}

    # Python primeiro para dias da semana — mais confiável que IA
    txt_lower_check = texto.lower()
    dias_semana_check = {"segunda", "terca", "terça", "quarta", "quinta", "sexta"}
    tem_dia_semana = any(d in txt_lower_check for d in dias_semana_check)
    resultado_python = _fallback_parse(texto)
    if tem_dia_semana and resultado_python.get("data"):
        print(f"[EXTRAIR-DATA] Python (dia semana): {resultado_python}", file=sys.stderr)
        return resultado_python

    try:
        import json as _json
        prompt = (
            f"Atue como um analista de dados. Analise o texto abaixo e extraia todas as informações de data e hora. "
            f"Converta-as para o formato ISO 8601 (YYYY-MM-DD ou YYYY-MM-DDTHH:MM:SS). "
            f"Se a hora não for mencionada, assuma 00:00:00. "
            f"Se a data for relativa (ex: 'próxima sexta'), converta para a data real baseada em hoje {hoje.strftime('%d/%m/%Y')} ({hoje_nome}).\n\n"
            f"Texto: \"{texto}\"\n\n"
            f"Retorne APENAS um JSON válido sem texto extra:\n"
            f"{{\"data\": \"YYYY-MM-DD\", \"hora\": \"HH:MM\"}}"
        )
        url_oai = "https://api.openai.com/v1/chat/completions"
        headers_oai = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
        payload_oai = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "max_tokens": 60, "temperature": 0}
        res_oai = requests.post(url_oai, json=payload_oai, headers=headers_oai, timeout=10)
        resp_text = res_oai.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        print(f"[EXTRAIR-DATA] '{texto[:40]}' → {resp_text}", file=sys.stderr)
        # Remove possíveis backticks
        resp_text = resp_text.replace("```json", "").replace("```", "").strip()
        result = _json.loads(resp_text)
        # Normaliza: extrai só data (YYYY-MM-DD) se vier com hora junto (YYYY-MM-DDTHH:MM:SS)
        if result.get("data") and "T" in str(result["data"]):
            partes = result["data"].split("T")
            result["data"] = partes[0]
            if not result.get("hora") and len(partes) > 1:
                result["hora"] = partes[1][:5]
        if not result.get("periodo"):
            result["periodo"] = None
        # Se IA falhou em extrair, usa fallback Python
        if not result.get("data") and not result.get("hora"):
            result = _fallback_parse(texto)
            print(f"[EXTRAIR-DATA] Fallback Python: {result}", file=sys.stderr)
        return result
    except Exception as e:
        print(f"[EXTRAIR-DATA] Erro: {e} — usando fallback Python", file=sys.stderr)
        return _fallback_parse(texto)

def cancelar_agendamento_feegow(agendamento_id, obs="Desmarcado pelo paciente via robô."):
    """Cancela agendamento no Feegow — StatusID=11 (Desmarcado pelo paciente)."""
    import sys
    if not FEEGOW_TOKEN or not agendamento_id: return False
    url = "https://api.feegow.com/v1/api/appoints/statusUpdate"
    payload = {"AgendamentoID": int(agendamento_id), "StatusID": "11", "Obs": obs}
    try:
        res = requests.post(url, json=payload, headers=get_feegow_headers(), timeout=10)
        print(f"[FEEGOW-CANCEL] HTTP {res.status_code} | {res.text[:200]}", file=sys.stderr)
        return res.status_code == 200 and res.json().get("success") != False
    except Exception as e:
        print(f"[FEEGOW-CANCEL] Exceção: {e}", file=sys.stderr)
        return False

def remarcar_agendamento_feegow(agendamento_id, obs="Remarcado pelo paciente via robô. Aguarda confirmação da recepção."):
    """Marca agendamento como Remarcado no Feegow — StatusID=15."""
    import sys
    if not FEEGOW_TOKEN or not agendamento_id: return False
    url = "https://api.feegow.com/v1/api/appoints/statusUpdate"
    payload = {"AgendamentoID": int(agendamento_id), "StatusID": "15", "Obs": obs}
    try:
        res = requests.post(url, json=payload, headers=get_feegow_headers(), timeout=10)
        print(f"[FEEGOW-REMARCAR] HTTP {res.status_code} | {res.text[:200]}", file=sys.stderr)
        return res.status_code == 200 and res.json().get("success") != False
    except Exception as e:
        print(f"[FEEGOW-REMARCAR] Exceção: {e}", file=sys.stderr)
        return False

def consultar_disponibilidade_feegow(local_id, procedimento_id, data_inicio_iso, data_fim_iso):
    """Consulta horarios disponiveis no Feegow.
    Endpoint: appoints/available-schedule
    Datas em ISO (YYYY-MM-DD) convertidas internamente para DD-MM-YYYY.
    """
    import sys
    if not FEEGOW_TOKEN: return []
    url = "https://api.feegow.com/v1/api/appoints/available-schedule"

    def _fmt(iso):
        try:
            p = iso.split('-')
            return f"{p[2]}-{p[1]}-{p[0]}"  # YYYY-MM-DD → DD-MM-YYYY
        except: return iso

    def _extrair_slots(dados):
        slots = []
        content = dados.get("content", {})
        if not content: return slots
        # Estrutura Feegow: {"profissional_id": {"1": {"local_id": {"4": {"2026-05-15": ["07:00:00",...]}}}}}
        if "profissional_id" in content:
            for prof_data in content["profissional_id"].values():
                if isinstance(prof_data, dict):
                    for loc_id_str, loc_data in prof_data.get("local_id", {}).items():
                        if isinstance(loc_data, dict):
                            for data_str, horarios in loc_data.items():
                                if isinstance(horarios, list):
                                    for h in horarios:
                                        _add_slot(slots, data_str, str(h)[:5], int(loc_id_str))
        else:
            for chave, valor in content.items():
                if isinstance(valor, dict):
                    for data_str, horarios in valor.items():
                        if isinstance(horarios, list):
                            for h in horarios:
                                _add_slot(slots, data_str, str(h)[:5])
                elif isinstance(valor, list):
                    _add_slot(slots, chave, str(valor[0])[:5] if valor else "")
        seen = set()
        unique = []
        for s in slots:
            k = (s["data"], s["hora"])
            if k not in seen:
                seen.add(k)
                unique.append(s)
        unique.sort(key=lambda x: (x["data"], x["hora"]))
        return unique

    def _add_slot(slots, data_str, hora, local_id_slot=None):
        try:
            if re.match(r'^\d{4}-\d{2}-\d{2}$', data_str):
                p = data_str.split('-')
                data_br = f"{p[2]}/{p[1]}/{p[0]}"
                data_iso = data_str
            elif re.match(r'^\d{2}-\d{2}-\d{4}$', data_str):
                p = data_str.split('-')
                data_iso = f"{p[2]}-{p[1]}-{p[0]}"
                data_br = f"{p[0]}/{p[1]}/{p[2]}"
            else:
                data_iso = data_str; data_br = data_str
            if hora:
                slot = {"data": data_iso, "data_br": data_br, "hora": hora, "label": f"🗓️ *{data_br} às {hora}*"}
                if local_id_slot is not None:
                    slot["local_id"] = local_id_slot
                slots.append(slot)
        except: pass

    data_ini_fmt = _fmt(data_inicio_iso)
    data_fim_fmt = _fmt(data_fim_iso)

    # Usar tipo=P + procedimento_id + unidade_id — filtra por unidade sem misturar agendas
    unidade_id = _LOCAL_ID_UNIDADE.get(local_id, 0)
    local_id_agenda = _LOCAL_ID_AGENDA.get(local_id)  # local_id que a API retorna nos slots
    tentativas = [
        {"tipo": "P", "procedimento_id": procedimento_id, "unidade_id": unidade_id, "data_start": data_ini_fmt, "data_end": data_fim_fmt},
    ]

    for params in tentativas:
        print(f"[FEEGOW-DISP] GET params={params}", file=sys.stderr)
        try:
            res = requests.get(url, params=params, headers=get_feegow_headers(), timeout=10)
            print(f"[FEEGOW-DISP] HTTP {res.status_code} | resp={res.text[:400]}", file=sys.stderr)
            if res.status_code == 200:
                dados = res.json()
                if dados.get("success") != False and dados.get("content"):
                    slots = _extrair_slots(dados)
                    # Filtra pelo local_id correto da agenda para eliminar slots fantasmas
                    if local_id_agenda and any(s.get("local_id") for s in slots):
                        antes = len(slots)
                        slots = [s for s in slots if s.get("local_id") == local_id_agenda]
                        print(f"[FEEGOW-DISP] Filtro local_id={local_id_agenda}: {antes} → {len(slots)} slots", file=sys.stderr)
                    if slots:
                        print(f"[FEEGOW-DISP] {len(slots)} horário(s) encontrado(s)", file=sys.stderr)
                        return slots
        except Exception as e:
            print(f"[FEEGOW-DISP] Exceção: {e}", file=sys.stderr)

    print(f"[FEEGOW-DISP] Nenhum horário para local_id={local_id}", file=sys.stderr)
    return []

def dias_uteis_a_partir(data_inicio, qtd_dias):
    """Retorna data_fim pulando domingos."""
    atual = data_inicio
    contados = 0
    while contados < qtd_dias:
        atual += timedelta(days=1)
        if atual.weekday() != 6:
            contados += 1
    return atual

def encontrar_horarios_proximos(slots, hora_preferida, qtd=2):
    """Retorna os qtd slots mais próximos — prioriza data mais próxima,
    desempata por proximidade de horário preferido."""
    if not slots: return []
    try:
        h_pref = datetime.strptime(hora_preferida, "%H:%M")
    except:
        return sorted(slots, key=lambda x: (x["data"], x["hora"]))[:qtd]
    def sort_key(slot):
        try:
            h_slot = datetime.strptime(slot["hora"], "%H:%M")
            diff_hora = abs((h_slot - h_pref).total_seconds())
        except:
            diff_hora = 999999
        return (slot["data"], diff_hora)
    return sorted(slots, key=sort_key)[:qtd]

def confirmar_presenca_feegow(agendamento_id):
    """Confirma presença no Feegow — StatusID=4 (Aguardando)."""
    import sys
    if not FEEGOW_TOKEN or not agendamento_id: return False
    url = "https://api.feegow.com/v1/api/appoints/statusUpdate"
    payload = {"AgendamentoID": int(agendamento_id), "StatusID": "4", "Obs": "Presença confirmada pelo paciente via robô."}
    try:
        res = requests.post(url, json=payload, headers=get_feegow_headers(), timeout=10)
        print(f"[FEEGOW-CONFIRM] HTTP {res.status_code} | {res.text[:200]}", file=sys.stderr)
        return res.status_code == 200 and res.json().get("success") != False
    except Exception as e:
        print(f"[FEEGOW-CONFIRM] Exceção: {e}", file=sys.stderr)
        return False

def integrar_feegow(phone, info):
    import sys
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
        except: pass

    elif feegow_id:
        try:
            res_pac = requests.get(f"{base_url}/patient/search?paciente_id={feegow_id}&photo=false", headers=get_feegow_headers(), timeout=10)
            pac_nome = info.get("title", "Paciente")
            pac_nasc = formatar_data_feegow(info.get("birthDate", ""))
            pac_email = info.get("email", "")
            
            if res_pac.status_code == 200 and res_pac.json().get("success") != False:
                conteudo = res_pac.json().get("content", [])
                if conteudo:
                    pac_data = conteudo[0] if isinstance(conteudo, list) else conteudo
                    pac_nome = pac_data.get("nome_completo", pac_data.get("nome", pac_nome))
                    if pac_data.get("data_nascimento"): pac_nasc = pac_data.get("data_nascimento")
                    if pac_data.get("email1"): pac_email = pac_data.get("email1")
            
            payload_edit = {
                "paciente_id": int(feegow_id),
                "nome_completo": pac_nome,
                "data_nascimento": pac_nasc,
                "celular1": celular,
                "email1": pac_email
            }
            
            if convenio_id > 0:
                payload_edit.update({
                    "convenio_id": convenio_id,
                    "plano_id": 0,
                    "matricula": matricula
                })
            
            res_edit = requests.post(f"{base_url}/patient/edit", json=payload_edit, headers=get_feegow_headers(), timeout=10)
            print(f"[FEEGOW] Atualização de veterano (ID {feegow_id}): status={res_edit.status_code} resp={res_edit.text[:200]}", file=sys.stderr)
        except Exception as e:
            print(f"[FEEGOW] Erro ao atualizar veterano: {e}", file=sys.stderr)

    fotos_enviadas = []
    if feegow_id:
        feegow_id_int = int(feegow_id)

        cart_storage_url = info.get("carteirinha_storage_url")
        ped_storage_url = info.get("pedido_storage_url")
        b64_cart_salvo = info.get("carteirinha_b64")
        b64_ped_salvo = info.get("pedido_b64")
        carteirinha_id = info.get("carteirinha_media_id")
        pedido_id = info.get("pedido_media_id")

        def _upload_feegow(feegow_id_int, descricao, storage_url, b64_salvo, media_id_orig):
            import sys

            conteudo_bytes = None
            mime_type = "image/jpeg"
            fonte = "N/A"

            if storage_url:
                try:
                    res_stor = requests.get(storage_url, timeout=20)
                    if res_stor.status_code == 200:
                        conteudo_bytes = res_stor.content
                        mime_type = res_stor.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
                        fonte = "Storage"
                        print(f"[FEEGOW-UPLOAD] {descricao} obtido do Storage ({len(conteudo_bytes)/1024:.1f} KB)", file=sys.stderr)
                    else:
                        print(f"[FEEGOW-UPLOAD] Storage retornou {res_stor.status_code} para {storage_url}", file=sys.stderr)
                except Exception as e:
                    print(f"[FEEGOW-UPLOAD] Erro ao baixar Storage: {e}", file=sys.stderr)

            if not conteudo_bytes and b64_salvo:
                try:
                    raw = b64_salvo.split(",", 1)[-1] if "," in b64_salvo else b64_salvo
                    conteudo_bytes = base64.b64decode(raw)
                    fonte = "Firestore"
                    print(f"[FEEGOW-UPLOAD] {descricao} obtido do Firestore ({len(conteudo_bytes)/1024:.1f} KB)", file=sys.stderr)
                except Exception as e:
                    print(f"[FEEGOW-UPLOAD] Erro ao decodificar b64 Firestore: {e}", file=sys.stderr)

            if not conteudo_bytes and media_id_orig:
                conteudo_bytes, mime_type = baixar_midia_whatsapp_raw(media_id_orig)
                if conteudo_bytes:
                    fonte = "WhatsApp API"
                    print(f"[FEEGOW-UPLOAD] {descricao} obtido do WhatsApp ({len(conteudo_bytes)/1024:.1f} KB)", file=sys.stderr)

            if not conteudo_bytes:
                print(f"[FEEGOW-UPLOAD] FALHA TOTAL: nenhuma fonte disponível para {descricao}", file=sys.stderr)
                return False

            if "jpeg" not in mime_type and "jpg" not in mime_type and "pdf" not in mime_type:
                mime_type = "image/jpeg"
            b64_puro = base64.b64encode(conteudo_bytes).decode("utf-8")
            data_uri = f"data:{mime_type};base64,{b64_puro}"

            headers_json = {
                "Content-Type": "application/json",
                "x-access-token": FEEGOW_TOKEN,
                "User-Agent": "Conectifisio-Integration/1.0"
            }

            try:
                payload_json = {
                    "paciente_id": feegow_id_int,
                    "arquivo_descricao": descricao,
                    "base64_file": data_uri
                }
                res = requests.post(
                    f"{base_url}/patient/upload-base64",
                    json=payload_json,
                    headers=headers_json,
                    timeout=30
                )
                print(f"[FEEGOW-UPLOAD] upload-base64 ({fonte}): HTTP {res.status_code} | {res.text[:500]}", file=sys.stderr)
                if res.status_code == 200:
                    try:
                        if res.json().get("success") != False:
                            return True
                    except:
                        return True
            except Exception as e:
                print(f"[FEEGOW-UPLOAD] Exceção upload-base64: {e}", file=sys.stderr)

            try:
                ext = "jpg" if "jpeg" in mime_type else ("pdf" if "pdf" in mime_type else "jpg")
                nome_arquivo = f"{descricao.replace(' ', '_').replace('(', '').replace(')', '')}.{ext}"
                res_mp = requests.post(
                    f"{base_url}/patient/upload-base64",
                    files={"arquivo": (nome_arquivo, conteudo_bytes, mime_type)},
                    data={"paciente_id": str(feegow_id_int), "arquivo_descricao": descricao},
                    headers={"x-access-token": FEEGOW_TOKEN, "User-Agent": "Conectifisio-Integration/1.0"},
                    timeout=30
                )
                print(f"[FEEGOW-UPLOAD] multipart ({fonte}): HTTP {res_mp.status_code} | {res_mp.text[:500]}", file=sys.stderr)
                if res_mp.status_code == 200:
                    try:
                        if res_mp.json().get("success") != False:
                            return True
                    except:
                        return True
            except Exception as e:
                print(f"[FEEGOW-UPLOAD] Exceção multipart: {e}", file=sys.stderr)

            print(f"[FEEGOW-UPLOAD] FALHA: todas estratégias falharam para {descricao} (paciente_id={feegow_id_int})", file=sys.stderr)
            return False

        if cart_storage_url or b64_cart_salvo or carteirinha_id:
            ok = _upload_feegow(feegow_id_int, "Carteirinha (Robô)",
                                cart_storage_url, b64_cart_salvo, carteirinha_id)
            if ok:
                fotos_enviadas.append("Carteirinha")

        if ped_storage_url or b64_ped_salvo or pedido_id:
            ok = _upload_feegow(feegow_id_int, "Pedido Médico (Robô)",
                                ped_storage_url, b64_ped_salvo, pedido_id)
            if ok:
                fotos_enviadas.append("Pedido")

        status_final = f"ID: {feegow_id_int}"
        if fotos_enviadas:
            status_final += f" | Anexos: {', '.join(fotos_enviadas)}"
        return {"feegow_id": feegow_id_int, "feegow_status": status_final}

    return {"feegow_status": "Erro Integração"}


# ==========================================
# INTEGRAÇÃO PORTO SEGURO — Elegibilidade via Playwright
# ==========================================
_porto_cache = {"token": None, "ts": 0}
_PORTO_TOKEN_TTL = 3000  # 50 minutos

def verificar_elegibilidade_porto_seguro(cpf_paciente, tuss=None):
    """Verifica elegibilidade Porto Seguro/Itaú Saúde via portal do prestador."""
    import sys, time, asyncio
    
    if tuss is None:
        tuss = PORTO_SEGURO_TUSS_FISIO
    
    if not PORTO_SEGURO_SENHA:
        print("[PORTO] PORTO_SEGURO_SENHA não configurada", file=sys.stderr)
        return {"erro": "Credenciais Porto Seguro não configuradas"}
    
    cpf_limpo = re.sub(r'\D', '', str(cpf_paciente))
    
    async def _executar():
        import requests as _req
        
        now = time.time()
        token = _porto_cache.get("token")
        if not token or (now - _porto_cache.get("ts", 0)) > _PORTO_TOKEN_TTL:
            token = await _fazer_login_porto()
            if token:
                _porto_cache["token"] = token
                _porto_cache["ts"] = now
        
        if not token:
            return {"erro": "Falha no login Porto Seguro"}
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://prestadores.portosaude.com.br",
            "Referer": "https://prestadores.portosaude.com.br/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "New-Index": "0"
        }
        BASE = "https://wwws.portoseguro.com.br/go-saud-jdig-prestador-api/v1"
        
        try:
            r1 = _req.post(f"{BASE}/authorization/health-card",
                json={"cpf": cpf_limpo, "carteirinha": ""},
                headers=headers, timeout=15)
            
            if r1.status_code in [401, 403]:
                _porto_cache["token"] = None
                return {"erro": "Token expirado — tente novamente"}
            
            if r1.status_code != 200:
                return {"erro": f"Beneficiário não encontrado (HTTP {r1.status_code})"}
            
            cards = r1.json().get("health_cards", [])
            if not cards:
                return {"elegivel": False, "motivo": "CPF não encontrado na base Porto Seguro"}
            
            card = cards[0]
            uuid = card.get("uuid")
            nome = card.get("nome", "")
            plano = card.get("nomePlano", "")
            validade = card.get("validadeCartao", "")
            
            r2 = _req.post(
                f"{BASE}/authorization/{PORTO_SEGURO_CODIGO_PRESTADOR}/elegibility-check",
                json={"uuid": uuid, "procedimento": tuss, "regime": "2"},
                headers=headers, timeout=15)
            
            if r2.status_code != 200:
                return {"erro": f"Erro elegibilidade (HTTP {r2.status_code})"}
            
            eleg = r2.json()
            elegivel = eleg.get("elegivel", False)
            negacao = eleg.get("negacao", "").strip().strip(",").strip()
            
            print(f"[PORTO] CPF {cpf_limpo[:3]}*** elegivel={elegivel}", file=sys.stderr)
            
            return {
                "elegivel": elegivel,
                "nome": nome,
                "plano": plano,
                "validade_carteira": validade,
                "negacao": negacao if negacao else None,
                "procedimento": tuss
            }
        except Exception as e:
            print(f"[PORTO] Erro: {e}", file=sys.stderr)
            return {"erro": str(e)}
    
    async def _fazer_login_porto():
        import sys
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox",
                          "--disable-blink-features=AutomationControlled",
                          "--disable-dev-shm-usage"]
                )
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
                    viewport={"width": 1366, "height": 768},
                    locale="pt-BR"
                )
                page = await context.new_page()
                await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
                
                token_capturado = None
                
                async def interceptar(request):
                    nonlocal token_capturado
                    auth = request.headers.get("authorization", "")
                    if auth.startswith("Bearer ") and len(auth) > 100:
                        token_capturado = auth.replace("Bearer ", "")
                
                page.on("request", interceptar)
                
                await page.goto("https://prestadores.portosaude.com.br/portal-prestador/",
                               wait_until="networkidle", timeout=30000)
                await page.fill("input[type=\"text\"]", PORTO_SEGURO_CPF)
                await page.fill("input[type=\"password\"]", PORTO_SEGURO_SENHA)
                await page.click("button[type=\"submit\"]")
                await page.wait_for_url("**/portal-prestador/**", timeout=15000)
                await page.wait_for_timeout(3000)
                await browser.close()
                
                if token_capturado:
                    print(f"[PORTO] Token capturado com sucesso", file=sys.stderr)
                return token_capturado
        except Exception as e:
            print(f"[PORTO] Erro login: {e}", file=sys.stderr)
            return None
    
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        resultado = loop.run_until_complete(_executar())
        loop.close()
        return resultado
    except Exception as e:
        print(f"[PORTO] Erro loop: {e}", file=sys.stderr)
        return {"erro": str(e)}

# ==========================================
# PORTO SEGURO — THREAD DE ELEGIBILIDADE EM BACKGROUND (SILENCIOSA)
#
# ARQUITETURA v2 (30/04/2026):
# - Paciente NUNCA é informado sobre resultado da elegibilidade
# - O bot continua o fluxo normal imediatamente após o CPF
# - Resultado vai APENAS para o card no Firestore/Kanban:
#     porto_elegibilidade_badge: "verde" | "vermelho" | "amarelo"
#     verde  = elegível confirmado
#     vermelho = inelegível confirmado
#     amarelo = não foi possível verificar (conferir manualmente)
# - Os dados da API (nome, plano, validade) enriquecem o cadastro quando disponíveis
# - Aplica-se como padrão para todos os convênios futuramente
# ==========================================
def _thread_verificar_porto(phone, cpf, numero_id):
    """Verifica elegibilidade Porto Seguro em background.
    
    IMPORTANTE: Esta função NÃO envia mensagens ao paciente.
    O resultado vai APENAS para o card no Kanban (badge colorido).
    O fluxo do paciente já foi avançado ANTES desta thread ser disparada.
    """
    import sys, time
    print(f"[PORTO-THREAD] Iniciando verificação silenciosa para {phone}", file=sys.stderr)

    resultado = verificar_elegibilidade_porto_seguro(cpf)

    elegivel = resultado.get("elegivel")  # True, False ou None (erro)
    erro = resultado.get("erro")
    negacao = resultado.get("negacao") or ""
    nome_api = resultado.get("nome", "")
    plano_api = resultado.get("plano", "")
    validade_api = resultado.get("validade_carteira", "")
    num_carteirinha_api = resultado.get("numeroCartao", "") or ""

    # ==========================================
    # Define badge para o Kanban
    # ==========================================
    if erro:
        badge = "amarelo"  # Não foi possível verificar — recepção confere manualmente
        print(f"[PORTO-THREAD] Erro na verificação: {erro} → badge=amarelo", file=sys.stderr)
    elif elegivel:
        badge = "verde"    # Elegível confirmado
        print(f"[PORTO-THREAD] Elegível confirmado → badge=verde", file=sys.stderr)
    else:
        badge = "vermelho" # Inelegível confirmado
        print(f"[PORTO-THREAD] Inelegível → badge=vermelho | motivo: {negacao}", file=sys.stderr)

    # ==========================================
    # Atualiza o card no Firestore — APENAS dados internos
    # Paciente não recebe nenhuma mensagem aqui
    # ==========================================
    campos_firestore = {
        "porto_elegibilidade_badge": badge,
        "porto_verificado_em": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S+00:00'),
        "porto_conferencia": badge in ["amarelo", "vermelho"],  # Sinaliza para recepção
    }

    if erro:
        campos_firestore["porto_erro"] = erro
    
    if elegivel is not None:
        campos_firestore["porto_elegivel"] = elegivel

    if negacao:
        campos_firestore["porto_negacao"] = negacao.strip(",").strip()

    # Enriquece o cadastro com dados vindos da API quando disponíveis
    if nome_api:
        campos_firestore["porto_nome_api"] = nome_api
        # Atualiza o nome do paciente se ainda não tiver sido preenchido manualmente
        paciente_atual = get_paciente(phone)
        if not paciente_atual.get("title") or paciente_atual.get("title") == "Paciente Sem Nome":
            campos_firestore["title"] = nome_api

    if plano_api:
        campos_firestore["plano_porto"] = plano_api

    if validade_api:
        campos_firestore["validade_carteirinha"] = validade_api

    if num_carteirinha_api:
        campos_firestore["numCarteirinha"] = num_carteirinha_api

    update_paciente(phone, campos_firestore)

    print(f"[PORTO-THREAD] Concluído para {phone} — badge={badge}, campos salvos no Firestore", file=sys.stderr)


def iniciar_verificacao_porto_background(phone, cpf, numero_id):
    """Dispara a verificação Porto Seguro em background e retorna imediatamente.
    
    FLUXO NOVO (silencioso):
    1. Bot já avançou o estado do paciente para 'data_nascimento' ANTES de chamar esta função
    2. Esta thread roda em paralelo, sem bloquear o fluxo do paciente
    3. Resultado vai apenas para o Kanban — paciente não é notificado
    """
    import threading
    t = threading.Thread(
        target=_thread_verificar_porto,
        args=(phone, cpf, numero_id),
        daemon=True
    )
    t.start()
    print(f"[PORTO-BG] Thread de elegibilidade disparada para {phone}", file=sys.stderr)


# ==========================================
# MENSAGERIA E IA
# ==========================================
def chamar_ia_custom(query):
    """
    Chama o modelo BASE gpt-4o-mini para acolhimento empático de queixas.
    IMPORTANTE: NÃO usar o modelo fine-tuned aqui —
    ele foi treinado para FAQ e gera respostas inadequadas para acolhimento.
    """
    if not OPENAI_API_KEY:
        return chamar_gemini(query)

    system_prompt = (
        "Você é o assistente virtual da ConectiFisio, clínica de fisioterapia em São Paulo. "
        "O paciente acabou de descrever sua queixa ou motivo de contato. "
        "Responda com UMA frase curta de acolhimento empático (máximo 2 linhas), "
        "reconhecendo a situação do paciente de forma calorosa e humana. "
        "NÃO ofereça informações sobre convênios, valores ou procedimentos. "
        "NÃO faça perguntas. Apenas acolha."
    )
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query[:300]}
        ],
        "max_tokens": 80,
        "temperature": 0.5
    }
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=15)
        if res.status_code == 200:
            resposta = res.json().get('choices', [{}])[0].get('message', {}).get('content', '').strip()
            import sys
            print(f"[ACOLHIMENTO] '{resposta[:80]}'", file=sys.stderr)
            return resposta
    except Exception as e:
        import sys
        print(f"[ACOLHIMENTO] Erro OpenAI: {e}", file=sys.stderr)
    return chamar_gemini(query)

def chamar_gemini(query):
    if not API_KEY: return None
    system_prompt = "Atue como o Assistente Virtual da clínica Conectifisio. Seu tom de voz deve ser brasileiro (PT-BR), acolhedor e focado na experiência do paciente. O paciente enviará a sua queixa clínica a seguir. Responda com UMA única frase empática se solidarizando com a dor dele."
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent?key={API_KEY}"
    payload = {"contents": [{"parts": [{"text": query[:300]}]}], "systemInstruction": {"parts": [{"text": system_prompt}]}}
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200: return res.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
    except: pass
    return None

def enviar_whatsapp(to, payload_msg, numero_id=None):
    """Envia mensagem pelo número correto."""
    pid = numero_id or getattr(_thread_local, "numero_id", None) or PHONE_NUMBER_ID
    url = f"https://graph.facebook.com/v19.0/{pid}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, **payload_msg}
    try: return requests.post(url, json=payload, headers=headers, timeout=10)
    except: return None

def responder_texto(to, texto, remetente="robo", numero_id=None):
    registrar_historico(to, remetente, "texto", texto)
    return enviar_whatsapp(to, {"type": "text", "text": {"body": texto}}, numero_id=numero_id)

def enviar_botoes(to, texto, botoes, numero_id=None):
    registrar_historico(to, "robo", "texto", texto)
    return enviar_whatsapp(to, {
        "type": "interactive",
        "interactive": {"type": "button", "body": {"text": texto}, "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}} for b in botoes]}}
    }, numero_id=numero_id)

def enviar_lista(to, texto, titulo_botao, secoes, numero_id=None):
    registrar_historico(to, "robo", "texto", texto)
    return enviar_whatsapp(to, {
        "type": "interactive",
        "interactive": {"type": "list", "body": {"text": texto}, "action": {"button": titulo_botao[:20], "sections": secoes}}
    }, numero_id=numero_id)

# ==========================================
# PROXY DE MÍDIA — Visualização de imagens do WhatsApp no Dashboard
# ==========================================
@app.route("/api/media", methods=["GET", "OPTIONS"])
def media_proxy():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200
    media_id = request.args.get("id")
    if not media_id:
        return jsonify({"error": "media_id obrigatório"}), 400
    try:
        url_info = f"https://graph.facebook.com/v19.0/{media_id}"
        headers_wa = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        res_info = requests.get(url_info, headers=headers_wa, timeout=10)
        if res_info.status_code != 200:
            return jsonify({"error": "Mídia não encontrada"}), 404
        media_url = res_info.json().get("url")
        mime_type = res_info.json().get("mime_type", "image/jpeg")
        res_download = requests.get(media_url, headers=headers_wa, timeout=15)
        if res_download.status_code != 200:
            return jsonify({"error": "Falha ao baixar mídia"}), 502
        from flask import Response
        return Response(
            res_download.content,
            mimetype=mime_type,
            headers={"Cache-Control": "private, max-age=3600"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==========================================
# WEBHOOK PRINCIPAL
# ==========================================
@app.route("/api/robo/status", methods=["GET", "POST", "OPTIONS"])
def robo_status():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200
    if not db:
        return jsonify({"error": "DB indisponivel"}), 500
    try:
        config_ref = db.collection("Config").document("global")
        if request.method == "GET":
            doc = config_ref.get()
            robo_on = doc.to_dict().get("robo_ligado", True) if doc.exists else True
            return jsonify({"robo_ligado": robo_on}), 200
        elif request.method == "POST":
            data = request.get_json()
            robo_on = data.get("robo_ligado", True)
            config_ref.set({"robo_ligado": robo_on}, merge=True)
            import sys
            print(f"[EMERGENCIA] Robô {'LIGADO' if robo_on else 'DESLIGADO'} globalmente", file=sys.stderr)
            return jsonify({"robo_ligado": robo_on, "ok": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/whatsapp", methods=["GET", "POST", "OPTIONS"])
def webhook():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200
        
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN: return request.args.get("hub.challenge"), 200
            
        if request.args.get("action") == "get_patients":
            try:
                import time
                if not db: return jsonify({"error": "Erro DB"}), 500
                now = time.time()
                if _patients_cache["data"] is not None and (now - _patients_cache["ts"]) < _CACHE_TTL:
                    return jsonify({"items": _patients_cache["data"], "cached": True}), 200
                try:
                    docs = db.collection("PatientsKanban").stream()
                    patients = []
                    for doc in docs:
                        data = doc.to_dict()
                        data["id"] = doc.id
                        # Remove historico do payload do kanban — reduz resposta de MB para KB
                        # O historico completo só é carregado quando a recepção abre um card específico
                        data.pop("historico", None)
                        data.pop("carteirinha_b64", None)
                        data.pop("pedido_b64", None)
                        if "lastInteraction" in data and data["lastInteraction"]:
                            try:
                                ts = data["lastInteraction"]
                                iso = ts.isoformat()
                                if '+' not in iso and 'Z' not in iso:
                                    iso = iso.split('.')[0] + '+00:00'
                                data["lastInteraction"] = iso
                            except: data["lastInteraction"] = str(data["lastInteraction"])
                        patients.append(data)
                    _patients_cache["data"] = patients
                    _patients_cache["ts"] = now
                    return jsonify({"items": patients}), 200
                except Exception as e_firestore:
                    err_str = str(e_firestore)
                    if ("429" in err_str or "Quota" in err_str or "RESOURCE_EXHAUSTED" in err_str) and _patients_cache["data"] is not None:
                        return jsonify({"items": _patients_cache["data"], "cached": True, "quota_warning": True}), 200
                    return jsonify({"error": err_str}), 500
            except Exception as e: return jsonify({"error": str(e)}), 500

        if request.args.get("action") == "get_historico":
            try:
                phone = request.args.get("phone")
                if not phone or not db:
                    return jsonify({"error": "phone obrigatório"}), 400
                doc = db.collection("PatientsKanban").document(phone).get()
                if not doc.exists:
                    return jsonify({"historico": [], "found": False}), 200
                data = doc.to_dict()
                historico = data.get("historico", [])
                return jsonify({"historico": historico, "found": True}), 200
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        if request.args.get("action") == "update_status":
            try:
                phone = request.args.get("phone")
                new_status = request.args.get("status")
                atendendo_por = request.args.get("atendendo_por", None)
                atendendo_em = request.args.get("atendendo_em", None)
                finalizado_por = request.args.get("finalizado_por", None)
                finalizado_em = request.args.get("finalizado_em", None)
                if not phone:
                    return jsonify({"success": False}), 400
                update_fields = {}
                if request.args.get("status_skip") != "1" and new_status:
                    update_fields["status"] = new_status
                if atendendo_por is not None:
                    if atendendo_por == "":
                        update_fields["atendendo_por"] = None
                        update_fields["atendendo_em"] = None
                    else:
                        update_fields["atendendo_por"] = atendendo_por
                        update_fields["atendendo_em"] = atendendo_em or firestore.SERVER_TIMESTAMP
                if finalizado_por:
                    update_fields["finalizado_por"] = finalizado_por
                    update_fields["finalizado_em"] = finalizado_em or datetime.utcnow().strftime('%d/%m/%Y %H:%M')
                append_raw = request.args.get("historico_atendentes_append", "")
                if append_raw:
                    try:
                        import json as _jj
                        entrada_ap = _jj.loads(append_raw)
                        update_fields["historico_atendentes"] = firestore.ArrayUnion([entrada_ap])
                    except: pass
                if update_fields:
                    db.collection("PatientsKanban").document(phone).set(update_fields, merge=True)
                    return jsonify({"success": True}), 200
                return jsonify({"success": False}), 400
            except Exception as e: return jsonify({"error": str(e)}), 500

        if request.args.get("action") == "export_historico":
            token_recebido = request.args.get("token", "")
            token_esperado = os.environ.get("FOLLOWUP_SECRET", "conectifisio_followup_2025")
            if token_recebido != token_esperado:
                return jsonify({"error": "Unauthorized"}), 401
            if not db:
                return jsonify({"error": "DB indisponivel"}), 500
            try:
                from datetime import timezone as _tz_exp
                agora = datetime.now(_tz_exp.utc)
                dias = int(request.args.get("dias", 7))
                desde = agora - timedelta(days=dias)
                
                docs = db.collection("PatientsKanban").stream()
                conversas = []
                
                for doc in docs:
                    p = doc.to_dict()
                    historico = p.get("historico", [])
                    if not historico:
                        continue
                    
                    msgs_semana = []
                    for msg in historico:
                        try:
                            data_msg = datetime.fromisoformat(
                                str(msg.get("data", "")).replace("Z", "+00:00")
                            )
                            if data_msg.tzinfo is None:
                                data_msg = data_msg.replace(tzinfo=_tz_exp.utc)
                            if data_msg >= desde:
                                msgs_semana.append(msg)
                        except:
                            msgs_semana.append(msg)
                    
                    if not msgs_semana:
                        continue
                    
                    linhas = []
                    for msg in msgs_semana:
                        remetente = msg.get("de", "?")
                        conteudo = msg.get("conteudo", "")
                        hora = msg.get("data", "")[:16].replace("T", " ")
                        if remetente == "paciente":
                            linhas.append(f"  [{hora}] PACIENTE: {conteudo}")
                        elif remetente == "robo":
                            linhas.append(f"  [{hora}] 🤖 BOT: {conteudo}")
                        elif remetente == "clinica":
                            linhas.append(f"  [{hora}] 👩 CLINICA: {conteudo}")
                    
                    conversas.append({
                        "phone": doc.id,
                        "nome": p.get("title", "Desconhecido"),
                        "status": p.get("status", ""),
                        "servico": p.get("servico", ""),
                        "modalidade": p.get("modalidade", ""),
                        "convenio": p.get("convenio", ""),
                        "total_msgs": len(msgs_semana),
                        "conversa": chr(10).join(linhas)
                    })
                
                conversas.sort(key=lambda x: x["total_msgs"], reverse=True)
                
                saida = []
                saida.append(f"=== HISTÓRICO DE CONVERSAS — ÚLTIMOS {dias} DIAS ===")
                saida.append(f"Total de pacientes com atividade: {len(conversas)}")
                saida.append("")
                
                for i, c in enumerate(conversas, 1):
                    saida.append(f"{'='*60}")
                    saida.append(f"#{i} | {c['nome']} | {c['phone']}")
                    saida.append(f"Status: {c['status']} | Serviço: {c['servico']} | {c['modalidade']} | {c['convenio']}")
                    saida.append(f"Mensagens: {c['total_msgs']}")
                    saida.append("")
                    saida.append(c["conversa"])
                    saida.append("")
                
                texto_final = chr(10).join(saida)
                
                from flask import Response
                return Response(
                    texto_final,
                    mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=historico_{dias}dias.txt"}
                )
            except Exception as e:
                import traceback as _tb
                return jsonify({"error": str(e), "trace": _tb.format_exc()}), 500
                
        if request.args.get("action") == "run_followup":
            token_recebido = request.args.get("token", "")
            token_esperado = os.environ.get("FOLLOWUP_SECRET", "conectifisio_followup_2025")
            if token_recebido != token_esperado:
                return jsonify({"error": "Unauthorized"}), 401
            if not db:
                return jsonify({"error": "DB indisponivel"}), 500
            try:
                import sys as _sys
                from datetime import timezone as _tz
                agora = datetime.now(_tz.utc)

                def dentro_horario_comercial(dt_utc):
                    dt_brt = dt_utc - timedelta(hours=3)
                    dia_semana = dt_brt.weekday()
                    hora = dt_brt.hour
                    if dia_semana == 6: return False
                    return 8 <= hora < 21

                def proximo_slot_comercial(dt_utc):
                    dt_brt = dt_utc - timedelta(hours=3)
                    proximo = dt_brt.replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
                    if proximo.weekday() == 6:
                        proximo += timedelta(days=1)
                    return proximo

                def pode_enviar(last_dt, minutos_necessarios):
                    agora_brt = agora - timedelta(hours=3)
                    minutos_passados = (agora - last_dt).total_seconds() / 60
                    if minutos_passados < minutos_necessarios:
                        return False
                    return dentro_horario_comercial(agora)

                def parse_timestamp(last_raw):
                    from datetime import timezone as _tz2
                    if isinstance(last_raw, datetime):
                        return last_raw if last_raw.tzinfo else last_raw.replace(tzinfo=_tz2.utc)
                    elif isinstance(last_raw, str):
                        s = last_raw.replace("Z", "+00:00")
                        if " " in s and "+" not in s: s = s.replace(" ", "T")
                        dt = datetime.fromisoformat(s)
                        return dt if dt.tzinfo else dt.replace(tzinfo=_tz2.utc)
                    elif hasattr(last_raw, "tzinfo"):
                        return last_raw if last_raw.tzinfo else last_raw.replace(tzinfo=_tz2.utc)
                    return datetime.fromisoformat(str(last_raw).replace("Z", "+00:00")).replace(tzinfo=_tz2.utc)

                STATUSES_CONVENIO = [
                    "nome_convenio", "num_carteirinha", "foto_carteirinha",
                    "foto_pedido_medico", "cadastrando_nome_completo",
                    "cpf", "data_nascimento", "coletando_email", "cadastrando_cpf",
                    "cadastrando_nascimento", "cadastrando_email"
                ]
                STATUSES_PILATES = [
                    "pilates_modalidade", "pilates_part_exp", "pilates_part_periodo",
                    "pilates_part_nome", "pilates_part_cpf", "pilates_part_nasc",
                    "pilates_part_email", "pilates_app_nome_completo", "pilates_app_cpf",
                    "pilates_app_nasc", "pilates_app_email", "pilates_app",
                    "pilates_wellhub_id", "pilates_app_periodo", "pilates_app_pref",
                    "pilates_caixa_nome", "pilates_caixa_cpf", "pilates_caixa_nasc",
                    "pilates_caixa_email", "pilates_caixa_foto_pedido",
                    "instagram_pilates_q1", "instagram_pilates_q2",
                    "transferencia_pilates"
                ]
                STATUSES_PARTICULAR = [
                    "cadastrando_queixa", "modalidade", "cadastrando_nome_completo",
                    "cpf", "data_nascimento", "coletando_email", "agendando"
                ]
                STATUSES_PROTEGIDOS = [
                    "atendimento_humano", "arquivado", "convertido",
                    "perdido", "followup_1", "followup_2", "followup_3",
                    "pausado"
                ]

                docs = db.collection("PatientsKanban").stream()
                enviados = []
                ignorados = []

                for doc in docs:
                    p = doc.to_dict()
                    phone_p = doc.id
                    status_p = p.get("status", "")
                    nome_p = (p.get("title", "Paciente") or "Paciente").split()[0]
                    modalidade_p = p.get("modalidade", "")
                    servico_p = p.get("servico", "")
                    origem_p = p.get("origem", "")
                    toque_atual = p.get("followup_toque", 0)

                    if status_p in STATUSES_PROTEGIDOS:
                        ignorados.append(phone_p)
                        continue

                    eh_convenio = (status_p in STATUSES_CONVENIO and modalidade_p == "Convenio") or (status_p in STATUSES_CONVENIO and modalidade_p not in ["Particular"])
                    eh_pilates = status_p in STATUSES_PILATES or servico_p == "Pilates Studio" or origem_p == "instagram_pilates"
                    eh_particular = modalidade_p == "Particular" and not eh_pilates

                    if not eh_convenio and not eh_pilates and not eh_particular:
                        ignorados.append(phone_p)
                        continue

                    last_raw = p.get("lastPatientInteraction") or p.get("lastInteraction")
                    if not last_raw:
                        ignorados.append(phone_p)
                        continue
                    try:
                        last_dt = parse_timestamp(last_raw)
                    except Exception as e_ts:
                        print(f"[followup] erro timestamp {phone_p}: {e_ts}", file=_sys.stderr)
                        ignorados.append(phone_p)
                        continue

                    minutos_inativo = (agora - last_dt).total_seconds() / 60

                    if eh_convenio and not eh_pilates:
                        if toque_atual == 0 and pode_enviar(last_dt, 30):
                            msg = (f"Oi {nome_p}! 😊 Percebi que você ficou a um passo de concluir o seu cadastro pelo convênio. "
                                   f"Para garantirmos a sua vaga na agenda, preciso apenas de mais algumas informações. "
                                   f"Leva menos de 2 minutinhos! Posso continuar?")
                            responder_texto(phone_p, msg)
                            db.collection("PatientsKanban").document(phone_p).set(
                                {"followup_toque": 1, "followup_enviado_em": agora.isoformat()}, merge=True)
                            enviados.append({"phone": phone_p, "toque": "convenio_t1"})

                        elif toque_atual == 1 and pode_enviar(last_dt, 60):
                            msg = (f"Olá {nome_p}! 🌟 Seu cadastro pelo convênio está quase pronto aqui comigo. "
                                   f"Nossas agendas estão preenchendo rápido — se finalizarmos agora, "
                                   f"consigo garantir um horário para você ainda esta semana. Podemos continuar?")
                            responder_texto(phone_p, msg)
                            db.collection("PatientsKanban").document(phone_p).set(
                                {"followup_toque": 2, "followup_enviado_em": agora.isoformat()}, merge=True)
                            enviados.append({"phone": phone_p, "toque": "convenio_t2"})

                        elif toque_atual == 2 and pode_enviar(last_dt, 90):
                            msg = (f"Oi {nome_p}! 💙 Esta será minha última tentativa de contato por hoje. "
                                   f"Seu cadastro ficou salvo aqui e, quando quiser retomar, é só me mandar um 'Oi' "
                                   f"que continuo de onde paramos. Estaremos sempre de portas abertas!")
                            responder_texto(phone_p, msg)
                            db.collection("PatientsKanban").document(phone_p).set(
                                {"followup_toque": 3, "followup_enviado_em": agora.isoformat()}, merge=True)
                            enviados.append({"phone": phone_p, "toque": "convenio_t3"})

                        elif toque_atual == 3 and pode_enviar(last_dt, 105):
                            db.collection("PatientsKanban").document(phone_p).set(
                                {"status": "arquivado", "motivo_encerramento": "followup_convenio_sem_resposta"}, merge=True)
                            enviados.append({"phone": phone_p, "toque": "convenio_arquivado"})

                    elif eh_pilates:
                        if toque_atual == 0 and pode_enviar(last_dt, 30):
                            msg = (f"Oi {nome_p}! 🧘‍♀️ Você estava tão perto de garantir sua vaga no Pilates Estúdio! "
                                   f"Me conta, ficou alguma dúvida sobre horários ou modalidades? "
                                   f"Estou aqui para te ajudar a encontrar a opção perfeita para você.")
                            responder_texto(phone_p, msg)
                            db.collection("PatientsKanban").document(phone_p).set(
                                {"followup_toque": 1, "followup_enviado_em": agora.isoformat()}, merge=True)
                            enviados.append({"phone": phone_p, "toque": "pilates_t1"})

                        elif toque_atual == 1 and pode_enviar(last_dt, 60):
                            msg = (f"Olá {nome_p}! ✨ Nossas turmas de Pilates estão com poucos lugares disponíveis. "
                                   f"Seu cadastro já está salvo aqui comigo — faltam só alguns minutinhos para "
                                   f"garantirmos a sua vaga. Podemos continuar?")
                            responder_texto(phone_p, msg)
                            db.collection("PatientsKanban").document(phone_p).set(
                                {"followup_toque": 2, "followup_enviado_em": agora.isoformat()}, merge=True)
                            enviados.append({"phone": phone_p, "toque": "pilates_t2"})

                        elif toque_atual == 2 and pode_enviar(last_dt, 90):
                            msg = (f"Oi {nome_p}! 💙 Vou deixar sua vaga reservada por enquanto. "
                                   f"Quando quiser retomar, é só me mandar um 'Oi' e continuamos de onde paramos. "
                                   f"Nossa equipe também pode te atender pessoalmente se preferir!")
                            responder_texto(phone_p, msg)
                            db.collection("PatientsKanban").document(phone_p).set(
                                {"followup_toque": 3, "status": "atendimento_humano",
                                 "followup_enviado_em": agora.isoformat(),
                                 "motivo_fila": "followup_pilates_sem_resposta"}, merge=True)
                            enviados.append({"phone": phone_p, "toque": "pilates_t3_fila"})

                    elif eh_particular:
                        if toque_atual == 0 and pode_enviar(last_dt, 30):
                            msg = (f"Oi {nome_p}! 😊 Você estava quase finalizando seu agendamento particular. "
                                   f"Ficou alguma dúvida sobre valores ou procedimentos? "
                                   f"Posso te ajudar agora mesmo!")
                            responder_texto(phone_p, msg)
                            db.collection("PatientsKanban").document(phone_p).set(
                                {"followup_toque": 1, "followup_enviado_em": agora.isoformat()}, merge=True)
                            enviados.append({"phone": phone_p, "toque": "particular_t1"})

                        elif toque_atual == 1 and pode_enviar(last_dt, 60):
                            msg = (f"Olá {nome_p}! 🌟 Seu cadastro particular está quase pronto. "
                                   f"Nossa agenda para essa semana está preenchendo — se finalizarmos agora, "
                                   f"consigo um horário especial para você. Continuamos?")
                            responder_texto(phone_p, msg)
                            db.collection("PatientsKanban").document(phone_p).set(
                                {"followup_toque": 2, "followup_enviado_em": agora.isoformat()}, merge=True)
                            enviados.append({"phone": phone_p, "toque": "particular_t2"})

                        elif toque_atual == 2 and pode_enviar(last_dt, 90):
                            msg = (f"Oi {nome_p}! 💙 Vou manter seu cadastro na nossa fila prioritária. "
                                   f"Quando quiser retomar, é só me mandar um 'Oi'. "
                                   f"Nossa equipe também está disponível para te atender diretamente!")
                            responder_texto(phone_p, msg)
                            db.collection("PatientsKanban").document(phone_p).set(
                                {"followup_toque": 3, "status": "atendimento_humano",
                                 "followup_enviado_em": agora.isoformat(),
                                 "motivo_fila": "followup_particular_sem_resposta"}, merge=True)
                            enviados.append({"phone": phone_p, "toque": "particular_t3_fila"})

                return jsonify({
                    "ok": True,
                    "hora_brasilia": (agora - timedelta(hours=3)).hour,
                    "horario_comercial": dentro_horario_comercial(agora),
                    "enviados": len(enviados),
                    "detalhes": enviados,
                    "ignorados": len(ignorados)
                }), 200
            except Exception as e:
                return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

        if request.args.get("action") == "search_arquivados":
            try:
                token_q = request.args.get("token","")
                if token_q != "conectifisio_followup_2025": return "Acesso Negado", 403
                q = request.args.get("q","").strip().lower()
                if not q or len(q) < 2: return jsonify({"results":[]}), 200
                if not db: return jsonify({"results":[]}), 200
                docs = db.collection("PatientsKanban").where("status","==","arquivado").stream()
                results = []
                for doc in docs:
                    d = doc.to_dict()
                    nome = (d.get("title","") or "").lower()
                    tel = (d.get("cellphone","") or d.get("id","") or "")
                    if q in nome or q in tel:
                        d["id"] = doc.id
                        results.append(d)
                return jsonify({"results": results[:50]}), 200
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        if request.args.get("action") == "send_reagendamento_confirmado":
            try:
                phone_r = request.args.get("phone","")
                nome_r  = request.args.get("nome","paciente")
                slot_r  = request.args.get("slot","")
                if not phone_r: return jsonify({"error":"phone obrigatório"}), 400
                msg_rea = (
                    f"Olá, {nome_r}! ✅\n\n"
                    f"Seu reagendamento foi *confirmado*!\n"
                    + (f"📅 Novo horário: *{slot_r}*\n\n" if slot_r else "\n")
                    + "Se precisar de algo, é só chamar. Até breve! 😊"
                )
                responder_texto(phone_r, msg_rea)
                return jsonify({"ok": True}), 200
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        if request.args.get("action") == "send_cancelamento_confirmado":
            try:
                phone_c = request.args.get("phone","")
                nome_c  = request.args.get("nome","paciente")
                if not phone_c: return jsonify({"error":"phone obrigatório"}), 400
                msg_can = (
                    f"Olá, {nome_c}! ✅\n\n"
                    "Seu cancelamento foi *registrado* em nosso sistema.\n\n"
                    "Se quiser reagendar ou precisar de algo, é só nos chamar. "
                    "Estamos sempre aqui! 💙"
                )
                responder_texto(phone_c, msg_can)
                return jsonify({"ok": True}), 200
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        if request.args.get("action") == "update_paciente":
            try:
                phone_u = request.args.get("phone","")
                if not phone_u or not db: return jsonify({"ok": False}), 400
                campos = {}
                for k in ["status","atendendo_por","atendendo_em","finalizado_por","finalizado_em"]:
                    v = request.args.get(k)
                    if v is not None: campos[k] = v if v != "" else None
                append_raw = request.args.get("historico_atendentes_append","")
                if append_raw:
                    try:
                        import json as _jj
                        from firebase_admin import firestore as _fsa
                        entrada_ap = _jj.loads(append_raw)
                        campos["historico_atendentes"] = _fsa.ArrayUnion([entrada_ap])
                    except: pass
                if campos:
                    db.collection("Pacientes").document(phone_u).set(campos, merge=True)
                return jsonify({"ok": True}), 200
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        if request.args.get("action") == "send_recomendacao":
            try:
                phone_p = request.args.get("phone", "")
                nome_p  = request.args.get("nome", "paciente")
                data_p  = request.args.get("data", "")
                unidade_p = request.args.get("unidade", "Ipiranga")

                if not phone_p:
                    return jsonify({"error": "phone obrigatório"}), 400

                info_unidade = UNIDADES.get(unidade_p, UNIDADES["Ipiranga"])
                link_maps = info_unidade["maps"]

                if data_p:
                    msg_recomendacao = (
                        f"Tudo certo para o seu atendimento, {nome_p}! ✅\n"
                        f"Esperamos por você no dia {data_p}.\n\n"
                        "Para que sua recepção seja tranquila, pedimos a gentileza de chegar "
                        "15 minutos antes e não esquecer o pedido médico original. "
                        "Vale lembrar que alguns planos de saúde pedem um token de validação "
                        "enviado ao seu celular na hora, então fique atento às notificações.\n\n"
                        "Até breve! Se precisar de algo, é só chamar. 😊"
                    )
                else:
                    msg_recomendacao = (
                        f"Tudo certo para o seu atendimento, {nome_p}! ✅\n\n"
                        "Para que sua recepção seja tranquila, pedimos a gentileza de chegar "
                        "15 minutos antes e não esquecer o pedido médico original. "
                        "Vale lembrar que alguns planos de saúde pedem um token de validação "
                        "enviado ao seu celular na hora, então fique atento às notificações.\n\n"
                        "Até breve! Se precisar de algo, é só chamar. 😊"
                    )

                responder_texto(phone_p, msg_recomendacao)
                import time as _t2
                _t2.sleep(1)
                responder_texto(phone_p, f"📍 Como chegar à nossa unidade {unidade_p}:\n{link_maps}")

                return jsonify({"ok": True, "enviado": True}), 200
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        return "Acesso Negado", 403

    # --- POST: RECEBER MENSAGENS WHATSAPP ---
    data = request.get_json()
    if not data or "entry" not in data: return jsonify({"status": "ok"}), 200
    
    if db:
        try:
            config_doc = db.collection("Config").document("global").get()
            if config_doc.exists and config_doc.to_dict().get("robo_ligado") == False:
                import sys
                print("[EMERGENCIA] Robô desligado globalmente — mensagem ignorada", file=sys.stderr)
                return jsonify({"status": "robo_desligado"}), 200
        except: pass

    try:
        val = data["entry"][0]["changes"][0]["value"]
        if "messages" not in val: return jsonify({"status": "not_a_message"}), 200

        message = val["messages"][0]
        phone = message["from"]
        msg_type = message.get("type")
        numero_id = val.get("metadata", {}).get("phone_number_id") or PHONE_NUMBER_ID
        _thread_local.numero_id = numero_id
        import sys; print(f"[DUAL-NUM] Mensagem recebida no número ID: {numero_id}", file=sys.stderr)
        
        info = get_paciente(phone)
        status_atual = info.get("status", "triagem") if info else "triagem"

        if msg_type in ["audio", "voice"]:
            if status_atual == "triagem":
                responder_texto(phone, "Ainda não consigo ouvir áudios por aqui 🎧. Como este é o nosso primeiro contato, por favor, digite um simples 'Olá' para eu te mostrar as opções de atendimento!")
            else:
                responder_texto(phone, "Ainda não consigo ouvir áudios por aqui 🎧. Para não interrompermos o seu agendamento, por favor, responda com um texto curto ou clique nos botões acima.")
            return jsonify({"status": "audio_bloqueado"}), 200
            
        if msg_type not in ["text", "interactive", "image", "document"]:
            return jsonify({"status": "tipo_ignorado"}), 200

        msg_recebida = "Anexo Recebido"
        tem_anexo = False
        media_id = None 

        if msg_type == "text": msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive": 
            inter = message["interactive"]
     