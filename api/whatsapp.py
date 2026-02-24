# 🧪 SCRIPT DE TESTE FEEGOW API
# Este script usa a mesma lógica do seu formulário Wix, mas em Python.
# Objetivo: Validar se o Token está correto e se conseguimos criar um paciente.

import requests
import json

# ⚠️ COLOQUE O SEU TOKEN AQUI (O mesmo que usa no Wix)
FEEGOW_TOKEN = "COLE_SEU_TOKEN_AQUI"

BASE_URL = "https://api.feegow.com/v1/api"
HEADERS = {
    "Content-Type": "application/json",
    "x-access-token": FEEGOW_TOKEN
}

def limpar_numeros(texto):
    return ''.join(filter(str.isdigit, str(texto)))

def buscar_paciente(cpf):
    """Tenta achar o paciente pelo CPF (Tentativa 1 do seu código Wix)"""
    cpf_limpo = limpar_numeros(cpf)
    url = f"{BASE_URL}/patient/search?paciente_cpf={cpf_limpo}&photo=false"
    
    print(f"🔍 Buscando CPF: {cpf_limpo}...")
    response = requests.get(url, headers=HEADERS)
    
    if response.status_code == 200:
        dados = response.json()
        if dados.get("success") != False and dados.get("content"):
            paciente_id = dados.get("content", {}).get("id") or dados.get("content", {}).get("paciente_id")
            if paciente_id:
                print(f"✅ Paciente ENCONTRADO! ID: {paciente_id}")
                return paciente_id
    
    print("❌ Paciente não encontrado na busca.")
    return None

def criar_paciente(nome, cpf, nascimento, email, celular):
    """Cria um paciente novo no Feegow"""
    url = f"{BASE_URL}/patient/create"
    
    payload = {
        "nome_completo": nome,
        "cpf": limpar_numeros(cpf),
        "data_nascimento": nascimento, # Formato YYYY-MM-DD
        "email1": email,
        "celular1": limpar_numeros(celular)
    }
    
    print(f"➕ Tentando criar paciente: {nome}...")
    response = requests.post(url, headers=HEADERS, json=payload)
    
    try:
        dados = response.json()
        if response.status_code == 200 and dados.get("success") != False:
            # Feegow às vezes retorna em content.paciente_id ou paciente_id direto
            paciente_id = dados.get("content", {}).get("paciente_id") or dados.get("paciente_id")
            print(f"🎉 SUCESSO! Paciente criado com ID: {paciente_id}")
            return paciente_id
        else:
            print(f"⚠️ Erro ao criar: {dados.get('message', 'Erro desconhecido')}")
    except Exception as e:
        print(f"⚠️ Erro fatal na requisição: {response.text}")
        
    return None

# ==========================================
# 🚀 ÁREA DE EXECUÇÃO DO TESTE
# ==========================================
if __name__ == "__main__":
    if FEEGOW_TOKEN == "COLE_SEU_TOKEN_AQUI":
        print("🛑 ATENÇÃO: Você esqueceu de colar o seu FEEGOW_TOKEN na linha 9 do código!")
    else:
        print("-" * 40)
        print("INICIANDO LABORATÓRIO FEEGOW...")
        print("-" * 40)
        
        # DADOS DE TESTE (Fique à vontade para mudar)
        cpf_teste = "12345678909"
        nome_teste = "Paciente Teste Robô Conectifisio"
        nasc_teste = "1980-05-15"
        email_teste = "teste@conectifisio.com"
        celular_teste = "11999999999"
        
        # 1. Primeiro tenta buscar
        paciente_id = buscar_paciente(cpf_teste)
        
        # 2. Se não achar, cria
        if not paciente_id:
            paciente_id = criar_paciente(nome_teste, cpf_teste, nasc_teste, email_teste, celular_teste)
            
        if paciente_id:
            print("-" * 40)
            print(f"🎯 INTEGRAÇÃO FEEGOW FUNCIONANDO PERFEITAMENTE!")
            print(f"O seu paciente está pronto no Feegow com ID {paciente_id}.")
            print("Já podemos plugar essa lógica no cérebro do WhatsApp!")
        else:
            print("-" * 40)
            print("🚨 A integração falhou. Verifique se o Token está correto e ativo.")
