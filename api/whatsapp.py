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
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "ft:gpt-4o-mini-2024-07-18:conectifisio:conectifisio-v8:DiX7KzHF")
OPENAI_FAQ_MODEL = os.environ.get("OPENAI_FAQ_MODEL", OPENAI_MODEL) # Modelo FAQ v8 — system prompt unificado
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
        "lat": -23.6028705845668,
        "lon": -46.61265078607649,
        "nome_oficial": "Conectifisio - Unidade Ipiranga",
        "dica_chegada": "📍 Estamos a *30m do Metrô Alto do Ipiranga* (Linha Verde).",
        "recomendacao": "Traga o pedido médico original, documento com foto e carteirinha. Chegue com 15 minutos de antecedência. Unidade próxima ao metrô Alto do Ipiranga (30 metros)."
    },
    "São Caetano": {
        "endereco": "Rua Alegre, 667 - Santa Paula, São Caetano do Sul - SP (Próximo ao Hotel Mercure)",
        "maps": "https://maps.app.goo.gl/mhct13HEmChxmfJF8",
        "lat": -23.622233790675214,
        "lon": -46.55177682658113,
        "nome_oficial": "Conectifisio - Unidade São Caetano",
        "dica_chegada": "📍 Temos *vaga de embarque/desembarque na porta*.",
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
    """🛡️ Retorna dict {"valida": bool, "data": str_normalizada, "menor_12": bool}.
    Normaliza ano de 2 dígitos para 4 dígitos (78 → 1978, 05 → 2005).
    Ver mapa_fluxos.md → bug Cleusa."""
    # Aceitar ano com 2 dígitos (ex: 15/04/60 → 15/04/1960)
    if re.match(r'^\d{2}/\d{2}/\d{2}$', data_str):
        partes = data_str.split('/')
        ano = int(partes[2])
        ano_completo = 1900 + ano if ano >= 20 else 2000 + ano
        data_str = f"{partes[0]}/{partes[1]}/{ano_completo}"
    if not re.match(r'^\d{2}/\d{2}/\d{4}$', data_str):
        return {"valida": False, "data": "", "menor_12": False}
    try:
        data_obj = datetime.strptime(data_str, "%d/%m/%Y")
        hoje = datetime.now()
        if data_obj > hoje or data_obj.year < (hoje.year - 120):
            return {"valida": False, "data": "", "menor_12": False}

        # Funcionalidade 1: Menor de 12 anos (Encaminhar para Humano)
        idade = hoje.year - data_obj.year - ((hoje.month, hoje.day) < (data_obj.month, data_obj.day))
        if idade < 12:
            return {"valida": True, "data": data_str, "menor_12": True}

        return {"valida": True, "data": data_str, "menor_12": False}
    except ValueError:
        return {"valida": False, "data": "", "menor_12": False}

# ==========================================
# FUNÇÕES DE MEMÓRIA E HISTÓRICO (FIREBASE)
# ==========================================
def get_paciente(phone):
    if not db: return {}
    doc = db.collection("PatientsKanban").document(phone).get()
    return doc.to_dict() if doc.exists else {}

# ==========================================
# FAQ COM IA — Modelo fine-tuned v8 (OpenAI)
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
    """Usa o modelo fine-tuned v8 para responder dúvidas da clínica.
    System prompt IDÊNTICO ao usado no treinamento v8 — obrigatório para evitar alucinações."""
    import sys
    if not OPENAI_API_KEY or len(mensagem.strip()) < 5:
        if not OPENAI_API_KEY:
            print("[FAQ-IA] OPENAI_API_KEY ausente", file=sys.stderr)
        return None

    # System prompt v8 — IDÊNTICO ao treinamento (não alterar!)
    system_prompt_v8 = (
        "Você é o assistente virtual da ConectiFisio, uma clínica de fisioterapia e pilates "
        "com unidades em São Caetano do Sul e Ipiranga (São Paulo). "
        "Seu tom é profissional, acolhedor e eficiente.\n\n"
        "REGRAS OBRIGATÓRIAS:\n"
        "1. NUNCA invente preços, valores ou tabelas. Para qualquer pergunta sobre preço, responda que vai encaminhar para a recepção.\n"
        "2. NUNCA confirme cobertura de convênios que NÃO são atendidos. Convênios ACEITOS: Amil, Bradesco Saúde, Porto Seguro Saúde, Prevent Senior, Saúde Caixa, Saúde Petrobras, Mediservice, Cassi e Geap Saúde. Qualquer outro convênio NÃO é atendido.\n"
        "3. Para Pilates: Saúde Caixa, Wellhub (plano Golden) e TotalPass (plano TP5). Nunca fale preço de Pilates no primeiro contato.\n"
        "4. Não atendemos menores de 12 anos (sem especialidade pediátrica).\n"
        "5. Não realizamos consultas médicas (somos clínica de fisioterapia e Pilates).\n"
        "6. Se a mensagem não for uma dúvida sobre a clínica (saudação, agradecimento, assunto fora do escopo), responda SOMENTE com a palavra NENHUMA.\n"
        "7. Responda de forma natural e direta. Use emojis com moderação."
    )

    url = "https://api.openai.com/v1/chat/completions"
    headers_oai = {
        "Authorization": "Bearer " + OPENAI_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "model": OPENAI_FAQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt_v8},
            {"role": "user", "content": mensagem[:3000]}
        ],
        "max_tokens": 800,
        "temperature": 0.0
    }

    try:
        res = requests.post(url, json=payload, headers=headers_oai, timeout=15)
        if res.status_code == 200:
            resposta_ia = res.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            print("[FAQ-IA] OpenAI v8: " + resposta_ia[:80], file=sys.stderr)
            # Rejeita respostas truncadas (menos de 15 chars ou sem pontuação final)
            if resposta_ia and resposta_ia.upper() != "NENHUMA" and len(resposta_ia) > 15:
                return resposta_ia
        else:
            print("[FAQ-IA] OpenAI HTTP " + str(res.status_code) + ": " + res.text[:200], file=sys.stderr)
    except Exception as e:
        print("[FAQ-IA] Erro: " + str(e), file=sys.stderr)

    return None

def consultar_faq(mensagem):
    """FAQ com modelo fine-tuned v8.
    Apenas o modelo treinado com system prompt unificado.
    Filtros de segurança bloqueiam alucinações residuais.

    Retorna tupla (resposta, motivo_filtro):
    - (texto, None): resposta normal do FAQ
    - (None, "massagem"): redirecionar para Liberação Miofascial
    - (None, "servico_nao_atendido:<nome>"): negar e oferecer alternativas
    - (None, None): sem resposta válida
    """
    import sys
    msg_limpa = mensagem.lower().strip()

    # PRÉ-FILTRO 1: Detecta pedido de massagem → redireciona para Liberação Miofascial
    PALAVRAS_MASSAGEM = ["massagem", "massoterapia", "massotera", "massagista"]
    if any(p in msg_limpa for p in PALAVRAS_MASSAGEM):
        # Só redireciona se NÃO mencionar liberação miofascial (paciente já sabe)
        if "miofascial" not in msg_limpa and "liberação" not in msg_limpa and "liberacao" not in msg_limpa:
            print(f"[FAQ-PRE] Detectado pedido de massagem → Liberação Miofascial", file=sys.stderr)
            return None, "massagem"

    # PRÉ-FILTRO 2: Detecta serviço não atendido na PERGUNTA
    SERVICOS_NAO_ATENDIDOS_KEYWORDS = {
        "ATM / disfunção temporomandibular": ["atm", "articulação temporomandibular", "temporomandibular", "disfunção temporomandibular", "disfuncao temporomandibular", "disfunção tm"],
        "fisioterapia facial / estética facial": ["fisioterapia facial", "fisio facial", "estética facial", "estetica facial"],
        "fisioterapia pediátrica": ["fisioterapia pediátrica", "fisioterapia pediatrica", "fisio pediátrica", "fisio pediatrica", "fisioterapia infantil", "fisio infantil"],
        "fisioterapia neuropediátrica": ["neuropediátrica", "neuropediatrica", "fisioterapia neurológica pediátrica", "fisio neuro pediátrica", "fisio neuro pediatrica"],
        "drenagem linfática": ["drenagem linfática", "drenagem linfatica", "drenagem"],
        "RPG": [" rpg ", " rpg?", " rpg.", " rpg!", "reeducação postural global"],
        "quiropraxia": ["quiropraxia", "quiroprática", "quiropratica"],
        "fisioterapia respiratória": ["fisioterapia respiratória", "fisioterapia respiratoria", "fisio respiratória", "fisio respiratoria"]
    }
    msg_pad = " " + msg_limpa + " "
    for nome_serv, kws in SERVICOS_NAO_ATENDIDOS_KEYWORDS.items():
        for kw in kws:
            if kw in msg_pad:
                print(f"[FAQ-PRE] Detectado serviço não atendido: {nome_serv}", file=sys.stderr)
                return None, f"servico_nao_atendido:{nome_serv}"

    # Modelo fine-tuned v8 — responde pelo treinamento com system prompt unificado
    match_ia = _busca_por_ia(mensagem, [])
    if match_ia:
        import re as _re_faq

        # FILTRO 1: Bloqueia respostas que contenham valores monetários inventados
        if _re_faq.search(r'R\$\s*[\d.,]+', match_ia):
            print(f"[FAQ-FILTRO] BLOQUEADO R$: {match_ia[:80]}", file=sys.stderr)
            return None, None

        # FILTRO 2: Bloqueia se afirmar que atende convênio não aceito
        _CONV_NAO_ATENDIDOS = ["notredame", "notre dame", "unimed", "sulamerica", "sulamérica", "hapvida", "golden cross", "apivida", "amesp", "qualicorp"]
        resp_lower = match_ia.lower()
        for conv in _CONV_NAO_ATENDIDOS:
            if conv in resp_lower:
                # Se mencionou o convênio MAS sem negar → alucinação
                tem_negacao = any(neg in resp_lower for neg in ["não", "nao", "infelizmente", "não somos", "não atendemos", "não trabalhamos"])
                if not tem_negacao:
                    print(f"[FAQ-FILTRO] BLOQUEADO convênio não atendido afirmado: {match_ia[:80]}", file=sys.stderr)
                    return None, None

        # FILTRO 3: Bloqueia se afirmar serviço não atendido sem negar (anti-alucinação ATM, RPG, etc)
        SERVICOS_BLOQUEADOS_NA_RESP = ["atm", "temporomandibular", "fisioterapia facial", "fisio facial",
                                       "fisioterapia pediátrica", "fisioterapia pediatrica", "fisio pediátrica",
                                       "neuropediátrica", "neuropediatrica", "drenagem linfática", "drenagem linfatica",
                                       "quiropraxia", "fisioterapia respiratória", "fisioterapia respiratoria"]
        for serv in SERVICOS_BLOQUEADOS_NA_RESP:
            if serv in resp_lower:
                tem_negacao = any(neg in resp_lower for neg in ["não", "nao", "infelizmente", "não realizamos", "não atendemos", "não fazemos", "não trabalhamos"])
                if not tem_negacao:
                    print(f"[FAQ-FILTRO] BLOQUEADO serviço não atendido afirmado: {match_ia[:80]}", file=sys.stderr)
                    return None, f"servico_nao_atendido:{serv}"

        # FILTRO 4: Anti-alucinação de convênio aceito
        # Se a resposta menciona um convênio específico mas a pergunta não mencionou → suspeita
        CONV_ACEITOS = ["amil", "bradesco saúde", "porto seguro", "prevent senior", "saúde caixa",
                        "saúde petrobras", "mediservice", "cassi", "geap"]
        msg_lower_pergunta = mensagem.lower()
        for conv_aceito in CONV_ACEITOS:
            if conv_aceito in resp_lower and conv_aceito not in msg_lower_pergunta:
                # Resposta afirma um convênio sem o paciente ter perguntado especificamente
                # Só bloqueia se a resposta for afirmativa ("Sim, atendemos X")
                if "sim" in resp_lower[:30] or "atendemos" in resp_lower[:60]:
                    # Caso especial: resposta lista TODOS os convênios (correto). Permite se listar 3+
                    qtd_conv = sum(1 for c in CONV_ACEITOS if c in resp_lower)
                    if qtd_conv < 3:
                        print(f"[FAQ-FILTRO] Suspeita de alucinação convênio: paciente não citou '{conv_aceito}'", file=sys.stderr)
                        return None, None

        print("[FAQ-IA] Respondendo via modelo v8", file=sys.stderr)
    return match_ia, None

# ============================================================
# DETECTOR DE PEDIDO DE RECEPÇÃO (preço, vaga, desconto)
# Não vai para Intervenção — mantém card na coluna + selo "Precisa Recepção"
# ============================================================
PALAVRAS_PEDIDO_RECEPCAO = [
    # Preço / valor / desconto
    "quanto custa", "quanto e", "quanto é", "qual o valor", "qual valor",
    "qual preço", "qual o preço", "qual o preco", "qual preco",
    "tabela de preço", "tabela de preco", "tabela de valores", "valores",
    "consegue desconto", "tem promoção", "tem promocao", "tem desconto",
    "fazem desconto", "faz desconto", "negociar valor", "parcelar", "parcelamento",
    # Vaga / agenda / horário disponível
    "tem vaga", "tem horário", "tem horario", "horário disponível", "horario disponivel",
    "horários disponíveis", "horarios disponiveis", "horários e dias", "horarios e dias",
    "agenda disponível", "agenda disponivel", "data mais próxima", "data mais proxima",
    "tem disponibilidade", "data próxima", "data proxima", "agenda essa semana",
    "vaga essa semana", "vaga semana", "primeira data", "data livre"
]

def detectar_pedido_recepcao(msg):
    """Detecta pedidos sobre preço/vaga/desconto que devem ser tratados pela recepção
    sem mover o card para Intervenção (mantém na coluna comercial)."""
    msg_lower = (msg or "").lower().strip()
    for kw in PALAVRAS_PEDIDO_RECEPCAO:
        if kw in msg_lower:
            return kw
    return None

def marcar_precisa_recepcao(phone, motivo):
    """Marca paciente como precisando da recepção (selo no card, mantém na coluna).
    🛡️ Reseta cooldown da mensagem de espera para que a próxima mensagem do paciente
    seja respondida (mesmo que a anterior tenha sido respondida há pouco tempo)."""
    import sys
    now_iso = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S+00:00')
    update_paciente(phone, {
        "precisa_recepcao": True,
        "motivo_recepcao": motivo,
        "marcado_recepcao_em": now_iso,
        "bot_pausado_recepcao": True,  # pausa o bot até "Resolvido"
        "ultima_resp_espera_em": "",  # reseta cooldown para próxima mensagem ser respondida
        "unread": True
    })
    print(f"[RECEPCAO] {phone} marcado: {motivo}", file=sys.stderr)

# ============================================================
# RETOMAR FLUXO — após FAQ/intervenção, oferece continuar ou voltar ao menu
# ============================================================
PERGUNTAS_RETOMADA = {
    "cadastrando_queixa": "Pode me contar o que te trouxe à clínica?",
    "cadastrando_queixa_veterano": "Pode me contar o que está sentindo?",
    "modalidade": "Deseja atendimento pelo CONVÊNIO ou de forma PARTICULAR?",
    "nome_convenio": "Qual o seu convênio?",
    "num_carteirinha": "Pode me informar o número da sua carteirinha?",
    "foto_carteirinha": "Pode me enviar a foto da carteirinha do convênio?",
    "foto_pedido_medico": "Pode me enviar o pedido médico?",
    "agendando": "Qual o melhor período — Manhã, Tarde ou Noite?",
    "confirmando_convenio_salvo": "Vamos seguir com o convênio anterior?",
    "menu_veterano": "Como posso te ajudar?",
    "escolhendo_unidade": "Qual unidade você prefere — São Caetano ou Ipiranga?",
    "escolhendo_especialidade": "Qual serviço você precisa?",
    "confirmando_servico_nova_guia": "Confirma o serviço escolhido?",
    "aguardando_token_convenio": "Pode me informar o token de autorização?",
    "gestao_agenda": "O que deseja fazer com sua agenda?",
}

