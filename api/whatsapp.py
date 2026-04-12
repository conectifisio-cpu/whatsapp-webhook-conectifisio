import os
import json
import re
import traceback
import io
import requests
import base64
from datetime import datetime, timedelta, timezone
try:
    from PIL import Image as PILImage
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False
from flask import Flask, request, jsonify, send_from_directory, render_template
import firebase_admin
from firebase_admin import credentials, firestore, storage as fb_storage

# Configura o Flask para encontrar templates e static a partir da raiz do projeto
_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
app = Flask(__name__, template_folder=os.path.join(_ROOT_DIR, 'templates'), static_folder=os.path.join(_ROOT_DIR, 'static'))

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
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "ft:gpt-4o-mini:conectifisio-v1") # Modelo customizado
FEEGOW_TOKEN = os.environ.get("FEEGOW_TOKEN", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "conectifisio_webhook_2026")

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

def consultar_faq(mensagem):
    if not db: return None
    msg_limpa = mensagem.lower()
    
    # 1. Busca por palavras-chave nas categorias do FAQ
    faq_ref = db.collection("FAQ").get()
    for doc in faq_ref:
        cat = doc.to_dict()
        for pq in cat.get("perguntas_frequentes", []):
            # Verifica pergunta principal e variações
            if any(v.lower() in msg_limpa for v in [pq["pergunta"]] + pq.get("variacoes", [])):
                return pq["resposta_ideal"]
            # Verifica se a mensagem do paciente contém a pergunta do FAQ (inverso)
            if any(msg_limpa in v.lower() for v in [pq["pergunta"]] + pq.get("variacoes", [])):
                return pq["resposta_ideal"]
                
    # 2. Busca semântica simples no TrainingData (opcional, se não achou no FAQ estruturado)
    # Por enquanto, ficaremos no FAQ estruturado para maior precisão.
    return None

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
        
        # [NORMALIZAÇÃO FEEGOW] Pillow DESATIVADO — enviando bytes originais do WhatsApp
        # Motivo: Pillow no Cloud Run produzia JPEG incompatível com o renderizador do Feegow.
        # No Vercel (onde funcionava em março/2026) o Pillow se comportava diferente.
        # Arquivo original do WhatsApp chega em formato que o Feegow aceita nativamente.
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
# O Firestore tem limite de 1MB por campo. Fotos de celular em Base64 podem ter 2-5MB.
# Solução: Salvar binário no Firebase Storage e guardar apenas a URL no Firestore.
# O frontend não é afetado (usa media_id via proxy /api/media).

def salvar_midia_storage(phone, tipo_doc, conteudo_bytes, mime_type):
    """Salva mídia no Firebase Storage e retorna a URL pública de download.
    Args:
        phone: telefone do paciente (usado como pasta)
        tipo_doc: 'carteirinha' ou 'pedido_medico'
        conteudo_bytes: bytes do arquivo
        mime_type: tipo MIME (ex: 'image/jpeg', 'application/pdf')
    Returns:
        URL de download público ou None em caso de falha
    """
    import sys
    if not storage_bucket or not conteudo_bytes:
        print(f"[STORAGE] Bucket indisponível ou conteúdo vazio para {phone}/{tipo_doc}", file=sys.stderr)
        return None
    try:
        ext = 'jpg' if 'jpeg' in mime_type else ('pdf' if 'pdf' in mime_type else 'bin')
        blob_path = f"pacientes/{phone}/{tipo_doc}.{ext}"
        blob = storage_bucket.blob(blob_path)
        blob.upload_from_string(conteudo_bytes, content_type=mime_type)
        # Tornar público para leitura (necessário para o integrar_feegow baixar depois)
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
        # Detectar mime_type pelo conteúdo ou usar hint
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
    """Fluxo completo: baixa do WhatsApp → salva no Storage → retorna dados para o Firestore.
    Retorna dict com campos para merge no Firestore:
      - {tipo_doc}_storage_url: URL do Storage
      - {tipo_doc}_b64: Base64 se < 900KB (compatibilidade), senão None
      - {tipo_doc}_media_id: media_id original
    """
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
    
    # Sempre salvar no Storage (fonte confiável sem limite de tamanho)
    storage_url = salvar_midia_storage(phone, tipo_doc, conteudo, mime_type)
    result[f"{tipo_doc}_storage_url"] = storage_url
    
    # Se cabe no Firestore (< 900KB em Base64), salvar também para compatibilidade
    if tamanho_b64 < 900_000:
        b64_data = base64.b64encode(conteudo).decode('utf-8')
        result[f"{tipo_doc}_b64"] = f"data:{mime_type};base64,{b64_data}"
        print(f"[MEDIA] {tipo_doc} salvo no Firestore (< 900KB)", file=sys.stderr)
    else:
        result[f"{tipo_doc}_b64"] = None  # Não salvar no Firestore — muito grande
        print(f"[MEDIA] {tipo_doc} NÃO salvo no Firestore ({tamanho_kb:.1f} KB > 900KB) — usando Storage", file=sys.stderr)
    
    return result

def buscar_feegow_por_telefone(phone):
    if not FEEGOW_TOKEN: return None
    celular = re.sub(r'\D', '', phone)
    if celular.startswith("55") and len(celular) > 11: celular = celular[2:]
    url = f"https://api.feegow.com/v1/api/patient/search?celular1={celular}&photo=false"
    try:
        res = requests.get(url, headers=get_feegow_headers(), timeout=10)
        if res.status_code == 200:
            dados = res.json()
            if dados.get("success") != False and dados.get("content"):
                p = dados["content"][0] if isinstance(dados["content"], list) else dados["content"]
                return {"id": p.get("paciente_id") or p.get("id"), "nome": p.get("nome_completo") or p.get("nome"), "cpf": p.get("cpf", "")}
    except: pass
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

def consultar_agenda_feegow(paciente_id):
    if not FEEGOW_TOKEN or not paciente_id: return None
    hoje = datetime.now()
    futuro = hoje + timedelta(days=90)
    url = f"https://api.feegow.com/v1/api/appoints/search?paciente_id={paciente_id}&data_start={hoje.strftime('%d-%m-%Y')}&data_end={futuro.strftime('%d-%m-%Y')}"
    try:
        res = requests.get(url, headers=get_feegow_headers(), timeout=10)
        if res.status_code == 200:
            dados = res.json()
            if dados.get("success") != False and dados.get("content"):
                sessoes = []
                for a in dados["content"]:
                    status_nome = str(a.get("status_nome", a.get("status", ""))).lower()
                    if "cancelado" not in status_nome and "falta" not in status_nome:
                        data_raw = str(a.get("data", "")).split("T")[0]
                        if data_raw >= hoje.strftime('%Y-%m-%d'):
                            proc = a.get("procedimento_nome") or (a.get("procedimento", {}).get("nome") if isinstance(a.get("procedimento"), dict) else "Sessão")
                            hora = str(a.get("horario") or a.get("hora", ""))[:5]
                            parts = data_raw.split('-')
                            if len(parts) == 3: sessoes.append(f"🗓️ *{parts[2]}/{parts[1]}/{parts[0]} às {hora}* - {proc}")
                return sessoes
    except: pass
    return None

def integrar_feegow(phone, info):
    import sys  # necessário para print(..., file=sys.stderr) em todo o escopo da função
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
        # Paciente veterano: Atualiza dados (especialmente convênio se mudou)
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
            
            # Se houver convênio novo, atualiza
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

        # Cadeia de prioridade para obter a mídia:
        # 1. Firebase Storage URL (sem limite de tamanho)
        # 2. Base64 salvo no Firestore (compatível com imagens < 900KB)
        # 3. WhatsApp API via media_id (fallback, pode ter expirado)
        cart_storage_url = info.get("carteirinha_storage_url")
        ped_storage_url = info.get("pedido_storage_url")
        b64_cart_salvo = info.get("carteirinha_b64")
        b64_ped_salvo = info.get("pedido_b64")
        carteirinha_id = info.get("carteirinha_media_id")
        pedido_id = info.get("pedido_media_id")

        def _upload_feegow(feegow_id_int, descricao, storage_url, b64_salvo, media_id_orig):
            """Faz upload de um arquivo para o prontuário do Feegow.
            Endpoint confirmado pelo Wix: /patient/upload-base64
            Payload: { paciente_id, arquivo_descricao, base64_file (data URI completo) }
            Loga HTTP status + resposta completa para diagnóstico."""
            import sys

            # --- Obter conteúdo binário ---
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

            # --- Monta data URI (formato confirmado pelo Wix) ---
            if "jpeg" not in mime_type and "jpg" not in mime_type and "pdf" not in mime_type:
                mime_type = "image/jpeg"
            b64_puro = base64.b64encode(conteudo_bytes).decode("utf-8")
            data_uri = f"data:{mime_type};base64,{b64_puro}"

            headers_json = {
                "Content-Type": "application/json",
                "x-access-token": FEEGOW_TOKEN,
                "User-Agent": "Conectifisio-Integration/1.0"
            }

            # --- Estratégia 1: JSON com base64 (endpoint confirmado pelo Wix) ---
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
                        return True  # 200 sem JSON = sucesso
            except Exception as e:
                print(f"[FEEGOW-UPLOAD] Exceção upload-base64: {e}", file=sys.stderr)

            # --- Estratégia 2: multipart/form-data (fallback) ---
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

        # --- Upload Carteirinha ---
        if cart_storage_url or b64_cart_salvo or carteirinha_id:
            ok = _upload_feegow(feegow_id_int, "Carteirinha (Robô)",
                                cart_storage_url, b64_cart_salvo, carteirinha_id)
            if ok:
                fotos_enviadas.append("Carteirinha")

        # --- Upload Pedido Médico ---
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
# MENSAGERIA E IA
# ==========================================
def chamar_ia_custom(query):
    """
    Chama o modelo customizado da OpenAI (conectifisio-v1) para acolhimento e triagem.
    """
    if not OPENAI_API_KEY: 
        # Fallback para Gemini se OpenAI não estiver configurada
        return chamar_gemini(query)
        
    system_prompt = "Você é o assistente virtual da ConectiFisio, uma clínica de fisioterapia e pilates com unidades em São Caetano e Ipiranga. Seu tom é profissional, acolhedor e eficiente. Use as informações do manual para responder dúvidas de pacientes de forma natural."
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query[:500]}
        ],
        "max_tokens": 150,
        "temperature": 0.7
    }
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=15)
        if res.status_code == 200:
            return res.json().get('choices', [{}])[0].get('message', {}).get('content', '').strip()
    except: pass
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

