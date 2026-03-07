import requests

# ⚠️ COLOQUE O SEU TOKEN AQUI (Aquele grandão da Vercel)
FEEGOW_TOKEN = "COLE_SEU_TOKEN_AQUI"

HEADERS = {
    "Content-Type": "application/json",
    "x-access-token": FEEGOW_TOKEN
}

# O seu número de teste que está no cadastro do João
CELULAR_BUSCA = "11971904516" 

print("-" * 40)
print(f"🕵️‍♂️ INICIANDO RAIO-X FEEGOW PARA O CELULAR: {CELULAR_BUSCA}")
print("-" * 40)

try:
    url_b = f"https://api.feegow.com/v1/api/patient/search?celular={CELULAR_BUSCA}"
    print(f"Enviando requisição GET para: {url_b}")
    
    res_b = requests.get(url_b, headers=HEADERS, timeout=10)
    
    print(f"\nStatus Code Recebido: {res_b.status_code}")
    print("\nResposta Crua da Feegow:")
    print(res_b.text)
    
    if res_b.status_code == 200:
        dados_f = res_b.json()
        if dados_f.get("success") != False and dados_f.get("content"):
            pacientes = dados_f["content"]
            print(f"\n✅ SUCESSO! A Feegow encontrou {len(pacientes)} paciente(s).")
            for p in pacientes:
                print(f" - Nome: {p.get('nome', p.get('nome_completo', 'Sem nome'))}")
                print(f" - CPF: {p.get('cpf', 'Sem CPF')}")
                print(f" - Celular no Feegow: {p.get('celular', 'Sem celular')}")
        else:
             print(f"\n❌ A requisição funcionou, mas a Feegow disse que NÃO EXISTE nenhum paciente com o celular {CELULAR_BUSCA}.")
             print("Aviso: A Feegow pode estar a exigir a formatação com o '+55' ou com o DDD sem o 9.")

except Exception as e:
    print(f"\n⚠️ Erro durante a requisição: {e}")

print("-" * 40)