def retomar_fluxo(phone, info, status_atual):
    """Após uma resposta do FAQ, oferece 2 botões:
    - Continuar de onde parou (repete pergunta do estado)
    - Voltar ao menu inicial (veterano → menu_veterano / novo → escolhendo_unidade)"""
    import sys
    pergunta = PERGUNTAS_RETOMADA.get(status_atual)
    if not pergunta:
        print(f"[RETOMAR] Status sem retomada definida: {status_atual}", file=sys.stderr)
        return False

    # Marca no banco que estamos aguardando escolha de retomada
    update_paciente(phone, {"status_anterior_retomada": status_atual})

    botoes = [
        {"id": "retomar_continuar", "title": "↩️ Continuar"},
        {"id": "retomar_menu", "title": "🏠 Voltar ao Menu"}
    ]
    enviar_botoes(phone, f"Voltando ao seu atendimento:\n\n{pergunta}", botoes)
    return True

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
    """🛡️ Converte data BR para formato Feegow (YYYY-MM-DD).
    Aceita tanto 8 dígitos (DD/MM/YYYY) quanto 6 dígitos (DD/MM/YY) como fallback.
    Defesa em profundidade — a validação principal está em validar_data_nascimento.
    Ver mapa_fluxos.md → bug Cleusa."""
    data_limpa = re.sub(r'\D', '', str(data_br))
    if len(data_limpa) == 8:
        # DD/MM/YYYY → YYYY-MM-DD
        return f"{data_limpa[4:]}-{data_limpa[2:4]}-{data_limpa[:2]}"
    if len(data_limpa) == 6:
        # DD/MM/YY → normaliza ano (78 → 1978, 05 → 2005) → YYYY-MM-DD
        ano_2dig = int(data_limpa[4:6])
        ano_4dig = 1900 + ano_2dig if ano_2dig >= 20 else 2000 + ano_2dig
        return f"{ano_4dig}-{data_limpa[2:4]}-{data_limpa[:2]}"
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
    Gemini removido — se OpenAI falhar, retorna None (fallback estático no código).
    """
    if not OPENAI_API_KEY:
        return None

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
        "temperature": 0.3
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

def enviar_localizacao(to, unidade, numero_id=None):
    """Envia mini mapa nativo do WhatsApp com a localização da unidade.
    🛡️ Ver mapa_fluxos.md → Mensagem de confirmação pós-agendamento."""
    info = UNIDADES.get(unidade, UNIDADES["Ipiranga"])
    registrar_historico(to, "robo", "texto", f"📍 Localização: {info['nome_oficial']}")
    return enviar_whatsapp(to, {
        "type": "location",
        "location": {
            "latitude": str(info["lat"]),
            "longitude": str(info["lon"]),
            "name": info["nome_oficial"],
            "address": info["endereco"]
        }
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

        if request.args.get("action") == "resolver_recepcao":
            # Limpa o selo "precisa_recepcao" e reativa o bot (card permanece na coluna)
            try:
                phone = request.args.get("phone")
                resolvido_por = request.args.get("resolvido_por", "Recepção")
                if not phone:
                    return jsonify({"success": False}), 400
                db.collection("PatientsKanban").document(phone).set({
                    "precisa_recepcao": False,
                    "bot_pausado_recepcao": False,
                    "motivo_recepcao": "",
                    "resolvido_recepcao_por": resolvido_por,
                    "resolvido_recepcao_em": datetime.utcnow().strftime('%d/%m/%Y %H:%M')
                }, merge=True)
                return jsonify({"success": True}), 200
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
                    "pilates_modalidade", "pilates_part_experiencia", "pilates_part_exp", "pilates_part_periodo",
                    "pilates_part_nome", "pilates_part_cpf", "pilates_part_nasc",
                    "pilates_part_email", "pilates_app_nome_completo", "pilates_app_cpf",
                    "pilates_app_nasc", "pilates_app_email", "pilates_app",
                    "pilates_app_confirma_plano", "pilates_app_cross_sell", "pilates_app_orientar",
                    "pilates_lead_morno",
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
                modalidade_p = request.args.get("modalidade", "")
                servico_p = request.args.get("servico", "")

                if not phone_p:
                    return jsonify({"error": "phone obrigatório"}), 400

                # 🛡️ Fallback: se modalidade ou servico não vierem do dashboard,
                # tenta puxar do Firebase para garantir texto correto.
                if not modalidade_p or not servico_p:
                    info_p = get_paciente(phone_p) or {}
                    modalidade_p = modalidade_p or info_p.get("modalidade", "")
                    servico_p = servico_p or info_p.get("servico", "")

                info_unidade = UNIDADES.get(unidade_p, UNIDADES["Ipiranga"])

                # Bloco de data: só aparece se a recepção preencheu
                bloco_data = f"Esperamos por você no *{data_p}*, na unidade *{unidade_p}*.\n\n" if data_p else f"Esperamos por você na unidade *{unidade_p}*.\n\n"

                # 🛡️ 3 variantes de texto baseadas em modalidade/serviço
                # Regra blindada (ver mapa_fluxos.md → Mensagem de confirmação pós-agendamento)
                if servico_p == "Pilates Studio":
                    # PILATES — roupa específica, meias antiderrapantes, 10 min antes
                    msg_recomendacao = (
                        f"Tudo certo para a sua aula de *Pilates*, {nome_p}! ✅\n"
                        f"{bloco_data}"
                        "👕 *O que trazer:*\n"
                        "• Roupa confortável (legging/short + camiseta)\n"
                        "• *Meias antiderrapantes* (obrigatórias no studio)\n"
                        "• Documento com foto\n\n"
                        "Chegue *10 minutos antes* para conhecer o espaço.\n\n"
                        "Até breve! Qualquer dúvida, nossa equipe está à disposição. 😊"
                    )
                elif modalidade_p == "Convênio":
                    # CONVÊNIO — pedido médico + carteirinha + aviso de token
                    servico_label = servico_p or "fisioterapia"
                    msg_recomendacao = (
                        f"Tudo certo para a sua sessão de *{servico_label}*, {nome_p}! ✅\n"
                        f"{bloco_data}"
                        "👕 *Dica importante:* para facilitar o seu tratamento, use roupas confortáveis "
                        "que permitam acesso à área a ser tratada (como shorts ou regata/top).\n\n"
                        "Chegue *15 minutos antes* e não esqueça do *pedido médico original* "
                        "e um *documento com foto*.\n\n"
                        "📱 Como vamos utilizar o seu plano de saúde, mantenha o celular por perto — "
                        "sua operadora pode pedir um token de validação na hora.\n\n"
                        "Até breve! Qualquer dúvida, nossa equipe está à disposição. 😊"
                    )
                else:
                    # PARTICULAR (Fisio Orto/Neuro/Pélvica/Acup/Recovery/Liberação)
                    # — só documento com foto, sem pedido nem token
                    servico_label = servico_p or "fisioterapia"
                    msg_recomendacao = (
                        f"Tudo certo para a sua sessão de *{servico_label}*, {nome_p}! ✅\n"
                        f"{bloco_data}"
                        "👕 *Dica importante:* use roupas confortáveis que permitam acesso "
                        "à área a ser tratada (como shorts ou regata/top).\n\n"
                        "Chegue *15 minutos antes* e traga apenas um *documento com foto* "
                        "para o seu prontuário.\n\n"
                        "Até breve! Qualquer dúvida, nossa equipe está à disposição. 😊"
                    )

                responder_texto(phone_p, msg_recomendacao)
                import time as _t2
                _t2.sleep(1)
                # 🛡️ Dica específica da unidade + mini mapa nativo
                # (ver mapa_fluxos.md → Mensagem de confirmação pós-agendamento)
                dica = info_unidade.get("dica_chegada", "")
                if dica:
                    responder_texto(phone_p, dica)
                    _t2.sleep(1)
                enviar_localizacao(phone_p, unidade_p)

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
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))
        elif msg_type in ["image", "document"]:
            tem_anexo = True
            media_id = message.get(msg_type, {}).get("id")

        if tem_anexo and media_id:
            registrar_historico(phone, "paciente", "anexo", msg_recebida, media_id=media_id)
        else:
            registrar_historico(phone, "paciente", "texto" if not tem_anexo else "anexo", msg_recebida)

        agora_iso = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S+00:00')
        # Reset contador de FAQ loops quando o paciente clica botão (interagiu com o fluxo)
        reset_faq = {"faq_calls_seguidas": 0} if msg_type == "interactive" else {}
        db.collection("PatientsKanban").document(phone).set(
            {"lastPatientInteraction": agora_iso,
             "numero_id": numero_id,
             **reset_faq,
             **(({"followup_toque": 0, "followup_retomado_em": agora_iso}) if info.get("followup_toque", 0) > 0 else {})},
            merge=True)

        msg_limpa = msg_recebida.lower()

        _cortesias_early = ["obrigad", "obg", "ok", "valeu", "certo", "tá bom", "perfeito", "beleza", "show", "combinado", "agradeço", "ótimo", "otimo", "maravilh", "excelente", "muito bom", "legal", "entendi", "entendido", "claro"]
        _emojis_early = ["👍", "🙏", "❤️", "👏", "😊", "🥰", "💙", "💚", "🤝", "✅"]
        is_cortesia = len(msg_limpa) <= 35 and (
            any(msg_limpa.startswith(w) for w in _cortesias_early) or
            any(char in msg_limpa for char in _emojis_early)
        )

        # Definição antecipada — usado pelo FAQ e pelo fluxo principal
        is_veteran = True if info.get("feegow_id") else False

        palavras_insta = ["interesse", "informações", "informacoes", "pilates"]
        eh_lead_instagram = (
            msg_type == "text" and
            sum(1 for p in palavras_insta if p in msg_limpa) >= 2 and
            not info.get("origem")
        )
        if eh_lead_instagram:
            import sys as _sys_insta
            print(f"[INSTAGRAM] Lead detectado de {phone}", file=_sys_insta.stderr)
            update_paciente(phone, {
                "status": "instagram_pilates_q1",
                "servico": "Pilates Studio",
                "unit": "São Caetano",
                "origem": "instagram_pilates",
                "followup_toque": 0
            })
            linha1 = "Olá! 😊 Sou o assistente virtual da Conectifisio e vou passar sua solicitação para o nosso fisioterapeuta."
            linha2 = "Vi que você tem interesse no nosso Pilates Estúdio em São Caetano do Sul."
            linha3 = "Você já praticou Pilates antes ou seria sua primeira experiência?"
            msg_boas_vindas = linha1 + " " + linha2 + chr(10) + chr(10) + linha3
            responder_texto(phone, msg_boas_vindas)
            return jsonify({"status": "instagram_lead_capturado"}), 200

        palavras_socorro = ["ajuda", "humano", "atendente", "recepção", "recepcao", "falar com alguém", "pessoa"]
        if any(palavra in msg_limpa for palavra in palavras_socorro):
            update_paciente(phone, {"status": "pausado", "ultima_mensagem_paciente": f"[PEDIDO DE AJUDA] {msg_recebida}"})
            responder_texto(phone, "Entendido! Pausei o meu sistema automático e já avisei a nossa equipa. 🚨 Em instantes um atendente humano vai assumir esta conversa para te ajudar!")
            return jsonify({"status": "pedido_ajuda"}), 200

        # NOTA: O detector global de token foi REMOVIDO daqui.
        # Token agora é processado APENAS no estado 'aguardando_token_convenio'
        # (acionado pelo botão "🔑 Enviar Token" do menu veterano).
        # Motivo: evitar capturar carteirinha, CPF e outros números como token.

        _periodos = ["manhã", "manha", "tarde", "noite"]
        # Detecta período mesmo dentro de frases ("Manhã e Tarde", "de manhã pode", etc)
        periodo_detectado = None
        if info.get("faq_encaminhou"):
            for p in _periodos:
                if p in msg_limpa:
                    periodo_detectado = p
                    break
        if periodo_detectado:
            import sys
            print(f"[FAQ→BOT] Período '{periodo_detectado}' detectado em '{msg_recebida[:40]}' — iniciando fluxo", file=sys.stderr)
            if info.get("feegow_id"):
                nome_s2 = info.get("title", "Paciente").split()[0]
                update_paciente(phone, {"status": "menu_veterano", "faq_encaminhou": False, "periodo_preferido": msg_recebida, "servico": "", "modalidade": ""})
                secoes_fp = [{"title": "Como posso ajudar?", "rows": [{"id": "v1", "title": "🗓️ Meus Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia/Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v5", "title": "🔑 Enviar Token"}, {"id": "v4", "title": "📁 Secretaria"}]}]
                enviar_lista(phone, f"Olá, {nome_s2}! 😊 Como posso te ajudar?", "Ver Opções", secoes_fp)
            else:
                # NOVO: paciente novo vai pra pedir o NOME (não unidade)
                update_paciente(phone, {"status": "cadastrando_nome", "faq_encaminhou": False, "periodo_preferido": msg_recebida})
                responder_texto(phone,
                    "Ótimo! Antes de prosseguirmos, preciso saber como você gostaria de ser chamado(a). 😊"
                )
            return jsonify({"status": "faq_periodo_capturado"}), 200

        # ============================================================
        # HANDLER: BOTÕES DE RETOMADA DE FLUXO
        # ============================================================
        if msg_recebida in ["↩️ Continuar", "Continuar", "retomar_continuar"]:
            status_anterior = info.get("status_anterior_retomada")
            if status_anterior:
                import sys
                print(f"[RETOMAR] Continuando fluxo: {status_anterior}", file=sys.stderr)
                update_paciente(phone, {"status": status_anterior, "status_anterior_retomada": ""})
                pergunta = PERGUNTAS_RETOMADA.get(status_anterior, "Como posso continuar te ajudando?")
                responder_texto(phone, f"Perfeito! {pergunta}")
                return jsonify({"status": "fluxo_continuado"}), 200

        if msg_recebida in ["🏠 Voltar ao Menu", "Voltar ao Menu", "retomar_menu"]:
            import sys
            print(f"[RETOMAR] Voltando ao menu inicial. Veterano={is_veteran}", file=sys.stderr)
            if is_veteran:
                # Veterano sempre vai ao menu_veterano (não pede unidade)
                nome_mv = info.get("title", "Paciente").split()[0]
                update_paciente(phone, {
                    "status": "menu_veterano", "status_anterior_retomada": "",
                    "faq_encaminhou": False, "servico": "", "modalidade": ""
                })
                secoes_mv = [{"title": "Como posso ajudar?", "rows": [
                    {"id": "v1", "title": "🗓️ Meus Agendamentos"},
                    {"id": "v2", "title": "🔄 Nova Guia/Tratamento"},
                    {"id": "v3", "title": "➕ Novo Serviço"},
                    {"id": "v5", "title": "🔑 Enviar Token"},
                    {"id": "v4", "title": "📁 Secretaria"}
                ]}]
                enviar_lista(phone, f"Como posso te ajudar, {nome_mv}? 😊", "Ver Opções", secoes_mv)
            else:
                # Novo lead — recomeça pela escolha de unidade
                update_paciente(phone, {
                    "status": "escolhendo_unidade", "status_anterior_retomada": "",
                    "faq_encaminhou": False
                })
                enviar_botoes(phone, "Vamos recomeçar! 😊 Em qual unidade você deseja ser atendido?",
                    [{"id": "u1", "title": "São Caetano"}, {"id": "u2", "title": "Ipiranga"}])
            return jsonify({"status": "menu_retomado"}), 200

        # ============================================================
        # DETECTOR DE PEDIDO DE RECEPÇÃO (preço, vaga, desconto)
        # NÃO vai para Intervenção — mantém card na coluna comercial
        # ============================================================
        if msg_type == "text" and not is_cortesia:
            kw_recepcao = detectar_pedido_recepcao(msg_recebida)
            if kw_recepcao:
                import sys
                print(f"[RECEPCAO] Detectado '{kw_recepcao}' em: {msg_recebida[:60]}", file=sys.stderr)
                marcar_precisa_recepcao(phone, f"Cliente perguntou: '{msg_recebida[:80]}'")
                responder_texto(phone, "Pra te passar valores e disponibilidade com precisão e atualizados, vou chamar nossa recepção pra te atender pessoalmente. Um momento! 😊")
                return jsonify({"status": "recepcao_acionada"}), 200

        # ============================================================
        # SAUDAÇÃO PURA EM TRIAGEM → vai direto pedir o nome
        # Evita que "Olá" caia no FAQ e sequestre o fluxo do novo paciente
        # ============================================================
        _saudacoes_puras = ["oi", "olá", "ola", "bom dia", "boa tarde", "boa noite",
                           "oi!", "olá!", "ola!", "bom dia!", "boa tarde!", "boa noite!",
                           "oii", "oiii", "ei", "alô", "alo", "hey", "hi", "hello"]
        eh_saudacao_pura = msg_limpa.strip().rstrip(".!?,") in _saudacoes_puras
        if eh_saudacao_pura and status_atual == "triagem" and not is_veteran:
            import sys
            print(f"[SAUDACAO→NOME] Novo paciente saudação → pede nome direto", file=sys.stderr)
            update_paciente(phone, {"status": "cadastrando_nome"})
            responder_texto(phone,
                "Olá! ✨ Seja muito bem-vindo(a) à Conectifisio.\n\n"
                "Para iniciarmos seu atendimento, como você gostaria de ser chamado(a)? 😊"
            )
            return jsonify({"status": "saudacao_pediu_nome"}), 200

        STATUSES_FAQ_PERMITIDOS = ["triagem", "finalizado", "arquivado", "menu_veterano"]
        # FAQ também responde durante fluxos ativos, mas chama retomar_fluxo depois.
        # NOTA CRÍTICA: estados que esperam DADOS ESPECÍFICOS (queixa, número, foto, período)
        # NÃO entram aqui — o modelo alucina interpretando o dado como dúvida.
        # Removidos: cadastrando_queixa, num_carteirinha, foto_carteirinha, foto_pedido_medico, agendando
        STATUSES_FAQ_COM_RETOMADA = [
            "modalidade", "nome_convenio", "confirmando_convenio_salvo",
            "escolhendo_unidade", "escolhendo_especialidade", "confirmando_servico_nova_guia"
        ]

        # PROTEÇÃO: FAQ NÃO processa mensagens só com números/símbolos
        # (CPF, carteirinha, telefone, token, datas — o modelo alucina interpretando como dúvida)
        msg_so_numeros_simbolos = bool(msg_limpa.strip()) and all(
            not c.isalpha() for c in msg_limpa.strip()
        )

        if msg_type == "text" and len(msg_limpa) > 3 and not is_cortesia and not msg_so_numeros_simbolos and (status_atual in STATUSES_FAQ_PERMITIDOS or status_atual in STATUSES_FAQ_COM_RETOMADA):
            resposta_faq, motivo_filtro = consultar_faq(msg_recebida)

            # CASO ESPECIAL 1: paciente pediu massagem → redireciona para Liberação Miofascial
            if motivo_filtro == "massagem":
                import sys
                print(f"[FAQ-MASSAGEM] Redirecionando para Liberação Miofascial", file=sys.stderr)
                msg_massagem = (
                    "Não realizamos massagem terapêutica tradicional, mas oferecemos a *Liberação Miofascial*! 💆\n\n"
                    "É uma técnica manual eficaz para tensão muscular, dores e nós musculares, "
                    "realizada por fisioterapeutas especializados.\n\n"
                    "É um serviço *particular*. Gostaria de saber mais?"
                )
                responder_texto(phone, msg_massagem)
                # Se estava em fluxo, retoma
                if status_atual in STATUSES_FAQ_COM_RETOMADA:
                    retomar_fluxo(phone, info, status_atual)
                return jsonify({"status": "faq_massagem_redirecionada"}), 200

            # CASO ESPECIAL 2: serviço não atendido (ATM, RPG, drenagem, etc) → nega + marca recepção
            if motivo_filtro and motivo_filtro.startswith("servico_nao_atendido:"):
                import sys
                nome_serv = motivo_filtro.replace("servico_nao_atendido:", "")
                print(f"[FAQ-NAO-ATENDIDO] Serviço não oferecido: {nome_serv}", file=sys.stderr)
                msg_neg = (
                    f"Infelizmente ainda não atendemos *{nome_serv}*. 💙\n\n"
                    "Outros serviços que oferecemos:\n"
                    "• Fisioterapia Ortopédica\n"
                    "• Fisioterapia Neurológica\n"
                    "• Fisioterapia Pélvica\n"
                    "• Acupuntura\n"
                    "• Pilates Studio\n"
                    "• Recovery\n"
                    "• Liberação Miofascial\n\n"
                    "Nossa recepção vai te orientar sobre as melhores opções para o seu caso. 😊"
                )
                responder_texto(phone, msg_neg)
                marcar_precisa_recepcao(phone, f"Solicitou serviço não atendido: {nome_serv}. Msg: {msg_recebida[:80]}")
                return jsonify({"status": "faq_servico_nao_atendido"}), 200

            if resposta_faq and resposta_faq.upper() != "NENHUMA":
                import sys
                print(f"[FAQ] Respondendo: '{resposta_faq[:60]}'", file=sys.stderr)
                responder_texto(phone, resposta_faq)

                # Conta chamadas FAQ consecutivas (anti-loop)
                faq_calls = info.get("faq_calls_seguidas", 0) + 1
                update_paciente(phone, {"faq_calls_seguidas": faq_calls})

                # Se chamou FAQ 3 vezes consecutivas sem progresso → marca recepção
                if faq_calls >= 3:
                    print(f"[FAQ-LOOP] {faq_calls} chamadas seguidas → marcando recepção", file=sys.stderr)
                    marcar_precisa_recepcao(phone, f"FAQ chamado {faq_calls}x sem progresso. Última msg: {msg_recebida[:80]}")
                    return jsonify({"status": "faq_loop_detectado"}), 200

                # Se o FAQ responder mas estamos no MEIO de um fluxo → retoma com 2 botões
                if status_atual in STATUSES_FAQ_COM_RETOMADA:
                    retomado = retomar_fluxo(phone, info, status_atual)
                    if retomado:
                        return jsonify({"status": "faq_respondido_com_retomada"}), 200

                if "vou encaminhar" in resposta_faq.lower():
                    # CIRURGIA: veterano não é redirecionado — mantém contexto
                    if is_veteran and status_atual in ["menu_veterano", "agendando", "cadastrando_queixa_veterano"]:
                        update_paciente(phone, {"ultima_mensagem_paciente": msg_recebida})
                        print(f"[FAQ→VETERANO] Respondeu FAQ sem resetar estado: {status_atual}", file=sys.stderr)
                    elif "manhã, tarde ou noite" in resposta_faq.lower():
                        update_paciente(phone, {
                            "status": "triagem",
                            "ultima_mensagem_paciente": msg_recebida,
                            "faq_encaminhou": True
                        })
                        print(f"[FAQ→BOT] Iniciando fluxo de cadastro para {phone}", file=sys.stderr)
                    else:
                        update_paciente(phone, {
                            "status": "pausado",
                            "ultima_mensagem_paciente": msg_recebida,
                            "unread": True,
                            "faq_encaminhou": True
                        })
                        print(f"[FAQ→HUMANO] Encaminhou para recepção: {msg_recebida[:50]}", file=sys.stderr)
                return jsonify({"status": "faq_respondido"}), 200
            elif (not resposta_faq or (resposta_faq and resposta_faq.upper() == "NENHUMA")) and status_atual in STATUSES_FAQ_COM_RETOMADA:
                # FAQ retornou NENHUMA durante fluxo → mensagem provavelmente fora de contexto
                # Retoma o fluxo silenciosamente
                import sys
                print(f"[FAQ-NENHUMA] Retomando fluxo: {status_atual}", file=sys.stderr)
                retomado = retomar_fluxo(phone, info, status_atual)
                if retomado:
                    return jsonify({"status": "fluxo_retomado"}), 200

        # 🛡️ REMOVIDO 30/maio/2026 — interceptor global de "Particular"/"Convênio"
        # bypassava o handler `modalidade` (linha ~4022) e pulava a pergunta de unidade
        # no fluxo Particular. Bug detectado no Teste 2 (Maria). Ver mapa_fluxos.md.
        # O handler `modalidade` cuida de tudo: Convênio → lista convênios | Particular → pergunta unidade.

        if msg_recebida.lower() in ["recomeçar", "reset", "menu inicial", "⬅️ voltar ao menu"]:
            if info.get("feegow_id"):
                # Veterano — vai direto ao menu sem pedir unidade
                nome_salvo = info.get("title", "Paciente").split()[0]
                unidade_salva = info.get("unit", "")
                update_paciente(phone, {"status": "menu_veterano", "servico": "", "modalidade": ""})
                secoes_r = [{"title": "Como posso ajudar?", "rows": [{"id": "v1", "title": "🗓️ Meus Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia/Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v5", "title": "🔑 Enviar Token"}, {"id": "v4", "title": "📁 Secretaria"}]}]
                unid_txt = f" (unidade {unidade_salva})" if unidade_salva else ""
                enviar_lista(phone, f"Atendimento reiniciado. 🔄\n\nOlá, {nome_salvo}! Como posso te ajudar{unid_txt}?", "Ver Opções", secoes_r)
            else:
                update_paciente(phone, {"status": "escolhendo_unidade", "cellphone": phone, "servico": "", "modalidade": ""})
                enviar_botoes(phone, "Atendimento reiniciado. 🔄\n\nEm qual unidade deseja ser atendido?", [{"id": "u1", "title": "São Caetano"}, {"id": "u2", "title": "Ipiranga"}])
            return jsonify({"status": "reset"}), 200

        # Busca no Feegow APENAS na primeira interação do paciente.
        # Depois disso, is_veteran é definido pelo feegow_id já gravado e nunca muda durante o fluxo.
        # Isso evita que um paciente novo vire "veterano" no meio do cadastro por causa
        # de um match acidental do Feegow (cadastro antigo, telefone reutilizado, etc).
        if not info.get("feegow_id") and not info.get("feegow_ja_buscado"):
            busca_tel = buscar_feegow_por_telefone(phone)
            if busca_tel:
                info.update({"feegow_id": busca_tel["id"], "title": busca_tel["nome"], "cpf": busca_tel["cpf"]})
                update_paciente(phone, {"feegow_id": busca_tel["id"], "title": busca_tel["nome"], "cpf": busca_tel["cpf"], "feegow_ja_buscado": True})
            else:
                update_paciente(phone, {"feegow_ja_buscado": True})
                doc_hist = db.collection("historico_contatos").document(phone).get()
                if doc_hist.exists:
                    info.update({"is_historico": True})
                    update_paciente(phone, {"is_historico": True})
                
        if not info:
            info = {"cellphone": phone, "status": "triagem"}
            update_paciente(phone, info)

        status = info.get("status", "triagem")

        # ============================================================
        # BOT PAUSADO POR PEDIDO À RECEPÇÃO (preço/vaga/desconto/Pilates)
        # Mantém card na coluna comercial, apenas silencia o bot.
        # 🛡️ Lógica:
        #   1. Se mensagem é cortesia (obrigado/ok/valeu) → "Por nada!..." (sem cooldown)
        #   2. Caso contrário → "Só mais um momento..." com cooldown de 15min
        # Recepção limpa a flag via botão "✅ Resolvido" no dashboard.
        # Ver mapa_fluxos.md → Bot pausado com mensagem de espera.
        # ============================================================
        if info.get("bot_pausado_recepcao"):
            update_paciente(phone, {"ultima_mensagem_paciente": msg_recebida, "unread": True})

            # CASO 1 — Cortesia (agradecimento): responde gentilmente sem cooldown
            if is_cortesia:
                responder_texto(phone, "Por nada! 😊 Nossa equipe já recebeu seus dados e confirmará tudo em instantes. Qualquer dúvida, é só chamar!")
                return jsonify({"status": "bot_aguardando_recepcao_cortesia"}), 200

            # CASO 2 — Outras mensagens: cooldown de 15min para mensagem de espera
            ultima_resp_str = info.get("ultima_resp_espera_em", "")
            deve_responder = True
            if ultima_resp_str:
                try:
                    ultima_dt = datetime.strptime(ultima_resp_str, '%Y-%m-%dT%H:%M:%S+00:00')
                    if (datetime.utcnow() - ultima_dt).total_seconds() < 15 * 60:
                        deve_responder = False
                except: pass

            if deve_responder:
                responder_texto(phone, "Só mais um momento, nossa equipe vai te atender 😊")
                update_paciente(phone, {"ultima_resp_espera_em": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S+00:00')})

            return jsonify({"status": "bot_aguardando_recepcao"}), 200

        if status == "pausado":
            _periodos_p = ["manhã", "manha", "tarde", "noite"]
            periodo_p = None
            if info.get("faq_encaminhou"):
                for p in _periodos_p:
                    if p in msg_limpa:
                        periodo_p = p
                        break
            if periodo_p:
                import sys
                print(f"[FAQ→BOT] Paciente respondeu período '{periodo_p}' (em '{msg_recebida[:40]}') — iniciando fluxo", file=sys.stderr)
                if info.get("feegow_id"):
                    nome_s3 = info.get("title", "Paciente").split()[0]
                    update_paciente(phone, {"status": "menu_veterano", "faq_encaminhou": False, "periodo_preferido": msg_recebida, "servico": "", "modalidade": ""})
                    secoes_fp2 = [{"title": "Como posso ajudar?", "rows": [{"id": "v1", "title": "🗓️ Meus Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia/Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v5", "title": "🔑 Enviar Token"}, {"id": "v4", "title": "📁 Secretaria"}]}]
                    enviar_lista(phone, f"Olá, {nome_s3}! 😊 Como posso te ajudar?", "Ver Opções", secoes_fp2)
                else:
                    # Novo: vai pra nome (não unidade)
                    update_paciente(phone, {"status": "cadastrando_nome", "faq_encaminhou": False, "periodo_preferido": msg_recebida})
                    responder_texto(phone, "Ótimo! Antes de prosseguirmos, preciso saber como você gostaria de ser chamado(a). 😊")
                return jsonify({"status": "faq_periodo_capturado"}), 200
            update_paciente(phone, {"ultima_mensagem_paciente": msg_recebida, "unread": True})
            return jsonify({"status": "bot_silenciado"}), 200
            
        if status == "arquivado":
            if is_cortesia:
                return jsonify({"status": "cortesia_arquivado_ignorada"}), 200
            # CIRURGIA 1 (fix): veterano vai direto ao menu, sem pedir unidade
            if info.get("feegow_id"):
                nome_salvo = info.get("title", "Paciente").split()[0]
                unidade_salva = info.get("unit", "")
                update_paciente(phone, {"status": "menu_veterano", "servico": "", "modalidade": ""})
                secoes = [{"title": "Como posso ajudar?", "rows": [{"id": "v1", "title": "🗓️ Meus Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia/Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v5", "title": "🔑 Enviar Token"}, {"id": "v4", "title": "📁 Secretaria"}]}]
                unid_txt = f" (unidade {unidade_salva})" if unidade_salva else ""
                enviar_lista(phone, f"Olá, {nome_salvo}! ✨ Que bom ter você de volta{unid_txt}. Como posso te ajudar hoje?", "Ver Opções", secoes)
                return jsonify({"status": "veterano_reativacao_direto"}), 200
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

        if is_cortesia and status in ["finalizado", "atendimento_humano", "pendente_feegow", "agendando"]:
            responder_texto(phone, "Por nada! 😊 Nossa equipe já recebeu seus dados e confirmará tudo em instantes. Qualquer dúvida, é só chamar!")
            return jsonify({"status": "courtesy_ignored"}), 200

        if status in ["finalizado", "atendimento_humano", "pendente_feegow"]:
            last_upd = info.get("lastUpdate")
            if last_upd and status == "finalizado":
                try:
                    dt_last = datetime.fromisoformat(last_upd.replace("Z", "+00:00"))
                    if (datetime.now(timezone.utc) - dt_last).total_seconds() < 1800:
                        if len(msg_limpa.split()) <= 2:
                            return jsonify({"status": "silence_window_ignored"}), 200
                except: pass

            enviar_botoes(phone, "Olá! Nossa equipe precisa de mais um tempinho para a resolução da sua solicitação, mas já avisei que você entrou em contato novamente! 😊\n\nSe quiser reiniciar o atendimento, clique abaixo:", [{"id": "menu_ini", "title": "Menu Inicial"}])
            return jsonify({"status": "aguardando_equipe"}), 200
            
        _saudacoes = ["oi", "olá", "ola", "bom dia", "boa tarde", "boa noite"]
        _eh_so_saudacao = any(msg_limpa.strip() == w or msg_limpa.strip() == w + "!" or msg_limpa.strip() == w + "." for w in _saudacoes)
        if _eh_so_saudacao and status not in ["triagem", "escolhendo_unidade", "finalizado", "atendimento_humano", "pendente_feegow"]:
             enviar_botoes(phone, "Olá! ✨ Notei que estávamos no meio do seu atendimento. Deseja continuar de onde paramos?", [{"id": "c_sim", "title": "Sim, continuar"}, {"id": "menu_ini", "title": "Recomeçar"}])
             return jsonify({"status": "retomada"}), 200
             
        if msg_recebida == "Sim, continuar":
             # Reapresenta o menu adequado para o status atual
             if status in ["menu_veterano", "gestao_agenda", "reagendando_preferencia", "escolhendo_horario_reagendamento", "cancelando_sessao"]:
                 nome_s = info.get("title", "Paciente").split()[0]
                 secoes_vet = [{"title": "Como posso ajudar?", "rows": [{"id": "v1", "title": "🗓️ Meus Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia/Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v5", "title": "🔑 Enviar Token"}, {"id": "v4", "title": "📁 Secretaria"}]}]
                 update_paciente(phone, {"status": "menu_veterano"})
                 enviar_lista(phone, f"Olá, {nome_s}! 😊 Retomando — como posso te ajudar?", "Ver Opções", secoes_vet)
             else:
                 responder_texto(phone, "Perfeito! Retomando...")
             return jsonify({"status": "retomada_confirmada"}), 200

        # ==========================================
        # LÓGICA DE ESTADOS
        # ==========================================
        if status == "instagram_pilates_q1":
            update_paciente(phone, {
                "status": "instagram_pilates_q2",
                "instagram_resp_q1": msg_recebida,
                "followup_toque": 0
            })
            responder_texto(phone, "Que ótimo! E qual o seu principal objetivo com o Pilates?")
            return jsonify({"status": "instagram_q1_respondida"}), 200

        elif status == "instagram_pilates_q2":
            nome_lead = info.get("title", "").split()[0] if info.get("title") else ""
            saudacao = f"Perfeito{', ' + nome_lead if nome_lead else ''}! 💙 " if nome_lead else "Perfeito! 💙 "
            update_paciente(phone, {
                "status": "atendimento_humano",
                "instagram_resp_q2": msg_recebida,
                "followup_toque": 0
            })
            responder_texto(phone, (
                f"{saudacao}Registrei suas informações. "
                f"Nossa equipe especializada vai entrar em contato em breve "
                f"para te apresentar as melhores opções de Pilates Estúdio. 😊"
            ))
            return jsonify({"status": "instagram_lead_qualificado"}), 200

        if status == "triagem":
            if info.get("faq_encaminhou"):
                # Veterano volta direto ao menu, sem pedir unidade
                if is_veteran:
                    nome_salvo = info.get("title", "Paciente").split()[0]
                    update_paciente(phone, {"status": "menu_veterano", "faq_encaminhou": False})
                    secoes = [{"title": "Como posso ajudar?", "rows": [{"id": "v1", "title": "🗓️ Meus Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia/Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v5", "title": "🔑 Enviar Token"}, {"id": "v4", "title": "📁 Secretaria"}]}]
                    enviar_lista(phone, f"Olá, {nome_salvo}! 😊 Como posso te ajudar?", "Ver Opções", secoes)
                    return jsonify({"status": "veterano_faq_para_menu"}), 200
                # Novo paciente: pede NOME (não pede unidade ainda)
                update_paciente(phone, {"status": "cadastrando_nome", "faq_encaminhou": False})
                responder_texto(phone,
                    "Olá! ✨ Seja muito bem-vindo(a) à Conectifisio.\n\n"
                    "Para iniciarmos seu atendimento, como você gostaria de ser chamado(a)? 😊"
                )
                return jsonify({"status": "faq_para_fluxo_nome"}), 200

            # Veterano: pula direto pro menu
            if is_veteran:
                import sys as _sys_vet
                nome_salvo = info.get("title", "Paciente").split()[0]
                unidade_salva = info.get("unit", "")
                try:
                    res_ag_vet = consultar_agenda_feegow(info.get("feegow_id"), retornar_raw=True)
                    if res_ag_vet and res_ag_vet.get("agendamentos"):
                        unidade_real_vet = res_ag_vet["agendamentos"][0].get("unidade", "")
                        if unidade_real_vet and unidade_real_vet != unidade_salva:
                            unidade_salva = unidade_real_vet
                            print(f"[VETERAN-UNIT] Unidade atualizada para {unidade_salva}", file=_sys_vet.stderr)
                except Exception as e_vet:
                    print(f"[VETERAN-UNIT] Erro: {e_vet}", file=_sys_vet.stderr)
                update_paciente(phone, {"status": "menu_veterano", "unit": unidade_salva})
                secoes = [{"title": "Como posso ajudar?", "rows": [{"id": "v1", "title": "🗓️ Meus Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia/Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v5", "title": "🔑 Enviar Token"}, {"id": "v4", "title": "📁 Secretaria"}]}]
                unid_txt = f" (unidade {unidade_salva})" if unidade_salva else ""
                enviar_lista(phone, f"Olá, {nome_salvo}! ✨ Que bom ter você de volta{unid_txt}. Como posso te ajudar hoje?", "Ver Opções", secoes)
                return jsonify({"status": "veterano_menu_direto"}), 200

            # NOVO PACIENTE: pede o NOME ANTES de qualquer outra coisa
            update_paciente(phone, {"status": "cadastrando_nome"})
            if info.get("is_historico"):
                responder_texto(phone,
                    "Olá! ✨ Que bom ter você de volta à Conectifisio.\n\n"
                    "Para iniciarmos seu atendimento, como você gostaria de ser chamado(a)? 😊"
                )
            else:
                responder_texto(phone,
                    "Olá! ✨ Seja muito bem-vindo(a) à Conectifisio.\n\n"
                    "Para iniciarmos seu atendimento, como você gostaria de ser chamado(a)? 😊"
                )

        elif status == "escolhendo_unidade":
            # Mantido para retrocompatibilidade quando volta ao Menu pelo retomar_fluxo
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
                    secoes = [{"title": "Como posso ajudar?", "rows": [{"id": "v1", "title": "🗓️ Meus Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia/Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v5", "title": "🔑 Enviar Token"}, {"id": "v4", "title": "📁 Secretaria"}]}]
                    enviar_lista(phone, f"Unidade {msg_recebida} selecionada! ✅\n\nOlá, {nome_salvo}! ✨ Que bom ter você de volta. Como posso te ajudar hoje?", "Ver Opções", secoes)
                else:
                    # Caso novo paciente caia aqui (retomada): vai pra serviço
                    if info.get("title") and not info.get("title", "").startswith("Paciente"):
                        update_paciente(phone, {"status": "escolhendo_especialidade"})
                        secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}, {"id": "e9", "title": "🔍 Não encontrei"}]}]
                        enviar_lista(phone, f"Unidade {msg_recebida} confirmada! ✅\n\nQual serviço você procura hoje?", "Ver Serviços", secoes)
                    else:
                        update_paciente(phone, {"status": "cadastrando_nome"})
                        responder_texto(phone, f"Unidade {msg_recebida} selecionada! ✅\n\nPara garantirmos um atendimento personalizado, como você gostaria de ser chamado(a)?")

        elif status == "cadastrando_nome":
            # VALIDAÇÃO RIGOROSA: rejeita qualquer coisa que não seja nome
            # (anexo, pergunta, URL, número, saudação, frase com verbos de pergunta)

            # 1. Rejeita anexos
            if tem_anexo:
                responder_texto(phone,
                    "Antes de seguir, preciso saber como você gostaria de ser chamado(a). 😊\n\n"
                    "Pode me dizer seu nome?"
                )
                return jsonify({"status": "nome_anexo_rejeitado"}), 200

            msg_stripped = msg_recebida.strip()
            msg_lower_strip = msg_stripped.lower()

            # 2. Rejeita perguntas (tem "?", "!")
            tem_pontuacao_pergunta = "?" in msg_stripped or msg_stripped.endswith("!")

            # 3. Rejeita URLs
            tem_url = "http" in msg_lower_strip or "www." in msg_lower_strip

            # 4. Rejeita só números
            so_numeros = msg_stripped.replace(" ", "").replace("-", "").replace(".", "").isdigit()

            # 5. Rejeita saudação isolada
            saudacoes_puras = ["oi", "olá", "ola", "bom dia", "boa tarde", "boa noite",
                               "oi!", "olá!", "ola!", "bom dia!", "boa tarde!", "boa noite!",
                               "oii", "oiii", "ei", "alô", "alo"]
            eh_saudacao = msg_lower_strip in saudacoes_puras

            # 6. Rejeita início com verbos/pronomes de pergunta (formas variadas de "você")
            verbos_pergunta = ["vocês", "voces", "voce", "vc", "vcs", "vces",
                              "qual", "quanto", "como", "onde",
                              "quando", "atendem", "atende", "atender", "fazem", "faz", "tem ",
                              "preciso", "queria", "gostaria de saber", "gostaria saber",
                              "podem", "pode me", "pode", "vou", "estou com dor", "estou querendo",
                              "aceita", "aceitam", "realizam", "cobre", "cobrem"]
            comeca_com_pergunta = any(msg_lower_strip.startswith(v) for v in verbos_pergunta)

            # 6b. Rejeita palavras de pergunta/agendamento em QUALQUER posição (frases com 3+ palavras)
            palavras_msg = msg_lower_strip.split()
            palavras_pergunta_meio = [
                "atende", "atendem", "atendimento", "atender",
                "fazem", "faz", "realizam", "realiza",
                "aceita", "aceitam", "cobrem", "cobre", "cobertura",
                "convênio", "convenio", "plano", "seguro saúde",
                "marcar", "agendar", "agendamento", "agenda",
                "preço", "preco", "valor", "valores", "custa", "custo",
                "disponível", "disponivel", "horário", "horario", "horarios",
                "vaga", "vagas", "atendimento",
                "particular", "consulta", "sessão", "sessao"
            ]
            tem_palavra_pergunta_no_meio = (
                len(palavras_msg) >= 3 and
                any(p in msg_lower_strip for p in palavras_pergunta_meio)
            )

            # 6c. Rejeita se mencionar nome de convênio (claramente não é nome de pessoa)
            convenios_mencionados = [
                "amil", "bradesco", "porto seguro", "porto", "prevent",
                "saúde caixa", "saude caixa", "saúde petrobras", "saude petrobras",
                "mediservice", "cassi", "geap", "unimed", "sulamerica", "sulamérica",
                "hapvida", "notredame", "notre dame", "wellhub", "totalpass", "gympass"
            ]
            tem_convenio = any(c in msg_lower_strip for c in convenios_mencionados)

            # 7. Rejeita mensagem muito longa (> 80 chars sugere frase, não nome)
            muito_longo = len(msg_stripped) > 80

            # 8. Rejeita texto sem letras
            tem_letra = any(c.isalpha() for c in msg_stripped)

            # 9. Tamanho mínimo
            muito_curto = len(msg_stripped) < 2

            nome_invalido = (tem_pontuacao_pergunta or tem_url or so_numeros or eh_saudacao
                            or comeca_com_pergunta or tem_palavra_pergunta_no_meio
                            or tem_convenio or muito_longo or not tem_letra or muito_curto)

            if nome_invalido:
                import sys
                motivo = []
                if tem_pontuacao_pergunta: motivo.append("pontuacao_pergunta")
                if comeca_com_pergunta: motivo.append("inicio_pergunta")
                if tem_palavra_pergunta_no_meio: motivo.append("palavra_pergunta_meio")
                if tem_convenio: motivo.append("convenio_mencionado")
                if eh_saudacao: motivo.append("saudacao")
                if muito_longo: motivo.append("muito_longo")
                if so_numeros: motivo.append("so_numeros")
                if tem_url: motivo.append("url")
                print(f"[NOME-BLOQUEIO] '{msg_recebida[:60]}' motivos={motivo}", file=sys.stderr)
                responder_texto(phone,
                    "Para prosseguirmos com seu atendimento, preciso primeiro saber como você gostaria de ser chamado(a). 😊\n\n"
                    "Pode me dizer seu nome?"
                )
                return jsonify({"status": "nome_invalido"}), 200

            # Detecta frase de terceiro ("estou agendando para minha mãe", etc)
            frases_terceiro = [
                "estou agendando para", "estou marcando para", "estou ligando para",
                "sou a mãe de", "sou o pai de", "sou a esposa de", "sou o marido de",
                "sou a filha de", "sou o filho de", "agendando para meu", "agendando para minha",
                "marcando para meu", "marcando para minha", "para o meu marido", "para a minha esposa",
                "para o meu pai", "para a minha mãe", "para meu filho", "para minha filha",
                "para meu irmão", "para minha irmã", "mas estou vendo atendimento para"
            ]
            eh_terceiro = any(frase in msg_lower_strip for frase in frases_terceiro)

            if eh_terceiro:
                update_paciente(phone, {"title": msg_recebida, "agendado_por_terceiro": True, "status": "confirmando_paciente_real"})
                responder_texto(phone, f"Entendido! 😊 Fico feliz em ajudar.\n\nPara garantirmos que o cadastro fique correto no sistema, por favor me informe o *NOME COMPLETO do paciente* que será atendido (conforme documento):")
            else:
                primeiro_nome = msg_stripped.split()[0].capitalize()
                # Salva o nome e pergunta para quem é o atendimento
                update_paciente(phone, {
                    "title": msg_recebida,
                    "primeiro_nome": primeiro_nome,
                    "status": "perguntando_para_quem"
                })
                enviar_botoes(phone,
                    f"Prazer em conhecer você, {primeiro_nome}! 😊\n\n"
                    f"Para te oferecermos a melhor experiência, vou te conduzir por algumas perguntas rápidas.\n\n"
                    f"O atendimento é para você ou para outra pessoa?",
                    [{"id": "pq_eu", "title": "Para mim"}, {"id": "pq_outro", "title": "Para outra pessoa"}]
                )

        elif status == "perguntando_para_quem":
            if "outra pessoa" in msg_limpa or msg_recebida == "Para outra pessoa":
                update_paciente(phone, {"agendado_por_terceiro": True, "status": "confirmando_paciente_real"})
                responder_texto(phone,
                    "Entendido! 😊\n\n"
                    "Por favor, me informe o *NOME COMPLETO* do paciente que será atendido (conforme documento):"
                )
            elif "mim" in msg_limpa or "para mim" in msg_limpa or msg_recebida == "Para mim":
                # Pula para escolha de serviço
                primeiro_nome = info.get("primeiro_nome") or info.get("title", "Paciente").split()[0]
                update_paciente(phone, {"status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [
                    {"id": "e1", "title": "Fisio Ortopédica"},
                    {"id": "e2", "title": "Fisio Neurológica"},
                    {"id": "e3", "title": "Fisio Pélvica"},
                    {"id": "e4", "title": "Acupuntura"},
                    {"id": "e5", "title": "Pilates Studio"},
                    {"id": "e6", "title": "Recovery"},
                    {"id": "e7", "title": "Liberação Miofascial"},
                    {"id": "e9", "title": "🔍 Não encontrei"}
                ]}]
                enviar_lista(phone,
                    f"Perfeito, {primeiro_nome}! 😊\n\nQual serviço você procura hoje?",
                    "Ver Serviços",
                    secoes
                )
            else:
                enviar_botoes(phone,
                    "Por favor, escolha uma das opções abaixo:",
                    [{"id": "pq_eu", "title": "Para mim"}, {"id": "pq_outro", "title": "Para outra pessoa"}]
                )

        elif status == "confirmando_paciente_real":
            if len(msg_limpa) < 2 or msg_recebida.isdigit():
                responder_texto(phone, "❌ Por favor, digite o nome completo do paciente.")
            else:
                nome_responsavel = info.get("title", "")
                update_paciente(phone, {"title": msg_recebida, "nome_responsavel": nome_responsavel, "status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [
                    {"id": "e1", "title": "Fisio Ortopédica"},
                    {"id": "e2", "title": "Fisio Neurológica"},
                    {"id": "e3", "title": "Fisio Pélvica"},
                    {"id": "e4", "title": "Acupuntura"},
                    {"id": "e5", "title": "Pilates Studio"},
                    {"id": "e6", "title": "Recovery"},
                    {"id": "e7", "title": "Liberação Miofascial"},
                    {"id": "e9", "title": "🔍 Não encontrei"}
                ]}]
                enviar_lista(phone, f"Perfeito! Cadastro em nome de *{msg_recebida}*. ✅\n\nQual serviço o paciente procura hoje?", "Ver Serviços", secoes)

        elif status == "menu_veterano":
            if "Novo Serviço" in msg_recebida:
                # Limpa dados antigos — paciente pode escolher modalidade/convênio diferente
                update_paciente(phone, {
                    "status": "escolhendo_especialidade",
                    "queixa": "", "queixa_ia": "",
                    "modalidade": "", "convenio": "",
                    "numCarteirinha": "", "carteirinha_media_id": "",
                    "pedido_media_id": ""
                })
                secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}, {"id": "e8", "title": "⬅️ Voltar ao Menu"}]}]
                enviar_lista(phone, "Perfeito! Qual novo serviço você deseja agendar?", "Ver Serviços", secoes)

            elif "Nova Guia" in msg_recebida or "Tratamento" in msg_recebida:
                # Limpa dados antigos — paciente pode mudar modalidade/convênio
                update_paciente(phone, {
                    "queixa": "", "queixa_ia": "",
                    "modalidade": "",
                    "numCarteirinha": "", "carteirinha_media_id": "",
                    "pedido_media_id": ""
                })
                feegow_id = info.get("feegow_id")
                servico_atual = _buscar_servico_atual_feegow(feegow_id) if feegow_id else None
                if servico_atual and servico_atual.get("servico"):
                    sv = servico_atual["servico"]
                    un = servico_atual.get("unidade", "")
                    un_txt = f" — unidade *{un}*" if un else ""
                    update_paciente(phone, {
                        "status": "confirmando_servico_nova_guia",
                        "nova_guia_servico": sv,
                        "nova_guia_unidade": un,
                        "nova_guia_local_id": servico_atual.get("local_id"),
                        "nova_guia_proc_id": servico_atual.get("procedimento_id")
                    })
                    enviar_botoes(phone,
                        f"Vou te ajudar a renovar a autorização do seu tratamento. ✅\n\n"
                        f"Vi que você realiza *{sv}*{un_txt}.\n\n"
                        f"Vamos organizar a nova guia para esse tratamento?",
                        [{"id": "ng_sim", "title": f"✅ Sim, {sv}"}, {"id": "ng_outro", "title": "↔️ Outro serviço"}, {"id": "ng_voltar", "title": "⬅️ Voltar"}])
                else:
                    update_paciente(phone, {"status": "escolhendo_especialidade", "nova_guia": True})
                    secoes_ng = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}, {"id": "e8", "title": "⬅️ Voltar ao Menu"}]}]
                    enviar_lista(phone, "Não identifiquei seu tratamento automaticamente. Qual serviço deseja renovar a guia?", "Ver Serviços", secoes_ng)

            elif "Reagendar" in msg_recebida or "Meus Agendamentos" in msg_recebida or msg_recebida == "v1":
                resultado_raw = consultar_agenda_feegow(info.get("feegow_id"), retornar_raw=True) if info.get("feegow_id") else None
                sessoes_labels = resultado_raw["sessoes"] if resultado_raw else []
                agendamentos_raw = resultado_raw["agendamentos"] if resultado_raw else []
                # Salva dados do primeiro agendamento para uso no reagendamento
                local_id_ag = agendamentos_raw[0]["local_id"] if agendamentos_raw else None
                proc_id_ag = agendamentos_raw[0]["procedimento_id"] if agendamentos_raw else None
                # Atualiza unidade com base no agendamento real (não na seleção do menu)
                unidade_real = agendamentos_raw[0].get("unidade", "") if agendamentos_raw else ""
                update_fields = {"status": "gestao_agenda", "agenda_local_id": local_id_ag, "agenda_procedimento_id": proc_id_ag, "agenda_agendamentos": agendamentos_raw[:10]}
                if unidade_real:
                    update_fields["unit"] = unidade_real
                update_paciente(phone, update_fields)
                if sessoes_labels:
                    secoes_gestao = [{"title": "O que deseja fazer?", "rows": [
                        {"id": "ga_consultar", "title": "📋 Ver minha agenda"},
                        {"id": "ga_confirmar", "title": "✅ Confirmar presença"},
                        {"id": "ga_reagendar", "title": "🔄 Reagendar sessão"},
                        {"id": "ga_cancelar",  "title": "❌ Cancelar sessão"}
                    ]}]
                    enviar_lista(phone, f"Localizei suas próximas sessões:\n\n{chr(10).join(sessoes_labels[:5])}\n\nO que deseja fazer?", "Ver Opções", secoes_gestao)
                else:
                    enviar_botoes(phone, "Não encontrei sessões futuras agendadas. 😊\n\nDeseja falar com nossa equipe?", [{"id": "ga_secretaria", "title": "📁 Falar com equipe"}, {"id": "menu_ini", "title": "⬅️ Voltar ao Menu"}])
            
            elif "Token" in msg_recebida or msg_recebida == "v5":
                update_paciente(phone, {"status": "aguardando_token_convenio"})
                responder_texto(phone, "Claro! 😊 Por favor, informe o código de autorização que você recebeu do seu convênio (Amil, Prevent, Bradesco...).\n\n_Normalmente é um número com 6 dígitos enviado por SMS ou pelo app do plano._")

            elif "Secretaria" in msg_recebida or "📁" in msg_recebida:
                update_paciente(phone, {"status": "menu_secretaria"})
                secoes = [{"title": "Serviços de Secretaria", "rows": [{"id": "s1", "title": "Declaração de Horas"}, {"id": "s2", "title": "Relatório Fisio"}, {"id": "s3", "title": "Atualização Cadastral"}, {"id": "s5", "title": "📁 Enviar Exames/Resultados"}, {"id": "s6", "title": "❌ Cancelar Tratamento"}, {"id": "s4", "title": "⬅️ Voltar ao Menu"}]}]
                enviar_lista(phone, "Acesso à Secretaria. O que você precisa solicitar?", "Ver Serviços", secoes)

            else:
                # Catch-all: mensagem não reconhecida → reapresenta o menu
                nome_mv = info.get("title", "Paciente").split()[0]
                secoes_mv = [{"title": "Como posso ajudar?", "rows": [{"id": "v1", "title": "🗓️ Meus Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia/Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v5", "title": "🔑 Enviar Token"}, {"id": "v4", "title": "📁 Secretaria"}]}]
                enviar_lista(phone, f"Como posso te ajudar, {nome_mv}? 😊", "Ver Opções", secoes_mv)

        elif status == "gestao_agenda":
            import sys
            _secoes_ga = [{"title": "O que deseja fazer?", "rows": [{"id": "ga_consultar", "title": "📋 Ver minha agenda"}, {"id": "ga_confirmar", "title": "✅ Confirmar presença"}, {"id": "ga_reagendar", "title": "🔄 Reagendar sessão"}, {"id": "ga_cancelar", "title": "❌ Cancelar sessão"}, {"id": "ga_voltar", "title": "⬅️ Voltar ao Menu"}]}]

            if msg_recebida in ["ga_consultar", "📋 Ver minha agenda", "Ver minha agenda"]:
                res_raw = consultar_agenda_feegow(info.get("feegow_id"), retornar_raw=True)
                sessoes_v = res_raw["sessoes"] if res_raw else []
                if sessoes_v:
                    responder_texto(phone, f"📋 Sua agenda:\n\n{chr(10).join(sessoes_v[:10])}")
                else:
                    responder_texto(phone, "Não encontrei sessões futuras agendadas no momento.")
                # Reapresenta menu veterano em vez de apenas "qualquer dúvida é só chamar"
                update_paciente(phone, {"status": "menu_veterano"})
                nome_va = info.get("title", "Paciente").split()[0]
                secoes_va = [{"title": "Como posso ajudar?", "rows": [
                    {"id": "v1", "title": "🗓️ Meus Agendamentos"},
                    {"id": "v2", "title": "🔄 Nova Guia/Tratamento"},
                    {"id": "v3", "title": "➕ Novo Serviço"},
                    {"id": "v5", "title": "🔑 Enviar Token"},
                    {"id": "v4", "title": "📁 Secretaria"}
                ]}]
                enviar_lista(phone, f"Posso te ajudar com mais alguma coisa, {nome_va}? 😊", "Ver Opções", secoes_va)
                return jsonify({"status": "agenda_consultada"}), 200

            elif msg_recebida in ["ga_confirmar", "✅ Confirmar presença", "Confirmar presença"]:
                agendamentos_raw = info.get("agenda_agendamentos", [])
                agendamento_id = agendamentos_raw[0]["agendamento_id"] if agendamentos_raw else None
                data_proxima = agendamentos_raw[0]["data_br"] if agendamentos_raw else "--"
                hora_proxima = agendamentos_raw[0]["hora"] if agendamentos_raw else "--"
                if agendamento_id:
                    ok = confirmar_presenca_feegow(agendamento_id)
                    if ok:
                        update_paciente(phone, {"status": "menu_veterano", "confirmou_presenca": True})
                        responder_texto(phone, f"✅ Presença confirmada para *{data_proxima} às {hora_proxima}*!\n\nTe esperamos! Lembre-se de chegar 10 minutinhos antes. 😊")
                    else:
                        update_paciente(phone, {"status": "atendimento_humano", "unread": True, "queixa": f"[CONFIRMAÇÃO]: Paciente tentou confirmar presença em {data_proxima} às {hora_proxima}"})
                        responder_texto(phone, "Não consegui confirmar automaticamente. Nossa equipe já foi notificada e confirma em instantes! 😊")
                else:
                    responder_texto(phone, "Não encontrei agendamento para confirmar. Nossa equipe pode te ajudar! 😊")
                    update_paciente(phone, {"status": "menu_veterano"})
                return jsonify({"status": "confirmacao_processada"}), 200

            elif msg_recebida in ["ga_reagendar", "🔄 Reagendar sessão", "Reagendar sessão"]:
                agendamentos_raw = info.get("agenda_agendamentos", [])
                if not agendamentos_raw:
                    responder_texto(phone, "Não encontrei sessões próximas para reagendar. Nossa equipe pode te ajudar! 😊")
                    update_paciente(phone, {"status": "menu_veterano"})
                    return jsonify({"status": "reagendar_sem_sessoes"}), 200

                # Agrupa por local_id — pega as 2 mais próximas de cada agenda
                local_id_visto = {}
                sessoes_exibir = []
                for ag in agendamentos_raw:
                    lid = ag.get("local_id")
                    if lid not in local_id_visto:
                        local_id_visto[lid] = 0
                    if local_id_visto[lid] < 2:
                        sessoes_exibir.append(ag)
                        local_id_visto[lid] += 1

                tem_mais = len(agendamentos_raw) > len(sessoes_exibir)

                if len(sessoes_exibir) == 1:
                    # Só uma sessão — vai direto para tipo
                    ag = sessoes_exibir[0]
                    update_paciente(phone, {"status": "reagendando_tipo", "agenda_sessao_selecionada": ag, "agenda_local_id": ag["local_id"], "agenda_procedimento_id": ag["procedimento_id"]})
                    enviar_botoes(phone,
                        f"Vamos reagendar sua sessão de *{ag['servico']}* em *{ag['data_br']} às {ag['hora']}*.\n\nO que você precisa?",
                        [{"id": "rt_horario", "title": "🕐 Mudar horário"}, {"id": "rt_dia", "title": "📅 Mudar o dia"}, {"id": "rt_voltar", "title": "⬅️ Voltar"}])
                else:
                    # Múltiplas sessões — deixa paciente escolher
                    rows = [{"id": f"rag_{i}", "title": f"{ag['servico']} {ag['data_br']} {ag['hora']}"[:24]} for i, ag in enumerate(sessoes_exibir)]
                    if tem_mais:
                        rows.append({"id": "rag_mais", "title": "📅 Ver mais sessões"})
                    rows.append({"id": "rag_voltar", "title": "⬅️ Voltar"})
                    update_paciente(phone, {"status": "escolhendo_sessao_reagendamento", "agenda_agendamentos": agendamentos_raw})
                    lista_txt = "\n".join([f"• 🗓️ *{ag['data_br']} às {ag['hora']}* - {ag['servico']}" for ag in sessoes_exibir])
                    enviar_lista(phone, f"Qual sessão deseja reagendar?\n\n{lista_txt}", "Selecionar", [{"title": "Sessões", "rows": rows}])
                return jsonify({"status": "reagendar_iniciado"}), 200

            elif msg_recebida in ["ga_cancelar", "❌ Cancelar sessão", "Cancelar sessão"]:
                agendamentos_raw = info.get("agenda_agendamentos", [])
                ag_cancel = agendamentos_raw[0] if agendamentos_raw else None
                ag_mesmo_dia_c = []
                if ag_cancel:
                    data_c = ag_cancel.get("data", "")
                    ag_mesmo_dia_c = [a for a in agendamentos_raw if a.get("data") == data_c]
                if len(ag_mesmo_dia_c) > 1:
                    opcoes_c = [{"id": f"ac_{i}", "title": f"{ag['servico']} {ag['hora']}"[:24]} for i, ag in enumerate(ag_mesmo_dia_c[:2])]
                    opcoes_c.append({"id": "ac_ambos", "title": "Cancelar ambos"})
                    opcoes_c.append({"id": "ac_voltar", "title": "⬅️ Voltar"})
                    update_paciente(phone, {"status": "escolhendo_sessao_cancelamento"})
                    enviar_lista(phone, f"Você tem dois atendimentos em *{ag_cancel.get('data_br','')}*. Qual deseja cancelar?", "Ver Sessões", [{"title": "Selecione", "rows": opcoes_c}])
                elif ag_cancel:
                    update_paciente(phone, {"status": "cancelando_sessao", "agenda_sessao_selecionada": ag_cancel})
                    data_br_c = ag_cancel.get("data_br", "")
                    hora_c = ag_cancel.get("hora", "")
                    servico_c = ag_cancel.get("servico", "Sessão")
                    enviar_botoes(phone,
                        f"Atenção: vou cancelar sua sessão de *{servico_c}* em *{data_br_c} às {hora_c}*.\n\nDeseja informar o motivo?",
                        [{"id": "cs_motivo", "title": "Sim, informar motivo"}, {"id": "cs_direto", "title": "Não, só cancelar"}, {"id": "cs_voltar", "title": "⬅️ Voltar"}])
                else:
                    responder_texto(phone, "Não encontrei sessões próximas para cancelar.")
                    update_paciente(phone, {"status": "menu_veterano"})
                return jsonify({"status": "cancelamento_iniciado"}), 200

            elif msg_recebida in ["ga_voltar", "⬅️ Voltar ao Menu", "Voltar ao Menu"]:
                nome_s = info.get("title", "Paciente").split()[0]
                update_paciente(phone, {"status": "menu_veterano"})
                secoes_vet_v = [{"title": "Como posso ajudar?", "rows": [{"id": "v1", "title": "🗓️ Meus Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia/Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v5", "title": "🔑 Enviar Token"}, {"id": "v4", "title": "📁 Secretaria"}]}]
                enviar_lista(phone, f"Voltando ao menu principal. Como posso te ajudar, {nome_s}?", "Ver Opções", secoes_vet_v)

            else:
                enviar_lista(phone, "Por favor, escolha uma das opções:", "Ver Opções", _secoes_ga)

        elif status == "escolhendo_sessao_reagendamento":
            agendamentos_raw = info.get("agenda_agendamentos", [])
            if msg_recebida in ["rag_voltar", "ag_voltar", "⬅️ Voltar"]:
                update_paciente(phone, {"status": "gestao_agenda"})
                _secoes_ga2 = [{"title": "O que deseja fazer?", "rows": [{"id": "ga_consultar", "title": "📋 Ver minha agenda"}, {"id": "ga_confirmar", "title": "✅ Confirmar presença"}, {"id": "ga_reagendar", "title": "🔄 Reagendar sessão"}, {"id": "ga_cancelar", "title": "❌ Cancelar sessão"}]}]
                enviar_lista(phone, "Voltando ao menu de gestão:", "Ver Opções", _secoes_ga2)
            elif msg_recebida == "rag_mais":
                nome_rm = info.get("title", "Paciente").split()[0]
                update_paciente(phone, {"status": "atendimento_humano", "unread": True, "queixa": "[REAGENDAMENTO]: paciente quer reagendar sessões além das próximas."})
                responder_texto(phone, f"Claro, {nome_rm}! Vou conectar você com nossa recepção para organizar as demais sessões. 💙")
            else:
                # Tenta casar por ID (rag_N ou ag_N) ou por título
                ag_sel = None
                for prefix in ["rag_", "ag_"]:
                    if msg_recebida.startswith(prefix) and msg_recebida.replace(prefix, "").isdigit():
                        idx = int(msg_recebida.replace(prefix, ""))
                        if idx < len(agendamentos_raw):
                            ag_sel = agendamentos_raw[idx]
                        break
                if not ag_sel:
                    # Casa por título — inclui serviço para evitar ambiguidade
                    msg_lower = msg_limpa
                    for ag in agendamentos_raw:
                        servico_ag = ag.get('servico','').lower()
                        data_ag = ag.get('data_br','')
                        hora_ag = ag.get('hora','')
                        # Verifica se o serviço e a data batem
                        servico_ok = servico_ag and servico_ag in msg_lower
                        data_ok = data_ag and data_ag.replace("/","") in msg_lower.replace("/","").replace("-","")
                        hora_ok = hora_ag and hora_ag[:5] in msg_lower
                        if servico_ok and (data_ok or hora_ok):
                            ag_sel = ag
                            break
                    # Fallback: casa só por data+hora se serviço não identificado
                    if not ag_sel:
                        for ag in agendamentos_raw:
                            data_ag = ag.get('data_br','')
                            hora_ag = ag.get('hora','')
                            if data_ag in msg_recebida and hora_ag[:5] in msg_recebida:
                                ag_sel = ag
                                break
                if ag_sel:
                    update_paciente(phone, {"status": "reagendando_tipo", "agenda_sessao_selecionada": ag_sel, "agenda_local_id": ag_sel["local_id"], "agenda_procedimento_id": ag_sel["procedimento_id"]})
                    enviar_botoes(phone,
                        f"Vamos reagendar sua sessão de *{ag_sel['servico']}* em *{ag_sel['data_br']} às {ag_sel['hora']}*.\n\nO que você precisa?",
                        [{"id": "rt_horario", "title": "🕐 Mudar horário"}, {"id": "rt_dia", "title": "📅 Mudar o dia"}, {"id": "rt_voltar", "title": "⬅️ Voltar"}])
                else:
                    enviar_lista(phone, "Qual sessão deseja reagendar?", "Selecionar", [{"title": "Sessões", "rows": [{"id": f"rag_{i}", "title": f"{ag['servico']} {ag['data_br']}"[:24]} for i, ag in enumerate(agendamentos_raw[:4])]}])

        elif status == "escolhendo_sessao_cancelamento":
            agendamentos_raw = info.get("agenda_agendamentos", [])
            if msg_recebida in ["ac_voltar", "⬅️ Voltar", "Voltar"]:
                update_paciente(phone, {"status": "gestao_agenda"})
                _secoes_ga3 = [{"title": "O que deseja fazer?", "rows": [{"id": "ga_consultar", "title": "📋 Ver minha agenda"}, {"id": "ga_confirmar", "title": "✅ Confirmar presença"}, {"id": "ga_reagendar", "title": "🔄 Reagendar sessão"}, {"id": "ga_cancelar", "title": "❌ Cancelar sessão"}]}]
                enviar_lista(phone, "Voltando ao menu de gestão:", "Ver Opções", _secoes_ga3)
            else:
                # Tenta achar a sessão: primeiro por ID (ac_N), depois por título
                ag_sel = None
                if msg_recebida.startswith("ac_") and msg_recebida.replace("ac_", "").isdigit():
                    idx = int(msg_recebida.replace("ac_", ""))
                    if idx < len(agendamentos_raw):
                        ag_sel = agendamentos_raw[idx]
                else:
                    # Busca por título — ex: "Fisioterapia 11:00"
                    for ag in agendamentos_raw:
                        titulo_ag = f"{ag.get('servico','')} {ag.get('hora','')}".strip()
                        if msg_limpa in titulo_ag.lower() or titulo_ag.lower() in msg_limpa:
                            ag_sel = ag
                            break
                if ag_sel:
                    update_paciente(phone, {"status": "cancelando_sessao", "agenda_sessao_selecionada": ag_sel})
                    data_br_c = ag_sel.get("data_br", "")
                    hora_c = ag_sel.get("hora", "")
                    servico_c = ag_sel.get("servico", "Sessão")
                    enviar_botoes(phone,
                        f"Atenção: vou cancelar sua sessão de *{servico_c}* em *{data_br_c} às {hora_c}*.\n\nDeseja informar o motivo?",
                        [{"id": "cs_motivo", "title": "Sim, informar motivo"}, {"id": "cs_direto", "title": "Não, só cancelar"}, {"id": "cs_voltar", "title": "⬅️ Voltar"}])
                else:
                    # Sessão não identificada — mostra a lista novamente
                    ag_mesmo_dia = agendamentos_raw[:2]
                    opcoes_c = [{"id": f"ac_{i}", "title": f"{ag['servico']} {ag['hora']}"[:24]} for i, ag in enumerate(ag_mesmo_dia)]
                    opcoes_c.append({"id": "ac_voltar", "title": "⬅️ Voltar"})
                    enviar_lista(phone, "Qual sessão deseja cancelar?", "Selecionar", [{"title": "Sessões", "rows": opcoes_c}])

        elif status == "reagendando_preferencia":
            import sys
            if msg_recebida in ["rp_voltar", "⬅️ Voltar", "Voltar"]:
                update_paciente(phone, {"status": "gestao_agenda"})
                _secoes_gav = [{"title": "O que deseja fazer?", "rows": [{"id": "ga_consultar", "title": "📋 Ver minha agenda"}, {"id": "ga_confirmar", "title": "✅ Confirmar presença"}, {"id": "ga_reagendar", "title": "🔄 Reagendar sessão"}, {"id": "ga_cancelar", "title": "❌ Cancelar sessão"}, {"id": "ga_voltar", "title": "⬅️ Voltar ao Menu"}]}]
                enviar_lista(phone, "Voltando às opções:", "Ver Opções", _secoes_gav)
                return jsonify({"status": "reagendamento_cancelado"}), 200
            # Usa IA para extrair preferência de data do texto livre
            pref = extrair_preferencia_data(msg_recebida)
            print(f"[REAGEND-PREF] Extraído: {pref}", file=sys.stderr)
            local_id = info.get("agenda_local_id") or 2
            proc_id = info.get("agenda_procedimento_id") or 9
            hoje = datetime.now()
            agendamentos_serie = info.get("agenda_agendamentos", [])

            # Determina horário preferido
            hora_preferida = pref.get("hora") or ("14:00" if pref.get("periodo") == "tarde" else "08:00")

            # Data alvo — já em YYYY-MM-DD, sem parsing frágil
            data_ini = hoje + timedelta(days=1)  # mínimo amanhã
            if pref.get("data"):
                try:
                    data_cand = datetime.strptime(pref["data"], "%Y-%m-%d")
                    if data_cand.date() >= hoje.date():
                        data_ini = data_cand
                except Exception as e_dp:
                    print(f"[REAGEND-DATA] Erro parse '{pref.get('data')}': {e_dp}", file=sys.stderr)
            elif pref.get("periodo") and not pref.get("data"):
                pass  # sem data específica → usa amanhã
            print(f"[REAGEND-DATA] data_ini={data_ini.strftime('%d/%m/%Y')} hora_pref={hora_preferida}", file=sys.stderr)

            data_fim = dias_uteis_a_partir(data_ini, 14)  # janela de 14 dias úteis

            # Monta mapa de conflitos: data → lista de horários já agendados
            def _tem_conflito(slot_data, slot_hora, agendamentos):
                """Retorna True se o slot conflita com agendamentos existentes.
                Regras: mesmo dia mesmo serviço OU mesmo dia horário com diff < 30min."""
                try:
                    sh = datetime.strptime(slot_hora, "%H:%M")
                except: return False
                for ag in agendamentos:
                    if ag.get("data") != slot_data: continue
                    try:
                        ah = datetime.strptime(ag.get("hora",""), "%H:%M")
                        diff_min = abs((sh - ah).total_seconds()) / 60
                        if diff_min < 30:
                            return True
                    except: continue
                return False

            update_paciente(phone, {"status": "escolhendo_horario_reagendamento", "reagendamento_hora_preferida": hora_preferida})
            responder_texto(phone, "Buscando horários disponíveis... ⏳")
            slots_all = consultar_disponibilidade_feegow(local_id, proc_id, data_ini.strftime('%Y-%m-%d'), data_fim.strftime('%Y-%m-%d'))

            # Filtra por data >= solicitada (Feegow ignora data_start)
            data_ini_str = data_ini.strftime('%Y-%m-%d')
            slots_all = [s for s in slots_all if s.get("data","") >= data_ini_str]

            # Filtro por unidade_id já feito na consulta — não precisa filtrar aqui
            print(f"[REAGEND-SLOTS] {len(slots_all)} slots unidade_id={_LOCAL_ID_UNIDADE.get(local_id, 0)}", file=sys.stderr)

            # Filtra conflitos com agenda existente
            slots_ok = [s for s in slots_all if not _tem_conflito(s.get("data",""), s.get("hora",""), agendamentos_serie)]
            proximos = encontrar_horarios_proximos(slots_ok, hora_preferida, qtd=2)
            if proximos:
                opcoes_txt = "\n".join([f"• {s['label']}" for s in proximos])
                update_paciente(phone, {
                    "reagendamento_opcoes": [{"data": s["data"], "hora": s["hora"], "label": s["label"]} for s in proximos],
                    "reagendamento_slots_cache": slots_ok,
                    "reagendamento_slots_vistos": [s["label"] for s in proximos]
                })
                rows_sl = [{"id": f"slot_{i}", "title": f"{s['data_br']} às {s['hora']}"} for i, s in enumerate(proximos[:8])]
                rows_sl.append({"id": "slot_outro", "title": "🔄 Ver outros horários"})
                rows_sl.append({"id": "eh_voltar", "title": "⬅️ Voltar"})
                opcoes_txt = "\n".join([f"• {s['label']}" for s in proximos])
                enviar_lista(phone, f"Horários disponíveis mais próximos:\n\n{opcoes_txt}\n\nQual prefere?", "Ver Horários", [{"title": "Selecione", "rows": rows_sl}])
            else:
                ag_sel = info.get("agenda_sessao_selecionada", {})
                nome_rp = info.get("title", "Paciente").split()[0]
                # Verifica se o problema é conflito (tem slots mas todos filtrados)
                tem_slots_brutos = len(slots_all) > 0
                if tem_slots_brutos:
                    # Conflito detectado — tem slots mas todos conflitam com agenda
                    conflitos = []
                    for ag in agendamentos_serie:
                        conflitos.append(f"{ag.get('data_br','')} às {ag.get('hora','')} ({ag.get('servico','')})")
                    conflitos_txt = "\n".join([f"• {c}" for c in conflitos[:3]])
                    queixa_conf = f"[REAGENDAMENTO CONFLITO]: {nome_rp} solicitou horário que conflita com agenda existente. Sessão original: {ag_sel.get('data_br','')} às {ag_sel.get('hora','')}. Preferência: {msg_recebida}."
                    update_paciente(phone, {"status": "atendimento_humano", "unread": True, "queixa": queixa_conf})
                    responder_texto(phone,
                        f"Atenção, {nome_rp}! ⚠️ O horário solicitado conflita com sessões já agendadas:\n\n{conflitos_txt}\n\n"
                        f"Vou encaminhar para nossa recepção encontrar o melhor horário sem conflito. 💙"
                    )
                else:
                    queixa_rp = f"[REAGENDAMENTO]: sem disponibilidade. Preferência: {msg_recebida}. Sessão original: {ag_sel.get('data_br','')}"
                    update_paciente(phone, {"status": "atendimento_humano", "unread": True, "queixa": queixa_rp})
                    responder_texto(phone, f"Não encontrei horários disponíveis no período solicitado, {nome_rp}. Nossa equipe vai entrar em contato para encontrar o melhor horário! 💙")

        elif status == "escolhendo_horario_reagendamento":
            opcoes = info.get("reagendamento_opcoes", [])
            if msg_recebida in ["slot_outro", "Outros horários", "Ver outros horários", "Outro horário", "🔄 Ver outros horários"]:
                hora_pref = info.get("reagendamento_hora_preferida", "08:00")
                ag_sel_r = info.get("agenda_sessao_selecionada", {})
                # Usa slots já em cache — não refaz chamada à API
                slots_cache = info.get("reagendamento_slots_cache", [])
                slots_ja_vistos = info.get("reagendamento_slots_vistos", [])
                # Filtra os que ainda não foram mostrados
                proximos_outros = [s for s in slots_cache if s.get("label") not in slots_ja_vistos]
                proximos_outros = encontrar_horarios_proximos(proximos_outros, hora_pref, qtd=5)
                if proximos_outros:
                    vistos_novos = slots_ja_vistos + [s["label"] for s in proximos_outros]
                    update_paciente(phone, {
                        "reagendamento_opcoes": proximos_outros,
                        "reagendamento_slots_vistos": vistos_novos
                    })
                    opcoes_txt2 = "\n".join([f"• {s['label']}" for s in proximos_outros])
                    rows_sl2 = [{"id": f"slot_{i}", "title": f"{s['data_br']} às {s['hora']}"} for i, s in enumerate(proximos_outros[:8])]
                    rows_sl2.append({"id": "slot_recepcao", "title": "👩 Falar com recepção"})
                    rows_sl2.append({"id": "eh_voltar", "title": "⬅️ Voltar"})
                    enviar_lista(phone, f"Outras opções disponíveis:\n\n{opcoes_txt2}\n\nQual prefere?", "Ver Horários", [{"title": "Selecione", "rows": rows_sl2}])
                else:
                    # Sem mais slots — transfere para recepção
                    nome_r = info.get("title", "Paciente").split()[0]
                    queixa_sem = f"[REAGENDAMENTO]: {nome_r} não aceitou nenhuma sugestão. Sessão original: {ag_sel_r.get('data_br','')} às {ag_sel_r.get('hora','')}. Preferência: {hora_pref}."
                    update_paciente(phone, {"status": "atendimento_humano", "unread": True, "queixa": queixa_sem})
                    responder_texto(phone, f"Entendido, {nome_r}! Vou passar para nossa equipe encontrar o melhor horário para você. Em breve entraremos em contato! 💙")

            elif msg_recebida in ["slot_recepcao", "👩 Falar com recepção"]:
                nome_r2 = info.get("title", "Paciente").split()[0]
                ag_sel_r2 = info.get("agenda_sessao_selecionada", {})
                queixa_r2 = f"[REAGENDAMENTO]: {nome_r2} pediu para falar com a recepção. Sessão original: {ag_sel_r2.get('data_br','')} às {ag_sel_r2.get('hora','')}."
                update_paciente(phone, {"status": "atendimento_humano", "unread": True, "queixa": queixa_r2})
                responder_texto(phone, f"Claro, {nome_r2}! Nossa equipe vai entrar em contato para encontrar o melhor horário. 💙")
            elif msg_recebida in ["eh_voltar", "⬅️ Voltar", "Voltar"]:
                update_paciente(phone, {"status": "gestao_agenda"})
                _secoes_ehv = [{"title": "O que deseja fazer?", "rows": [{"id": "ga_consultar", "title": "📋 Ver minha agenda"}, {"id": "ga_confirmar", "title": "✅ Confirmar presença"}, {"id": "ga_reagendar", "title": "🔄 Reagendar sessão"}, {"id": "ga_cancelar", "title": "❌ Cancelar sessão"}, {"id": "ga_voltar", "title": "⬅️ Voltar ao Menu"}]}]
                enviar_lista(phone, "Voltando às opções:", "Ver Opções", _secoes_ehv)
            else:
                # Tenta casar por ID (slot_N) ou por título ("15/05/2026 às 08:00")
                slot = None
                if msg_recebida.startswith("slot_") and msg_recebida.replace("slot_", "").isdigit():
                    idx = int(msg_recebida.replace("slot_", ""))
                    slot = opcoes[idx] if idx < len(opcoes) else None
                else:
                    # Lista retorna título — casa por data/hora
                    for o in opcoes:
                        titulo = f"{o.get('data_br','')} às {o.get('hora','')}"
                        if msg_recebida == titulo or o.get("hora","") in msg_recebida and o.get("data_br","") in msg_recebida:
                            slot = o
                            break

                if slot:
                    ag_orig = info.get("agenda_sessao_selecionada", {})
                    nome_pac = info.get("title", "Paciente").split()[0]
                    queixa_r = (
                        f"[REAGENDAMENTO]: {nome_pac} escolheu *{slot['label']}*. "
                        f"Sessão original: {ag_orig.get('data_br','')} às {ag_orig.get('hora','')} "
                        f"({ag_orig.get('servico','')}) — aguarda confirmação da recepção."
                    )
                    update_paciente(phone, {
                        "status": "atendimento_humano",
                        "unread": True,
                        "queixa": queixa_r,
                        "reagendamento_solicitado": slot
                    })
                    responder_texto(phone,
                        f"Ótimo, {nome_pac}! ✅\n\n"
                        f"Sua preferência de horário *{slot['label']}* foi registrada.\n\n"
                        f"Vou informar nossa recepção agora e em breve você receberá a confirmação do reagendamento. 😊"
                    )
                elif opcoes:
                    rows_back = [{"id": f"slot_{i}", "title": f"{o['data_br']} às {o['hora']}"} for i, o in enumerate(opcoes[:8])]
                    rows_back.append({"id": "slot_outro", "title": "🔄 Ver outros horários"})
                    rows_back.append({"id": "eh_voltar", "title": "⬅️ Voltar"})
                    enviar_lista(phone, "Por favor, escolha uma das opções:", "Ver Horários", [{"title": "Selecione", "rows": rows_back}])

        elif status == "cancelando_sessao":
            ag_sel = info.get("agenda_sessao_selecionada", {})
            ag_id_cs = ag_sel.get("agendamento_id")
            ref_sessao = f" *{ag_sel.get('data_br','')} às {ag_sel.get('hora','')}*" if ag_sel else ""
            if msg_recebida in ["cs_voltar", "⬅️ Voltar", "Voltar"]:
                update_paciente(phone, {"status": "gestao_agenda"})
                _secoes_csv = [{"title": "O que deseja fazer?", "rows": [{"id": "ga_consultar", "title": "📋 Ver minha agenda"}, {"id": "ga_confirmar", "title": "✅ Confirmar presença"}, {"id": "ga_reagendar", "title": "🔄 Reagendar sessão"}, {"id": "ga_cancelar", "title": "❌ Cancelar sessão"}, {"id": "ga_voltar", "title": "⬅️ Voltar ao Menu"}]}]
                enviar_lista(phone, "Voltando às opções:", "Ver Opções", _secoes_csv)
            elif msg_recebida in ["cs_motivo", "Sim, informar motivo"]:
                update_paciente(phone, {"status": "informando_motivo_cancelamento_sessao"})
                responder_texto(phone, f"Entendido. Por favor, me informe o motivo do cancelamento da sessão de{ref_sessao}:")
            elif msg_recebida in ["cs_direto", "Não, só cancelar"] or "apenas" in msg_recebida.lower():
                obs_cs = f"Desmarcado pelo paciente via robô. Sessão:{ref_sessao}"
                ok_cs = cancelar_agendamento_feegow(ag_id_cs, obs=obs_cs) if ag_id_cs else False
                import sys; print(f"[CANCEL-SESSAO] id={ag_id_cs} ok={ok_cs}", file=sys.stderr)
                tag_cs = "[CANCELAMENTO]" if ok_cs else "[CANCELAMENTO — confirmar no Feegow]"
                update_paciente(phone, {"status": "menu_veterano", "unread": True, "queixa": f"{tag_cs}: sessão{ref_sessao} cancelada."})
                enviar_botoes(phone, f"Cancelamento registrado! ✅\n\nGostaria de reagendar para outro horário?",
                    [{"id": "cs_rea", "title": "Sim, reagendar"}, {"id": "cs_nao", "title": "Não, obrigado"}])
                update_paciente(phone, {"status": "pos_cancelamento_sessao"})
            elif msg_recebida in ["cs_rea", "Sim, reagendar"] or "cancel_reagendar" in msg_recebida or "reagendar" in msg_recebida.lower():
                update_paciente(phone, {"status": "reagendando_preferencia"})
                responder_texto(phone, "Vamos encontrar um novo horário! 😊\n\nQual dia e período você prefere?\n\n_Exemplo: quinta de manhã, semana que vem_")
            elif msg_recebida in ["cs_nao", "Não, obrigado"]:
                nome_cs = info.get("title", "Paciente").split()[0]
                update_paciente(phone, {"status": "menu_veterano"})
                responder_texto(phone, f"Tudo certo, {nome_cs}! ✅ Sua solicitação foi enviada para nossa recepção. Se precisar de algo mais, é só chamar. 😊")
            else:
                enviar_botoes(phone, f"Como deseja prosseguir com a sessão de{ref_sessao}?",
                    [{"id": "cs_motivo", "title": "Informar motivo"}, {"id": "cs_direto", "title": "Cancelar direto"}, {"id": "cs_rea", "title": "Cancelar e reagendar"}, {"id": "cs_voltar", "title": "⬅️ Voltar"}])

        elif status == "informando_motivo_cancelamento_sessao":
            motivo_cs = msg_recebida
            ag_sel_cs = info.get("agenda_sessao_selecionada", {})
            ag_id_mcs = ag_sel_cs.get("agendamento_id")
            ref_cs = f" *{ag_sel_cs.get('data_br','')} às {ag_sel_cs.get('hora','')}*" if ag_sel_cs else ""
            obs_mcs = f"Desmarcado pelo paciente. Motivo: {motivo_cs}"
            ok_mcs = cancelar_agendamento_feegow(ag_id_mcs, obs=obs_mcs) if ag_id_mcs else False
            import sys; print(f"[CANCEL-MOTIVO] id={ag_id_mcs} ok={ok_mcs}", file=sys.stderr)
            tag_mcs = "[CANCELAMENTO]" if ok_mcs else "[CANCELAMENTO — confirmar no Feegow]"
            update_paciente(phone, {"status": "pos_cancelamento_sessao", "unread": True, "queixa": f"{tag_mcs}: sessão{ref_cs}. Motivo: {motivo_cs}"})
            enviar_botoes(phone, f"Cancelamento registrado! ✅\n\nGostaria de reagendar para outro horário?",
                [{"id": "cs_rea", "title": "Sim, reagendar"}, {"id": "cs_nao", "title": "Não, obrigado"}])

        elif status == "pos_cancelamento_sessao":
            nome_pcs = info.get("title", "Paciente").split()[0]
            if msg_recebida in ["cs_rea", "Sim, reagendar"] or "reagendar" in msg_recebida.lower():
                update_paciente(phone, {"status": "reagendando_preferencia"})
                responder_texto(phone, f"Vamos encontrar um novo horário para você, {nome_pcs}! 😊\n\nQual dia e período você prefere?\n\n_Exemplo: quinta de manhã, semana que vem_")
            elif msg_recebida in ["cs_nao", "Não, obrigado"] or "obrigad" in msg_recebida.lower() or "nao" in msg_recebida.lower() or "não" in msg_recebida.lower():
                update_paciente(phone, {"status": "menu_veterano"})
                responder_texto(phone, f"Tudo certo, {nome_pcs}! ✅ Sua solicitação foi enviada para nossa recepção. Qualquer dúvida, é só chamar. 😊")

        elif status == "aguardando_token_convenio":
            import re as _re_tk
            nums = _re_tk.findall(r'[0-9]{4,10}', msg_recebida)
            palavras_saida = ["ok", "obrigado", "obrigada", "valeu", "voltar", "menu", "cancelar", "tchau", "nao", "não"]
            eh_saida = any(w in msg_limpa for w in palavras_saida) and not nums
            if nums:
                token_val = nums[0]
                from firebase_admin import firestore as _fs_tk
                conv_tk = info.get("convenio", "")
                update_paciente(phone, {
                    "historico_tokens": _fs_tk.ArrayUnion([{"token": token_val, "convenio": conv_tk, "ts": agora_iso}]),
                    "token_convenio": token_val,
                    "status": "menu_veterano",
                    "unread": True
                })
                nome_tk = info.get("title", "Paciente").split()[0]
                responder_texto(phone, f"Token *{token_val}* registrado! ✅\nNossa recepção já recebeu a autorização. 😊")
                import time as _t_tk; _t_tk.sleep(1)
                secoes_tk = [{"title": "Como posso ajudar?", "rows": [{"id": "v1", "title": "🗓️ Meus Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia/Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v5", "title": "🔑 Enviar Token"}, {"id": "v4", "title": "📁 Secretaria"}]}]
                enviar_lista(phone, f"Mais alguma coisa, {nome_tk}?", "Ver Opções", secoes_tk)
            elif eh_saida:
                nome_tk2 = info.get("title", "Paciente").split()[0]
                update_paciente(phone, {"status": "menu_veterano"})
                secoes_tk2 = [{"title": "Como posso ajudar?", "rows": [{"id": "v1", "title": "🗓️ Meus Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia/Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v5", "title": "🔑 Enviar Token"}, {"id": "v4", "title": "📁 Secretaria"}]}]
                enviar_lista(phone, f"Como posso te ajudar, {nome_tk2}? 😊", "Ver Opções", secoes_tk2)
            else:
                responder_texto(phone, "❌ Não reconheci esse código. O token de autorização tem entre 4 e 10 dígitos numéricos.\n\nPode tentar novamente?")

        elif status == "confirmando_servico_nova_guia":
            if msg_recebida in ["ng_sim"] or "Sim" in msg_recebida:
                sv_ng = info.get("nova_guia_servico", "")
                update_paciente(phone, {"status": "cadastrando_queixa_veterano", "servico": sv_ng})
                responder_texto(phone, f"Ótimo! ✅ Vamos renovar a guia para *{sv_ng}*.\n\nPara registrarmos corretamente, me conte brevemente sua situação atual — como está se sentindo e o que motivou a renovação?")
            elif msg_recebida in ["ng_outro"] or "Outro" in msg_recebida:
                update_paciente(phone, {"status": "escolhendo_especialidade", "nova_guia": True})
                secoes_ng2 = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}, {"id": "e8", "title": "⬅️ Voltar ao Menu"}]}]
                enviar_lista(phone, "Qual serviço você deseja renovar a guia?", "Ver Serviços", secoes_ng2)
            elif msg_recebida in ["ng_voltar"] or "Voltar" in msg_recebida:
                update_paciente(phone, {"status": "menu_veterano"})
                nome_ng = info.get("title", "Paciente").split()[0]
                secoes_ng_v = [{"title": "Como posso ajudar?", "rows": [{"id": "v1", "title": "🗓️ Meus Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia/Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v5", "title": "🔑 Enviar Token"}, {"id": "v4", "title": "📁 Secretaria"}]}]
                enviar_lista(phone, f"Voltando ao menu. Como posso ajudar, {nome_ng}?", "Ver Opções", secoes_ng_v)
            else:
                sv_ng = info.get("nova_guia_servico", "")
                un_ng = info.get("nova_guia_unidade", "")
                un_ng_txt = f" — unidade *{un_ng}*" if un_ng else ""
                enviar_botoes(phone, f"Você realiza *{sv_ng}*{un_ng_txt}. Vamos renovar a guia para esse tratamento?",
                    [{"id": "ng_sim", "title": f"✅ Sim, {sv_ng}"}, {"id": "ng_outro", "title": "↔️ Outro serviço"}, {"id": "ng_voltar", "title": "⬅️ Voltar"}])

        elif status == "reagendando_tipo":
            ag_orig = info.get("agenda_sessao_selecionada", info.get("agenda_agendamentos", [{}])[0] if info.get("agenda_agendamentos") else {})
            if msg_recebida in ["rt_horario"] or "horário" in msg_recebida.lower() or "horario" in msg_recebida.lower():
                data_orig = ag_orig.get("data", "")
                local_id_rt = info.get("agenda_local_id") or 2
                proc_id_rt = info.get("agenda_procedimento_id") or 9
                hora_orig = ag_orig.get("hora", "08:00")
                update_paciente(phone, {"status": "escolhendo_horario_reagendamento", "reagendamento_mesmo_dia": True, "reagendamento_hora_preferida": hora_orig})
                responder_texto(phone, "Buscando horários disponíveis no mesmo dia... ⏳")
                if data_orig:
                    slots_dia = consultar_disponibilidade_feegow(local_id_rt, proc_id_rt, data_orig, data_orig)
                    proximos_dia = encontrar_horarios_proximos(slots_dia, hora_orig, qtd=3)
                    if proximos_dia:
                        update_paciente(phone, {"reagendamento_opcoes": [{"data": s["data"], "hora": s["hora"], "label": s["label"]} for s in proximos_dia]})
                        botoes_dia = [{"id": f"slot_{i}", "title": f"{s['hora']}"[:20]} for i, s in enumerate(proximos_dia)]
                        botoes_dia.append({"id": "slot_outro", "title": "Outro horário"})
                        botoes_dia.append({"id": "eh_voltar", "title": "⬅️ Voltar"})
                        data_br_orig = ag_orig.get("data_br", "")
                        enviar_botoes(phone, f"Horários disponíveis em *{data_br_orig}* mais próximos das {hora_orig}:", botoes_dia)
                    else:
                        responder_texto(phone, "Não há outros horários disponíveis nesse dia. Quer tentar outro dia?")
                        update_paciente(phone, {"status": "reagendando_preferencia"})
                        responder_texto(phone, "Qual dia e período você prefere?\n\n_Exemplo: quinta de manhã, semana que vem_")
                else:
                    update_paciente(phone, {"status": "reagendando_preferencia"})
                    responder_texto(phone, "Qual dia e período você prefere?\n\n_Exemplo: quinta de manhã_")
            elif msg_recebida in ["rt_dia"] or "dia" in msg_recebida.lower():
                update_paciente(phone, {"status": "reagendando_preferencia", "reagendamento_mesmo_dia": False})
                responder_texto(phone, "Qual dia e período você prefere?\n\n_Exemplo: quinta de manhã, semana que vem, dia 20..._")
            elif msg_recebida in ["rt_voltar"] or "Voltar" in msg_recebida:
                update_paciente(phone, {"status": "gestao_agenda"})
                _secoes_rtv = [{"title": "O que deseja fazer?", "rows": [{"id": "ga_consultar", "title": "📋 Ver minha agenda"}, {"id": "ga_confirmar", "title": "✅ Confirmar presença"}, {"id": "ga_reagendar", "title": "🔄 Reagendar sessão"}, {"id": "ga_cancelar", "title": "❌ Cancelar sessão"}, {"id": "ga_voltar", "title": "⬅️ Voltar ao Menu"}]}]
                enviar_lista(phone, "Voltando às opções:", "Ver Opções", _secoes_rtv)
            else:
                data_br_rt = ag_orig.get("data_br", "")
                hora_rt = ag_orig.get("hora", "")
                enviar_botoes(phone, f"O que deseja fazer com a sessão de *{data_br_rt} às {hora_rt}*?",
                    [{"id": "rt_horario", "title": "🕐 Mudar horário (mesmo dia)"}, {"id": "rt_dia", "title": "📅 Mudar o dia"}, {"id": "rt_voltar", "title": "⬅️ Voltar"}])

        elif status == "cancelando_tratamento":
            if msg_recebida in ["ct_motivo", "Sim, informar motivo"] or "motivo" in msg_recebida.lower():
                update_paciente(phone, {"status": "informando_motivo_cancelamento_trat"})
                responder_texto(phone, "Entendido. Por favor, me conte o motivo do cancelamento do tratamento:")
            elif msg_recebida in ["ct_direto", "Não, só cancelar"] or "não" in msg_recebida.lower() or "nao" in msg_recebida.lower():
                ag_prox = (info.get("agenda_agendamentos") or [{}])[0]
                ag_id_ct = ag_prox.get("agendamento_id")
                if ag_id_ct:
                    cancelar_agendamento_feegow(ag_id_ct, obs="Cancelamento de tratamento solicitado pelo paciente via robô.")
                update_paciente(phone, {"status": "atendimento_humano", "unread": True, "queixa": "[CANCELAR TRATAMENTO]: Paciente solicitou encerramento do tratamento."})
                responder_texto(phone, "Cancelamento registrado. 💙\n\nNossa equipe responsável entrará em contato para entender melhor sua situação e garantir o melhor cuidado para você.")
            else:
                enviar_botoes(phone, "Deseja informar o motivo do cancelamento?",
                    [{"id": "ct_motivo", "title": "Sim, informar motivo"}, {"id": "ct_direto", "title": "Não, só cancelar"}])

        elif status == "informando_motivo_cancelamento_trat":
            motivo_ct = msg_recebida
            ag_prox_ct = (info.get("agenda_agendamentos") or [{}])[0]
            ag_id_ct2 = ag_prox_ct.get("agendamento_id")
            if ag_id_ct2:
                cancelar_agendamento_feegow(ag_id_ct2, obs=f"Cancelamento de tratamento — motivo: {motivo_ct}")
            update_paciente(phone, {"status": "atendimento_humano", "unread": True, "queixa": f"[CANCELAR TRATAMENTO]: {motivo_ct}"})
            responder_texto(phone, "Obrigado por nos explicar. 💙\n\nNossa equipe responsável entrará em contato para entender melhor sua situação e garantir o melhor cuidado para você.")

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
                nome_s_sec = info.get("title", "Paciente").split()[0]
                secoes = [{"title": "Como posso ajudar?", "rows": [{"id": "v1", "title": "🗓️ Meus Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia/Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v5", "title": "🔑 Enviar Token"}, {"id": "v4", "title": "📁 Secretaria"}]}]
                enviar_lista(phone, f"Voltando ao menu principal. Como posso ajudar, {nome_s_sec}?", "Ver Opções", secoes)
            elif "Exames" in msg_recebida:
                update_paciente(phone, {"status": "enviando_exames"})
                responder_texto(phone, "Perfeito! ✅ Pode enviar os arquivos (PDF ou Foto) agora mesmo. Eu vou anexá-los diretamente ao seu prontuário para o fisioterapeuta analisar.")
            elif "Cancelar Tratamento" in msg_recebida or msg_recebida == "s6":
                update_paciente(phone, {"status": "cancelando_tratamento"})
                enviar_botoes(phone,
                    "Entendo. Cancelar um tratamento é uma decisão importante e nossa equipe quer garantir o melhor para você. 💙\n\n"
                    "Deseja informar o motivo do cancelamento?",
                    [{"id": "ct_motivo", "title": "Sim, informar motivo"}, {"id": "ct_direto", "title": "Não, só cancelar"}])
            else:
                update_paciente(phone, {"status": "atendimento_humano", "queixa": f"[SECRETARIA]: {msg_recebida}"})
                responder_texto(phone, f"A sua solicitação para '{msg_recebida}' foi registada com sucesso. A nossa equipe de secretaria vai assumir o atendimento para providenciar os detalhes. Aguarde um instante! 👩‍💻")

        elif status == "enviando_exames":
            if tem_anexo:
                media_data = salvar_midia_imediata(phone, "exame", media_id) if media_id else {}
                update_fields = {
                    "status": "atendimento_humano",
                    "queixa": "[EXAME ENVIADO]: Paciente enviou exames via robô.",
                    "tem_exame": True,
                }
                update_fields.update(media_data)
                update_paciente(phone, update_fields)
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
                secoes = [{"title": "Como posso ajudar?", "rows": [{"id": "v1", "title": "🗓️ Meus Agendamentos"}, {"id": "v2", "title": "🔄 Nova Guia/Tratamento"}, {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v5", "title": "🔑 Enviar Token"}, {"id": "v4", "title": "📁 Secretaria"}]}]
                enviar_lista(phone, "Voltando ao menu principal. Como posso ajudar?", "Ver Opções", secoes)

            # OPÇÃO "Não encontrei" — paciente descreve em texto livre
            elif "Não encontrei" in msg_recebida or "Nao encontrei" in msg_recebida or msg_recebida == "🔍 Não encontrei":
                update_paciente(phone, {"status": "interpretando_servico_livre"})
                responder_texto(phone,
                    "Sem problemas! 😊\n\n"
                    "Me conte com suas palavras: qual tratamento você está procurando?\n\n"
                    "_Pode descrever o que está sentindo ou o nome do procedimento que você conhece._"
                )

            elif msg_recebida in ["Recovery", "Liberação Miofascial"]:
                # Exceção 2: sempre Particular — vai direto pra queixa (sem perguntar modalidade)
                update_paciente(phone, {"servico": msg_recebida, "modalidade": "Particular", "status": "cadastrando_queixa"})
                responder_texto(phone, f"Ótima escolha! {msg_recebida} é um serviço particular. ✨\n\nPara prepararmos o atendimento, me conte brevemente: o que te trouxe aqui hoje?")

            elif msg_recebida == "Fisio Neurológica":
                # Etapa extra de mobilidade ANTES da queixa
                update_paciente(phone, {"servico": msg_recebida, "status": "triagem_neuro"})
                texto_neuro = "Queremos garantir que sua experiência na Conectifisio seja a mais confortável e segura possível. 😊\n\nPoderia nos contar em qual dessas opções de suporte você se enquadra hoje?\n\n1️⃣ Preciso de auxílio integral (ajuda de outra pessoa para me movimentar).\n2️⃣ Preciso de auxílio parcial (utilizo bengala, andador).\n3️⃣ Tenho autonomia total."
                enviar_botoes(phone, texto_neuro, [{"id": "n1", "title": "1️⃣ Auxílio integral"}, {"id": "n2", "title": "2️⃣ Auxílio parcial"}, {"id": "n3", "title": "3️⃣ Autonomia total"}])

            elif msg_recebida == "Pilates Studio":
                # Exceção 1: Pilates só em São Caetano — fluxo próprio
                if info.get("unit") == "Ipiranga":
                    update_paciente(phone, {"servico": msg_recebida, "status": "transferencia_pilates"})
                    enviar_botoes(phone, "O Pilates Studio é uma modalidade exclusiva da nossa unidade de *São Caetano*. 🧘‍♀️\n\nDeseja transferir o seu atendimento para lá para realizar o Pilates?", [{"id": "tp_sim", "title": "Sim, mudar p/ São Caetano"}, {"id": "tp_nao", "title": "Não, escolher outro"}])
                else:
                    update_paciente(phone, {"servico": msg_recebida, "unit": "São Caetano", "status": "pilates_modalidade"})
                    secoes = [{"title": "Modalidade Pilates", "rows": [{"id": "p_part", "title": "💎 Plano Particular"}, {"id": "p_caixa", "title": "🏦 Saúde Caixa"}, {"id": "p_app", "title": "💪 Wellhub/Totalpass"}, {"id": "p_vol", "title": "⬅️ Voltar"}]}]
                    enviar_lista(phone, "Excelente escolha! 🧘‍♀️ O Pilates é fundamental para a correção postural e fortalecimento.\n\n📍 Atendemos Pilates exclusivamente em *São Caetano*.\n\nComo você pretende realizar as aulas?", "Ver Opções", secoes)

            else:
                # Fisio Ortopédica, Pélvica, Acupuntura → vai DIRETO pra queixa (unidade vem depois da modalidade)
                update_paciente(phone, {"servico": msg_recebida, "status": "cadastrando_queixa"})
                responder_texto(phone, f"Entendido! {msg_recebida} selecionada. ✅\n\nPara garantirmos o conforto e segurança no seu atendimento, me conte brevemente: o que te trouxe à clínica hoje?")

        elif status == "escolhendo_unidade_apos_servico":
            if msg_recebida not in ["São Caetano", "Ipiranga"]:
                enviar_botoes(phone,
                    "Por favor, escolha uma das unidades abaixo:",
                    [{"id": "u1", "title": "São Caetano"}, {"id": "u2", "title": "Ipiranga"}]
                )
                return jsonify({"status": "unidade_invalida"}), 200
            unidade_info = UNIDADES.get(msg_recebida, {})
            update_paciente(phone, {
                "unit": msg_recebida,
                "address": unidade_info.get("endereco"),
                "maps_link": unidade_info.get("maps"),
                "recommendation": unidade_info.get("recomendacao")
            })
            # Unidade escolhida DEPOIS da modalidade → agora vai pro cadastro
            # (veterano pula cadastro e vai pro fluxo de convênio/agendamento conforme modalidade)
            modalidade_atual = info.get("modalidade", "")
            convenio_atual = info.get("convenio", "")
            primeiro_nome = info.get("primeiro_nome", "")

            if is_veteran:
                # Veterano: já tem cadastro — vai direto conforme modalidade
                if convenio_atual and convenio_atual.lower() != "particular":
                    update_paciente(phone, {"status": "num_carteirinha"})
                    responder_texto(phone, f"Unidade {msg_recebida} confirmada! ✅\n\nComo você já é nosso paciente, qual o NÚMERO DA SUA CARTEIRINHA? (Apenas números)")
                else:
                    update_paciente(phone, {"status": "agendando"})
                    enviar_botoes(phone, f"Unidade {msg_recebida} confirmada! ✅\n\nQual o melhor período para você? ☀️⛅", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])
                return jsonify({"status": "unidade_veterano"}), 200

            # Novo paciente: vai pro cadastro de nome completo
            update_paciente(phone, {"status": "cadastrando_nome_completo"})
            if primeiro_nome:
                msg_nome = f"Unidade {msg_recebida} confirmada! ✅\n\nAgora, {primeiro_nome}, para o seu prontuário preciso do seu *nome completo conforme documento* (exigência do registro clínico):"
            else:
                msg_nome = f"Unidade {msg_recebida} confirmada! ✅\n\nPara o seu prontuário, preciso do seu *nome completo conforme documento*:"
            responder_texto(phone, msg_nome)

        elif status == "interpretando_servico_livre":
            # Paciente descreveu em texto livre — usa o modelo v8 para interpretar
            import sys
            print(f"[SERVICO-LIVRE] Texto recebido: {msg_recebida[:80]}", file=sys.stderr)

            # Primeiro: filtros locais antes da IA
            msg_lower_serv = msg_recebida.lower()

            # Detecta massagem → Liberação Miofascial
            if any(w in msg_lower_serv for w in ["massagem", "massoterapia", "massotera"]):
                if "miofascial" not in msg_lower_serv and "liberação" not in msg_lower_serv:
                    update_paciente(phone, {"status": "confirmando_servico_livre", "servico_sugerido": "Liberação Miofascial"})
                    enviar_botoes(phone,
                        "Não realizamos massagem terapêutica tradicional, mas oferecemos a *Liberação Miofascial*! 💆\n\n"
                        "É uma técnica manual eficaz para tensão muscular e dores, realizada por fisioterapeutas especializados.\n\n"
                        "É um serviço *particular*. É isso que você está procurando?",
                        [{"id": "sl_sim", "title": "✅ Sim, é isso"}, {"id": "sl_nao", "title": "❌ Não é isso"}]
                    )
                    return jsonify({"status": "sugerido_liberacao"}), 200

            # Detecta serviços não atendidos
            SERVICOS_NAO_ATENDIDOS_KEYWORDS = {
                "ATM / disfunção temporomandibular": ["atm", "articulação temporomandibular", "temporomandibular", "disfunção tm"],
                "fisioterapia facial / estética facial": ["fisioterapia facial", "fisio facial", "estética facial", "estetica facial"],
                "fisioterapia pediátrica": ["pediátrica", "pediatrica", "infantil"],
                "fisioterapia neuropediátrica": ["neuropediátrica", "neuropediatrica"],
                "drenagem linfática": ["drenagem linfática", "drenagem linfatica", "drenagem"],
                "RPG": [" rpg", "reeducação postural global"],
                "quiropraxia": ["quiropraxia", "quiroprática"],
                "fisioterapia respiratória": ["respiratória", "respiratoria"]
            }
            msg_pad_serv = " " + msg_lower_serv + " "
            servico_nao_atendido = None
            for nome_s, kws in SERVICOS_NAO_ATENDIDOS_KEYWORDS.items():
                if any(kw in msg_pad_serv for kw in kws):
                    servico_nao_atendido = nome_s
                    break

            if servico_nao_atendido:
                responder_texto(phone,
                    f"Infelizmente ainda não atendemos *{servico_nao_atendido}*. 💙\n\n"
                    "Outros serviços que oferecemos:\n"
                    "• Fisioterapia Ortopédica\n"
                    "• Fisioterapia Neurológica\n"
                    "• Fisioterapia Pélvica\n"
                    "• Acupuntura\n"
                    "• Pilates Studio\n"
                    "• Recovery\n"
                    "• Liberação Miofascial\n\n"
                    "Nossa recepção vai te orientar sobre as melhores opções para o seu caso. 😊"
                )
                marcar_precisa_recepcao(phone, f"Solicitou serviço não atendido: {servico_nao_atendido}. Msg: {msg_recebida[:80]}")
                return jsonify({"status": "servico_livre_nao_atendido"}), 200

            # Tenta interpretar com o modelo
            prompt_interpretacao = (
                f"Paciente descreveu: \"{msg_recebida[:200]}\"\n\n"
                "Qual dos serviços abaixo melhor corresponde ao que ele procura?\n"
                "- Fisio Ortopédica\n"
                "- Fisio Neurológica\n"
                "- Fisio Pélvica\n"
                "- Acupuntura\n"
                "- Pilates Studio\n"
                "- Recovery\n"
                "- Liberação Miofascial\n\n"
                "Responda APENAS com o nome exato do serviço da lista (sem explicações). "
                "Se nenhum bater, responda NENHUM."
            )
            try:
                url_oai = "https://api.openai.com/v1/chat/completions"
                headers_oai = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
                payload_oai = {
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt_interpretacao}],
                    "max_tokens": 30,
                    "temperature": 0.0
                }
                res_oai = requests.post(url_oai, json=payload_oai, headers=headers_oai, timeout=10)
                resp_interp = res_oai.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                print(f"[SERVICO-LIVRE] Modelo sugeriu: {resp_interp}", file=sys.stderr)

                SERVICOS_VALIDOS = ["Fisio Ortopédica", "Fisio Neurológica", "Fisio Pélvica", "Acupuntura", "Pilates Studio", "Recovery", "Liberação Miofascial"]
                servico_match = None
                for sv in SERVICOS_VALIDOS:
                    if sv.lower() in resp_interp.lower() or resp_interp.lower() in sv.lower():
                        servico_match = sv
                        break

                if servico_match:
                    update_paciente(phone, {"status": "confirmando_servico_livre", "servico_sugerido": servico_match})
                    enviar_botoes(phone,
                        f"Pelo que você descreveu, *{servico_match}* parece a melhor opção. 😊\n\nÉ isso que você procura?",
                        [{"id": "sl_sim", "title": "✅ Sim, é isso"}, {"id": "sl_nao", "title": "❌ Não é isso"}]
                    )
                else:
                    # Modelo não conseguiu identificar → recepção
                    marcar_precisa_recepcao(phone, f"Serviço não identificado. Descrição livre: {msg_recebida[:120]}")
                    responder_texto(phone,
                        "Obrigado por compartilhar! 💙\n\n"
                        "Para te oferecermos a melhor orientação, vou conectar você com nossa recepção. "
                        "Em instantes alguém da equipe vai te atender pessoalmente. 😊"
                    )
            except Exception as e_int:
                print(f"[SERVICO-LIVRE] Erro IA: {e_int}", file=sys.stderr)
                marcar_precisa_recepcao(phone, f"Erro ao interpretar serviço. Descrição: {msg_recebida[:120]}")
                responder_texto(phone,
                    "Obrigado por compartilhar! 💙\n\n"
                    "Vou conectar você com nossa recepção para te orientar melhor. 😊"
                )

        elif status == "confirmando_servico_livre":
            servico_sug = info.get("servico_sugerido", "")
            if msg_recebida in ["✅ Sim, é isso", "sl_sim"] or "sim" in msg_limpa[:5]:
                # Confirmado: entra no fluxo normal do serviço (queixa primeiro, igual escolhendo_especialidade)
                update_paciente(phone, {"servico": servico_sug, "servico_sugerido": ""})
                if servico_sug in ["Recovery", "Liberação Miofascial"]:
                    # Exceção 2: sempre particular → queixa direto
                    update_paciente(phone, {"modalidade": "Particular", "status": "cadastrando_queixa"})
                    responder_texto(phone, f"Perfeito! {servico_sug} é um serviço particular. ✨\n\nPara prepararmos o atendimento, me conte brevemente: o que te trouxe aqui hoje?")
                elif servico_sug == "Pilates Studio":
                    # Exceção 1: Pilates fluxo próprio
                    update_paciente(phone, {"unit": "São Caetano", "status": "pilates_modalidade"})
                    secoes = [{"title": "Modalidade Pilates", "rows": [{"id": "p_part", "title": "💎 Plano Particular"}, {"id": "p_caixa", "title": "🏦 Saúde Caixa"}, {"id": "p_app", "title": "💪 Wellhub/Totalpass"}, {"id": "p_vol", "title": "⬅️ Voltar"}]}]
                    enviar_lista(phone, "Excelente! 🧘‍♀️\n\n📍 Atendemos Pilates exclusivamente em *São Caetano*.\n\nComo você pretende realizar as aulas?", "Ver Opções", secoes)
                elif servico_sug == "Fisio Neurológica":
                    # Etapa extra de mobilidade
                    update_paciente(phone, {"status": "triagem_neuro"})
                    texto_neuro = "Queremos garantir que sua experiência seja a mais confortável e segura possível. 😊\n\nPoderia nos contar em qual dessas opções de suporte você se enquadra hoje?\n\n1️⃣ Preciso de auxílio integral (ajuda de outra pessoa para me movimentar).\n2️⃣ Preciso de auxílio parcial (utilizo bengala, andador).\n3️⃣ Tenho autonomia total."
                    enviar_botoes(phone, texto_neuro, [{"id": "n1", "title": "1️⃣ Auxílio integral"}, {"id": "n2", "title": "2️⃣ Auxílio parcial"}, {"id": "n3", "title": "3️⃣ Autonomia total"}])
                else:
                    # Ortopédica, Pélvica, Acupuntura → queixa direto
                    update_paciente(phone, {"status": "cadastrando_queixa"})
                    responder_texto(phone, f"Perfeito! {servico_sug} confirmado. ✅\n\nPara prepararmos o atendimento, me conte brevemente: o que te trouxe à clínica hoje?")
            elif msg_recebida in ["❌ Não é isso", "sl_nao"] or "não" in msg_limpa[:5] or "nao" in msg_limpa[:5]:
                # Não é o serviço sugerido → marca recepção
                marcar_precisa_recepcao(phone, f"Não confirmou serviço sugerido ({servico_sug}). Recepção avalia.")
                responder_texto(phone,
                    "Entendido! Vou conectar você com nossa recepção para te orientar melhor sobre as opções disponíveis. 💙"
                )
            else:
                enviar_botoes(phone,
                    f"O serviço *{servico_sug}* é o que você procura?",
                    [{"id": "sl_sim", "title": "✅ Sim, é isso"}, {"id": "sl_nao", "title": "❌ Não é isso"}]
                )

        elif status == "transferencia_pilates":
            if "Sim" in msg_recebida or "mudar" in msg_recebida.lower():
                update_paciente(phone, {"unit": "São Caetano", "status": "pilates_modalidade"})
                secoes = [{"title": "Modalidade Pilates", "rows": [{"id": "p_part", "title": "💎 Plano Particular"}, {"id": "p_caixa", "title": "🏦 Saúde Caixa"}, {"id": "p_app", "title": "💪 Wellhub/Totalpass"}, {"id": "p_vol", "title": "⬅️ Voltar"}]}]
                enviar_lista(phone, "Perfeito! A sua unidade foi alterada para **São Caetano** com sucesso. ✅\n\nAgora, como você pretende realizar as aulas de Pilates?", "Ver Opções", secoes)
            else:
                update_paciente(phone, {"servico": "", "status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}]}]
                enviar_lista(phone, "Sem problemas! Mantemos o seu atendimento na unidade **Ipiranga**. Qual outro serviço você procura hoje?", "Ver Serviços", secoes)

        elif status.startswith("pilates_"):
            if status == "pilates_modalidade":
                if "Voltar" in msg_recebida:
                    update_paciente(phone, {"status": "escolhendo_especialidade"})
                    secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}, {"id": "e5", "title": "Pilates Studio"}, {"id": "e6", "title": "Recovery"}, {"id": "e7", "title": "Liberação Miofascial"}]}]
                    enviar_lista(phone, "Voltando ao menu de especialidades. Qual serviço você procura hoje?", "Ver Serviços", secoes)
                elif "Wellhub" in msg_recebida or "Totalpass" in msg_recebida:
                    # 🛡️ Wellhub/TotalPass: informar planos aceitos antes da escolha
                    # Ver mapa_fluxos.md → Fluxo Wellhub / TotalPass
                    update_paciente(phone, {"modalidade": "Parceria App", "status": "pilates_app"})
                    enviar_botoes(phone, "Atendemos *Wellhub (Golden)* e *TotalPass (TP5)*. Qual o seu?", [{"id": "w1", "title": "Wellhub"}, {"id": "t1", "title": "Totalpass"}])
                elif "Saúde Caixa" in msg_recebida:
                    update_paciente(phone, {"modalidade": "Convênio", "convenio": "Saúde Caixa", "status": "pilates_caixa_foto_pedido"})
                    responder_texto(phone, "Entendido! 🏦 Para o plano Saúde Caixa, envie uma FOTO ou PDF do seu PEDIDO MÉDICO atualizado para seguirmos.")
                elif "Particular" in msg_recebida:
                    # 🛡️ Pilates Particular: pergunta experiência ANTES da aula experimental
                    # Ver mapa_fluxos.md → Experiência com Pilates
                    update_paciente(phone, {"modalidade": "Particular", "status": "pilates_part_experiencia"})
                    enviar_botoes(phone, "Para personalizarmos sua experiência, você já praticou Pilates? 🧘",
                        [{"id": "exp_atual", "title": "☑️ Já pratico"}, {"id": "exp_pass", "title": "🌱 Já pratiquei"}, {"id": "exp_nao", "title": "❌ Nunca pratiquei"}])

            elif status == "pilates_part_experiencia":
                # 🛡️ Captura nível de experiência do lead Pilates Particular
                # Ver mapa_fluxos.md → Experiência com Pilates
                update_paciente(phone, {"experiencia_pilates": msg_recebida, "status": "pilates_part_exp"})
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
                validacao = validar_data_nascimento(msg_recebida)
                if not validacao["valida"]: responder_texto(phone, "❌ Data inválida. Digite uma data real no formato DD/MM/AAAA (ex: 15/05/1980).")
                else:
                    # Salva data NORMALIZADA (4 dígitos) — ver mapa_fluxos.md bug Cleusa
                    update_paciente(phone, {"birthDate": validacao["data"], "status": "pilates_part_email"})
                    responder_texto(phone, "Para completarmos, qual seu melhor E-MAIL?")

            elif status == "pilates_part_email":
                if "@" not in msg_recebida or "." not in msg_recebida: responder_texto(phone, "❌ E-mail inválido. Por favor, digite um e-mail válido.")
                else:
                    # 🛡️ Pilates: cadastra aluno no Feegow + marca precisa_recepcao
                    # Card permanece na coluna Lead Pilates (ver index.html filtros)
                    # Ver mapa_fluxos.md → Fluxo Pilates
                    update_paciente(phone, {"email": msg_recebida})
                    info_atualizado = get_paciente(phone)
                    resultado_feegow = integrar_feegow(phone, info_atualizado)
                    update_data = {"status": "pilates_pendente_recepcao"}
                    if resultado_feegow:
                        update_data.update(resultado_feegow)
                    update_paciente(phone, update_data)
                    marcar_precisa_recepcao(phone, "Atenção lead Pilates")
                    responder_texto(phone, "Tudo pronto! Nossa equipe vai assumir o atendimento agora mesmo para confirmar o seu horário. Aguarde um instante! 👩‍⚕️")

            elif status == "pilates_app":
                # 🛡️ Wellhub/TotalPass: após escolher app, confirma se tem plano elegível
                # Ver mapa_fluxos.md → Fluxo Wellhub / TotalPass
                plano = "Golden" if msg_recebida == "Wellhub" else "TP5"
                update_paciente(phone, {"convenio": msg_recebida, "status": "pilates_app_confirma_plano"})
                enviar_botoes(phone, f"⚠️ Atendemos apenas o plano *{plano}* do {msg_recebida}. Você está com este plano?",
                    [{"id": "pl_sim", "title": f"✅ Tenho {plano}"}, {"id": "pl_nsei", "title": "❓ Não sei"}, {"id": "pl_outro", "title": "❌ Tenho outro"}])

            elif status == "pilates_app_confirma_plano":
                # 🛡️ Roteia para A (segue Golden/TP5), B (cross-sell), C (orientar verificação)
                info_app = info.get("convenio", "Wellhub")
                if "✅" in msg_recebida or "Tenho Golden" in msg_recebida or "Tenho TP5" in msg_recebida:
                    # CAMINHO A — segue fluxo Wellhub/TotalPass normal
                    if info_app == "Wellhub":
                        update_paciente(phone, {"status": "pilates_wellhub_id"})
                        responder_texto(phone, "Perfeito! Por favor, informe o seu Wellhub ID.")
                    else:
                        update_paciente(phone, {"status": "pilates_app_periodo"})
                        enviar_botoes(phone, "Perfeito! ✅ Para agilizarmos o agendamento, qual o melhor período para você?",
                            [{"id": "pe_m", "title": "☀️ Manhã"}, {"id": "pe_t", "title": "⛅ Tarde"}, {"id": "pe_n", "title": "🌙 Noite"}])
                elif "❌" in msg_recebida or "outro" in msg_recebida.lower():
                    # CAMINHO B — cross-sell
                    update_paciente(phone, {"status": "pilates_app_cross_sell"})
                    enviar_botoes(phone,
                        f"Que pena! Atualmente atendemos apenas o plano de cobertura completa pelo {info_app}.\n\n"
                        "Mas temos o nosso *Plano Particular* que pode te interessar. Gostaria de conhecer?",
                        [{"id": "cs_sim", "title": "Sim, quero conhecer"}, {"id": "cs_nao", "title": "Agora não"}])
                elif "❓" in msg_recebida or "não sei" in msg_recebida.lower() or "nao sei" in msg_recebida.lower():
                    # CAMINHO C — orientar verificação
                    update_paciente(phone, {"status": "pilates_app_orientar"})
                    enviar_botoes(phone,
                        f"Sem problemas! 😊\n\nPara verificar, abra o app do {info_app} em *'Minha conta'* — o plano aparece logo abaixo do seu nome.\n\n"
                        "Enquanto verifica, posso te orientar sobre nosso *Plano Particular*, caso seu plano não cubra. Deseja conhecer?",
                        [{"id": "or_sim", "title": "Sim, quero conhecer"}, {"id": "or_nao", "title": "Vou verificar e volto"}])
                else:
                    enviar_botoes(phone, "Por favor, escolha uma das opções:",
                        [{"id": "pl_sim", "title": "✅ Tenho o plano"}, {"id": "pl_nsei", "title": "❓ Não sei"}, {"id": "pl_outro", "title": "❌ Tenho outro"}])

            elif status == "pilates_app_cross_sell":
                # 🛡️ Caminho B: paciente tinha plano fora de cobertura
                info_app = info.get("convenio", "Wellhub")
                if "Sim" in msg_recebida:
                    # Migra para fluxo Particular
                    update_paciente(phone, {"modalidade": "Particular", "convenio": "", "status": "pilates_part_experiencia"})
                    enviar_botoes(phone, "Ótima decisão! ✨\n\nPara personalizarmos sua experiência, você já praticou Pilates? 🧘",
                        [{"id": "exp_atual", "title": "☑️ Já pratico"}, {"id": "exp_pass", "title": "🌱 Já pratiquei"}, {"id": "exp_nao", "title": "❌ Nunca pratiquei"}])
                else:
                    # Lead morno
                    update_paciente(phone, {"status": "pilates_lead_morno",
                                            "motivo_lead_morno": f"Plano {info_app} fora da cobertura"})
                    marcar_precisa_recepcao(phone, "Recuperar lead")
                    responder_texto(phone, "Tudo certo! Volte quando quiser, será um prazer atender. 😊")

            elif status == "pilates_app_orientar":
                # 🛡️ Caminho C: paciente não sabia o plano
                info_app = info.get("convenio", "Wellhub")
                if "Sim" in msg_recebida:
                    update_paciente(phone, {"modalidade": "Particular", "convenio": "", "status": "pilates_part_experiencia"})
                    enviar_botoes(phone, "Ótima decisão! ✨\n\nPara personalizarmos sua experiência, você já praticou Pilates? 🧘",
                        [{"id": "exp_atual", "title": "☑️ Já pratico"}, {"id": "exp_pass", "title": "🌱 Já pratiquei"}, {"id": "exp_nao", "title": "❌ Nunca pratiquei"}])
                else:
                    update_paciente(phone, {"status": "pilates_lead_morno",
                                            "motivo_lead_morno": f"Não sabia o plano {info_app}"})
                    marcar_precisa_recepcao(phone, "Recuperar lead")
                    responder_texto(phone, "Tudo certo! Volte quando quiser, será um prazer atender. 😊")

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
                validacao = validar_data_nascimento(msg_recebida)
                if not validacao["valida"]: responder_texto(phone, "❌ Data inválida. Digite uma data real no formato DD/MM/AAAA (ex: 15/05/1980).")
                else:
                    # Salva data NORMALIZADA (4 dígitos) — ver mapa_fluxos.md bug Cleusa
                    update_paciente(phone, {"birthDate": validacao["data"], "status": "pilates_app_email"})
                    responder_texto(phone, "Para completarmos o registro, qual seu melhor E-MAIL?")

            elif status == "pilates_app_email":
                if "@" not in msg_recebida or "." not in msg_recebida: responder_texto(phone, "❌ E-mail inválido. Por favor, digite um e-mail válido.")
                else:
                    # 🛡️ Wellhub/TotalPass (Golden/TP5): cadastra no Feegow + recepção transfere pro NextFit
                    # Ver mapa_fluxos.md → Fluxo Wellhub / TotalPass
                    update_paciente(phone, {"email": msg_recebida})
                    info_atualizado = get_paciente(phone)
                    resultado_feegow = integrar_feegow(phone, info_atualizado)
                    update_data = {"status": "pilates_pendente_recepcao"}
                    if resultado_feegow:
                        update_data.update(resultado_feegow)
                    update_paciente(phone, update_data)
                    marcar_precisa_recepcao(phone, "Transferir NextFit")

                    nome_app = info_atualizado.get("convenio", "do seu aplicativo")
                    responder_texto(phone,
                        "Cadastro concluído! 🎉\n\n"
                        f"📲 *O agendamento das aulas é feito direto pelo app do {nome_app}*. "
                        "Nossa unidade aparece como *Conectifisio São Caetano* — "
                        "escolha o horário disponível direto por lá.\n\n"
                        "Qualquer dúvida, nossa equipe está à disposição. 😊"
                    )

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
                validacao = validar_data_nascimento(msg_recebida)
                if not validacao["valida"]: responder_texto(phone, "❌ Data inválida. Digite no formato DD/MM/AAAA (ex: 15/05/1980).")
                else:
                    # Salva data NORMALIZADA (4 dígitos) — ver mapa_fluxos.md bug Cleusa
                    update_paciente(phone, {"birthDate": validacao["data"], "status": "pilates_caixa_email"})
                    responder_texto(phone, "Ótimo! Qual seu melhor E-MAIL?")

            elif status == "pilates_caixa_email":
                if "@" not in msg_recebida or "." not in msg_recebida: responder_texto(phone, "❌ E-mail inválido. Por favor, digite um e-mail válido.")
                else:
                    # 🛡️ Pilates: cadastra aluno no Feegow + marca precisa_recepcao
                    # Ver mapa_fluxos.md → Fluxo Pilates
                    update_paciente(phone, {"email": msg_recebida})
                    info_atualizado = get_paciente(phone)
                    resultado_feegow = integrar_feegow(phone, info_atualizado)
                    update_data = {"status": "pilates_pendente_recepcao"}
                    if resultado_feegow:
                        update_data.update(resultado_feegow)
                    update_paciente(phone, update_data)
                    marcar_precisa_recepcao(phone, "Atenção lead Pilates")
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
            # VALIDAÇÃO: rejeita respostas que não são queixa (paciente clicou achando que era avanço)
            msg_strip_queixa = msg_recebida.strip().lower()
            palavras_avanco = ["próximo", "proximo", "next", "avançar", "avancar", "ok",
                              "sim", "tá", "ta", "blz", "beleza", "continuar", "vai",
                              "manda", "pode mandar", "?", "."]
            eh_resposta_curta_invalida = (
                len(msg_strip_queixa) <= 12 and
                (msg_strip_queixa in palavras_avanco or msg_strip_queixa.replace("!", "").replace(".", "").replace("?", "") in palavras_avanco)
            )
            if eh_resposta_curta_invalida:
                responder_texto(phone,
                    "Por favor, me conte um pouquinho mais sobre o que está sentindo ou o motivo da consulta. 😊\n\n"
                    "_Pode descrever em poucas palavras: dor, lesão, recuperação, etc._"
                )
                return jsonify({"status": "queixa_invalida"}), 200

            acolhimento = chamar_ia_custom(msg_recebida) or "Compreendo perfeitamente, e saiba que estamos aqui para cuidar de você da melhor forma."
            if servico in ["Recovery", "Liberação Miofascial"]:
                # Exceção 2: sempre particular, sem perguntar modalidade → vai pra unidade
                if is_veteran:
                    update_paciente(phone, {"queixa": msg_recebida, "queixa_ia": acolhimento, "status": "agendando"})
                    enviar_botoes(phone, f"{acolhimento}\n\nComo você já é nosso paciente, vamos direto para a agenda. Qual o melhor período para você? ☀️⛅", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])
                else:
                    update_paciente(phone, {"queixa": msg_recebida, "queixa_ia": acolhimento, "status": "escolhendo_unidade_apos_servico"})
                    enviar_botoes(phone,
                        f"{acolhimento}\n\nEm qual unidade você prefere ser atendido?",
                        [{"id": "u1", "title": "São Caetano"}, {"id": "u2", "title": "Ipiranga"}]
                    )
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
                    # Novo + Particular: pergunta UNIDADE antes do cadastro
                    update_paciente(phone, {"modalidade": "Particular", "status": "escolhendo_unidade_apos_servico"})
                    enviar_botoes(phone,
                        "Perfeito, atendimento Particular! ✨\n\nEm qual unidade você prefere ser atendido?",
                        [{"id": "u1", "title": "São Caetano"}, {"id": "u2", "title": "Ipiranga"}]
                    )

        elif status == "nome_convenio":
            convenio_selecionado = msg_recebida
            CONVENIOS_VALIDOS = ["Saúde Petrobras", "Mediservice", "Cassi", "Geap Saúde", "Amil",
                                  "Bradesco Saúde", "Porto Seguro Saúde", "Prevent Senior", "Saúde Caixa"]
            CONVENIOS_NAO_ATENDIDOS = ["Unimed", "Sulamerica", "SulAmérica", "Hapvida", "NotreDame", "Notre Dame", "Golden Cross", "Apivida"]
            if any(c.lower() in convenio_selecionado.lower() for c in CONVENIOS_NAO_ATENDIDOS):
                responder_texto(phone, f"Infelizmente não atendemos o convênio {convenio_selecionado}. 😊 Os convênios que aceitamos são: Amil, Bradesco Saúde, Porto Seguro, Prevent Senior, Saúde Caixa, Saúde Petrobras, Mediservice, Cassi e Geap Saúde. Gostaria de verificar outra opção ou realizar o atendimento particular?")
            elif convenio_selecionado not in CONVENIOS_VALIDOS:
                secoes = [{"title": "Convênios Aceitos", "rows": [{"id": "c1", "title": "Saúde Petrobras"}, {"id": "c2", "title": "Mediservice"}, {"id": "c3", "title": "Cassi"}, {"id": "c4", "title": "Geap Saúde"}, {"id": "c5", "title": "Amil"}, {"id": "c6", "title": "Bradesco Saúde"}, {"id": "c7", "title": "Porto Seguro Saúde"}, {"id": "c8", "title": "Prevent Senior"}, {"id": "c9", "title": "Saúde Caixa"}]}]
                enviar_lista(phone, "❌ Por favor, selecione um dos convênios disponíveis na lista abaixo:", "Ver Convênios", secoes)
            elif not verificar_cobertura(convenio_selecionado, servico):
                update_paciente(phone, {"convenio": convenio_selecionado, "status": "cobertura_recusada"})
                enviar_botoes(phone, f"⚠️ O seu plano *{convenio_selecionado}* não possui cobertura direta para *{servico}* na nossa clínica.\n\nNo entanto, você pode realizar o atendimento Particular para solicitar reembolso. Deseja seguir no particular?", [{"id": "part", "title": "Seguir Particular"}, {"id": "out", "title": "Escolher outro"}])
            else:
                if is_veteran:
                    update_paciente(phone, {"convenio": convenio_selecionado, "status": "num_carteirinha"})
                    responder_texto(phone, f"Anotado: {convenio_selecionado}! ✅\n\nComo você já é nosso paciente, pulei o preenchimento de CPF e E-mail! Para atualizarmos o seu cadastro, qual o NÚMERO DA SUA NOVA CARTEIRINHA? (Apenas números)")
                else:
                    # Novo + Convênio: pergunta UNIDADE antes do cadastro
                    update_paciente(phone, {"convenio": convenio_selecionado, "status": "escolhendo_unidade_apos_servico"})
                    enviar_botoes(phone,
                        f"Anotado: {convenio_selecionado}! ✅\n\nEm qual unidade você prefere ser atendido?",
                        [{"id": "u1", "title": "São Caetano"}, {"id": "u2", "title": "Ipiranga"}]
                    )

        elif status == "cobertura_recusada":
            if "Particular" in msg_recebida:
                if is_veteran:
                    update_paciente(phone, {"modalidade": "Particular", "status": "agendando"})
                    enviar_botoes(phone, "Perfeito! Mudamos para Particular. Qual o melhor período para você? ☀️ ⛅", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])
                else:
                    # Novo: ainda não escolheu unidade (recusa veio antes) → pergunta unidade
                    update_paciente(phone, {"modalidade": "Particular", "convenio": "", "status": "escolhendo_unidade_apos_servico"})
                    enviar_botoes(phone,
                        "Perfeito! Mudamos para Particular. ✨\n\nEm qual unidade você prefere ser atendido?",
                        [{"id": "u1", "title": "São Caetano"}, {"id": "u2", "title": "Ipiranga"}]
                    )
            else:
                update_paciente(phone, {"status": "escolhendo_especialidade"})
                secoes = [{"title": "Nossos Serviços", "rows": [{"id": "e1", "title": "Fisio Ortopédica"}, {"id": "e2", "title": "Fisio Neurológica"}, {"id": "e3", "title": "Fisio Pélvica"}, {"id": "e4", "title": "Acupuntura"}]}]
                enviar_lista(phone, "Sem problemas! Qual outro serviço você gostaria de buscar?", "Ver Serviços", secoes)

        elif status == "cadastrando_nome_completo":
            partes_nc = [p for p in msg_limpa.split() if len(p) >= 2]
            if len(partes_nc) < 2 or msg_recebida.isdigit(): responder_texto(phone, "❌ Por favor, digite seu NOME E SOBRENOME completos:")
            else:
                update_paciente(phone, {"title": msg_recebida, "status": "cpf"})
                responder_texto(phone, "Nome registrado! ✅ Agora, para validarmos o seu registro com segurança junto ao sistema, digite o seu CPF (apenas os 11 números):")

        elif status == "cpf":
            cpf_limpo = re.sub(r'\D', '', msg_recebida)
            if not validar_cpf(cpf_limpo):
                responder_texto(phone, "❌ CPF inválido. Por favor, verifique os números e digite novamente:")
            else:
                conv_atual = info.get("convenio", "")

                # ==========================================
                # PORTO SEGURO — ELEGIBILIDADE SILENCIOSA
                #
                # MUDANÇA ARQUITETURAL (30/04/2026):
                # 1. Salva o CPF e avança IMEDIATAMENTE para data de nascimento
                # 2. Thread de elegibilidade roda em paralelo (silenciosa)
                # 3. Resultado vai APENAS para o card no Kanban (badge verde/vermelho/amarelo)
                # 4. Paciente nunca sabe se conseguimos ou não verificar a elegibilidade
                # ==========================================
                if any(x in conv_atual for x in ["Porto Seguro", "Itaú"]) and PORTO_SEGURO_SENHA:
                    # Salva CPF e avança o estado ANTES de disparar a thread
                    update_paciente(phone, {
                        "cpf": cpf_limpo,
                        "status": "data_nascimento",
                        "porto_elegibilidade_badge": "amarelo",  # Badge inicial: aguardando verificação
                        "porto_verificando": True
                    })
                    # Resposta natural — sem mencionar elegibilidade
                    responder_texto(phone, "CPF recebido! ✅ Para completarmos sua ficha clínica, qual sua data de nascimento? (Ex: 15/05/1980)")
                    # Thread dispara em background — não bloqueia o fluxo
                    iniciar_verificacao_porto_background(phone, cpf_limpo, numero_id)
                    return jsonify({"status": "cpf_recebido_porto_verificando"}), 200

                # Outros convênios / Particular — fluxo padrão
                busca = buscar_feegow_por_cpf(cpf_limpo)
                if busca:
                    if modalidade == "Particular":
                        update_paciente(phone, {"cpf": cpf_limpo, "title": busca['nome'], "feegow_id": busca['id'], "status": "agendando"})
                        enviar_botoes(phone, f"Reconheci seu cadastro, {busca['nome']}! ✨\n\nPulei as etapas de e-mail e nascimento. Qual o melhor período para você?", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])
                    else:
                        update_paciente(phone, {"cpf": cpf_limpo, "title": busca['nome'], "feegow_id": busca['id'], "status": "num_carteirinha"})
                        responder_texto(phone, f"Reconheci seu cadastro, {busca['nome']}! ✨\n\nPulei as etapas de e-mail e nascimento. Para atualizarmos o seu cadastro, qual o NÚMERO DA SUA CARTEIRINHA? (Apenas números)")
                else:
                    update_paciente(phone, {"cpf": cpf_limpo, "status": "data_nascimento"})
                    responder_texto(phone, "Recebido! ✅ Para completarmos sua ficha clínica, qual sua data de nascimento? (Ex: 15/05/1980)")

        elif status == "data_nascimento":
            validacao = validar_data_nascimento(msg_recebida)
            if not validacao["valida"]:
                responder_texto(phone, "❌ Data de nascimento inválida. Digite uma data real no formato DD/MM/AAAA (ex: 15/05/1980).")
            elif validacao["menor_12"]:
                # Permite ao paciente confirmar se foi engano antes de bloquear
                # Salva a data NORMALIZADA (4 dígitos) — ver mapa_fluxos.md bug Cleusa
                update_paciente(phone, {"birthDate": validacao["data"], "status": "confirmando_menor_12"})
                enviar_botoes(phone,
                    f"⚠️ A data *{validacao['data']}* indica que o paciente tem menos de 12 anos.\n\n"
                    "Foi um engano de digitação?",
                    [{"id": "menor_engano", "title": "Sim, foi engano"}, {"id": "menor_correto", "title": "Não, é correta"}]
                )
            else:
                # Salva a data NORMALIZADA (4 dígitos) — ver mapa_fluxos.md bug Cleusa
                update_paciente(phone, {"birthDate": validacao["data"], "status": "coletando_email"})
                responder_texto(phone, "Ótimo! Para finalizar seu cadastro, qual seu melhor E-MAIL?")

        elif status == "confirmando_menor_12":
            if "engano" in msg_limpa or msg_recebida == "Sim, foi engano":
                update_paciente(phone, {"status": "data_nascimento", "birthDate": ""})
                responder_texto(phone, "Sem problemas! 😊 Pode me informar a data de nascimento correta? (Ex: 15/05/1980)")
            elif "correta" in msg_limpa or msg_recebida == "Não, é correta":
                update_paciente(phone, {"status": "finalizado", "robo_ligado": False})
                responder_texto(phone,
                    "⚠️ Infelizmente não possuímos especialidade pediátrica em nossas unidades. "
                    "Não poderemos realizar este agendamento.\n\n"
                    "Recomendamos a busca por profissionais especializados na área infantil. "
                    "Obrigado pela compreensão! 🙏"
                )
            else:
                enviar_botoes(phone,
                    "Por favor, confirme:",
                    [{"id": "menor_engano", "title": "Sim, foi engano"}, {"id": "menor_correto", "title": "Não, é correta"}]
                )

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
            _espera = ["momento", "aguarda", "agora não", "depois", "vou pegar", "não tenho", "nao tenho", "vou ver", "preciso ver"]
            if any(e in msg_limpa for e in _espera) and len(num_limpo) < 4:
                update_paciente(phone, {"status": "pausado", "unread": True, "ultima_mensagem_paciente": msg_recebida})
                responder_texto(phone, "Sem problema! 😊 Quando tiver o número da carteirinha em mãos, é só me enviar aqui que continuamos.")
            elif len(num_limpo) < 4:
                responder_texto(phone, "❌ Número de carteirinha inválido. Por favor, digite apenas os números da sua carteirinha (mínimo 4 dígitos):")
            else:
                update_paciente(phone, {"numCarteirinha": num_limpo, "status": "foto_carteirinha"})
                responder_texto(phone, "Anotado! ✅ Agora a parte documental:\n\nEnvie uma FOTO NÍTIDA da sua carteirinha (use o ícone de clipe ou câmera do WhatsApp).")

        elif status == "foto_carteirinha":
            if not tem_anexo: responder_texto(phone, "❌ Não recebi a imagem. Por favor, envie a foto da sua carteirinha.")
            else:
                media_data = salvar_midia_imediata(phone, "carteirinha", media_id) if media_id else {}
                update_fields = {
                    "status": "foto_pedido_medico",
                    "tem_foto_carteirinha": True,
                }
                update_fields.update(media_data)
                update_paciente(phone, update_fields)
                responder_texto(phone, "Foto recebida! ✅\n\nAgora, envie a FOTO DO SEU PEDIDO MÉDICO.")

        elif status == "foto_pedido_medico":
            _links_pedido = ["memed.com.br", "drconnect", "bula.fiocruz", "receita", "http", "https"]
            _tem_link_pedido = any(lp in msg_limpa for lp in _links_pedido) and not tem_anexo
            if _tem_link_pedido:
                update_paciente(phone, {"status": "agendando", "tem_foto_pedido": True, "pedido_link": msg_recebida})
                enviar_botoes(phone, "Pedido médico digital recebido! 🎉\n\nQual o melhor período para verificarmos a sua vaga?", [{"id": "t1", "title": "Manhã"}, {"id": "t2", "title": "Tarde"}])
            elif not tem_anexo: responder_texto(phone, "❌ Por favor, envie a foto do seu Pedido Médico.")
            else:
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
                if not modalidade and (info.get("convenio") or info.get("carteirinha_media_id")):
                    modalidade = "Convênio"
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

        paciente_info = get_paciente(phone) or {}
        pid = paciente_info.get("numero_id")

        if not pid:
            unidade = str(paciente_info.get("unit", "")).lower()
            if "ipiranga" in unidade:
                pid = os.environ.get("PHONE_NUMBER_ID_IPIRANGA", "947053595167511")
            else:
                pid = os.environ.get("PHONE_NUMBER_ID", "1059746060556447")

        url = f"https://graph.facebook.com/v19.0/{pid}/messages"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}

        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        
        session = requests.Session()
        retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[ 500, 502, 503, 504 ])
        session.mount('https://', HTTPAdapter(max_retries=retries))

        if file_b64:
            b64_data = file_b64.split(",")[1] if "," in file_b64 else file_b64
            file_bytes = base64.b64decode(b64_data)
            url_media = f"https://graph.facebook.com/v19.0/{pid}/media"
            
            res_m = session.post(url_media, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, files={'file': (file_name, file_bytes, mime_type)}, data={'messaging_product': 'whatsapp'}, timeout=15)
            m_id = res_m.json().get("id")
            
            if not m_id:
                return jsonify({"success": False, "error": f"Falha no upload da imagem: {res_m.text}"}), 500
                
            msg_type = "image" if "image" in mime_type else "document"
            payload = {"messaging_product": "whatsapp", "to": phone, "type": msg_type, msg_type: {"id": m_id}}
            if message_text: payload[msg_type]["caption"] = message_text
            
            res = session.post(url, json=payload, headers=headers, timeout=15)
            
        else:
            payload = {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": message_text}}
            res = session.post(url, json=payload, headers=headers, timeout=15)

        if res.status_code == 200:
            registrar_historico(phone, "clinica", "texto" if not file_b64 else "anexo", message_text or "[Arquivo]")
            update_paciente(phone, {"status": "pausado", "unread": False, "robo_ligado": False})
            return jsonify({"success": True}), 200
        else:
            import sys
            print(f"[ERRO-META] {res.text}", file=sys.stderr)
            return jsonify({"success": False, "error": f"WhatsApp recusou: {res.text}"}), 500

    except Exception as e:
        import traceback, sys
        print(traceback.format_exc(), file=sys.stderr)
        return jsonify({"success": False, "error": f"Erro de conexão/servidor: {str(e)}"}), 500

@app.route("/api/diagnostico/slots", methods=["GET"])
def diagnostico_slots():
    """Endpoint de diagnóstico: consulta disponibilidade para cada local_id
    e retorna qual local_id a API retorna nos slots.
    
    Uso:
    GET /api/diagnostico/slots?token=conectifisio_followup_2025
    GET /api/diagnostico/slots?token=...&local_id=5  (testa só um)
    GET /api/diagnostico/slots?token=...&proc_id=42  (procedimento específico)
    """
    import sys
    token = request.args.get("token", "")
    if token != os.environ.get("FOLLOWUP_SECRET", "conectifisio_followup_2025"):
        return jsonify({"error": "Unauthorized"}), 401

    local_id_param = request.args.get("local_id")
    proc_id_param = int(request.args.get("proc_id", 42))

    local_ids_testar = [int(local_id_param)] if local_id_param else [2, 3, 4, 5, 6, 7, 8]

    hoje = datetime.now()
    data_ini = hoje.strftime('%Y-%m-%d')
    data_fim = (hoje + timedelta(days=14)).strftime('%Y-%m-%d')

    resultado = []
    for lid in local_ids_testar:
        slots = consultar_disponibilidade_feegow(lid, proc_id_param, data_ini, data_fim)
        # Agrupa por local_id dos slots retornados
        slot_local_ids = {}
        for s in slots:
            slid = s.get("local_id", "?")
            if slid not in slot_local_ids:
                slot_local_ids[slid] = []
            slot_local_ids[slid].append(f"{s['data']} {s['hora']}")

        info_map = _LOCAL_ID_MAP.get(lid, {})
        resultado.append({
            "agendamento_local_id": lid,
            "unidade": info_map.get("unidade", "?"),
            "servico": info_map.get("servico", "?"),
            "total_slots": len(slots),
            "slots_por_local_id": {
                str(k): {
                    "quantidade": len(v),
                    "horarios": sorted(set(h.split(" ")[1] for h in v)),
                    "datas": sorted(set(h.split(" ")[0] for h in v)),
                    "primeiros": v[:6]
                } for k, v in slot_local_ids.items()
            },
            "mapeamento_atual": _LOCAL_ID_SLOTS.get(lid, [lid])
        })
        print(f"[DIAG-SLOTS] local_id={lid} → {len(slots)} slots em local_ids={list(slot_local_ids.keys())}", file=sys.stderr)

    return jsonify({
        "data_consulta": hoje.strftime('%d/%m/%Y'),
        "procedimento_id": proc_id_param,
        "janela": f"{data_ini} a {data_fim}",
        "resultados": resultado
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