def enviar_whatsapp(to, payload_msg):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, **payload_msg}
    try: return requests.post(url, json=payload, headers=headers, timeout=10)
    except: return None

def responder_texto(to, texto, remetente="robo"):
    # Registrar no histórico e enviar via WhatsApp API
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
@app.route("/api/whatsapp", methods=["GET", "POST", "OPTIONS"])
def webhook():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200
        
    # --- GET: DASHBOARD ---
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN: return request.args.get("hub.challenge"), 200
            
        if request.args.get("action") == "get_patients":
            try:
                import time
                if not db: return jsonify({"error": "Erro DB"}), 500
                now = time.time()
                # Usa cache se ainda válido
                if _patients_cache["data"] is not None and (now - _patients_cache["ts"]) < _CACHE_TTL:
                    return jsonify({"items": _patients_cache["data"], "cached": True}), 200
                try:
                    docs = db.collection("PatientsKanban").stream()
                    patients = []
                    for doc in docs:
                        data = doc.to_dict()
                        data["id"] = doc.id
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
                    # Se cota excedida (429) E temos cache antigo, retorna o cache com aviso
                    if ("429" in err_str or "Quota" in err_str or "RESOURCE_EXHAUSTED" in err_str) and _patients_cache["data"] is not None:
                        return jsonify({"items": _patients_cache["data"], "cached": True, "quota_warning": True}), 200
                    return jsonify({"error": err_str}), 500
            except Exception as e: return jsonify({"error": str(e)}), 500

        if request.args.get("action") == "update_status":
            try:
                phone = request.args.get("phone")
                new_status = request.args.get("status")
                if phone and new_status:
                    db.collection("PatientsKanban").document(phone).set({"status": new_status}, merge=True)
                    return jsonify({"success": True}), 200
                return jsonify({"success": False}), 400
            except Exception as e: return jsonify({"error": str(e)}), 500
                
        # ==========================================
        # FOLLOW-UP AUTOMÁTICO — 3 TOQUES
        # Chamado pelo cron-job.org a cada hora
        # Protegido por token secreto
        # ==========================================
        if request.args.get("action") == "run_followup":
            token_recebido = request.args.get("token", "")
            token_esperado = os.environ.get("FOLLOWUP_SECRET", "conectifisio_followup_2025")
            if token_recebido != token_esperado:
                return jsonify({"error": "Unauthorized"}), 401
            if not db:
                return jsonify({"error": "DB indisponivel"}), 500
            try:
                from datetime import timezone as _tz
                agora = datetime.now(_tz.utc)

                # ==========================================
                # JANELAS DE ENGAJAMENTO B2C (horário Brasília = UTC-3)
                # Só dispara mensagens dentro dessas janelas
                # ==========================================
                hora_brasilia = (agora - timedelta(hours=3)).hour
                JANELAS = [(8, 10), (12, 14), (17, 19)]
                dentro_da_janela = any(inicio <= hora_brasilia < fim for inicio, fim in JANELAS)

                # Statuses elegíveis para follow-up
                STATUSES_FOLLOWUP = [
                    "triagem", "escolhendo_unidade", "cadastrando_nome",
                    "escolhendo_especialidade", "cadastrando_queixa",
                    "modalidade", "nome_convenio", "foto_carteirinha",
                    "foto_pedido_medico", "cadastrando_nome_completo",
                    "cadastrando_cpf", "cadastrando_nascimento",
                    "cadastrando_email", "agendando", "finalizado",
                    "num_carteirinha", "data_nascimento", "coletando_email",
                    "pausado"
                ]
                # Statuses de Particular/Pilates — só alerta interno, sem mensagem ao paciente
                STATUSES_ALERTA_HUMANO = [
                    "agendando", "finalizado"
                ]
                # Statuses protegidos — nunca recebem follow-up
                STATUSES_PROTEGIDOS = [
                    "atendimento_humano", "arquivado", "convertido",
                    "perdido", "followup_1", "followup_2", "followup_3"
                ]
                # Statuses de abandono de cadastro (documentos pendentes)
                STATUSES_CADASTRO = [
                    "foto_carteirinha", "foto_pedido_medico",
                    "num_carteirinha", "cadastrando_nome_completo",
                    "data_nascimento", "coletando_email"
                ]

                docs = db.collection("PatientsKanban").stream()
                enviados = []
                ignorados = []
                alertas_humano = []

                for doc in docs:
                    p = doc.to_dict()
                    phone_p = doc.id
                    status_p = p.get("status", "")
                    nome_p = p.get("title", "Paciente").split()[0]
                    modalidade_p = p.get("modalidade", "")
                    toque_atual = p.get("followup_toque", 0)

                    if status_p in STATUSES_PROTEGIDOS or status_p not in STATUSES_FOLLOWUP:
                        ignorados.append(phone_p)
                        continue

                    last_raw = p.get("lastPatientInteraction") or p.get("lastInteraction")
                    if not last_raw:
                        ignorados.append(phone_p)
                        continue
                    try:
                        from datetime import timezone as _tz2
                        # Se for um objeto datetime do Python (ou do SDK do Firebase)
                        if isinstance(last_raw, datetime):
                            last_dt = last_raw if last_raw.tzinfo else last_raw.replace(tzinfo=_tz2.utc)
                        # Se for uma string ISO
                        elif isinstance(last_raw, str):
                            # Trata formatos comuns e garante offset
                            s = last_raw.replace('Z', '+00:00')
                            if ' ' in s and '+' not in s: s = s.replace(' ', 'T')
                            last_dt = datetime.fromisoformat(s)
                            if last_dt.tzinfo is None:
                                last_dt = last_dt.replace(tzinfo=_tz2.utc)
                        # Fallback para outros objetos que tenham tzinfo ou possam ser convertidos
                        elif hasattr(last_raw, 'tzinfo'):
                            last_dt = last_raw if last_raw.tzinfo else last_raw.replace(tzinfo=_tz2.utc)
                        else:
                            # Tenta converter a representação em string
                            last_dt = datetime.fromisoformat(str(last_raw).replace('Z', '+00:00')).replace(tzinfo=_tz2.utc)
                    except Exception as e_ts:
                        print(f'[followup] erro timestamp {phone_p}: {e_ts} raw={last_raw}')
                        ignorados.append(phone_p)
                        continue

                    horas_inativo = (agora - last_dt).total_seconds() / 3600
                    minutos_inativo = (agora - last_dt).total_seconds() / 60

                    # ==========================================
                    # TIPO 3: Particular ou Pilates — ALERTA INTERNO (sem msg ao paciente)
                    # ==========================================
                    eh_particular = modalidade_p in ["Particular"] or "pilates" in status_p.lower()
                    if eh_particular and not p.get("alerta_resgate_enviado") and horas_inativo >= 1:
                        # Registra alerta no Firebase para o dashboard exibir
                        hora_alerta = agora.isoformat()
                        db.collection("PatientsKanban").document(phone_p).set({
                            "alerta_resgate": True,
                            "alerta_resgate_em": hora_alerta,
                            "alerta_resgate_enviado": True,
                            "alerta_resgate_texto": f"🚨 ALERTA DE RESGATE: Paciente {nome_p} iniciou cotação para {modalidade_p} e parou. Assuma o atendimento!"
                        }, merge=True)
                        alertas_humano.append({"phone": phone_p, "tipo": "alerta_particular"})
                        continue  # Não manda mensagem ao paciente

                    # ==========================================
                    # TIPO 1: Abandono de Cadastro (documentos pendentes)
                    # ==========================================
                    if status_p in STATUSES_CADASTRO:
                        if toque_atual == 0 and not p.get("lembrete_cadastro_enviado") and minutos_inativo >= 30:
                            if dentro_da_janela:
                                msg = (f"🤖 Oi {nome_p}! Vi que paramos no meio do seu cadastro. "
                                       f"Para eu conseguir liberar sua vaga, consegue me enviar a foto do pedido médico e da carteirinha agora?")
                                responder_texto(phone_p, msg)
                                db.collection("PatientsKanban").document(phone_p).set(
                                    {"lembrete_cadastro_enviado": True, "followup_toque": 1,
                                     "followup_enviado_em": agora.isoformat()}, merge=True)
                                enviados.append({"phone": phone_p, "toque": "tipo1_t1"})
                            continue

                        elif toque_atual == 1 and horas_inativo >= 24:
                            if dentro_da_janela:
                                msg = (f"🤖 Olá! Passando para lembrar do seu agendamento. "
                                       f"Iniciar o tratamento o quanto antes é fundamental. Consegue me dar um retorno hoje?")
                                responder_texto(phone_p, msg)
                                db.collection("PatientsKanban").document(phone_p).set(
                                    {"followup_toque": 2, "followup_enviado_em": agora.isoformat()}, merge=True)
                                enviados.append({"phone": phone_p, "toque": "tipo1_t2"})
                            continue

                        elif toque_atual == 2 and horas_inativo >= 48:
                            if dentro_da_janela:
                                msg = (f"🤖 Oi, {nome_p}! Como não tivemos retorno, estou encerrando seu atendimento "
                                       f"por aqui e liberando o horário para outros pacientes aguardando vaga. "
                                       f"Se precisar no futuro, é só mandar um 'Oi'. Melhoras!")
                                responder_texto(phone_p, msg)
                                db.collection("PatientsKanban").document(phone_p).set(
                                    {"followup_toque": 3, "status": "perdido",
                                     "followup_enviado_em": agora.isoformat()}, merge=True)
                                enviados.append({"phone": phone_p, "toque": "tipo1_t3_arquivado"})
                            continue

                    # ==========================================
                    # TIPO 2: Documentação enviada mas não agendou (Convênio)
                    # ==========================================
                    if status_p in ["agendando", "finalizado"] and modalidade_p == "Convênio":
                        if toque_atual == 0 and horas_inativo >= 2:
                            if dentro_da_janela:
                                msg = (f"🤖 Olá, {nome_p}! Sua documentação já está certinha! 🎉 "
                                       f"Responda essa mensagem para escolhermos o melhor dia e período para a sua sessão.")
                                responder_texto(phone_p, msg)
                                db.collection("PatientsKanban").document(phone_p).set(
                                    {"followup_toque": 1, "followup_enviado_em": agora.isoformat()}, merge=True)
                                enviados.append({"phone": phone_p, "toque": "tipo2_t1"})
                            continue

                        elif toque_atual == 1 and horas_inativo >= 24:
                            if dentro_da_janela:
                                msg = (f"🤖 Olá! Passando para lembrar do seu agendamento. "
                                       f"Iniciar o tratamento o quanto antes é fundamental. Consegue me dar um retorno hoje?")
                                responder_texto(phone_p, msg)
                                db.collection("PatientsKanban").document(phone_p).set(
                                    {"followup_toque": 2, "followup_enviado_em": agora.isoformat()}, merge=True)
                                enviados.append({"phone": phone_p, "toque": "tipo2_t2"})
                            continue

                        elif toque_atual == 2 and horas_inativo >= 48:
                            if dentro_da_janela:
                                msg = (f"🤖 Oi, {nome_p}! Como não tivemos retorno, estou encerrando seu atendimento "
                                       f"por aqui e liberando o horário para outros pacientes aguardando vaga. "
                                       f"Se precisar no futuro, é só mandar um 'Oi'. Melhoras!")
                                responder_texto(phone_p, msg)
                                db.collection("PatientsKanban").document(phone_p).set(
                                    {"followup_toque": 3, "status": "perdido",
                                     "followup_enviado_em": agora.isoformat()}, merge=True)
                                enviados.append({"phone": phone_p, "toque": "tipo2_t3_arquivado"})
                            continue

                    # ==========================================
                    # FALLBACK: Outros statuses — follow-up genérico nas janelas
                    # ==========================================
                    if toque_atual == 0 and horas_inativo >= 2 and dentro_da_janela:
                        msg = (f"Oi {nome_p}! 😊 Vi que não conseguimos finalizar o seu agendamento. "
                               f"Ficou alguma dúvida? Estou por aqui para te ajudar!")
                        responder_texto(phone_p, msg)
                        db.collection("PatientsKanban").document(phone_p).set(
                            {"followup_toque": 1, "followup_enviado_em": agora.isoformat()}, merge=True)
                        enviados.append({"phone": phone_p, "toque": "generico_t1"})

                    elif toque_atual == 1 and horas_inativo >= 24 and dentro_da_janela:
                        msg = (f"Bom dia, {nome_p}! 🌅 Nossas agendas estão preenchendo rápido. "
                               f"Seu cadastro já está pré-aprovado. Quer garantir sua vaga?")
                        responder_texto(phone_p, msg)
                        db.collection("PatientsKanban").document(phone_p).set(
                            {"followup_toque": 2, "followup_enviado_em": agora.isoformat()}, merge=True)
                        enviados.append({"phone": phone_p, "toque": "generico_t2"})

                    elif toque_atual == 2 and horas_inativo >= 48 and dentro_da_janela:
                        msg = (f"Olá {nome_p}! Como não tivemos retorno, estou pausando o seu atendimento. "
                               f"Quando quiser retomar, é só mandar um 'Oi'. Estaremos de portas abertas! 💙")
                        responder_texto(phone_p, msg)
                        db.collection("PatientsKanban").document(phone_p).set(
                            {"followup_toque": 3, "status": "perdido",
                             "followup_enviado_em": agora.isoformat()}, merge=True)
                        enviados.append({"phone": phone_p, "toque": "generico_t3"})

                return jsonify({
                    "ok": True,
                    "dentro_da_janela": dentro_da_janela,
                    "hora_brasilia": hora_brasilia,
                    "enviados": len(enviados),
                    "alertas_humano": len(alertas_humano),
                    "detalhes": enviados,
                    "ignorados": len(ignorados)
                }), 200
            except Exception as e:
                return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

        # ==========================================
        # ENVIO SEMIAUTOMÁTICO DE RECOMENDAÇÕES
        # Chamado pelo dashboard ao Finalizar ou Arquivar
        # ==========================================
        if request.args.get("action") == "send_recomendacao":
            try:
                phone_p = request.args.get("phone", "")
                nome_p  = request.args.get("nome", "paciente")
                data_p  = request.args.get("data", "")   # formato livre, ex: "25/03 às 14:30"
                unidade_p = request.args.get("unidade", "Ipiranga")

                if not phone_p:
                    return jsonify({"error": "phone obrigatório"}), 400

                info_unidade = UNIDADES.get(unidade_p, UNIDADES["Ipiranga"])
                link_maps = info_unidade["maps"]

                # Monta a mensagem personalizada
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

                # Envia mensagem de recomendação
                responder_texto(phone_p, msg_recomendacao)
                import time as _t2
                _t2.sleep(1)
                # Envia link do Maps
                responder_texto(phone_p, f"📍 Como chegar à nossa unidade {unidade_p}:\n{link_maps}")

                return jsonify({"ok": True, "enviado": True}), 200
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        return "Acesso Negado", 403

    # --- POST: RECEBER MENSAGENS WHATSAPP ---
    data = request.get_json()
    if not data or "entry" not in data: return jsonify({"status": "ok"}), 200

    try:
        val = data["entry"][0]["changes"][0]["value"]
        if "messages" not in val: return jsonify({"status": "not_a_message"}), 200

        message = val["messages"][0]
        phone = message["from"]
        msg_type = message.get("type")
        
        info = get_paciente(phone)
        status_atual = info.get("status", "triagem") if info else "triagem"

        # ==========================================
        # 🛡️ ESCUDOS DE PROTEÇÃO DINÂMICA
        # ==========================================
        # 1. Escudo Anti-Áudio
        if msg_type in ["audio", "voice"]:
            if status_atual == "triagem":
                responder_texto(phone, "Ainda não consigo ouvir áudios por aqui 🎧. Como este é o nosso primeiro contato, por favor, digite um simples 'Olá' para eu te mostrar as opções de atendimento!")
            else:
                responder_texto(phone, "Ainda não consigo ouvir áudios por aqui 🎧. Para não interrompermos o seu agendamento, por favor, responda com um texto curto ou clique nos botões acima.")
            return jsonify({"status": "audio_bloqueado"}), 200
            
        # 2. Escudo Lixo (Figurinhas, Ligações)
        if msg_type not in ["text", "interactive", "image", "document"]:
            return jsonify({"status": "tipo_ignorado"}), 200

        msg_recebida = "Anexo Recebido"
        tem_anexo = False
        media_id = None 

        if msg_type == "text": msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive": 
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))
        elif msg_type in ["image", "document"]:
            tem_anexo = True
            media_id = message.get(msg_type, {}).get("id")

        # Salva o histórico do paciente (com media_id para exibir miniatura no Dashboard)
        if tem_anexo and media_id:
            registrar_historico(phone, "paciente", "anexo", msg_recebida, media_id=media_id)
        else:
            registrar_historico(phone, "paciente", "texto" if not tem_anexo else "anexo", msg_recebida)

        # ==========================================
        # 🔄 RESET DO FOLLOW-UP: Paciente respondeu, zera o contador
        # Também atualiza lastPatientInteraction (usado pelo lembrete de cadastro)
        # ==========================================
        agora_iso = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S+00:00')  # UTC explícito
        db.collection("PatientsKanban").document(phone).set(
            {"lastPatientInteraction": agora_iso,
             **(({"followup_toque": 0, "followup_retomado_em": agora_iso}) if info.get("followup_toque", 0) > 0 else {})},
            merge=True)

        # ==========================================
        # 🚨 O BOTÃO DE PÂNICO (TRANSBORDO HUMANO)
        # ==========================================
        msg_limpa = msg_recebida.lower()

        # Detecção antecipada de cortesia (usada abaixo no FAQ e na lógica de estados)
        _cortesias_early = ["obrigad", "obg", "ok", "valeu", "certo", "tá bom", "perfeito", "beleza", "show", "combinado", "agradeço", "ótimo", "otimo", "maravilh", "excelente", "muito bom", "legal", "entendi", "entendido", "claro"]
        _emojis_early = ["👍", "🙏", "❤️", "👏", "😊", "🥰", "💙", "💚", "🤝", "✅"]
        is_cortesia = len(msg_limpa) <= 35 and (
            any(msg_limpa.startswith(w) for w in _cortesias_early) or
            any(char in msg_limpa for char in _emojis_early)
        )

        palavras_socorro = ["ajuda", "humano", "atendente", "recepção", "recepcao", "falar com alguém", "pessoa"]
        if any(palavra in msg_limpa for palavra in palavras_socorro):
            update_paciente(phone, {"status": "pausado", "ultima_mensagem_paciente": f"[PEDIDO DE AJUDA] {msg_recebida}"})
            responder_texto(phone, "Entendido! Pausei o meu sistema automático e já avisei a nossa equipa. 🚨 Em instantes um atendente humano vai assumir esta conversa para te ajudar!")
            return jsonify({"status": "pedido_ajuda"}), 200

        # ==========================================
        # 🧠 CONSULTA AO FAQ (INTELIGÊNCIA DE DADOS REAIS)
        # NÃO consulta FAQ para mensagens de cortesia (evita reiniciar fluxo)
        # ==========================================
        if msg_type == "text" and len(msg_limpa) > 5 and not is_cortesia:
            resposta_faq = consultar_faq(msg_recebida)
            if resposta_faq:
                responder_texto(phone, resposta_faq)
                # Se o paciente já estava em um fluxo, avisa que pode continuar
                if status_atual not in ["triagem", "finalizado", "atendimento_humano"]:
                    import time as _t_faq
                    _t_faq.sleep(1.5)
                    responder_texto(phone, "Espero ter ajudado com sua dúvida! 😊\n\nPodemos continuar o seu agendamento de onde paramos?")
                return jsonify({"status": "faq_respondido"}), 200

        # ==========================================
        # 🚀 A MÁQUINA DO TEMPO (INTERCEPTAÇÃO GLOBAL)
        # ==========================================
        if msg_recebida in ["Particular", "Convênio"]:
            update_paciente(phone, {"modalidade": msg_recebida})
            if msg_recebida == "Particular":
                status_alvo = "agendando" if info.get("feegow_id") else "cadastrando_nome_completo"
                update_paciente(phone, {"status": status_alvo})
                if info.get("feegow_id"):
                    enviar_botoes(phone, "Perfeito! Como você já é nosso paciente, vamos direto para a agenda. Qual o melhor período para você? ☀️⛅", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])
                else:
                    responder_texto(phone, "Perfeito! Para seu cadastro particular, digite seu NOME COMPLETO (conforme documento):")
                return jsonify({"status": "time_travel_particular"}), 200
            elif msg_recebida == "Convênio":
                update_paciente(phone, {"status": "nome_convenio"})
                secoes = [{"title": "Convênios Aceitos", "rows": [{"id": "c1", "title": "Saúde Petrobras"}, {"id": "c2", "title": "Mediservice"}, {"id": "c3", "title": "Cassi"}, {"id": "c4", "title": "Geap Saúde"}, {"id": "c5", "title": "Amil"}, {"id": "c6", "title": "Bradesco Saúde"}, {"id": "c7", "title": "Porto Seguro Saúde"}, {"id": "c8", "title": "Prevent Senior"}, {"id": "c9", "title": "Saúde Caixa"}]}]
                enviar_lista(phone, "Entendido! Selecione o seu plano de saúde para validarmos a cobertura:", "Ver Convênios", secoes)
                return jsonify({"status": "time_travel_convenio"}), 200

        # 🛑 RESET GERAL
        if msg_recebida.lower() in ["recomeçar", "reset", "menu inicial", "⬅️ voltar ao menu"]:
            update_paciente(phone, {"status": "escolhendo_unidade", "cellphone": phone, "servico": "", "modalidade": ""})
            enviar_botoes(phone, "Atendimento reiniciado. 🔄\n\nEm qual unidade deseja ser atendido?", [{"id": "u1", "title": "São Caetano"}, {"id": "u2", "title": "Ipiranga"}])
            return jsonify({"status": "reset"}), 200

        # --- BUSCA FEEGOW PELO TELEFONE ---
        if not info.get("feegow_id"):
            busca_tel = buscar_feegow_por_telefone(phone)
            if busca_tel:
                info.update({"feegow_id": busca_tel["id"], "title": busca_tel["nome"], "cpf": busca_tel["cpf"]})
                update_paciente(phone, {"feegow_id": busca_tel["id"], "title": busca_tel["nome"], "cpf": busca_tel["cpf"]})
            else:
                # Se não está no Feegow, verifica se está no histórico de 8.000 contatos
                doc_hist = db.collection("historico_contatos").document(phone).get()
                if doc_hist.exists:
                    info.update({"is_historico": True})
                    update_paciente(phone, {"is_historico": True})
                
        if not info:
            info = {"cellphone": phone, "status": "triagem"}
            update_paciente(phone, info)

        status = info.get("status", "triagem")

        # MUTE / ARQUIVADO
        if status == "pausado":
            update_paciente(phone, {"ultima_mensagem_paciente": msg_recebida, "unread": True})
            return jsonify({"status": "bot_silenciado"}), 200
            
        if status == "arquivado":
            update_paciente(phone, {"status": "escolhendo_unidade", "servico": "", "modalidade": ""})
            enviar_botoes(phone, "Olá! ✨ Que bom ter você de volta.\n\nPara iniciarmos, em qual unidade você deseja ser atendido?", [{"id": "u1", "title": "São Caetano"}, {"id": "u2", "title": "Ipiranga"}])
            return jsonify({"status": "reativacao_arquivado"}), 200
        
        estados_anexo = ["foto_carteirinha", "foto_pedido_medico", "pilates_caixa_foto_cart", "pilates_caixa_foto_pedido"]
        if tem_anexo and status not in estados_anexo:
            responder_texto(phone, "❌ Por favor, responda com *texto* ou clique nos botões. Ainda não é o momento de enviar arquivos.")
            return jsonify({"status": "anexo_bloqueado"}), 200

        servico = info.get("servico", "")
        is_veteran = True if info.get("feegow_id") else False
        
        modalidade = info.get("modalidade", "")
        convenio = info.get("convenio", "")
        if not modalidade and convenio: modalidade = "Convênio"
        elif not modalidade and servico in ["Recovery", "Liberação Miofascial"]: modalidade = "Particular"

        # Cortesia: responde e encerra (is_cortesia já definido acima)
        if is_cortesia and status in ["finalizado", "atendimento_humano", "pendente_feegow", "agendando"]:
            responder_texto(phone, "Por nada! 😊 Nossa equipe já recebeu seus dados e confirmará tudo em instantes. Qualquer dúvida, é só chamar!")
            return jsonify({"status": "courtesy_ignored"}), 200

        if status in ["finalizado", "atendimento_humano"]:
            # Janela de Silêncio: Se finalizado há menos de 30 min e for mensagem curta, ignora
            last_upd = info.get("lastUpdate")
            if last_upd and status == "finalizado":
                try:
                    dt_last = datetime.fromisoformat(last_upd.replace("Z", "+00:00"))
                    if (datetime.now(timezone.utc) - dt_last).total_seconds() < 1800: # 30 min
                        if len(msg_limpa.split()) <= 2: # "Ok obrigado", "Valeu", etc
                            return jsonify({"status": "silence_window_ignored"}), 200
                except: pass

            enviar_botoes(phone, "Olá! Nossa equipe precisa de mais um tempinho para a resolução da sua solicitação, mas já avisei que você entrou em contato novamente! 😊\n\nSe quiser reiniciar o atendimento, clique abaixo:", [{"id": "menu_ini", "title": "Menu Inicial"}])
            return jsonify({"status": "aguardando_equipe"}), 200
            
        if any(msg_limpa.startswith(w) for w in ["oi", "olá", "ola", "bom dia", "boa tarde", "boa noite"]) and status not in ["triagem", "escolhendo_unidade"]:
             enviar_botoes(phone, "Olá! ✨ Notei que estávamos no meio do seu atendimento. Deseja continuar de onde paramos?", [{"id": "c_sim", "title": "Sim, continuar"}, {"id": "menu_ini", "title": "Recomeçar"}])
             return jsonify({"status": "retomada"}), 200
             
        if msg_recebida == "Sim, continuar":
             responder_texto(phone, "Perfeito! Retomando...")
             return jsonify({"status": "retomada_confirmada"}), 200

        # ==========================================
        # LÓGICA DE ESTADOS
        # ==========================================
        if status == "triagem":
            update_paciente(phone, {"status": "escolhendo_unidade"})
            if info.get("is_historico"):
                enviar_botoes(phone, "Olá! ✨ Que bom ter você de volta à Conectifisio.\n\nPara iniciarmos seu novo atendimento, em qual unidade você deseja ser atendido?", [{"id": "u1", "title": "São Caetano"}, {"id": "u2", "title": "Ipiranga"}])
            else:
                enviar_botoes(phone, "Olá! ✨ Seja muito bem-vindo à Conectifisio.\n\nPara iniciarmos, em qual unidade você deseja ser atendido?", [{"id": "u1", "title": "São Caetano"}, {"id": "u2", "title": "Ipiranga"}])

        elif status == "escolhendo_unidade":
            if msg_recebida not in ["São Caetano", "Ipiranga"]:
                 enviar_botoes(phone, "Por favor, utilize os botões abaixo para escolher a unidade:", [{"id": "u1", "title": "São Caetano"}, {"id": "u2", "title": "Ipiranga"}])
            else:
                unidade_info = UNIDADES.get(msg_recebida, {})
                update_paciente(phone, {
                    "unit": msg_recebida,
                    "address": unidade_info.get("endereco"),
                    "maps_link": unidade_info.get("maps"),
                    "recommendation": unidade_info.get("recomendacao")
                })
                if is_veteran:
                    nome_salvo = info.get("title", "Paciente").split()[0]
                    update_paciente(phone, {"status": "menu_veterano"})
                    secoes = [{"title": "Como posso ajudar?", "rows": [{"id": "v1", "title": "🗓️ Reagendar Sessão"}, {"id": "v2", "title": "🔄 Nova Guia/Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v4", "title": "📁 Secretaria"}]}]
                    enviar_lista(phone, f"Unidade {msg_recebida} selecionada! ✅\n\nOlá, {nome_salvo}! ✨ Que bom ter você de volta. Como posso te ajudar hoje?", "Ver Opções", secoes)
                elif info.get("is_historico"):
                    update_paciente(phone, {"status": "cadastrando_nome"})
                    responder_texto(phone, f"Unidade {msg_recebida} selecionada! ✅\n\nComo você já conversou conosco antes, para agilizarmos seu cadastro, por favor me informe seu NOME COMPLETO:")
                else:
                    update_paciente(phone, {"status": "cadastrando_nome"})
                    responder_texto(phone, f"Unidade {msg_recebida} selecionada! ✅\n\nPara garantirmos um atendimento personalizado, como você gostaria de ser chamado(a)?")

        elif status == "cadastrando_nome":
            # Bug 6: Nome Curto (Exigir pelo menos Nome e Sobrenome)
            if len(msg_limpa.split()) < 2 or msg_recebida.isdigit():
                responder_texto(phone, "❌ Por favor, digite seu NOME E SOBRENOME para o cadastro:")
            else:
                # ==========================================
                # DETECÇÃO DE TERCEIRO AGENDANDO PARA OUTRO
                # Ex: "Meu nome é Bianca, mas estou agendando para o meu marido Luís"
                # ==========================================
                frases_terceiro = [
                    "estou agendando para", "estou marcando para", "estou ligando para",
                    "sou a mãe de", "sou o pai de", "sou a esposa de", "sou o marido de",
                    "sou a filha de", "sou o filho de", "agendando para meu", "agendando para minha",
                    "marcando para meu", "marcando para minha", "para o meu marido", "para a minha esposa",
                    "para o meu pai", "para a minha mãe", "para meu filho", "para minha filha",
                    "para meu irmão", "para minha irmã", "mas estou vendo atendimento para"
                ]
                eh_terceiro = any(frase in msg_limpa for frase in frases_terceiro)

                if eh_terceiro:
                    # Salva o nome de quem está contatando e sinaliza que é terceiro
                    update_paciente(phone, {"title": msg_recebida, "agendado_por_terceiro": True, "status": "confirmando_paciente_real"})
                    responder_texto(phone, f"Entendido! 😊 Fico feliz em ajudar.\n\nPara garantirmos que o cadastro fique correto no sistema, por favor me informe o *NOME COMPLETO do paciente* que será atendido (conforme documento):")
                else:
                    update_paciente(phone, {"title": msg_recebida, "status": "escolhendo_especialidade"})
                    secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}]}]
                    enviar_lista(phone, f"Prazer, {msg_recebida}! 😊\n\nPara direcionarmos o seu atendimento, qual serviço você procura hoje?", "Ver Serviços", secoes)

        elif status == "confirmando_paciente_real":
            # Terceiro informou o nome do paciente real
            if len(msg_limpa) < 2 or msg_recebida.isdigit():
                responder_texto(phone, "❌ Por favor, digite o nome completo do paciente.")
            else:
                nome_responsavel = info.get("title", "")
                update_paciente(phone, {"title": msg_recebida, "nome_responsavel": nome_responsavel, "status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}]}]
                enviar_lista(phone, f"Perfeito! Cadastro em nome de *{msg_recebida}*. ✅\n\nQual serviço o paciente procura hoje?", "Ver Serviços", secoes)

        elif status == "menu_veterano":
            if "Novo Serviço" in msg_recebida:
                update_paciente(phone, {"status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}, {"id": "e8", "title": "⬅️ Voltar ao Menu"}]}]
                enviar_lista(phone, "Perfeito! Qual novo serviço você deseja agendar?", "Ver Serviços", secoes)
            
            elif "Nova Guia" in msg_recebida or "Tratamento" in msg_recebida:
                # Nova Pergunta para Veteranos: Queixa
                update_paciente(phone, {"status": "cadastrando_queixa_veterano"})
                responder_texto(phone, "Entendido! Vamos organizar sua nova guia. ✅\n\nPara garantirmos o conforto e segurança no seu atendimento, me conte brevemente: o que te trouxe à clínica hoje?")
            
            elif "Reagendar" in msg_recebida:
                sessoes = consultar_agenda_feegow(info.get("feegow_id")) if info.get("feegow_id") else None
                # Correção do Limbo: Garante que o paciente não desaparece se não tiver modalidade
                mod_salva = info.get("modalidade") if info.get("modalidade") else "Particular"
                update_paciente(phone, {"status": "agendando", "modalidade": mod_salva})
                botoes = [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}]
                if sessoes and len(sessoes) > 0:
                    enviar_botoes(phone, f"Localizei suas próximas sessões:\n\n{chr(10).join(sessoes[:5])}\n\nPara qual período você gostaria de reagendar o seu atendimento? ☀️ ⛅", botoes)
                else:
                    enviar_botoes(phone, "Não encontrei agendamentos futuros próximos no sistema. Mas não se preocupe, vamos organizar isso agora! 😊\n\nQual o melhor período para você? ☀️ ⛅", botoes)
            
            elif "Secretaria" in msg_recebida or "📁" in msg_recebida:
                update_paciente(phone, {"status": "menu_secretaria"})
                # Funcionalidade 2: Botão Enviar Exames/Resultados
                secoes = [{"title": "Serviços de Secretaria", "rows": [{"id": "s1", "title": "Declaração de Horas"}, {"id": "s2", "title": "Relatório Fisio"}, {"id": "s3", "title": "Atualização Cadastral"}, {"id": "s5", "title": "📁 Enviar Exames/Resultados"}, {"id": "s4", "title": "⬅️ Voltar ao Menu"}]}]
                enviar_lista(phone, "Acesso à Secretaria. O que você precisa solicitar?", "Ver Serviços", secoes)

        elif status == "cadastrando_queixa_veterano":
            acolhimento = chamar_ia_custom(msg_recebida) or "Compreendo perfeitamente, e saiba que estamos aqui para cuidar de você da melhor forma."
            conv_salvo = info.get("convenio", "")
            update_paciente(phone, {"queixa": msg_recebida, "queixa_ia": acolhimento})
            
            if conv_salvo and conv_salvo.lower() != "particular":
                update_paciente(phone, {"status": "confirmando_convenio_salvo"})
                enviar_botoes(phone, f"{acolhimento}\n\nVi aqui que você utilizou o convênio *{conv_salvo}* anteriormente. Vamos seguir com ele?", [{"id": "c_manter", "title": "Sim, manter plano"}, {"id": "c_trocar", "title": "Troquei de plano"}, {"id": "c_part", "title": "Mudar p/ Particular"}])
            else:
                update_paciente(phone, {"status": "modalidade"})
                enviar_botoes(phone, f"{acolhimento}\n\nAs novas sessões serão pelo seu CONVÊNIO ou de forma PARTICULAR?", [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}])

        elif status == "menu_secretaria":
            if "Voltar" in msg_recebida:
                update_paciente(phone, {"status": "menu_veterano"})
                secoes = [{"title": "Como posso ajudar?", "rows": [{"id": "v1", "title": "🗓️ Reagendar Sessão"}, {"id": "v2", "title": "🔄 Nova Guia/Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v4", "title": "📁 Secretaria"}]}]
                enviar_lista(phone, "Voltando ao menu principal. Como posso ajudar?", "Ver Opções", secoes)
            elif "Exames" in msg_recebida:
                update_paciente(phone, {"status": "enviando_exames"})
                responder_texto(phone, "Perfeito! ✅ Pode enviar os arquivos (PDF ou Foto) agora mesmo. Eu vou anexá-los diretamente ao seu prontuário para o fisioterapeuta analisar.")
            else:
                update_paciente(phone, {"status": "atendimento_humano", "queixa": f"[SECRETARIA]: {msg_recebida}"})
                responder_texto(phone, f"A sua solicitação para '{msg_recebida}' foi registada com sucesso. A nossa equipe de secretaria vai assumir o atendimento para providenciar os detalhes. Aguarde um instante! 👩‍💻")

        elif status == "enviando_exames":
            if tem_anexo:
                # Salva no Storage imediatamente (media_id expira em ~5 min)
                media_data = salvar_midia_imediata(phone, "exame", media_id) if media_id else {}
                update_fields = {
                    "status": "atendimento_humano",
                    "queixa": "[EXAME ENVIADO]: Paciente enviou exames via robô.",
                    "tem_exame": True,
                }
                update_fields.update(media_data)
                update_paciente(phone, update_fields)
                # Tenta enviar ao prontuário do Feegow se já tiver feegow_id
                if info.get("feegow_id") and media_data.get("exame_storage_url"):
                    import sys
                    try:
                        feegow_id_int = int(info["feegow_id"])
                        conteudo_bytes, mime_type = baixar_midia_whatsapp_raw(media_id) if media_id else (None, None)
                        if not conteudo_bytes:
                            res_stor = requests.get(media_data["exame_storage_url"], timeout=20)
                            conteudo_bytes = res_stor.content if res_stor.status_code == 200 else None
                            mime_type = res_stor.headers.get("Content-Type", "image/jpeg") if conteudo_bytes else None
                        if conteudo_bytes:
                            b64_puro = base64.b64encode(conteudo_bytes).decode("utf-8")
                            data_uri = f"data:{mime_type};base64,{b64_puro}"
                            headers_up = {"x-access-token": FEEGOW_TOKEN, "Content-Type": "application/json", "User-Agent": "Conectifisio-Integration/1.0"}
                            for ep in ["/patient/upload-prontuario", "/patient/upload-base64"]:
                                res_up = requests.post(f"https://api.feegow.com/v1/api{ep}",
                                    json={"paciente_id": feegow_id_int, "arquivo_descricao": "Exame (Robô)", "base64_file": data_uri},
                                    headers=headers_up, timeout=30)
                                print(f"[FEEGOW-EXAME] {ep}: HTTP {res_up.status_code} | {res_up.text[:300]}", file=sys.stderr)
                                if res_up.status_code == 200:
                                    break
                    except Exception as e_ex:
                        import sys
                        print(f"[FEEGOW-EXAME] Erro: {e_ex}", file=sys.stderr)
                responder_texto(phone, "Recebido com sucesso! 📁 O arquivo foi salvo e nossa equipe vai analisar em breve.")
            else:
                responder_texto(phone, "❌ Não recebi o arquivo. Por favor, envie o seu exame ou resultado (Foto ou PDF).")

        elif status == "confirmando_convenio_salvo":
            if "manter" in msg_recebida.lower():
                conv_salvo = info.get("convenio", "")
                if not verificar_cobertura(conv_salvo, servico or "Fisio Ortopédica"):
                    update_paciente(phone, {"status": "cobertura_recusada"})
                    enviar_botoes(phone, f"⚠️ O seu plano *{conv_salvo}* não possui cobertura para *{servico}*.\n\nVocê pode realizar o atendimento Particular para reembolso. Deseja seguir no particular?", [{"id": "part", "title": "Seguir Particular"}, {"id": "out", "title": "Escolher outro"}])
                else:
                    update_paciente(phone, {"modalidade": "Convênio", "status": "foto_pedido_medico"})
                    responder_texto(phone, "Perfeito! ✅ Como você manteve o plano, precisamos apenas do novo pedido médico.\n\nPor favor, envie a FOTO ou PDF DO SEU PEDIDO MÉDICO atualizado.")
            elif "Particular" in msg_recebida:
                update_paciente(phone, {"modalidade": "Particular", "status": "agendando"})
                enviar_botoes(phone, "Perfeito! Mudamos para Particular. Qual o melhor período para você? ☀️ ⛅", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])
            else:
                update_paciente(phone, {"status": "nome_convenio"})
                secoes = [{"title": "Convênios Aceitos", "rows": [{"id": "c1", "title": "Saúde Petrobras"}, {"id": "c2", "title": "Mediservice"}, {"id": "c3", "title": "Cassi"}, {"id": "c4", "title": "Geap Saúde"}, {"id": "c5", "title": "Amil"}, {"id": "c6", "title": "Bradesco Saúde"}, {"id": "c7", "title": "Porto Seguro Saúde"}, {"id": "c8", "title": "Prevent Senior"}, {"id": "c9", "title": "Saúde Caixa"}]}]
                enviar_lista(phone, "Entendido! Selecione o seu NOVO plano de saúde:", "Ver Convênios", secoes)

        elif status == "escolhendo_especialidade":
            if "Voltar" in msg_recebida:
                update_paciente(phone, {"status": "menu_veterano"})
                secoes = [{"title": "Como posso ajudar?", "rows": [{"id": "v1", "title": "🗓️ Reagendar Sessão"}, {"id": "v2", "title": "🔄 Nova Guia/Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v4", "title": "📁 Secretaria"}]}]
                enviar_lista(phone, "Voltando ao menu principal. Como posso ajudar?", "Ver Opções", secoes)
            elif msg_recebida in ["Recovery", "Liberação Miofascial"]:
                update_paciente(phone, {"servico": msg_recebida, "modalidade": "Particular", "status": "cadastrando_queixa"})
                responder_texto(phone, f"Ótima escolha para performance em {msg_recebida}! 🚀\n\nPara prepararmos o consultório com a estrutura correta para você, me conte brevemente: o que te trouxe aqui hoje?")
            elif msg_recebida == "Fisio Neurológica":
                update_paciente(phone, {"servico": msg_recebida, "status": "triagem_neuro"})
                texto_neuro = "Queremos garantir que sua experiência na Conectifisio seja a mais confortável e segura possível. 😊\n\nPoderia nos contar em qual dessas opções de suporte você se enquadra hoje?\n\n1️⃣ Preciso de auxílio integral (ajuda de outra pessoa para me movimentar).\n2️⃣ Preciso de auxílio parcial (utilizo bengala, andador).\n3️⃣ Tenho autonomia total."
                enviar_botoes(phone, texto_neuro, [{"id": "n1", "title": "1️⃣ Auxílio integral"}, {"id": "n2", "title": "2️⃣ Auxílio parcial"}, {"id": "n3", "title": "3️⃣ Autonomia total"}])
            elif msg_recebida == "Pilates Studio":
                if info.get("unit") == "Ipiranga":
                    update_paciente(phone, {"servico": msg_recebida, "status": "transferencia_pilates"})
                    enviar_botoes(phone, "O Pilates Studio é uma modalidade exclusiva da nossa unidade de **São Caetano**. 🧘‍♀️\n\nDeseja transferir o seu atendimento para lá para realizar o Pilates?", [{"id": "tp_sim", "title": "Sim, mudar p/ São Caetano"}, {"id": "tp_nao", "title": "Não, escolher outro"}])
                else:
                    update_paciente(phone, {"servico": msg_recebida, "status": "pilates_modalidade"})
                    secoes = [{"title": "Modalidade Pilates", "rows": [{"id": "p_part", "title": "💎 Plano Particular"}, {"id": "p_caixa", "title": "🏦 Saúde Caixa"}, {"id": "p_app", "title": "💪 Wellhub/Totalpass"}, {"id": "p_vol", "title": "⬅️ Voltar"}]}]
                    enviar_lista(phone, "Excelente escolha! 🧘‍♀️ O Pilates é fundamental para a correção postural e fortalecimento.\n\nPara passarmos as informações corretas de horários e valores, como você pretende realizar as aulas?", "Ver Opções", secoes)
            else:
                update_paciente(phone, {"servico": msg_recebida, "status": "cadastrando_queixa"})
                responder_texto(phone, f"Entendido! {msg_recebida} selecionada. ✅\n\nPara garantirmos o conforto e segurança no seu atendimento, me conte brevemente: o que te trouxe à clínica hoje?")

        elif status == "transferencia_pilates":
            if "Sim" in msg_recebida or "mudar" in msg_recebida.lower():
                update_paciente(phone, {"unit": "São Caetano", "status": "pilates_modalidade"})
                secoes = [{"title": "Modalidade Pilates", "rows": [{"id": "p_part", "title": "💎 Plano Particular"}, {"id": "p_caixa", "title": "🏦 Saúde Caixa"}, {"id": "p_app", "title": "💪 Wellhub/Totalpass"}, {"id": "p_vol", "title": "⬅️ Voltar"}]}]
                enviar_lista(phone, "Perfeito! A sua unidade foi alterada para **São Caetano** com sucesso. ✅\n\nAgora, como você pretende realizar as aulas de Pilates?", "Ver Opções", secoes)
            else:
                update_paciente(phone, {"servico": "", "status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}]}]
                enviar_lista(phone, "Sem problemas! Mantemos o seu atendimento na unidade **Ipiranga**. Qual outro serviço você procura hoje?", "Ver Serviços", secoes)

        # ==========================================
        # FAST-TRACK PILATES (Fluxo Ultra Rápido)
        # ==========================================
        elif status.startswith("pilates_"):
            if status == "pilates_modalidade":
                if "Voltar" in msg_recebida:
                    update_paciente(phone, {"status": "escolhendo_especialidade"})
                    secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}]}]
                    enviar_lista(phone, "Voltando ao menu de especialidades. Qual serviço você procura hoje?", "Ver Serviços", secoes)
                elif "Wellhub" in msg_recebida or "Totalpass" in msg_recebida:
                    update_paciente(phone, {"modalidade": "Parceria App", "status": "pilates_app"})
                    enviar_botoes(phone, "Qual desses aplicativos você utiliza para o seu plano?", [{"id": "w1", "title": "Wellhub"}, {"id": "t1", "title": "Totalpass"}])
                elif "Saúde Caixa" in msg_recebida:
                    update_paciente(phone, {"modalidade": "Convênio", "convenio": "Saúde Caixa", "status": "pilates_caixa_foto_pedido"})
                    responder_texto(phone, "Entendido! 🏦 Para o plano Saúde Caixa, envie uma FOTO ou PDF do seu PEDIDO MÉDICO atualizado para seguirmos.")
                elif "Particular" in msg_recebida:
                    update_paciente(phone, {"modalidade": "Particular", "status": "pilates_part_exp"})
                    enviar_botoes(phone, "Ótima escolha! ✨ O Pilates vai ajudar a fortalecer o corpo. Gostaria de agendar uma aula experimental gratuita para conhecer o nosso estúdio?", [{"id": "pe_sim", "title": "Sim, gostaria"}, {"id": "pe_nao", "title": "Não, já quero começar"}])

            elif status == "pilates_part_exp":
                update_paciente(phone, {"interesse_experimental": msg_recebida, "status": "pilates_part_periodo"})
                enviar_botoes(phone, "Agradecemos a escolha! Para o agendamento, qual o melhor período para você?", [{"id": "pe_m", "title": "☀️ Manhã"}, {"id": "pe_t", "title": "⛅ Tarde"}, {"id": "pe_n", "title": "🌙 Noite"}])

            elif status == "pilates_part_periodo":
                update_paciente(phone, {"periodo": msg_recebida})
                if is_veteran:
                    update_paciente(phone, {"status": "atendimento_humano"})
                    responder_texto(phone, "Tudo pronto! Nossa equipe vai assumir o atendimento agora mesmo para alinhar seu horário. Aguarde um instante! 👩‍⚕️")
                else:
                    update_paciente(phone, {"status": "pilates_part_nome"})
                    responder_texto(phone, "Para agilizarmos seu atendimento de Pilates, por favor, digite seu NOME E SOBRENOME:")
            
            elif status == "pilates_part_nome":
                update_paciente(phone, {"title": msg_recebida, "status": "pilates_part_cpf"})
                responder_texto(phone, "Nome registrado! ✅ Agora, para validarmos o seu registro com segurança, digite o seu CPF (apenas os 11 números):")

            elif status == "pilates_part_cpf":
                cpf_limpo = re.sub(r'\D', '', msg_recebida)
                if not validar_cpf(cpf_limpo): responder_texto(phone, "❌ CPF inválido. Por favor, verifique os números e digite novamente:")
                else:
                    update_paciente(phone, {"cpf": cpf_limpo, "status": "pilates_part_nasc"})
                    responder_texto(phone, "Recebido! ✅ Qual sua data de nascimento? (Ex: 15/05/1980)")

            elif status == "pilates_part_nasc":
                if not validar_data_nascimento(msg_recebida): responder_texto(phone, "❌ Data inválida. Digite uma data real no formato DD/MM/AAAA (ex: 15/05/1980).")
                else:
                    update_paciente(phone, {"birthDate": msg_recebida, "status": "pilates_part_email"})
                    responder_texto(phone, "Para completarmos, qual seu melhor E-MAIL?")

            elif status == "pilates_part_email":
                if "@" not in msg_recebida or "." not in msg_recebida: responder_texto(phone, "❌ E-mail inválido. Por favor, digite um e-mail válido.")
                else:
                    update_paciente(phone, {"email": msg_recebida, "status": "atendimento_humano"})
                    responder_texto(phone, "Tudo pronto! Nossa equipe vai assumir o atendimento agora mesmo para confirmar o seu horário. Aguarde um instante! 👩‍⚕️")

            elif status == "pilates_app":
                update_paciente(phone, {"convenio": msg_recebida})
                if msg_recebida == "Wellhub":
                    update_paciente(phone, {"status": "pilates_wellhub_id"})
                    responder_texto(phone, "Por favor, informe o seu Wellhub ID.")
                else:
                    update_paciente(phone, {"status": "pilates_app_periodo"})
                    enviar_botoes(phone, "Tudo certo com o Totalpass! ✅ Para agilizarmos o agendamento, qual o melhor período para você?", [{"id": "pe_m", "title": "☀️ Manhã"}, {"id": "pe_t", "title": "⛅ Tarde"}, {"id": "pe_n", "title": "🌙 Noite"}])
            
            elif status == "pilates_wellhub_id":
                update_paciente(phone, {"numCarteirinha": msg_recebida, "status": "pilates_app_periodo"})
                enviar_botoes(phone, "ID recebido com sucesso! ✅ Para agilizarmos, qual o melhor período para você?", [{"id": "pe_m", "title": "☀️ Manhã"}, {"id": "pe_t", "title": "⛅ Tarde"}, {"id": "pe_n", "title": "🌙 Noite"}])
                
            elif status == "pilates_app_periodo":
                periodo_limpo = msg_recebida.replace("☀️ ", "").replace("⛅ ", "").replace("🌙 ", "")
                update_paciente(phone, {"periodo": msg_recebida})
                if is_veteran:
                    update_paciente(phone, {"status": "atendimento_humano"})
                    responder_texto(phone, f"Tudo pronto! Nossa equipe vai confirmar o horário para a {periodo_limpo} em instantes. 👩‍⚕️")
                else:
                    update_paciente(phone, {"status": "pilates_app_nome_completo"})
                    responder_texto(phone, "Para finalizarmos, digite o seu NOME E SOBRENOME:")

            elif status == "pilates_app_nome_completo":
                update_paciente(phone, {"title": msg_recebida, "status": "pilates_app_cpf"})
                responder_texto(phone, "Nome registrado! ✅ Agora, digite o seu CPF (apenas os 11 números):")

            elif status == "pilates_app_cpf":
                cpf_limpo = re.sub(r'\D', '', msg_recebida)
                if not validar_cpf(cpf_limpo): responder_texto(phone, "❌ CPF inválido. Por favor, verifique os números e digite novamente:")
                else:
                    update_paciente(phone, {"cpf": cpf_limpo, "status": "pilates_app_nasc"})
                    responder_texto(phone, "Recebido! ✅ Qual sua data de nascimento? (Ex: 15/05/1980)")

            elif status == "pilates_app_nasc":
                if not validar_data_nascimento(msg_recebida): responder_texto(phone, "❌ Data inválida. Digite uma data real no formato DD/MM/AAAA (ex: 15/05/1980).")
                else:
                    update_paciente(phone, {"birthDate": msg_recebida, "status": "pilates_app_email"})
                    responder_texto(phone, "Para completarmos o registro, qual seu melhor E-MAIL?")

            elif status == "pilates_app_email":
                if "@" not in msg_recebida or "." not in msg_recebida: responder_texto(phone, "❌ E-mail inválido. Por favor, digite um e-mail válido.")
                else:
                    update_paciente(phone, {"email": msg_recebida, "status": "atendimento_humano"})
                    responder_texto(phone, "Cadastro concluído! 🎉 Nossa equipe vai confirmar o seu horário de Pilates e logo retorna. 👩‍⚕️")

            # Fluxo Caixa: Exige Documento
            elif status == "pilates_caixa_foto_pedido":
                if not tem_anexo: responder_texto(phone, "❌ Por favor, envie o Pedido Médico.")
                else:
                    update_paciente(phone, {"status": "pilates_caixa_periodo", "tem_foto_pedido": True, "pedido_media_id": media_id})
                    enviar_botoes(phone, "Documentação recebida com sucesso! ✅ Para agilizarmos o agendamento, qual o melhor período para você?", [{"id": "pe_m", "title": "☀️ Manhã"}, {"id": "pe_t", "title": "⛅ Tarde"}, {"id": "pe_n", "title": "🌙 Noite"}])
            
            elif status == "pilates_caixa_periodo":
                update_paciente(phone, {"periodo": msg_recebida})
                if is_veteran:
                    update_paciente(phone, {"status": "atendimento_humano"})
                    responder_texto(phone, "Tudo pronto! Nossa equipe vai assumir o atendimento agora mesmo para alinhar seu horário. 👩‍⚕️")
                else:
                    update_paciente(phone, {"status": "pilates_caixa_nome"})
                    responder_texto(phone, "Para finalizarmos, por favor digite o seu NOME E SOBRENOME:")

            elif status == "pilates_caixa_nome":
                update_paciente(phone, {"title": msg_recebida, "status": "pilates_caixa_cpf"})
                responder_texto(phone, "Nome registrado! ✅ Agora, digite seu CPF (apenas os 11 números):")

            elif status == "pilates_caixa_cpf":
                cpf_limpo = re.sub(r'\D', '', msg_recebida)
                if not validar_cpf(cpf_limpo): responder_texto(phone, "❌ CPF inválido. Digite apenas os 11 números.")
                else:
                    update_paciente(phone, {"cpf": cpf_limpo, "status": "pilates_caixa_nasc"})
                    responder_texto(phone, "Recebido! ✅ Qual sua data de nascimento? (Ex: 15/05/1980)")

            elif status == "pilates_caixa_nasc":
                if not validar_data_nascimento(msg_recebida): responder_texto(phone, "❌ Data inválida. Digite no formato DD/MM/AAAA (ex: 15/05/1980).")
                else:
                    update_paciente(phone, {"birthDate": msg_recebida, "status": "pilates_caixa_email"})
                    responder_texto(phone, "Ótimo! Qual seu melhor E-MAIL?")

            elif status == "pilates_caixa_email":
                if "@" not in msg_recebida or "." not in msg_recebida: responder_texto(phone, "❌ E-mail inválido. Por favor, digite um e-mail válido.")
                else:
                    update_paciente(phone, {"email": msg_recebida, "status": "atendimento_humano"})
                    responder_texto(phone, "Recebido! ✅ Tudo pronto! Nossa equipe vai confirmar o seu horário e logo retorna. 👩‍⚕️")

        elif status == "triagem_neuro":
            if "integral" in msg_limpa or "1" in msg_limpa:
                update_paciente(phone, {"mobilidade": "Necessidade de auxílio integral", "status": "triagem_neuro_queixa"})
                responder_texto(phone, "Agradeço por compartilhar. ❤️ Para prepararmos o consultório com a estrutura correta para você, me conte brevemente: o que te trouxe à clínica hoje?")
            else:
                mobilidade = "Preciso de auxílio parcial" if "parcial" in msg_limpa or "2" in msg_limpa else "Autonomia total"
                update_paciente(phone, {"mobilidade": mobilidade, "status": "triagem_neuro_queixa"})
                responder_texto(phone, "Anotado! ✅\n\nPara prepararmos o consultório com a estrutura correta para você, me conte brevemente: o que te trouxe à clínica hoje?")

        elif status == "triagem_neuro_queixa":
            acolhimento = chamar_ia_custom(msg_recebida) or "Compreendo perfeitamente, e saiba que estamos aqui para cuidar de você da melhor forma."
            conv_salvo = info.get("convenio", "")
            update_paciente(phone, {"queixa": msg_recebida, "queixa_ia": acolhimento, "lastPatientInteraction": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S+00:00'), "status": "modalidade"})
            if is_veteran and conv_salvo and conv_salvo.lower() != "particular":
                update_paciente(phone, {"status": "confirmando_convenio_salvo"})
                enviar_botoes(phone, f"{acolhimento}\n\nVi aqui que você já utilizou o convênio *{conv_salvo}*. Vamos seguir com ele para este serviço?", [{"id": "c_manter", "title": "Sim, manter plano"}, {"id": "c_trocar", "title": "Troquei de plano"}, {"id": "c_part", "title": "Mudar p/ Particular"}])
            else:
                enviar_botoes(phone, f"{acolhimento}\n\nDeseja atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}])

        elif status == "cadastrando_queixa":
            acolhimento = chamar_ia_custom(msg_recebida) or "Compreendo perfeitamente, e saiba que estamos aqui para cuidar de você da melhor forma."
            if servico in ["Recovery", "Liberação Miofascial"]:
                if is_veteran:
                    update_paciente(phone, {"queixa": msg_recebida, "queixa_ia": acolhimento, "status": "agendando"})
                    enviar_botoes(phone, f"{acolhimento}\n\nComo você já é nosso paciente, vamos direto para a agenda. Qual o melhor período para você? ☀️⛅", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])
                else:
                    update_paciente(phone, {"queixa": msg_recebida, "queixa_ia": acolhimento, "status": "cadastrando_nome_completo"})
                    responder_texto(phone, f"{acolhimento}\n\nPara iniciarmos seu cadastro, por favor digite seu NOME COMPLETO (conforme documento):")
            else:
                update_paciente(phone, {"queixa": msg_recebida, "queixa_ia": acolhimento, "status": "modalidade"})
                conv_salvo = info.get("convenio", "")
                if is_veteran and conv_salvo and conv_salvo.lower() != "particular":
                    update_paciente(phone, {"status": "confirmando_convenio_salvo"})
                    enviar_botoes(phone, f"{acolhimento}\n\nVi aqui que você já utilizou o convênio *{conv_salvo}*. Vamos seguir com ele para este serviço?", [{"id": "c_manter", "title": "Sim, manter plano"}, {"id": "c_trocar", "title": "Troquei de plano"}, {"id": "c_part", "title": "Mudar p/ Particular"}])
                else:
                    enviar_botoes(phone, f"{acolhimento}\n\nDeseja atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", [{"id": "m1", "title": "Convênio"}, {"id": "m2", "title": "Particular"}])

        elif status == "modalidade":
            if "Convênio" in msg_recebida:
                update_paciente(phone, {"modalidade": "Convênio", "status": "nome_convenio"})
                secoes = [{"title": "Convênios Aceitos", "rows": [{"id": "c1", "title": "Saúde Petrobras"}, {"id": "c2", "title": "Mediservice"}, {"id": "c3", "title": "Cassi"}, {"id": "c4", "title": "Geap Saúde"}, {"id": "c5", "title": "Amil"}, {"id": "c6", "title": "Bradesco Saúde"}, {"id": "c7", "title": "Porto Seguro Saúde"}, {"id": "c8", "title": "Prevent Senior"}, {"id": "c9", "title": "Saúde Caixa"}]}]
                enviar_lista(phone, "Selecione o seu plano de saúde para validarmos a cobertura:", "Ver Convênios", secoes)
            else:
                if is_veteran:
                    update_paciente(phone, {"modalidade": "Particular", "status": "agendando"})
                    enviar_botoes(phone, "Perfeito! Como você já é nosso paciente, vamos direto para a agenda. Qual o melhor período para você? ☀️⛅", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])
                else:
                    update_paciente(phone, {"modalidade": "Particular", "status": "cadastrando_nome_completo"})
                    responder_texto(phone, "Perfeito! Para seu cadastro particular, digite seu NOME COMPLETO (conforme documento):")

        elif status == "nome_convenio":
            convenio_selecionado = msg_recebida
            if not verificar_cobertura(convenio_selecionado, servico):
                update_paciente(phone, {"convenio": convenio_selecionado, "status": "cobertura_recusada"})
                enviar_botoes(phone, f"⚠️ O seu plano *{convenio_selecionado}* não possui cobertura direta para *{servico}* na nossa clínica.\n\nNo entanto, você pode realizar o atendimento Particular para solicitar reembolso. Deseja seguir no particular?", [{"id": "part", "title": "Seguir Particular"}, {"id": "out", "title": "Escolher outro"}])
            else:
                if is_veteran:
                    update_paciente(phone, {"convenio": convenio_selecionado, "status": "num_carteirinha"})
                    responder_texto(phone, f"Anotado: {convenio_selecionado}! ✅\n\nComo você já é nosso paciente, pulei o preenchimento de CPF e E-mail! Para atualizarmos o seu cadastro, qual o NÚMERO DA SUA NOVA CARTEIRINHA? (Apenas números)")
                else:
                    update_paciente(phone, {"convenio": convenio_selecionado, "status": "cadastrando_nome_completo"})
                    responder_texto(phone, f"Anotado: {convenio_selecionado}! ✅\n\nAgora, digite seu NOME COMPLETO (conforme documento):")

        elif status == "cobertura_recusada":
            if "Particular" in msg_recebida:
                update_paciente(phone, {"modalidade": "Particular", "status": "agendando" if is_veteran else "cadastrando_nome_completo"})
                if is_veteran: enviar_botoes(phone, "Perfeito! Mudamos para Particular. Qual o melhor período para você? ☀️ ⛅", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])
                else: responder_texto(phone, "Perfeito! Para seu cadastro particular, digite seu NOME COMPLETO (conforme documento):")
            else:
                update_paciente(phone, {"status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}]}]
                enviar_lista(phone, "Sem problemas! Qual outro serviço você gostaria de buscar?", "Ver Serviços", secoes)

        elif status == "cadastrando_nome_completo":
            if len(msg_limpa) < 2 or msg_recebida.isdigit(): responder_texto(phone, "❌ Por favor, digite um nome válido.")
            else:
                update_paciente(phone, {"title": msg_recebida, "status": "cpf"})
                responder_texto(phone, "Nome registrado! ✅ Agora, para validarmos o seu registro com segurança junto ao sistema, digite o seu CPF (apenas os 11 números):")

        elif status == "cpf":
            cpf_limpo = re.sub(r'\D', '', msg_recebida)
            if not validar_cpf(cpf_limpo):
                responder_texto(phone, "❌ CPF inválido. Por favor, verifique os números e digite novamente:")
            else:
                busca = buscar_feegow_por_cpf(cpf_limpo)
                if busca:
                    # Veterano reconhecido: pula dados pessoais MAS exige documentos do novo agendamento
                    if modalidade == "Particular":
                        # Particular veterano: vai direto para agenda (sem documentos)
                        update_paciente(phone, {"cpf": cpf_limpo, "title": busca['nome'], "feegow_id": busca['id'], "status": "agendando"})
                        enviar_botoes(phone, f"Reconheci seu cadastro, {busca['nome']}! ✨\n\nPulei as etapas de e-mail e nascimento. Qual o melhor período para você?", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])
                    else:
                        # Convênio veterano: pula dados pessoais mas EXIGE carteirinha e pedido médico
                        update_paciente(phone, {"cpf": cpf_limpo, "title": busca['nome'], "feegow_id": busca['id'], "status": "num_carteirinha"})
                        responder_texto(phone, f"Reconheci seu cadastro, {busca['nome']}! ✨\n\nPulei as etapas de e-mail e nascimento. Para atualizarmos o seu cadastro, qual o NÚMERO DA SUA CARTEIRINHA? (Apenas números)")
                else:
                    update_paciente(phone, {"cpf": cpf_limpo, "status": "data_nascimento"})
                    responder_texto(phone, "Recebido! ✅ Para completarmos sua ficha clínica, qual sua data de nascimento? (Ex: 15/05/1980)")

        elif status == "data_nascimento":
            validacao = validar_data_nascimento(msg_recebida)
            if validacao == "menor_12":
                update_paciente(phone, {"birthDate": msg_recebida, "status": "finalizado", "robo_ligado": False})
                responder_texto(phone, "⚠️ Atenção: Identificamos que o paciente é menor de 12 anos. Informamos que não possuímos especialidade pediátrica em nossas unidades. Infelizmente, não poderemos realizar este agendamento. Recomendamos a busca por profissionais especializados na área infantil. Obrigado! 🙏")
            elif not validacao:
                responder_texto(phone, "❌ Data de nascimento inválida. Digite uma data real no formato DD/MM/AAAA (ex: 15/05/1980).")
            else:
                update_paciente(phone, {"birthDate": msg_recebida, "status": "coletando_email"})
                responder_texto(phone, "Ótimo! Para finalizar seu cadastro, qual seu melhor E-MAIL?")

        elif status == "coletando_email":
            if "@" not in msg_recebida or "." not in msg_recebida: responder_texto(phone, "❌ E-mail inválido. Por favor, digite um e-mail válido.")
            else:
                if modalidade == "Particular":
                    update_paciente(phone, {"email": msg_recebida, "status": "agendando"})
                    enviar_botoes(phone, "Cadastro concluído! 🎉\n\nQual o melhor período para verificarmos a agenda particular?", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])
                else:
                    update_paciente(phone, {"email": msg_recebida, "status": "num_carteirinha"})
                    responder_texto(phone, "Certo! E qual o NÚMERO DA CARTEIRINHA do seu plano? (apenas números)")

        elif status == "num_carteirinha":
            num_limpo = re.sub(r'\D', '', msg_recebida)
            update_paciente(phone, {"numCarteirinha": num_limpo, "status": "foto_carteirinha"})
            responder_texto(phone, "Anotado! ✅ Agora a parte documental:\n\nEnvie uma FOTO NÍTIDA da sua carteirinha (use o ícone de clipe ou câmera do WhatsApp).")

        elif status == "foto_carteirinha":
            if not tem_anexo: responder_texto(phone, "❌ Não recebi a imagem. Por favor, envie a foto da sua carteirinha.")
            else:
                # Baixa do WhatsApp IMEDIATAMENTE e salva no Firebase Storage
                # (media_id expira em ~5 min, Storage não tem limite de 1MB)
                media_data = salvar_midia_imediata(phone, "carteirinha", media_id) if media_id else {}
                update_fields = {
                    "status": "foto_pedido_medico",
                    "tem_foto_carteirinha": True,
                }
                update_fields.update(media_data)
                update_paciente(phone, update_fields)
                responder_texto(phone, "Foto recebida! ✅\n\nAgora, envie a FOTO DO SEU PEDIDO MÉDICO.")

        elif status == "foto_pedido_medico":
            if not tem_anexo: responder_texto(phone, "❌ Por favor, envie a foto do seu Pedido Médico.")
            else:
                # Baixa do WhatsApp IMEDIATAMENTE e salva no Firebase Storage
                media_data = salvar_midia_imediata(phone, "pedido", media_id) if media_id else {}
                update_fields = {
                    "status": "agendando",
                    "tem_foto_pedido": True,
                }
                update_fields.update(media_data)
                update_paciente(phone, update_fields)
                enviar_botoes(phone, "Documentação completa! 🎉\n\nQual o melhor período para verificarmos a sua vaga?", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])

        elif status == "agendando":
            if msg_recebida in ["Manhã", "Tarde", "Noite"]:
                info["periodo"] = msg_recebida
                # Bug Fix: Garantir que modalidade está correta antes de definir a coluna destino
                # Se o paciente tem convênio salvo mas modalidade vazia, força "Convênio"
                # Se tem fotos de carteirinha/pedido, também é Convênio
                if not modalidade and (info.get("convenio") or info.get("carteirinha_media_id")):
                    modalidade = "Convênio"
                # Convênio vai para 'pendente_feegow' para aparecer na coluna correta do Kanban
                # Particular vai para 'finalizado' (sem necessidade de validação)
                novo_status = "pendente_feegow" if modalidade == "Convênio" else "finalizado"
                update_data = {"periodo": msg_recebida, "status": novo_status, "modalidade": modalidade}
                
                if servico and "Pilates" not in servico:
                    resultado_feegow = integrar_feegow(phone, info)
                    if resultado_feegow: update_data.update(resultado_feegow)
                
                update_paciente(phone, update_data)
                
                if modalidade == "Convênio":
                    texto_final = (f"Período selecionado com sucesso! ✅ Nossa recepção já recebeu as suas fotos e está realizando a validação de cobertura junto ao seu plano de saúde.\n\n"
                                   f"Assim que a elegibilidade for confirmada, enviaremos as opções de horários disponíveis. Fique de olho por aqui! 😊")
                else:
                    texto_final = (f"Período selecionado com sucesso! ✅ Tudo pronto! A nossa equipe já está verificando a disponibilidade dos nossos especialistas para o período da {msg_recebida}.\n\n"
                                   f"Em instantes voltaremos com as opções exatas para confirmarmos o seu horário. Fique de olho por aqui! ✨")
                
                responder_texto(phone, texto_final)
                # Recomendações e endereço são enviados MANUALMENTE pelo colaborador
                # após confirmar o agendamento (fluxo semiautomaticó)
            else:
                enviar_botoes(phone, "Por favor, escolha o período:", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])

        return jsonify({"status": "success"}), 200
    except Exception as e:
        print(f"❌ Erro Crítico POST: {traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 200

# ==========================================
# ROTA: CHAT MANUAL E UPLOAD DE FOTOS/PDF DO DASHBOARD
# ==========================================
@app.route("/api/chat/send", methods=["POST", "OPTIONS"])
def chat_manual():
    if request.method == "OPTIONS": return jsonify({"status": "ok"}), 200
    try:
        data = request.get_json()
        phone = data.get("phone")
        message_text = data.get("message", "")
        file_b64 = data.get("file_b64")
        file_name = data.get("file_name", "arquivo")
        mime_type = data.get("mime_type", "")
        
        if not phone: return jsonify({"success": False, "error": "Falta telefone"}), 400

        # 1. Enviar Anexo (Se houver)
        if file_b64:
            b64_data = file_b64.split(",")[1] if "," in file_b64 else file_b64
            file_bytes = base64.b64decode(b64_data)
            
            url_media = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/media"
            files = {'file': (file_name, file_bytes, mime_type)}
            res_media = requests.post(url_media, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, files=files, data={'messaging_product': 'whatsapp'})
            media_id = res_media.json().get("id")

            if media_id:
                msg_type = "image" if "image" in mime_type else "document"
                payload = {"messaging_product": "whatsapp", "to": phone, "type": msg_type}
                
                if msg_type == "image":
                    payload["image"] = {"id": media_id}
                    if message_text: payload["image"]["caption"] = message_text
                else:
                    payload["document"] = {"id": media_id, "filename": file_name}
                    if message_text: payload["document"]["caption"] = message_text

                requests.post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages", json=payload, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"})
                
                txt_hist = f"[📎 Anexo enviado] {message_text}"
                registrar_historico(phone, "clinica", "anexo", txt_hist)
                update_paciente(phone, {"status": "pausado", "ultima_mensagem_clinica": txt_hist, "unread": False})
                return jsonify({"success": True}), 200

        # 2. Enviar Só Texto
        if message_text and not file_b64:
            res = responder_texto(phone, message_text, remetente="clinica")
            if res.status_code == 200:
                # Auto-Pause: Desliga o robô quando o humano fala
                update_paciente(phone, {"status": "pausado", "robo_ligado": False, "ultima_mensagem_clinica": message_text, "unread": False})
                return jsonify({"success": True}), 200
            
        return jsonify({"success": False, "error": "Falha geral no envio"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    # Cloud Run define PORT=8080 automaticamente. Fallback para 5000 em desenvolvimento local.
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
