import requests
import os
from datetime import datetime

# =====================================================================
# Configurações da API Feegow
# =====================================================================
BASE_URL = "https://api.feegow.com/v1/api"
API_TOKEN = os.environ.get("FEEGOW_TOKEN")

# Cabeçalhos atualizados com a recomendação de segurança da Feegow
HEADERS = {
    "x-access-token": API_TOKEN,
    "token": API_TOKEN,
    "Content-Type": "application/json",
    "User-Agent": "Integracao-Conectifisio/1.0 (contato@ictusfisioterapia.com.br)"
}

def buscar_agendamento_hoje_por_cpf(cpf_limpo):
    """
    Passo 1: Busca o ID do paciente pelo CPF.
    Passo 2: Busca o agendamento de hoje usando o ID do paciente.
    """
    # ---------------------------------------------------------
    # PASSO 1: Encontrar o paciente_id usando o CPF
    # ---------------------------------------------------------
    url_paciente = f"{BASE_URL}/patient/list"
    
    # Parâmetros enviados na URL (conforme a doc GET Listar pacientes)
    params_paciente = {"cpf": cpf_limpo}
    
    try:
        response_paciente = requests.get(url_paciente, headers=HEADERS, params=params_paciente)
        response_paciente.raise_for_status()
        dados_paciente = response_paciente.json()
        
        # Verifica se retornou sucesso e se a lista de pacientes não está vazia
        if not dados_paciente.get("success") or not dados_paciente.get("content"):
            return {"erro": "Paciente não encontrado no sistema."}
            
        # Pega o primeiro paciente da lista
        paciente = dados_paciente["content"][0]
        # A chave exata do ID pode variar, garantimos pegando as mais comuns:
        paciente_id = paciente.get("id") or paciente.get("patient_id")
        nome_paciente = paciente.get("nome") or paciente.get("name", "Paciente")

        # ---------------------------------------------------------
        # PASSO 2: Buscar agendamentos desse paciente para a data de hoje
        # ---------------------------------------------------------
        hoje = datetime.now().strftime("%Y-%m-%d")
        url_agenda = f"{BASE_URL}/appoints/search"
        
        # Parâmetros conforme o print que você enviou
        params_agenda = {
            "data_start": hoje,
            "data_end": hoje,
            "paciente_id": paciente_id
        }
        
        response_agenda = requests.get(url_agenda, headers=HEADERS, params=params_agenda)
        response_agenda.raise_for_status()
        dados_agenda = response_agenda.json()
        
        if not dados_agenda.get("success") or not dados_agenda.get("content"):
            return {"erro": "Nenhum agendamento localizado para você hoje."}
            
        # Pegamos a primeira consulta do dia (caso o paciente tenha mais de uma)
        agendamento = dados_agenda["content"][0]
        
        return {
            "sucesso": True,
            "agendamento_id": agendamento["agendamento_id"],
            "paciente": nome_paciente,
            "horario": agendamento["horario"]
        }

    except requests.exceptions.RequestException as e:
        # Captura o ID do Cloudflare para mandar para o suporte se precisar
        cf_ray = e.response.headers.get('cf-ray') if e.response else "N/A"
        print(f"Erro Feegow (CF-RAY: {cf_ray}): {e}")
        return {"erro": "Falha de comunicação com o servidor da clínica."}

def confirmar_checkin_totem(agendamento_id):
    """
    PASSO 3: Altera o status do agendamento para 'Aguardando' (ID 4).
    """
    url_atualizar_status = f"{BASE_URL}/appoints/statusUpdate"
    
    payload_status = {
        "AgendamentoID": agendamento_id,
        "StatusID": "4", 
        "Obs": "Paciente confirmou chegada pelo Totem de Autoatendimento."
    }
    
    try:
        response = requests.post(url_atualizar_status, json=payload_status, headers=HEADERS)
        response.raise_for_status()
        dados = response.json()
        
        if dados.get("success"):
            return {"sucesso": True, "mensagem": "Status atualizado com sucesso."}
        else:
            return {"sucesso": False, "erro": "Não foi possível atualizar o status na recepção."}
            
    except requests.exceptions.RequestException as e:
        # Captura o ID do Cloudflare aqui também
        cf_ray = e.response.headers.get('cf-ray') if e.response else "N/A"
        print(f"Erro na requisição de atualização (CF-RAY: {cf_ray}): {e}")
        return {"sucesso": False, "erro": "Erro ao confirmar a presença no sistema."}


# =====================================================================
# FUNÇÕES EXCLUSIVAS DO TOTEM DE AUTOATENDIMENTO (ONDAS DE CHOQUE)
# =====================================================================

def buscar_agendamento_hoje_por_cpf(cpf_limpo):
    url_paciente = f"{BASE_URL}/patient/list"
    try:
        response_paciente = requests.get(url_paciente, headers=HEADERS, params={"cpf": cpf_limpo})
        response_paciente.raise_for_status()
        dados_paciente = response_paciente.json()
        
        if not dados_paciente.get("success") or not dados_paciente.get("content"):
            return {"erro": "CPF não localizado. Por favor, dirija-se à recepção."}
            
        paciente = dados_paciente["content"][0]
        paciente_id = paciente.get("id") or paciente.get("patient_id")
        nome_paciente = paciente.get("nome") or paciente.get("name", "Paciente")

        hoje = datetime.now().strftime("%Y-%m-%d")
        url_agenda = f"{BASE_URL}/appoints/search"
        
        params_agenda = {
            "data_start": hoje,
            "data_end": hoje,
            "paciente_id": paciente_id,
            "resource_id": "2"  
        }
        
        response_agenda = requests.get(url_agenda, headers=HEADERS, params=params_agenda)
        response_agenda.raise_for_status()
        dados_agenda = response_agenda.json()
        
        if not dados_agenda.get("success") or not dados_agenda.get("content"):
            primeiro_nome = nome_paciente.split()[0]
            return {"erro": f"Olá {primeiro_nome}, não localizamos sessão de Ondas de Choque para hoje."}
            
        agendamento = dados_agenda["content"][0]
        convenio_nome = agendamento.get("convenio", "Particular")
        
        return {
            "sucesso": True,
            "agendamento_id": agendamento["agendamento_id"],
            "paciente": nome_paciente,
            "convenio": convenio_nome,
            "horario": agendamento.get("horario") or agendamento.get("hora", "00:00")
        }

    except requests.exceptions.RequestException as e:
        cf_ray = e.response.headers.get('cf-ray') if e.response else "N/A"
        return {"erro": f"Falha de comunicação com o sistema (CF-RAY: {cf_ray})."}

def confirmar_checkin_totem(agendamento_id):
    url_atualizar_status = f"{BASE_URL}/appoints/statusUpdate"
    payload_status = {
        "AgendamentoID": agendamento_id,
        "StatusID": "4", 
        "Obs": "Check-in via Totem Autoatendimento (Equipamento 2)"
    }
    
    try:
        response = requests.post(url_atualizar_status, json=payload_status, headers=HEADERS)
        response.raise_for_status()
        dados = response.json()
        
        if dados.get("success"):
            return {"sucesso": True, "mensagem": "Status atualizado."}
        else:
            return {"sucesso": False, "erro": "Falha ao atualizar recepção."}
            
    except requests.exceptions.RequestException:
        return {"sucesso": False, "erro": "Erro de conexão ao confirmar presença."}
